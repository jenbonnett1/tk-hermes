#!/usr/bin/env bash
set -euo pipefail
/opt/hermes/.venv/bin/python /opt/data/social-monitors/src/apify_instagram_reels_monitor.py \
  --brand-id flock_safety \
  --query "Flock Safety" \
  --max-pages 1 \
  --max-charge-usd 0.05 \
  --send
