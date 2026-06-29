#!/usr/bin/env python3
"""Cost-capped Apify TikTok keyword/video monitor for Flock Safety.

Discovery actor: epctex/tiktok-search-scraper

The scheduled run searches recent public TikTok videos by keyword. It keeps the
pilot metadata-only: no comments, no transcripts, and no video download beyond
whatever preview/play URLs the actor includes in the dataset.
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
DEFAULT_SAMPLE_DIR = Path("/opt/data/social-monitors/samples/apify/flock_safety_tiktok")
HIMALAYA_BIN = Path("/opt/data/profiles/tk/home/.local/bin/himalaya")
HIMALAYA_CONFIG = Path("/opt/data/profiles/tk/home/.config/himalaya/config.toml")
APIFY_TIKTOK_ACTOR = "epctex~tiktok-search-scraper"
APIFY_TIKTOK_ACTOR_DISPLAY = "epctex/tiktok-search-scraper"

HIGH_RISK_TERMS = {
    "lawsuit", "sue", "illegal", "fraud", "privacy", "surveillance",
    "unconstitutional", "dangerous", "boycott", "scam", "class action",
    "warrantless", "fourth amendment", "tracking", "spying",
    "mass surveillance", "orwellian", "security failure", "police",
    "license plate reader", "license plate readers", "alpr", "lpr",
    "creepy", "complaint", "warning", "oppose", "reject", "abuse",
    "do not play", "don't play", "dont play",
}

BRAND_CONTEXT_TERMS = {
    "flock safety", "flocksafety", "flock cameras", "flock camera",
    "flockcameras", "flockcamera", "flock lpr", "flock license plate",
    "flock license plate reader", "flock license plate readers",
}

EXCLUSION_TERMS = {
    "flock of birds", "flock wallpaper", "flock fabric", "safety glasses",
    "safety goggles", "safety gear", "safety first", "safety essentials",
}


@dataclass
class TikTokFinding:
    url: str
    video_id: str
    author_name: str
    author_handle: str
    description: str
    matched_terms: list[str]
    score: int
    severity: str
    created_at: str = ""
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    followers: int | None = None
    duration: float | None = None


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
        if brand.get("id") == brand_id or brand.get("name") == brand_id:
            return brand
    raise KeyError(f"Unknown brand: {brand_id}")


def poll_run(token: str, run: dict) -> dict:
    run_id = run["id"]
    status = run.get("status")
    for _ in range(60):
        if status in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
            break
        time.sleep(5)
        req = urllib.request.Request(
            f"https://api.apify.com/v2/actor-runs/{run_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            run = json.load(resp)["data"]
        status = run.get("status")
    return run


def fetch_items(token: str, run: dict) -> list[dict]:
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        return []
    req = urllib.request.Request(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items?clean=true&format=json",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def run_actor(query: str, max_items: int, max_charge_usd: float, date_range: str = "THIS_WEEK") -> tuple[dict, list[dict]]:
    token = apify_token()
    run_input = {
        "search": [query],
        "maxItems": max_items,
        "endPage": 1,
        "location": "US",
        "dateRange": date_range,
        "sortType": "DATE_POSTED",
    }
    url = f"https://api.apify.com/v2/acts/{APIFY_TIKTOK_ACTOR}/runs?waitForFinish=180&maxTotalChargeUsd={max_charge_usd:.2f}"
    req = urllib.request.Request(
        url,
        data=json.dumps(run_input).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        run = json.load(resp)["data"]
    run = poll_run(token, run)
    return run, fetch_items(token, run)


def text_from_item(item: dict) -> str:
    text = item.get("desc") or item.get("description") or item.get("title") or ""
    return re.sub(r"\s+", " ", str(text)).strip()


def int_metric(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.replace(",", "").isdigit():
        return int(value.replace(",", ""))
    return None


def item_metric(item: dict, *paths: str) -> int | None:
    for path in paths:
        cur = item
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
                break
        value = int_metric(cur)
        if value is not None:
            return value
    return None


def created_at_from_item(item: dict) -> str:
    value = item.get("createTime") or item.get("createdAt") or item.get("uploadedAt")
    ts = int_metric(value)
    if ts:
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p ET")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value or "")


def author_handle(item: dict) -> str:
    handle = item.get("nickname") or item.get("uniqueId") or item.get("authorName")
    author = item.get("author")
    if isinstance(author, dict):
        handle = handle or author.get("uniqueId") or author.get("nickname") or author.get("id")
    elif isinstance(author, str):
        handle = handle or author
    return str(handle or "unknown").lstrip("@")


def author_name(item: dict) -> str:
    author = item.get("author")
    if isinstance(author, dict):
        return str(author.get("nickname") or author.get("uniqueId") or author.get("id") or "unknown")
    return str(author or item.get("nickname") or "unknown")


def video_duration(item: dict) -> float | None:
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    value = video.get("duration") or item.get("duration")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def has_brand_context(text: str, brand: dict) -> bool:
    haystack = text.lower()
    if any(exclusion in haystack for exclusion in EXCLUSION_TERMS | {e.lower() for e in brand.get("exclusions", [])}):
        # Allow explicit Flock-camera context to override generic safety-product exclusions.
        if not any(term in haystack for term in BRAND_CONTEXT_TERMS):
            return False
    canonical_terms = {str(term).lower() for term in brand.get("canonical_terms", [])}
    return any(term in haystack for term in canonical_terms | BRAND_CONTEXT_TERMS)


def classify_item(item: dict, brand: dict) -> TikTokFinding | None:
    description = text_from_item(item)
    url = item.get("url") or ""
    video_id = str(item.get("id") or item_metric(item, "video.id") or "")
    if not url and video_id:
        handle = author_handle(item)
        url = f"https://www.tiktok.com/@{handle}/video/{video_id}"
    if not url:
        return None
    if not has_brand_context(description, brand):
        return None

    haystack = description.lower()
    terms: list[str] = []
    score = 20  # brand context found
    for term in brand.get("negative_keywords", []):
        t = term.lower()
        if t in haystack:
            terms.append(term)
            score += 18 if t in HIGH_RISK_TERMS else 10
    for term in HIGH_RISK_TERMS:
        if term in haystack and term not in [t.lower() for t in terms]:
            terms.append(term)
            score += 18

    views = item_metric(item, "stats.playCount", "playCount", "views")
    likes = item_metric(item, "stats.diggCount", "stats.likeCount", "likes")
    comments = item_metric(item, "stats.commentCount", "comments")
    shares = item_metric(item, "stats.shareCount", "shares")
    followers = item_metric(item, "authorStats.followerCount", "author.followerCount", "followers")

    if views and views >= 1_000_000:
        score += 20
    elif views and views >= 250_000:
        score += 14
    elif views and views >= 50_000:
        score += 8
    if followers and followers >= 100_000:
        score += 8
    elif followers and followers >= 25_000:
        score += 4
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
    return TikTokFinding(
        url=url,
        video_id=video_id,
        author_name=author_name(item),
        author_handle=author_handle(item),
        description=description,
        matched_terms=terms,
        score=score,
        severity=severity,
        created_at=created_at_from_item(item),
        views=views,
        likes=likes,
        comments=comments,
        shares=shares,
        followers=followers,
        duration=video_duration(item),
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


def save_run(sample_dir: Path, run: dict, items: list[dict], query: str, max_items: int, max_charge_usd: float, date_range: str) -> tuple[Path, Path]:
    sample_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    items_path = sample_dir / f"{ts}_scheduled_items.json"
    run_path = sample_dir / f"{ts}_scheduled_run.json"
    items_path.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    safe_run = {k: run.get(k) for k in ["id", "actId", "status", "startedAt", "finishedAt", "defaultDatasetId", "usage", "usageTotalUsd", "chargedEventCounts", "stats", "exitCode", "statusMessage"]}
    safe_run["input_used"] = {"search": [query], "maxItems": max_items, "endPage": 1, "location": "US", "dateRange": date_range, "sortType": "DATE_POSTED", "maxTotalChargeUsd": max_charge_usd}
    run_path.write_text(json.dumps(safe_run, indent=2, ensure_ascii=False))
    return run_path, items_path


def fmt_num(n: int | None) -> str:
    return "unavailable" if n is None else f"{n:,}"


def render_report(brand_name: str, query: str, run: dict, items: list[dict], findings: list[TikTokFinding], new_findings: list[TikTokFinding], run_path: Path, items_path: Path, max_items: int, max_charge_usd: float, date_range: str) -> str:
    now = datetime.now(ZoneInfo("America/New_York"))
    start = now - timedelta(hours=6)
    lines = [
        f"{brand_name} TikTok Apify Pilot",
        "",
        f"Window checked: {start:%Y-%m-%d %I:%M %p ET} – {now:%Y-%m-%d %I:%M %p ET}",
        f"Discovery query: {query}",
        f"Actor: {APIFY_TIKTOK_ACTOR_DISPLAY}",
        f"Run status: {run.get('status')}",
        f"Apify reported usage: ${float(run.get('usageTotalUsd') or 0):.4f}",
        f"Items returned: {len(items)} total TikTok videos from this run",
        f"Brand-relevant/reportable items: {len(findings)}",
        f"New items after dedupe: {len(new_findings)}",
        f"Run ID: {run.get('id')}",
        f"Dataset ID: {run.get('defaultDatasetId')}",
        "",
    ]
    if new_findings:
        lines.append("New reportable TikToks:")
        for idx, f in enumerate(sorted(new_findings, key=lambda x: x.score, reverse=True), start=1):
            excerpt = f.description if len(f.description) <= 420 else f.description[:419].rstrip() + "…"
            lines.extend([
                f"{idx}. [{f.severity.upper()} | score {f.score}] @{f.author_handle} ({f.author_name})",
                f"   URL: {f.url}",
                f"   Created: {f.created_at or 'unavailable'}",
                f"   Engagement: views {fmt_num(f.views)} | likes {fmt_num(f.likes)} | comments {fmt_num(f.comments)} | shares {fmt_num(f.shares)} | author followers {fmt_num(f.followers)}",
                f"   Duration: {f.duration:.1f}s" if f.duration is not None else "   Duration: unavailable",
                f"   Matched terms: {', '.join(f.matched_terms) if f.matched_terms else '(brand context only)' }",
                f"   Caption: {excerpt}",
                "",
            ])
    else:
        lines.extend([
            "No new reportable TikTok videos after brand-context filtering and dedupe.",
            "The actor may still have returned generic false positives for the words 'flock' or 'safety'; those are suppressed unless caption context points to Flock Safety / Flock cameras.",
            "",
        ])
    lines.extend([
        "Pilot cost guardrails:",
        f"- maxItems={max_items}",
        "- endPage=1",
        "- location=US",
        f"- dateRange={date_range}",
        f"- maxTotalChargeUsd=${max_charge_usd:.2f}",
        "- no comments scrape",
        "- no transcript add-on",
        "- no video download/mirroring step outside actor metadata",
        "",
        "Limitations:",
        "- TikTok search can return fuzzy/non-brand results; this pilot filters on explicit Flock Safety/Flock cameras context.",
        "- Coverage is limited to public/search-visible TikTok videos the actor can retrieve.",
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
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="apify-tiktok-monitor-email-", suffix=".txt") as f:
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
    parser.add_argument("--max-items", type=int, default=10)
    parser.add_argument("--max-charge-usd", type=float, default=0.05)
    parser.add_argument("--date-range", default="THIS_WEEK")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--sample-dir", default=str(DEFAULT_SAMPLE_DIR))
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config))
    brand = get_brand(config, args.brand_id)
    run, items = run_actor(args.query, args.max_items, args.max_charge_usd, args.date_range)
    run_path, items_path = save_run(Path(args.sample_dir), run, items, args.query, args.max_items, args.max_charge_usd, args.date_range)

    findings: list[TikTokFinding] = []
    for item in items:
        finding = classify_item(item, brand)
        if finding and finding.score >= 35:
            findings.append(finding)

    state_path = Path(args.state_dir) / f"{brand['id']}_tiktok_seen.json"
    seen = state_seen(state_path)
    new_findings = [f for f in findings if f.url not in seen]
    body = render_report(brand["name"], args.query, run, items, findings, new_findings, run_path, items_path, args.max_items, args.max_charge_usd, args.date_range)
    now = datetime.now(ZoneInfo("America/New_York"))
    subject_suffix = "No New TikToks" if not new_findings else f"{len(new_findings)} New Candidate TikToks"
    subject = f"{brand['name']} TikTok Apify Pilot — {subject_suffix} — {now:%Y-%m-%d %I:%M %p ET}"
    if args.send:
        recipients = config.get("report_recipients", {})
        send_email(recipients.get("to", []), recipients.get("cc", []), subject, body)
        mark_seen(state_path, [f.url for f in new_findings])
    print(body)
    print(f"\n---\nEmail sent: {'yes' if args.send else 'no (dry run)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
