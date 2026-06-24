#!/usr/bin/env bash
set -euo pipefail
/opt/hermes/.venv/bin/python /opt/data/social-monitors/src/apify_tiktok_posts_monitor.py \
  --brand-id flock_safety \
  --query "Flock Safety" \
  --max-items 10 \
  --max-charge-usd 0.05 \
  --date-range THIS_WEEK \
  --send
