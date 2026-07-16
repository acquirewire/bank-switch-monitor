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
import subprocess
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

# Verified product pages (2026-07-16) so alerts carry an apply link
APPLY_LINKS = {
    "HSBC": "https://www.hsbc.co.uk/current-accounts/products/bank-account/",
    "First Direct": "https://www.firstdirect.com/banking/current-account/",
    "Barclays": "https://www.barclays.co.uk/current-accounts/bank-account/",
    "NatWest": "https://www.natwest.com/current-accounts.html",
    "Santander": "https://www.santander.co.uk/personal/current-accounts",
    "Nationwide": "https://www.nationwide.co.uk/current-accounts/flexdirect/",
    "Bank of Scotland": "https://www.bankofscotland.co.uk/bankaccounts/classic.html",
    "Co-op Bank": "https://www.co-operativebank.co.uk/products/bank-accounts/switch-offer/",
    "Lloyds": "https://www.lloydsbank.com/current-accounts.html",
    "Halifax": "https://www.halifax.co.uk/bankaccounts.html",
    "RBS": "https://www.rbs.co.uk/current-accounts.html",
    "TSB": "https://www.tsb.co.uk/current-accounts/",
    "Virgin Money": "https://uk.virginmoney.com/current-accounts/",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")


def fetch_text(url):
    """Fetch a page and reduce it to visible-ish text.

    Uses curl rather than urllib: some CDNs (MSE's) 403 Python's TLS
    fingerprint from datacenter IPs but serve curl fine.
    """
    last_err = None
    for attempt in range(3):
        proc = subprocess.run(
            ["curl", "-s", "--max-time", "30", "-A", USER_AGENT,
             "-H", "Accept: text/html,application/xhtml+xml",
             "-H", "Accept-Language: en-GB,en;q=0.9",
             "-w", "\n%{http_code}", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        body, _, code = proc.stdout.rpartition("\n")
        if proc.returncode == 0 and code.strip() == "200" and body:
            raw = body
            break
        last_err = f"curl rc={proc.returncode} http={code.strip() or '?'}"
        time.sleep(5 * (attempt + 1))
    else:
        raise RuntimeError(f"fetch failed: {url}: {last_err}")
    raw = re.sub(r"<(script|style)\b.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", "\n", raw)
    return html.unescape(raw)


def extract_offers(text, min_gbp, max_gbp, window=180):
    """Return {(bank, amount): context_snippet} for £amounts near a bank name.

    Keeps the longest surrounding text per pair — headings are short, the card
    body with the offer's conditions is long, so the longest snippet is the
    most informative one to show in the alert.
    """
    low = text.lower()
    offers = {}
    for bank, aliases in BANKS.items():
        for alias in aliases:
            for m in re.finditer(rf"\b{re.escape(alias)}\b", low):
                snippet = text[m.start() : m.start() + window * 3]
                near = snippet[:window]
                for am in re.finditer(r"£\s?(\d{1,3}(?:,\d{3})?)(?!\d)", near):
                    amount = int(am.group(1).replace(",", ""))
                    if not (min_gbp <= amount <= max_gbp):
                        continue
                    clean = re.sub(r"\s+", " ", snippet).strip()
                    key = (bank, amount)
                    if len(clean) > len(offers.get(key, "")):
                        offers[key] = clean
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
            lines = []
            for k in added:
                bank, amount = k.split("|")
                line = f"• **{bank}** — £{amount}"
                if bank in APPLY_LINKS:
                    line += f" · [apply]({APPLY_LINKS[bank]})"
                snippet = offers.get((bank, int(amount)), "")
                if snippet:
                    line += f"\n> {snippet[:220]}"
                lines.append(line)
            fields.append({
                "name": "\U0001f195 New / changed",
                "value": "\n".join(lines)[:1024],
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
