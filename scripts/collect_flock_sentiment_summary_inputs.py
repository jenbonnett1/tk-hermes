#!/usr/bin/env python3
"""Collect recent Flock Safety general-sentiment report outputs for summary job."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json

CRON_OUTPUT = Path("/opt/data/profiles/tk/cron/output")
MAX_CHARS_PER_REPORT = 9000
INVISIBLE_UNICODE_TRANSLATION = {
    ord("\u200b"): None,
    ord("\u200c"): None,
    ord("\u200d"): None,
    ord("\ufeff"): None,
}

JOBS = [
    ("X", "a31eb876a0b1", "Flock Safety X general sentiment monitor"),
    ("Facebook", "410b4e5bab3f", "Flock Safety Facebook general sentiment report"),
    ("LinkedIn", "bbce45d7a268", "Flock Safety LinkedIn general sentiment report"),
    ("Reddit", "2e573de822fc", "Flock Safety Reddit general sentiment report"),
    ("Instagram/Reels", "7d70695581b9", "Flock Safety Instagram/Reels general sentiment report"),
    ("TikTok", "28d80dc3528e", "Flock Safety TikTok general sentiment report"),
]


def latest_report(job_id: str) -> Path | None:
    job_dir = CRON_OUTPUT / job_id
    if not job_dir.exists():
        return None
    reports = sorted(job_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def read_limited(path: Path) -> str:
    text = path.read_text(errors="replace")
    marker = "\n## Response\n"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    text = text.translate(INVISIBLE_UNICODE_TRANSLATION)
    if len(text) <= MAX_CHARS_PER_REPORT:
        return text
    return text[:MAX_CHARS_PER_REPORT] + "\n\n[TRUNCATED by collector for length]\n"


def main() -> int:
    print("# Flock Safety General Sentiment Summary Source Packet")
    print(f"Collected at UTC: {datetime.now(timezone.utc).isoformat()}")
    print()
    print("Use these general-sentiment source reports to draft ONE customer-facing executive email. This stream covers positive, mixed/neutral, and negative themes.")
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
            print("No report file found yet for this sentiment monitor.")
            continue
        print(f"Report path: {report}")
        print(f"Modified UTC: {datetime.fromtimestamp(report.stat().st_mtime, tz=timezone.utc).isoformat()}")
        print("----- BEGIN REPORT -----")
        print(read_limited(report))
        print("----- END REPORT -----")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
