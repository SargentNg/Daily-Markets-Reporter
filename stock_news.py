#!/usr/bin/env python3
"""
stock_news.py — Fetch recent news headlines for a set of stock tickers and
email a digest containing only items not seen on a previous run.

Dependency-free: uses only the Python 3.10+ standard library so it runs
unmodified from cron.

Usage:
    python3 stock_news.py                 # fetch, dedupe, email (if anything new)
    python3 stock_news.py --dry-run       # fetch + print digest, send nothing, keep state
    python3 stock_news.py --force         # email even when there is nothing new

Configuration lives in config.json (next to this script). SMTP credentials
live in .env (SMTP_USER / SMTP_PASS) and are intentionally kept out of
config.json.

Exit codes:
    0  success (including "nothing new, skipped email" and dry-run)
    1  config/credential problem or a hard failure
    2  all tickers failed to fetch
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from html import escape, unescape
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"
STATE_PATH = BASE_DIR / "state.json"

USER_AGENT = "Mozilla/5.0 (compatible; StockNewsDigest/1.0; +cron)"
HTTP_TIMEOUT = 25
ET_TZ = ZoneInfo("America/New_York")

SEEN_PRUNE_AGE_DAYS = 14     # drop remembered links older than this
SEEN_MAX_ENTRIES = 8000      # hard cap on remembered links
SNIPPET_MAX = 240            # max characters of the description snippet

TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {msg}",
          flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    raise SystemExit(code)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_rss_date(raw: str | None) -> datetime | None:
    """Parse an RFC-822 or ISO-8601 timestamp into an aware UTC datetime."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def clean_text(raw: str | None) -> str:
    """Strip HTML tags, decode entities, collapse whitespace."""
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = unescape(text)
    return WHITESPACE_RE.sub(" ", text).strip()


def snippet(raw: str | None, limit: int = SNIPPET_MAX) -> str:
    text = clean_text(raw)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"  # ellipsis character


