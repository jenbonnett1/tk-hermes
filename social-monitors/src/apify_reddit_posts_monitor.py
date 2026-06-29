#!/usr/bin/env python3
"""Cost-capped Apify Reddit post monitor for Flock Safety.

Discovery actor: harshmaur/reddit-search-scraper

The scheduled run searches recent Reddit posts only. It does not search comments,
communities, or crawl comments under each post by default, because comments can
multiply result count and cost.
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
DEFAULT_SAMPLE_DIR = Path("/opt/data/social-monitors/samples/apify/flock_safety_reddit")
HIMALAYA_BIN = Path("/opt/data/profiles/tk/home/.local/bin/himalaya")
HIMALAYA_CONFIG = Path("/opt/data/profiles/tk/home/.config/himalaya/config.toml")
APIFY_REDDIT_ACTOR = "harshmaur~reddit-search-scraper"
APIFY_REDDIT_ACTOR_DISPLAY = "harshmaur/reddit-search-scraper"

HIGH_RISK_TERMS = {
    "lawsuit", "sue", "illegal", "fraud", "privacy", "surveillance",
    "unconstitutional", "dangerous", "boycott", "scam", "class action",
    "warrantless", "fourth amendment", "tracking", "spying",
    "mass surveillance", "orwellian", "security failure", "police",
    "license plate reader", "alpr", "lpr", "sexual assault", "crime",
    "oppose", "reject", "abuse", "cancel contract", "renewal",
}

@dataclass
class RedditPostFinding:
    url: str
    post_id: str
    subreddit: str
    author_name: str
    title: str
    body: str
    matched_terms: list[str]
    score: int
    severity: str
    created_at: str = ""
    upvotes: int | None = None
    comments_count: int | None = None
    subreddit_subscribers: int | None = None
    outbound_url: str = ""


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


def run_actor(query: str, max_posts: int, max_charge_usd: float, search_time: str = "day") -> tuple[dict, list[dict]]:
    token = apify_token()
    run_input = {
        "searchTerms": [query],
        "searchPosts": True,
        "searchComments": False,
        "searchCommunities": False,
        "withinCommunity": "",
        "searchSort": "new",
        "searchTime": search_time,
        "maxPostsCount": max_posts,
        "maxCommentsCount": 0,
        "maxCommunitiesCount": 0,
        "crawlCommentsPerPost": False,
        "maxCommentsPerPost": 0,
        "includeNSFW": False,
        "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
    }
    url = f"https://api.apify.com/v2/acts/{APIFY_REDDIT_ACTOR}/runs?waitForFinish=180&maxTotalChargeUsd={max_charge_usd:.2f}"
    req = urllib.request.Request(url, data=json.dumps(run_input).encode(), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=240) as resp:
        run = json.load(resp)["data"]
    run = poll_run(token, run)
    return run, fetch_items(token, run)


def poll_run(token: str, run: dict) -> dict:
    run_id = run["id"]
    status = run.get("status")
    for _ in range(60):
        if status in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
            break
        time.sleep(5)
        req = urllib.request.Request(f"https://api.apify.com/v2/actor-runs/{run_id}", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            run = json.load(resp)["data"]
        status = run.get("status")
    return run


def fetch_items(token: str, run: dict) -> list[dict]:
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        return []
    req = urllib.request.Request(f"https://api.apify.com/v2/datasets/{dataset_id}/items?clean=true&format=json", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def text_clean(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("text") or value.get("body") or json.dumps(value, ensure_ascii=False)
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


def post_url(item: dict) -> str:
    return str(item.get("postUrl") or item.get("url") or item.get("permalink") or "")


def excluded(haystack: str, exclusions: list[str]) -> bool:
    return any(ex.lower() in haystack for ex in exclusions)


def brand_context(text: str, terms: list[str] | None = None, radius: int = 600) -> str:
    """Return text near brand mentions so unrelated long-post risk terms don't dominate scoring."""
    terms = terms or ["flock safety", "flock cameras", "flock camera", "flock"]
    low = text.lower()
    windows: list[str] = []
    for term in terms:
        start = 0
        while True:
            idx = low.find(term, start)
            if idx < 0:
                break
            windows.append(text[max(0, idx - radius): idx + len(term) + radius])
            start = idx + len(term)
    return "\n".join(windows) if windows else text[: radius * 2]


