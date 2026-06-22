#!/usr/bin/env bash
set -euo pipefail
/opt/hermes/.venv/bin/python /opt/data/social-monitors/src/apify_linkedin_posts_monitor.py \
  --brand-id flock_safety \
  --query '"Flock Safety"' \
  --posted-limit 24h \
  --max-posts 10 \
  --max-charge-usd 0.05 \
  --send
