#!/usr/bin/env python3
"""Collect recent Flock Safety monitor outputs for the customer-facing summary job.

This script is intentionally read-only. It prints the latest report files from each
Flock Safety monitor so the scheduled LLM job can synthesize a high-level customer
email without crawling the filesystem itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json

CRON_OUTPUT = Path("/opt/data/profiles/tk/cron/output")
MAX_CHARS_PER_REPORT = 9000

JOBS = [
    ("X", "60877de454f9", "Flock Safety X negative-post PR monitor"),
    ("Facebook", "61cc9da25f0a", "Flock Safety Facebook Apify posts monitor"),
    ("LinkedIn", "c28a11cca3d2", "Flock Safety LinkedIn Apify posts monitor"),
    ("Reddit", "3150bf37a5e4", "Flock Safety Reddit Apify posts monitor"),
    ("Instagram/Reels", "b35f08f0050b", "Flock Safety Instagram/Reels Apify pilot monitor"),
    ("TikTok", "f875933cbd41", "Flock Safety TikTok Apify posts monitor"),
]


def latest_report(job_id: str) -> Path | None:
    job_dir = CRON_OUTPUT / job_id
    if not job_dir.exists():
        return None
    reports = sorted(job_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def read_limited(path: Path) -> str:
    text = path.read_text(errors="replace")
    # Agent-driven cron outputs include the full prompt before the actual result.
    # For synthesis, the response section is the report evidence we want.
    marker = "\n## Response\n"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    if len(text) <= MAX_CHARS_PER_REPORT:
        return text
    return text[:MAX_CHARS_PER_REPORT] + "\n\n[TRUNCATED by collector for length]\n"


def main() -> int:
    print("# Flock Safety Customer Summary Source Packet")
    print(f"Collected at UTC: {datetime.now(timezone.utc).isoformat()}")
    print()
    print("Use these source reports to draft ONE customer-facing executive email. Individual platform reports remain internal.")
    print()
    manifest = []
    for platform, job_id, name in JOBS:
        report = latest_report(job_id)
        manifest.append({
            "platform": platform,
            "job_id": job_id,
            "name": name,
            "latest_report": str(report) if report else None,
            "mtime_utc": datetime.fromtimestamp(report.stat().st_mtime, tz=timezone.utc).isoformat() if report else None,
        })
    print("## Source manifest")
    print(json.dumps(manifest, indent=2))
    print()
    for platform, job_id, name in JOBS:
        report = latest_report(job_id)
        print(f"\n\n===== SOURCE: {platform} | {name} | {job_id} =====")
        if not report:
            print("No report file found yet for this monitor.")
            continue
        print(f"Report path: {report}")
        print(f"Modified UTC: {datetime.fromtimestamp(report.stat().st_mtime, tz=timezone.utc).isoformat()}")
        print("----- BEGIN REPORT -----")
        print(read_limited(report))
        print("----- END REPORT -----")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
