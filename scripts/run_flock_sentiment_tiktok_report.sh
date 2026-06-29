#!/usr/bin/env bash
set -euo pipefail
/opt/hermes/.venv/bin/python /opt/data/profiles/tk/scripts/generate_flock_sentiment_platform_report.py tiktok --send
