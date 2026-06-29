#!/usr/bin/env python3
"""Cost-capped Apify Instagram/Reels pilot monitor for Flock Safety.

Discovery actor: patient_discovery/instagram-search-reels
Direct enrichment actor is intentionally not used in scheduled pilot runs yet;
keep the 2-3 run pilot cheap and metadata/caption only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_CONFIG = Path("/opt/data/social-monitors/config/brands.json")
DEFAULT_STATE_DIR = Path("/opt/data/social-monitors/state/apify")
DEFAULT_SAMPLE_DIR = Path("/opt/data/social-monitors/samples/apify/flock_safety_instagram_reels")
HIMALAYA_BIN = Path("/opt/data/profiles/tk/home/.local/bin/himalaya")
HIMALAYA_CONFIG = Path("/opt/data/profiles/tk/home/.config/himalaya/config.toml")
APIFY_DISCOVERY_ACTOR = "patient_discovery~instagram-search-reels"

HIGH_RISK_TERMS = {
    "lawsuit",
    "sue",
    "illegal",
    "fraud",
    "privacy",
    "surveillance",
    "unconstitutional",
    "dangerous",
    "boycott",
    "scam",
    "class action",
    "warrantless",
    "fourth amendment",
    "tracking",
    "spying",
    "mass surveillance",
    "orwellian",
    "security failure",
}

@dataclass
class ReelFinding:
    url: str
    owner: str
    caption: str
    matched_terms: list[str]
    score: int
    severity: str
    play_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    taken_at: int | None = None
    video_url_present: bool = False


def load_dotenv(path: Path = Path("/opt/data/.env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def apify_token() -> str:
    load_dotenv()
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is not configured")
    return token


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    return json.loads(path.read_text())


def get_brand(config: dict, brand_id: str) -> dict:
    for brand in config.get("brands", []):
        if brand.get("id") == brand_id:
            return brand
    raise KeyError(f"Unknown brand: {brand_id}")


def run_actor(query: str, max_pages: int, max_charge_usd: float) -> tuple[dict, list[dict]]:
    token = apify_token()
    run_input = {"query": query, "maxPages": max_pages}
    url = f"https://api.apify.com/v2/acts/{APIFY_DISCOVERY_ACTOR}/runs?waitForFinish=180&maxTotalChargeUsd={max_charge_usd:.2f}"
    req = urllib.request.Request(
        url,
        data=json.dumps(run_input).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        run = json.load(resp)["data"]
    run_id = run["id"]
    status = run.get("status")
    for _ in range(60):
        if status in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
            break
        time.sleep(5)
        poll_req = urllib.request.Request(
            f"https://api.apify.com/v2/actor-runs/{run_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(poll_req, timeout=30) as resp:
            run = json.load(resp)["data"]
        status = run.get("status")
    items: list[dict] = []
    dataset_id = run.get("defaultDatasetId")
    if dataset_id:
        items_req = urllib.request.Request(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items?clean=true&format=json",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(items_req, timeout=60) as resp:
            items = json.load(resp)
    return run, items


def caption_from_item(item: dict) -> str:
    caption = item.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text") or json.dumps(caption, ensure_ascii=False)
    caption = caption or item.get("text") or item.get("description") or ""
    return re.sub(r"\s+", " ", str(caption)).strip()


def reel_url(item: dict) -> str:
    code = item.get("code") or item.get("shortCode") or item.get("id")
    return item.get("url") or (f"https://www.instagram.com/reel/{code}/" if code else "")


def owner_from_item(item: dict) -> str:
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    return user.get("username") or item.get("username") or item.get("ownerUsername") or "unknown"


def metric(item: dict, *keys: str) -> int | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def classify_item(item: dict, negative_keywords: list[str]) -> ReelFinding | None:
    caption = caption_from_item(item)
    url = reel_url(item)
    if not url:
        return None
    haystack = caption.lower()
    terms = []
    score = 0
    for term in negative_keywords:
        t = term.lower()
        if t in haystack:
            terms.append(term)
            score += 18 if t in HIGH_RISK_TERMS else 10
    for term in HIGH_RISK_TERMS:
        if term in haystack and term not in [t.lower() for t in terms]:
            terms.append(term)
            score += 18
    plays = metric(item, "ig_play_count", "play_count", "video_play_count", "videoPlayCount")
    likes = metric(item, "like_count", "likesCount")
    comments = metric(item, "comment_count", "commentsCount")
    shares = metric(item, "share_count", "sharesCount")
    # Engagement bumps: high-reach negative reels warrant higher attention.
    if plays and plays >= 1_000_000:
        score += 20
    elif plays and plays >= 250_000:
        score += 14
    elif plays and plays >= 50_000:
        score += 8
    if comments and comments >= 1000:
        score += 10
    elif comments and comments >= 100:
        score += 5
    if shares and shares >= 10_000:
        score += 10
    elif shares and shares >= 1000:
        score += 5
    score = min(score, 100)
    severity = "high" if score >= 70 else "medium" if score >= 35 else "low"
    return ReelFinding(
        url=url,
        owner=owner_from_item(item),
        caption=caption,
        matched_terms=terms,
        score=score,
        severity=severity,
        play_count=plays,
        like_count=likes,
        comment_count=comments,
        share_count=shares,
        taken_at=metric(item, "taken_at", "taken_at_timestamp"),
        video_url_present=bool(item.get("video_url") or item.get("videoUrl") or item.get("video_versions") or item.get("video_versions")),
    )


def state_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def mark_seen(path: Path, urls: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = state_seen(path)
    seen.update(urls)
    path.write_text(json.dumps(sorted(seen), indent=2))


def save_run(sample_dir: Path, run: dict, items: list[dict], query: str, max_pages: int, max_charge_usd: float) -> tuple[Path, Path]:
    sample_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    items_path = sample_dir / f"{ts}_scheduled_items.json"
    run_path = sample_dir / f"{ts}_scheduled_run.json"
    items_path.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    safe_run = {k: run.get(k) for k in ["id", "actId", "status", "startedAt", "finishedAt", "defaultDatasetId", "usage", "usageTotalUsd", "chargedEventCounts", "stats", "exitCode"]}
    safe_run["input_used"] = {"query": query, "maxPages": max_pages, "maxTotalChargeUsd": max_charge_usd}
    run_path.write_text(json.dumps(safe_run, indent=2, ensure_ascii=False))
    return run_path, items_path


def fmt_num(n: int | None) -> str:
    return "unavailable" if n is None else f"{n:,}"


def render_report(brand_name: str, query: str, run: dict, findings: list[ReelFinding], new_findings: list[ReelFinding], run_path: Path, items_path: Path) -> str:
    now = datetime.now(ZoneInfo("America/New_York"))
    start = now - timedelta(hours=6)
    lines = [
        f"{brand_name} Instagram/Reels Apify Pilot",
        "",
        f"Window checked: {start:%Y-%m-%d %I:%M %p ET} – {now:%Y-%m-%d %I:%M %p ET}",
        f"Discovery query: {query}",
        f"Actor: patient_discovery/instagram-search-reels",
        f"Run status: {run.get('status')}",
        f"Apify reported usage: ${float(run.get('usageTotalUsd') or 0):.4f}",
        f"Items returned: {len(findings)} reportable-looking reels from this run",
        f"New items after dedupe: {len(new_findings)}",
        f"Run ID: {run.get('id')}",
        f"Dataset ID: {run.get('defaultDatasetId')}",
        "",
    ]
    if new_findings:
        lines.append("New reportable reels:")
        for idx, f in enumerate(sorted(new_findings, key=lambda x: x.score, reverse=True), start=1):
            excerpt = f.caption if len(f.caption) <= 420 else f.caption[:419].rstrip() + "…"
            lines.extend([
                f"{idx}. [{f.severity.upper()} | score {f.score}] @{f.owner}",
                f"   URL: {f.url}",
                f"   Engagement: plays {fmt_num(f.play_count)} | likes {fmt_num(f.like_count)} | comments {fmt_num(f.comment_count)} | shares {fmt_num(f.share_count)}",
                f"   Video URL present: {'yes' if f.video_url_present else 'no'}",
                f"   Matched terms: {', '.join(f.matched_terms) if f.matched_terms else '(none)'}",
                f"   Caption: {excerpt}",
                "",
            ])
    else:
        lines.extend([
            "No new reportable reels after dedupe.",
            "The actor may still have returned already-seen reels; those are suppressed from repeated alerts.",
            "",
        ])
    lines.extend([
        "Pilot cost guardrails:",
        "- maxPages=1",
        "- maxTotalChargeUsd=$0.05",
        "- no video download add-on",
        "- no transcript add-on",
        "- not scheduled beyond the short 2–3 run test unless approved",
        "",
        "Local audit files:",
        f"- {run_path}",
        f"- {items_path}",
    ])
    return "\n".join(lines)


def send_email(to: list[str], cc: list[str], subject: str, body: str) -> str:
    cc_header = f"Cc: {', '.join(cc)}\n" if cc else ""
    message = (
        "From: Hermes Reports <hermes-agent@trybemedia.com>\n"
        f"To: {', '.join(to)}\n"
        f"{cc_header}"
        f"Subject: {subject}\n\n"
        f"{body}\n"
    )
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="apify-ig-monitor-email-", suffix=".txt") as f:
        f.write(message)
        msg_path = f.name
    env = os.environ.copy()
    env["HIMALAYA_CONFIG"] = str(HIMALAYA_CONFIG)
    try:
        with open(msg_path, "rb") as stdin:
            proc = subprocess.run([str(HIMALAYA_BIN), "template", "send"], stdin=stdin, env=env, text=False, capture_output=True, timeout=120)
    finally:
        Path(msg_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace") or proc.stdout.decode(errors="replace"))
    return proc.stdout.decode(errors="replace").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand-id", default="flock_safety")
    parser.add_argument("--query", default="Flock Safety")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--max-charge-usd", type=float, default=0.05)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--sample-dir", default=str(DEFAULT_SAMPLE_DIR))
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config))
    brand = get_brand(config, args.brand_id)
    run, items = run_actor(args.query, args.max_pages, args.max_charge_usd)
    run_path, items_path = save_run(Path(args.sample_dir), run, items, args.query, args.max_pages, args.max_charge_usd)
    findings = []
    for item in items:
        finding = classify_item(item, brand.get("negative_keywords", []))
        if finding and finding.score >= 35:
            findings.append(finding)
    state_path = Path(args.state_dir) / f"{brand['id']}_instagram_reels_seen.json"
    seen = state_seen(state_path)
    new_findings = [f for f in findings if f.url not in seen]
    body = render_report(brand["name"], args.query, run, findings, new_findings, run_path, items_path)
    now = datetime.now(ZoneInfo("America/New_York"))
    subject_suffix = "No New Reels" if not new_findings else f"{len(new_findings)} New Candidate Reels"
    subject = f"{brand['name']} Instagram/Reels Apify Pilot — {subject_suffix} — {now:%Y-%m-%d %I:%M %p ET}"
    if args.send:
        recipients = config.get("report_recipients", {})
        send_email(recipients.get("to", []), recipients.get("cc", []), subject, body)
        mark_seen(state_path, [f.url for f in new_findings])
    print(body)
    print(f"\n---\nEmail sent: {'yes' if args.send else 'no (dry run)'}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
