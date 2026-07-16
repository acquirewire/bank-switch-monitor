#!/usr/bin/env python3
"""Bank switch-offer monitor.

Watches MoneySavingExpert's switch-offer and student-account pages, extracts
(bank, £amount) pairs, and posts a Discord webhook alert whenever the set of
offers changes (new bonus launched, bonus amount changed, offer ended).

Diff-based by design: the first run baselines silently; only changes notify.
Stdlib only - no dependencies.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Canonical bank name -> aliases matched in page text (word-bounded, lowercase)
BANKS = {
    "HSBC": ["hsbc"],
    "First Direct": ["first direct"],
    "Barclays": ["barclays"],
    "NatWest": ["natwest"],
    "RBS": ["rbs", "royal bank of scotland"],
    "Ulster Bank": ["ulster bank"],
    "Santander": ["santander"],
    "Lloyds": ["lloyds"],
    "Halifax": ["halifax"],
    "Bank of Scotland": ["bank of scotland"],
    "Nationwide": ["nationwide"],
    "Co-op Bank": ["co-op", "co-operative bank"],
    "TSB": ["tsb"],
    "Virgin Money": ["virgin money"],
    "Monzo": ["monzo"],
    "Starling": ["starling"],
    "Chase": ["chase"],
    "Club Lloyds": ["club lloyds"],
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")


def fetch_text(url):
    """Fetch a page and reduce it to visible-ish text."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            break
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError(f"fetch failed: {url}: {last_err}")
    raw = re.sub(r"<(script|style)\b.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", "\n", raw)
    return html.unescape(raw)


def extract_offers(text, min_gbp, max_gbp, window=180):
    """Return {(bank, amount)} for £amounts appearing shortly after a bank name."""
    low = text.lower()
    offers = set()
    for bank, aliases in BANKS.items():
        for alias in aliases:
            for m in re.finditer(rf"\b{re.escape(alias)}\b", low):
                snippet = text[m.start() : m.start() + window]
                for am in re.finditer(r"£\s?(\d{1,3}(?:,\d{3})?)(?!\d)", snippet):
                    amount = int(am.group(1).replace(",", ""))
                    if min_gbp <= amount <= max_gbp:
                        offers.add((bank, amount))
    return offers


def send_discord(webhook, content, embeds):
    payload = {"content": content, "embeds": embeds, "allowed_mentions": {"parse": ["everyone"]}}
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(float(e.headers.get("Retry-After", "2")), 30) + 0.5)
                continue
            raise


def run():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    first_run = not state.get("initialized")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    pages_state = state.setdefault("pages", {})
    embeds = []

    for page in cfg["pages"]:
        try:
            text = fetch_text(page["url"])
        except RuntimeError as e:
            log(f"WARN: {page['name']}: {e}")
            continue
        offers = extract_offers(text, page["min_gbp"], page["max_gbp"])
        keys = sorted(f"{b}|{a}" for b, a in offers)
        prev = set(pages_state.get(page["name"], []))
        added = [k for k in keys if k not in prev]
        removed = sorted(prev - set(keys))
        pages_state[page["name"]] = keys
        log(f"{page['name']}: {len(keys)} pairs, +{len(added)} -{len(removed)}")

        if first_run or (not added and not removed):
            continue
        fields = []
        if added:
            fields.append({
                "name": "\U0001f195 New / changed",
                "value": "\n".join(f"• **{k.split('|')[0]}** — £{k.split('|')[1]}" for k in added)[:1024],
                "inline": False,
            })
        if removed:
            fields.append({
                "name": "❌ No longer listed",
                "value": "\n".join(f"• {k.split('|')[0]} — £{k.split('|')[1]}" for k in removed)[:1024],
                "inline": False,
            })
        embeds.append({
            "title": f"Bank offers changed: {page['name']}",
            "url": page["url"],
            "color": 0x5865F2,
            "description": "Amounts are detected near bank names — check the page for full terms.",
            "fields": fields,
            "footer": {"text": "bank-switch-monitor"},
        })

    state["initialized"] = True
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")

    if first_run:
        log("first run: baselined current offers, no notifications sent")
    elif embeds:
        if not webhook:
            log("WARN: changes detected but DISCORD_WEBHOOK_URL is not set")
        else:
            mention = cfg.get("discord_mention", "")
            send_discord(webhook, f"{mention} \U0001f3e6 Bank switch offers changed".strip(), embeds)
            log(f"sent {len(embeds)} notification(s) to Discord")
    else:
        log("no changes")


if __name__ == "__main__":
    run()
