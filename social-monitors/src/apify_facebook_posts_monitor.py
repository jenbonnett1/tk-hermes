#!/usr/bin/env python3
"""Cost-capped Apify Facebook post monitor for Flock Safety.

Discovery actor: scrapeforge/facebook-search-posts

This replaces the weak Facebook side of the search-index MVP with a paid but
strictly capped Apify actor run. It does not download media or enrich comments;
it only scores post metadata/text returned by the actor.
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
DEFAULT_SAMPLE_DIR = Path("/opt/data/social-monitors/samples/apify/flock_safety_facebook_posts")
HIMALAYA_BIN = Path("/opt/data/profiles/tk/home/.local/bin/himalaya")
HIMALAYA_CONFIG = Path("/opt/data/profiles/tk/home/.config/himalaya/config.toml")
APIFY_FACEBOOK_ACTOR = "scrapeforge~facebook-search-posts"
APIFY_FACEBOOK_ACTOR_DISPLAY = "scrapeforge/facebook-search-posts"

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
    "vandalized",
    "destroying",
    "destruction of property",
}

@dataclass
class FacebookPostFinding:
    url: str
    post_id: str
    author_name: str
    author_url: str
    message: str
    matched_terms: list[str]
    score: int
    severity: str
    timestamp: int | None = None
    reactions_count: int | None = None
    comments_count: int | None = None
    reshare_count: int | None = None
    video_view_count: int | None = None
    media_type: str = "post"


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


def run_actor(query: str, max_results: int, max_charge_usd: float, recent_posts: bool = True) -> tuple[dict, list[dict]]:
    """Run the Facebook search actor with explicit result and spend caps."""
    token = apify_token()
    run_input = {
        "query": query,
        "search_type": "posts",
        "max_results": max_results,
        "recent_posts": recent_posts,
    }
    url = f"https://api.apify.com/v2/acts/{APIFY_FACEBOOK_ACTOR}/runs?waitForFinish=180&maxTotalChargeUsd={max_charge_usd:.2f}"
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


def text_clean(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("text") or value.get("message") or json.dumps(value, ensure_ascii=False)
    return re.sub(r"\s+", " ", str(value)).strip()


def metric(item: dict, *keys: str) -> int | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.replace(",", "").isdigit():
            return int(value.replace(",", ""))
    return None


def author_fields(item: dict) -> tuple[str, str]:
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    return str(author.get("name") or item.get("author_title") or "unknown"), str(author.get("url") or "")


def post_url(item: dict) -> str:
    return str(item.get("url") or item.get("post_url") or item.get("postUrl") or item.get("link") or "")


def media_type_from_item(item: dict) -> str:
    if item.get("video") or item.get("video_files") or item.get("video_thumbnail") or item.get("video_view_count"):
        return "video"
    if item.get("image") or item.get("album_preview"):
        return "image"
    return str(item.get("type") or "post")


def classify_item(item: dict, negative_keywords: list[str]) -> FacebookPostFinding | None:
    url = post_url(item)
    if not url:
        return None
    message = text_clean(item.get("message") or item.get("message_rich") or item.get("description"))
    author_name, author_url = author_fields(item)
    haystack = "\n".join([message, author_name, url]).lower()
    matched: list[str] = []
    score = 0

    for term in negative_keywords:
        t = term.lower()
        if t in haystack:
            matched.append(term)
            score += 18 if t in HIGH_RISK_TERMS else 10
    matched_lowers = {m.lower() for m in matched}
    high_hits = []
    for term in HIGH_RISK_TERMS:
        if term in haystack:
            high_hits.append(term)
            if term not in matched_lowers:
                matched.append(term)
                score += 18
    if high_hits:
        score += 25

    reactions = metric(item, "reactions_count", "reactionCount", "reactionsCount")
    comments = metric(item, "comments_count", "commentCount", "commentsCount")
    reshares = metric(item, "reshare_count", "shareCount", "sharesCount")
    views = metric(item, "video_view_count", "videoViewCount")

    engagement = max(v or 0 for v in [reactions, comments, reshares, views])
    if engagement >= 100_000:
        score += 18
    elif engagement >= 10_000:
        score += 12
    elif engagement >= 1_000:
        score += 7
    elif engagement >= 100:
        score += 4

    # The actor can return neutral Flock public-safety posts. Keep those below
    # report threshold unless there is a clear risk term or material engagement.
    score = min(score, 100)
    severity = "high" if score >= 70 else "medium" if score >= 35 else "low"

    return FacebookPostFinding(
        url=url,
        post_id=str(item.get("post_id") or ""),
        author_name=author_name,
        author_url=author_url,
        message=message,
        matched_terms=matched,
        score=score,
        severity=severity,
        timestamp=metric(item, "timestamp", "createdAt", "publishedAt"),
        reactions_count=reactions,
        comments_count=comments,
        reshare_count=reshares,
        video_view_count=views,
        media_type=media_type_from_item(item),
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


def save_run(sample_dir: Path, run: dict, items: list[dict], query: str, max_results: int, max_charge_usd: float) -> tuple[Path, Path]:
    sample_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    items_path = sample_dir / f"{ts}_scheduled_items.json"
    run_path = sample_dir / f"{ts}_scheduled_run.json"
    items_path.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    safe_run = {k: run.get(k) for k in ["id", "actId", "status", "statusMessage", "startedAt", "finishedAt", "defaultDatasetId", "usage", "usageTotalUsd", "chargedEventCounts", "accountedChargedEventCounts", "stats", "exitCode"]}
    safe_run["input_used"] = {"query": query, "search_type": "posts", "max_results": max_results, "maxTotalChargeUsd": max_charge_usd}
    run_path.write_text(json.dumps(safe_run, indent=2, ensure_ascii=False))
    return run_path, items_path


def fmt_num(n: int | None) -> str:
    return "unavailable" if n is None else f"{n:,}"


def fmt_timestamp(ts: int | None) -> str:
    if not ts:
        return "unavailable"
    try:
        return datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p ET")
    except Exception:
        return str(ts)


def excerpt(text: str, limit: int = 520) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_report(
    brand_name: str,
    query: str,
    run: dict,
    findings: list[FacebookPostFinding],
    new_findings: list[FacebookPostFinding],
    run_path: Path,
    items_path: Path,
) -> str:
    now = datetime.now(ZoneInfo("America/New_York"))
    start = now - timedelta(hours=6)
    lines = [
        f"{brand_name} Facebook Posts Apify Monitor",
        "",
        f"Window checked: {start:%Y-%m-%d %I:%M %p ET} – {now:%Y-%m-%d %I:%M %p ET}",
        f"Discovery query: {query}",
        f"Actor: {APIFY_FACEBOOK_ACTOR_DISPLAY}",
        f"Run status: {run.get('status')}",
        f"Apify reported usage: ${float(run.get('usageTotalUsd') or 0):.4f}",
        f"Dataset items returned: {run.get('itemCount', 'see audit file')}",
        f"Reportable items before dedupe: {len(findings)}",
        f"New items after dedupe: {len(new_findings)}",
        f"Run ID: {run.get('id')}",
        f"Dataset ID: {run.get('defaultDatasetId')}",
        "",
    ]
    if new_findings:
        lines.append("New reportable Facebook posts:")
        for idx, f in enumerate(sorted(new_findings, key=lambda x: x.score, reverse=True), start=1):
            lines.extend([
                f"{idx}. [{f.severity.upper()} | score {f.score}] {f.author_name}",
                f"   URL: {f.url}",
                f"   Author URL: {f.author_url or 'unavailable'}",
                f"   Published: {fmt_timestamp(f.timestamp)}",
                f"   Media type: {f.media_type}",
                f"   Engagement: reactions {fmt_num(f.reactions_count)} | comments {fmt_num(f.comments_count)} | reshares {fmt_num(f.reshare_count)} | video views {fmt_num(f.video_view_count)}",
                f"   Matched terms: {', '.join(f.matched_terms) if f.matched_terms else '(none)'}",
                f"   Message: {excerpt(f.message) if f.message else '(no message text available)'}",
                "",
            ])
    else:
        lines.extend([
            "No new medium/high-risk Facebook posts after scoring and dedupe.",
            "Low-confidence or neutral brand mentions are saved in raw audit files but suppressed from alerts.",
            "",
        ])
    lines.extend([
        "Cost guardrails:",
        "- search_type=posts",
        "- comments/reactions/profile enrichment disabled beyond fields returned by the actor",
        "- no media download or transcript add-ons",
        "- max_results and maxTotalChargeUsd set explicitly every run",
        "",
        "Limitations:",
        "- Apify may miss private, restricted, deleted, or non-indexed Facebook activity.",
        "- Actor pricing/coverage can change; keep checking run usage while in pilot.",
        "- Already-seen URLs are excluded from repeat alerts only after a send-mode run.",
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
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="apify-fb-monitor-email-", suffix=".txt") as f:
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


def run_monitor(
    config: dict,
    brand_id: str,
    query: str,
    state_dir: Path,
    sample_dir: Path,
    max_results: int,
    max_charge_usd: float,
    send: bool,
    recent_posts: bool = True,
) -> tuple[str, list[FacebookPostFinding], Path, Path]:
    brand = get_brand(config, brand_id)
    run, items = run_actor(query, max_results, max_charge_usd, recent_posts=recent_posts)
    run["itemCount"] = len(items)
    run_path, items_path = save_run(sample_dir, run, items, query, max_results, max_charge_usd)
    scored = [f for item in items if (f := classify_item(item, brand.get("negative_keywords", [])))]
    reportable = [f for f in scored if f.score >= 35]
    state_path = state_dir / f"{brand['id']}_facebook_posts_seen.json"
    seen = state_seen(state_path)
    new_findings = [f for f in reportable if f.url not in seen and (f.post_id not in seen if f.post_id else True)]
    body = render_report(brand["name"], query, run, reportable, new_findings, run_path, items_path)
    now = datetime.now(ZoneInfo("America/New_York"))
    subject_suffix = "No Negative Threats" if not new_findings else f"{len(new_findings)} Potential Negative Items"
    subject = f"{brand['name']} Facebook Posts Apify Monitor — {subject_suffix} — {now:%Y-%m-%d %I:%M %p ET}"
    if send:
        recipients = config.get("report_recipients", {})
        send_email(recipients.get("to", []), recipients.get("cc", []), subject, body)
        mark_seen(state_path, [f.url for f in new_findings] + [f.post_id for f in new_findings if f.post_id])
    return body, new_findings, run_path, items_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand-id", default="flock_safety")
    parser.add_argument("--query", default='"Flock Safety"')
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--max-charge-usd", type=float, default=0.05)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--sample-dir", default=str(DEFAULT_SAMPLE_DIR))
    parser.add_argument("--all-posts", action="store_true", help="Do not request recent_posts sorting/filtering")
    parser.add_argument("--send", action="store_true", help="Send the report email and mark reported URLs/post IDs as seen")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config))
    body, findings, _run_path, _items_path = run_monitor(
        config=config,
        brand_id=args.brand_id,
        query=args.query,
        state_dir=Path(args.state_dir),
        sample_dir=Path(args.sample_dir),
        max_results=args.max_results,
        max_charge_usd=args.max_charge_usd,
        send=args.send,
        recent_posts=not args.all_posts,
    )
    print(body)
    print(f"\n---\nNew reportable findings: {len(findings)}")
    print(f"Email sent: {'yes' if args.send else 'no (dry run)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