def classify_item(item: dict, negative_keywords: list[str], exclusions: list[str] | None = None) -> RedditPostFinding | None:
    if item.get("dataType") and item.get("dataType") != "post":
        return None
    url = post_url(item)
    if not url:
        return None
    title = text_clean(item.get("title") or item.get("postTitle"))
    body = text_clean(item.get("body") or item.get("selftext") or item.get("description"))
    subreddit = str(item.get("communityName") or item.get("subredditName") or item.get("parsedCommunityName") or "")
    author_name = str(item.get("authorName") or item.get("username") or "unknown")
    full_text = "\n".join([title, body, subreddit, url])
    haystack = full_text.lower()
    if excluded(haystack, exclusions or []):
        return None
    risk_haystack = brand_context(full_text).lower()
    matched: list[str] = []
    score = 0
    for term in negative_keywords:
        t = term.lower()
        if t in risk_haystack:
            matched.append(term)
            score += 18 if t in HIGH_RISK_TERMS else 10
    matched_lowers = {m.lower() for m in matched}
    high_hits = []
    for term in HIGH_RISK_TERMS:
        if term in risk_haystack:
            high_hits.append(term)
            if term not in matched_lowers:
                matched.append(term)
                score += 18
    if high_hits:
        score += 25
    upvotes = metric(item, "upVotes", "score", "postUpVotes")
    comments = metric(item, "commentsCount", "postCommentsCount", "numComments")
    subscribers = metric(item, "subredditSubscribers", "membersCount")
    engagement = max(v or 0 for v in [upvotes, comments])
    if engagement >= 10_000:
        score += 15
    elif engagement >= 1_000:
        score += 10
    elif engagement >= 100:
        score += 6
    elif engagement >= 25:
        score += 3
    if subscribers and subscribers >= 1_000_000:
        score += 8
    elif subscribers and subscribers >= 100_000:
        score += 4
    score = min(score, 100)
    severity = "high" if score >= 70 else "medium" if score >= 35 else "low"
    return RedditPostFinding(url=url, post_id=str(item.get("parsedId") or item.get("id") or ""), subreddit=subreddit, author_name=author_name, title=title, body=body, matched_terms=matched, score=score, severity=severity, created_at=str(item.get("createdAt") or item.get("postCreatedAt") or ""), upvotes=upvotes, comments_count=comments, subreddit_subscribers=subscribers, outbound_url=str(item.get("contentUrl") or item.get("urlOverriddenByDest") or ""))