def rel_time(dt: datetime | None, ref: datetime) -> str:
    if dt is None:
        return ""
    secs = max(0, int((ref - dt).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h {secs % 3600 // 60}m ago"
    return f"{secs // 86400}d ago"


def link_hash(link: str) -> str:
    return hashlib.sha1((link or "").strip().encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        die(f"config not found: {CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        cfg = json.load(fh)

    if not cfg.get("tickers"):
        die("config.json: 'tickers' is empty")

    cfg.setdefault("ticker_labels", {})
    cfg.setdefault("smtp_host", "smtp.gmail.com")
    cfg.setdefault("smtp_port", 587)
    cfg.setdefault("starttls", True)
    cfg.setdefault("send_empty", False)
    cfg.setdefault("first_run_lookback_hours", 12)
    cfg.setdefault("max_items_per_ticker", 15)
    cfg["tickers"] = [t.strip().upper() for t in cfg["tickers"] if t.strip()]
    cfg["to"] = [a.strip() for a in (cfg.get("to") or []) if a.strip()]
    cfg["from"] = (cfg.get("from") or "").strip()
    return cfg


def load_env() -> dict:
    """Minimal .env parser: KEY=VALUE lines, '#' comments, optional quotes."""
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        values[key] = val
    # real environment variables take precedence over the .env file
    values["SMTP_USER"] = os.environ.get("SMTP_USER", values.get("SMTP_USER", ""))
    values["SMTP_PASS"] = os.environ.get("SMTP_PASS", values.get("SMTP_PASS", ""))
    return values


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def _item_link(item: ET.Element) -> str:
    link_el = item.find("link")
    if link_el is not None:
        text = (link_el.text or "").strip()
        if text:
            return text
        return (link_el.get("href") or "").strip()
    # Atom-style: <link href="..."/>
    for el in item:
        if el.tag.split("}")[-1] == "link":
            return (el.get("href") or (el.text or "")).strip()
    return ""


def parse_rss(data: bytes) -> list[dict]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # Some feeds lead with a BOM or stray bytes; retry after normalising.
        try:
            text = data.decode("utf-8-sig").strip()
            root = ET.fromstring(text)
        except Exception:
            return []

    items: list[dict] = []
    for it in root.iter("item"):
        def field(tag: str) -> str:
            el = it.find(tag)
            return (el.text or "").strip() if (el is not None and el.text) else ""

        title = clean_text(field("title"))
        link = _item_link(it).strip()
        desc = field("description")
        if desc == "":
            desc = field("summary")
        pub = parse_rss_date(field("pubDate"))
        if not title and not link:
            continue
        items.append({
            "title": title or "(untitled)",
            "link": link,
            "description": desc,
            "pub_date": pub,
        })
    return items


def yahoo_url(ticker: str) -> str:
    return (f"https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={ticker}&region=US&lang=en-US")


def google_url(ticker: str) -> str:
    return ("https://news.google.com/rss/search"
            f"?q=stock:{ticker}&hl=en-US&gl=US&ceid=US:en")


def fetch_ticker(ticker: str) -> tuple[list[dict], str] | None:
    """Return (items, source_name) or None if every source failed.

    Preference order: Yahoo Finance, then Google News as fallback.
    """
    for name, url in (("yahoo", yahoo_url(ticker)), ("google", google_url(ticker))):
        try:
            items = parse_rss(fetch_url(url))
        except Exception as exc:
            log(f"  {ticker} [{name}] failed: {exc}")
            continue
        if items:
            return items, name
        log(f"  {ticker} [{name}] returned 0 items; trying fallback")
    return None


# --------------------------------------------------------------------------- #
# State (dedupe)
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            log("state.json unreadable; starting fresh")
    return {"seen": {}, "last_run": None}


def prune_seen(seen: dict, ref: datetime) -> dict:
    cutoff = (ref - timedelta(days=SEEN_PRUNE_AGE_DAYS)).timestamp()
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    if len(pruned) > SEEN_MAX_ENTRIES:
        ordered = sorted(pruned.items(), key=lambda kv: kv[1], reverse=True)
        pruned = dict(ordered[:SEEN_MAX_ENTRIES])
    return pruned


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


# --------------------------------------------------------------------------- #
# Digest building
# --------------------------------------------------------------------------- #

def render_sections(new_by_ticker: dict, labels: dict, ref: datetime) -> tuple[str, str]:
    """Return (plain_text_body, html_body) for the non-empty sections."""
    text_parts: list[str] = []
    html_parts: list[str] = []

    html_li_style = "margin:0 0 10px 0;font-size:14px;line-height:1.45;"
    html_title_style = ("display:block;font-weight:600;color:#111;"
                        "text-decoration:none;")
    html_meta_style = "display:block;color:#555;font-size:12px;margin-top:2px;"
    html_head_style = ("font-size:16px;font-weight:700;margin:0 0 8px 0;"
                       "padding-bottom:4px;border-bottom:2px solid #eee;")
    html_item_wrap = ("padding:10px 12px;margin-bottom:10px;"
                      "border:1px solid #e5e5e5;border-radius:6px;"
                      "background:#fafafa;")

    for ticker in new_by_ticker:
        items = new_by_ticker[ticker]
        if not items:
            continue
        label = labels.get(ticker, ticker)
        heading = ticker if ticker == label else f"{label} ({ticker})"

        # plain text
        text_parts.append(f"=== {heading} — {len(items)} item(s) ===")
        for i, it in enumerate(items, 1):
            text_parts.append(f"[{i}] {it['title']}")
            if it["link"]:
                text_parts.append(f"    {it['link']}")
            when = rel_time(it["pub_date"], ref)
            snip = snippet(it["description"])
            line = "    "
            if when:
                line += f"When: {when}"
            if snip:
                line += (f" | {snip}" if when else f"Snippet: {snip}")
            if line.strip():
                text_parts.append(line)
        text_parts.append("")

        # html
        html_parts.append(f'<div style="{html_item_wrap}">')
        html_parts.append(f'<div style="{html_head_style}">{escape(heading)}'
                          f' <span style="color:#888;font-weight:400;font-size:12px;">'
                          f'{len(items)} new</span></div>')
        for it in items:
            title = escape(it["title"])
            when = rel_time(it["pub_date"], ref)
            snip = escape(snippet(it["description"]))
            if it["link"]:
                open_tag = (f'<a href="{escape(it["link"], quote=True)}" '
                            f'style="{html_title_style}">')
                close_tag = "</a>"
            else:
                open_tag = f'<span style="{html_title_style}">'
                close_tag = "</span>"
            meta_bits = []
            if when:
                meta_bits.append(when)
            meta = " ".join(meta_bits)
            html_parts.append(
                f'<div style="{html_li_style}">'
                f'{open_tag}{title}{close_tag}'
                + (f'<span style="{html_meta_style}">{escape(meta)}</span>' if meta else "")
                + (f'<span style="{html_meta_style}">{snip}</span>' if snip else "")
                + "</div>"
            )
        html_parts.append("</div>")

    plain = "\n".join(text_parts).strip() or ""
    html_body = "\n".join(html_parts)
    return plain, html_body


def build_digest(new_by_ticker: dict, labels: dict, ref: datetime,
                 status_line: str) -> tuple[list[str], str, str, str, str]:
    """Return (per-ticker ordered, subject, plain_body, html_body, count)."""
    total = sum(len(v) for v in new_by_ticker.values())
    with_items = [t for t, v in new_by_ticker.items() if v]
    plain_sections, html_sections = render_sections(
        {t: new_by_ticker[t] for t in with_items}, labels, ref)

    et_now = ref.astimezone(ET_TZ)
    subject = (f"Stock News Digest — {et_now.strftime('%a, %b %d, %Y %I:%M %p')} ET"
               f" — {total} new item{'s' if total != 1 else ''}")

    plain_header = (
        f"Stock News Digest\n"
        f"{et_now.strftime('%A, %B %d, %Y  %I:%M %p %Z')}\n"
        f"{status_line}\n"
        f"Tickers: {', '.join(new_by_ticker.keys())}\n"
        f"New items: {total}\n"
        + "-" * 60
    )
    plain_body = plain_header + "\n\n" + (plain_sections or "(no new items)")

    html_body = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#ffffff;">
  <div style="max-width:640px;margin:0 auto;padding:20px 16px;font-family:Helvetica,Arial,sans-serif;color:#222;">
    <div style="font-size:20px;font-weight:700;margin-bottom:4px;">Stock News Digest</div>
    <div style="font-size:13px;color:#666;margin-bottom:4px;">
      {escape(et_now.strftime('%A, %B %d, %Y %I:%M %p %Z'))}
    </div>
    <div style="font-size:13px;color:#666;margin-bottom:16px;">{escape(status_line)}</div>
    {html_sections}
    <div style="margin-top:18px;font-size:11px;color:#999;">
      Sent automatically by stock-news (cron). Unsubscribe by removing the
      crontab entry or editing config.json.
    </div>
  </div>
</body></html>"""
    return with_items, subject, plain_body, html_body, str(total)


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

def send_email(cfg: dict, cred_user: str, cred_pass: str, subject: str,
               plain_body: str, html_body: str, to_addrs: list[str]) -> None:
    sender = (cfg["from"] or cred_user).strip()
    msg = EmailMessage()
    msg["From"] = sender  # bare address or "Name <addr>"; both are valid headers
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    # formatdate(usegmt=True): passing a datetime raises TypeError on 3.12
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = make_msgid(domain="localhost")
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=40) as smtp:
        if cfg["starttls"]:
            smtp.starttls(context=ctx)  # re-EHLOs automatically
        smtp.login(cred_user, cred_pass)
        smtp.send_message(msg)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and print the digest; send nothing and do not "
                             "touch state.json")
    parser.add_argument("--force", action="store_true",
                        help="send an email even if there is nothing new")
    args = parser.parse_args(argv)

    cfg = load_config()
    labels = {k.upper(): v for k, v in cfg.get("ticker_labels", {}).items()}
    state = load_state()
    seen: dict = state.get("seen", {})
    last_run_raw = state.get("last_run")
    first_run = last_run_raw is None
    ref = now_utc()

    # Window cutoff: since the previous successful run, or a lookback on run 1.
    if last_run_raw is not None:
        try:
            cutoff = datetime.fromtimestamp(float(last_run_raw), tz=timezone.utc)
        except Exception:
            cutoff = ref - timedelta(hours=float(cfg["first_run_lookback_hours"]))
    else:
        cutoff = ref - timedelta(hours=float(cfg["first_run_lookback_hours"]))

    log(f"run start | tickers={cfg['tickers']} first_run={first_run} "
        f"cutoff={cutoff.strftime('%Y-%m-%d %H:%M UTC')} "
        f"mode={'dry-run' if args.dry_run else 'send'}")

    new_by_ticker: dict[str, list[dict]] = {}
    sources: dict[str, str] = {}
    failures = 0
    for ticker in cfg["tickers"]:
        result = fetch_ticker(ticker)
        if result is None:
            log(f"  {ticker}: all sources failed")
            failures += 1
            new_by_ticker[ticker] = []
            continue
        items, source = result
        sources[ticker] = source
        fresh = []
        for it in items:
            h = link_hash(it["link"])
            if h in seen:
                continue
            pub = it["pub_date"]
            if pub is not None and pub < cutoff:
                continue  # older than the window for this run
            fresh.append(it)
            seen[h] = ref.timestamp()  # mark on first sight (saved after send)
        fresh = fresh[: int(cfg["max_items_per_ticker"])]
        new_by_ticker[ticker] = fresh
        log(f"  {ticker} [{source}]: {len(items)} fetched, {len(fresh)} new")

    if failures == len(cfg["tickers"]) and cfg["tickers"]:
        die("every ticker failed to fetch; no digest produced")

    status_bits = []
    if first_run:
        status_bits.append("first run (lookback "
                           f"{cfg['first_run_lookback_hours']}h)")
    else:
        status_bits.append("since last run "
                           f"{rel_parse_drift(last_run_raw, ref)}")
    if failures:
        status_bits.append(f"{failures} ticker(s) could not be fetched")

    with_items, subject, plain_body, html_body, total = build_digest(
        new_by_ticker, labels, ref, "; ".join(status_bits))

    if args.dry_run:
        print("\n" + "=" * 64)
        print(f"DRY RUN — subject: {subject}")
        print("=" * 64 + "\n")
        print(plain_body)
        print("\n" + "-" * 64)
        print(f"[dry-run] total new items: {total}; nothing sent, state untouched")
        return

    nothing_new = int(total) == 0
    if nothing_new and not cfg["send_empty"] and not args.force:
        log(f"nothing new; skipping email (use --force or send_empty to change)")
        finish_run(state, seen, ref, sent=nothing_new, skipped=True)
        return

    cred_user = load_env().get("SMTP_USER", "").strip()
    cred_pass = load_env().get("SMTP_PASS", "").strip()
    if not cred_user or not cred_pass:
        die("SMTP credentials missing. Put SMTP_USER and SMTP_PASS in .env "
            "(a Gmail app password, 2-step verification must be on).")
    if not cfg["to"]:
        die("no recipients configured: set 'to' in config.json")

    try:
        send_email(cfg, cred_user, cred_pass, subject, plain_body, html_body,
                   cfg["to"])
    except Exception as exc:
        log(f"email failed: {exc}")
        die(f"email send failed: {exc}")

    log(f"sent digest to {', '.join(cfg['to'])} ({total} item(s))")
    finish_run(state, seen, ref, sent=True, skipped=False)


def rel_parse_drift(last_run_raw, ref: datetime) -> str:
    try:
        last = datetime.fromtimestamp(float(last_run_raw), tz=timezone.utc)
        return f"({rel_time(last, ref)})"
    except Exception:
        return ""


def finish_run(state: dict, seen: dict, ref: datetime,
               sent: bool, skipped: bool) -> None:
    state["seen"] = prune_seen(seen, ref)
    state["last_run"] = ref.timestamp()
    save_state(state)
    if not skipped:
        log("state updated")


if __name__ == "__main__":
    main()