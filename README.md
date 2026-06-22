# TK Hermes Social Monitors

This repository contains the code and configurations for the brand‑focused social‑risk monitors used by TK Hermes.

## Contents

- `social-monitors/` – FB/IG MVP monitor and Apify Instagram/Reels pilot monitor.
- `scripts/` – Helper shell scripts to run the monitors.
- `cron/` – Example cron job definitions (including X monitors that now include follower counts).

## Quick start

### FB/IG MVP (DuckDuckGo search‑index)

```bash
python social-monitors/src/brand_monitor.py   --brand-id flock_safety   --platforms facebook,instagram   --max-results 5   --send   # omit for dry‑run
```

### Apify Instagram/Reels pilot (Flock Safety)

```bash
scripts/run_flock_apify_instagram_pilot.sh   # sends email
# or dry‑run:
python social-monitors/src/apify_instagram_reels_monitor.py --send
```

### X monitors (already configured as cron jobs)

See `cron/jobs.json` for the three scheduled X‑risk monitors that now include author follower counts in their reports.

## Configuration

- APIFY_TOKEN must be set in the environment (or in `~/.env`) for the Apify pilot to work.
- Email reports are sent via Himalaya using the SMTP credentials already configured in the Hermes environment.

## Notes

- The Apify pilot is currently limited to 2‑3 test runs; see the cron job `Flock Safety Instagram/Reels Apify pilot monitor` (repeat: 3).
- All monitors deduplicate already‑seen URLs/post‑IDs to avoid repeat alerts.