def state_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def mark_seen(path: Path, urls: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = state_seen(path)
    seen.update(urls)
    path.write_text(json.dumps(sorted(seen), indent=2))


def save_run(sample_dir: Path, run: dict, items: list[dict], query: str, max_posts: int, max_charge_usd: float, search_time: str) -> tuple[Path, Path]:
    sample_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    items_path = sample_dir / f"{ts}_scheduled_items.json"
    run_path = sample_dir / f"{ts}_scheduled_run.json"
    items_path.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    safe_run = {k: run.get(k) for k in ["id", "actId", "status", "statusMessage", "startedAt", "finishedAt", "defaultDatasetId", "usage", "usageTotalUsd", "chargedEventCounts", "accountedChargedEventCounts", "stats", "exitCode"]}
    safe_run["input_used"] = {"searchTerms": [query], "searchPosts": True, "searchComments": False, "searchCommunities": False, "searchSort": "new", "searchTime": search_time, "maxPostsCount": max_posts, "crawlCommentsPerPost": False, "maxTotalChargeUsd": max_charge_usd}
    run_path.write_text(json.dumps(safe_run, indent=2, ensure_ascii=False))
    return run_path, items_path


def fmt_num(n: int | None) -> str:
    return "unavailable" if n is None else f"{n:,}"


def excerpt(text: str, limit: int = 520) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def excerpt_around_brand(text: str, limit: int = 520) -> str:
    snippet = brand_context(text, radius=max(240, limit // 2))
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return excerpt(snippet, limit)


def render_report(brand_name: str, query: str, run: dict, findings: list[RedditPostFinding], new_findings: list[RedditPostFinding], run_path: Path, items_path: Path) -> str:
    now = datetime.now(ZoneInfo("America/New_York"))
    start = now - timedelta(hours=6)
    lines = [
        f"{brand_name} Reddit Posts Apify Monitor", "",
        f"Window checked: {start:%Y-%m-%d %I:%M %p ET} – {now:%Y-%m-%d %I:%M %p ET}",
        f"Discovery query: {query}", f"Actor: {APIFY_REDDIT_ACTOR_DISPLAY}",
        f"Run status: {run.get('status')}", f"Apify reported usage: ${float(run.get('usageTotalUsd') or 0):.4f}",
        f"Dataset items returned: {run.get('itemCount', 'see audit file')}",
        f"Reportable items before dedupe: {len(findings)}", f"New items after dedupe: {len(new_findings)}",
        f"Run ID: {run.get('id')}", f"Dataset ID: {run.get('defaultDatasetId')}", "",
    ]
    if new_findings:
        lines.append("New reportable Reddit posts:")
        for idx, f in enumerate(sorted(new_findings, key=lambda x: x.score, reverse=True), start=1):
            lines.extend([
                f"{idx}. [{f.severity.upper()} | score {f.score}] {f.title}",
                f"   URL: {f.url}", f"   Subreddit: {f.subreddit or 'unavailable'} | Author: u/{f.author_name}",
                f"   Published: {f.created_at or 'unavailable'}", f"   Outbound URL: {f.outbound_url or 'none'}",
                f"   Engagement: upvotes {fmt_num(f.upvotes)} | comments {fmt_num(f.comments_count)} | subreddit subscribers {fmt_num(f.subreddit_subscribers)}",
                f"   Matched terms: {', '.join(f.matched_terms) if f.matched_terms else '(none)'}",
                f"   Body: {excerpt_around_brand(f.title + ' ' + f.body) if (f.title or f.body) else '(no body text available)'}", "",
            ])
    else:
        lines.extend(["No new medium/high-risk Reddit posts after scoring and dedupe.", "Neutral or already-seen brand mentions are saved in raw audit files but suppressed from alerts.", ""])
    lines.extend([
        "Cost guardrails:", "- maxPostsCount and maxTotalChargeUsd set explicitly every run", "- searchTime=day and searchSort=new", "- searchComments/searchCommunities disabled", "- crawlCommentsPerPost disabled", "",
        "Limitations:", "- Apify/Reddit search may miss deleted, private, blocked, or non-indexed activity.", "- Comments are not searched in the scheduled pilot unless separately approved.", "- Already-seen URLs/post IDs are excluded from repeat alerts only after a send-mode run.", "",
        "Local audit files:", f"- {run_path}", f"- {items_path}",
    ])
    return "\n".join(lines)


def send_email(to: list[str], cc: list[str], subject: str, body: str) -> str:
    cc_header = f"Cc: {', '.join(cc)}\n" if cc else ""
    message = "From: Hermes Reports <hermes-agent@trybemedia.com>\n" + f"To: {', '.join(to)}\n" + cc_header + f"Subject: {subject}\n\n{body}\n"
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="apify-reddit-monitor-email-", suffix=".txt") as f:
        f.write(message); msg_path = f.name
    env = os.environ.copy(); env["HIMALAYA_CONFIG"] = str(HIMALAYA_CONFIG)
    try:
        with open(msg_path, "rb") as stdin:
            proc = subprocess.run([str(HIMALAYA_BIN), "template", "send"], stdin=stdin, env=env, text=False, capture_output=True, timeout=120)
    finally:
        Path(msg_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace") or proc.stdout.decode(errors="replace"))
    return proc.stdout.decode(errors="replace").strip()


def run_monitor(config: dict, brand_id: str, query: str, state_dir: Path, sample_dir: Path, max_posts: int, max_charge_usd: float, send: bool, search_time: str = "day") -> tuple[str, list[RedditPostFinding], Path, Path]:
    brand = get_brand(config, brand_id)
    run, items = run_actor(query, max_posts, max_charge_usd, search_time=search_time)
    run["itemCount"] = len(items)
    run_path, items_path = save_run(sample_dir, run, items, query, max_posts, max_charge_usd, search_time)
    scored = [f for item in items if (f := classify_item(item, brand.get("negative_keywords", []), brand.get("exclusions", [])))]
    reportable = [f for f in scored if f.score >= 35]
    state_path = state_dir / f"{brand['id']}_reddit_posts_seen.json"
    seen = state_seen(state_path)
    new_findings = [f for f in reportable if f.url not in seen and (f.post_id not in seen if f.post_id else True)]
    body = render_report(brand["name"], query, run, reportable, new_findings, run_path, items_path)
    now = datetime.now(ZoneInfo("America/New_York"))
    subject_suffix = "No Negative Threats" if not new_findings else f"{len(new_findings)} Potential Negative Items"
    subject = f"{brand['name']} Reddit Posts Apify Monitor — {subject_suffix} — {now:%Y-%m-%d %I:%M %p ET}"
    if send:
        recipients = config.get("report_recipients", {})
        send_email(recipients.get("to", []), recipients.get("cc", []), subject, body)
        mark_seen(state_path, [f.url for f in new_findings] + [f.post_id for f in new_findings if f.post_id])
    return body, new_findings, run_path, items_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand-id", default="flock_safety")
    parser.add_argument("--query", default='"Flock Safety"')
    parser.add_argument("--max-posts", type=int, default=10)
    parser.add_argument("--max-charge-usd", type=float, default=0.04)
    parser.add_argument("--search-time", default="day", choices=["hour", "day", "week", "month", "year", "all"])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--sample-dir", default=str(DEFAULT_SAMPLE_DIR))
    parser.add_argument("--send", action="store_true", help="Send report email and mark reported URLs/post IDs as seen")
    args = parser.parse_args(argv)
    body, findings, _run_path, _items_path = run_monitor(load_config(Path(args.config)), args.brand_id, args.query, Path(args.state_dir), Path(args.sample_dir), args.max_posts, args.max_charge_usd, args.send, args.search_time)
    print(body)
    print(f"\n---\nNew reportable findings: {len(findings)}")
    print(f"Email sent: {'yes' if args.send else 'no (dry run)'}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
