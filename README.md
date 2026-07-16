# bank-switch-monitor

Checks MoneySavingExpert's [current account switch offers](https://www.moneysavingexpert.com/banking/compare-best-bank-accounts/)
and [student bank accounts](https://www.moneysavingexpert.com/students/student-bank-account/)
pages every 4 hours via GitHub Actions, extracts (bank, £amount) pairs found
near bank names, and pings Discord (`DISCORD_WEBHOOK_URL` secret) when the set
changes — a new bonus launching, an amount changing, or an offer ending.

Diff-based: the first run baselines silently. Extraction is deliberately loose
(some cross-pairing noise from adjacent headings), but noise is stable so
alerts only fire on genuine page changes. Fetches use `curl` because MSE's CDN
403s Python's urllib TLS fingerprint from datacenter IPs.

Run locally with `python monitor.py`. State lives in `state.json`, committed
back by the workflow.
