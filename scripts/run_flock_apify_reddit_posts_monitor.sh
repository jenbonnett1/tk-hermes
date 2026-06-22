#!/usr/bin/env bash
set -euo pipefail
/opt/hermes/.venv/bin/python /opt/data/social-monitors/src/apify_reddit_posts_monitor.py \
  --brand-id flock_safety \
  --query '"Flock Safety"' \
  --search-time day \
  --max-posts 10 \
  --max-charge-usd 0.04 \
  --send
