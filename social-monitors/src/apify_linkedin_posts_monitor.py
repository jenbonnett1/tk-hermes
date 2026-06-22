#!/usr/bin/env python3
"""Cost-capped Apify LinkedIn post monitor for Flock Safety.

Discovery actor: harvestapi/linkedin-post-search

The scheduled run searches recent public LinkedIn posts only. It keeps profile,
reaction, and comment enrichment disabled to avoid cost multipliers.
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
DEFAULT_SAMPLE_DIR = Path("/opt/data/social-monitors/samples/apify/flock_safety_linkedin")
HIMALAYA_BIN = Path("/opt/data/profiles/tk/home/.local/bin/himalaya")
HIMALAYA_CONFIG = Path("/opt/data/profiles/tk/home/.config/himalaya/config.toml")
APIFY_LINKEDIN_ACTOR = "harvestapi~linkedin-post-search"
APIFY_LINKEDIN_ACTOR_DISPLAY = "harvestapi/linkedin-post-search"

HIGH_RISK_TERMS = {
    "lawsuit", "sue", "illegal", "fraud", "privacy", "surveillance",
    "unconstitutional", "dangerous", "boycott", "scam", "class action",
    "warrantless", "fourth amendment", "tracking", "spying",
    "mass surveillance", "orwellian", "security failure", "police",
    "license plate reader", "alpr", "lpr",
}

@dataclass
class LinkedInPostFinding:
    url: str
    post_id: str
    author_name: str
    author_url: str
    content: str
    matched_terms: list[str]
    score: int
    severity: str
    posted_at: str = ""
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    author_info: str = ""


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


def run_actor(query: str, max_posts: int, max_charge_usd: float, posted_limit: str = "24h") -> tuple[dict, list[dict]]:
    token = apify_token()
    run_input = {
        "searchQueries": [query],
        "maxPosts": max_posts,
        "postedLimit": posted_limit,
        "sortBy": "date",
        "contentType": "all",
        "profileScraperMode": "short",
        "scrapeReactions": False,
        "scrapeComments": False,
    }
    url = f"https://api.apify.com/v2/acts/{APIFY_LINKEDIN_ACTOR}/runs?waitForFinish=180&maxTotalChargeUsd={max_charge_usd:.2f}"
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
        value = value.get("text") or value.get("content") or json.dumps(value, ensure_ascii=False)
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
    return str(item.get("linkedinUrl") or item.get("shareLinkedinUrl") or item.get("url") or "")


def author_fields(item: dict) -> tuple[str, str, str]:
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    return str(author.get("name") or item.get("authorName") or "unknown"), str(author.get("linkedinUrl") or ""), str(author.get("info") or "")


def posted_at(item: dict) -> str:
    posted = item.get("postedAt") if isinstance(item.get("postedAt"), dict) else {}
    return str(posted.get("date") or posted.get("postedAgoText") or item.get("createdAt") or "")


def engagement(item: dict) -> tuple[int | None, int | None, int | None]:
    eng = item.get("engagement") if isinstance(item.get("engagement"), dict) else {}
    return metric(eng, "likes", "reactions") or metric(item, "likes", "likesCount"), metric(eng, "comments") or metric(item, "comments", "commentsCount"), metric(eng, "shares") or metric(item, "shares", "sharesCount")


def excluded(haystack: str, exclusions: list[str]) -> bool:
    return any(ex.lower() in haystack for ex in exclusions)


def classify_item(item: dict, negative_keywords: list[str], exclusions: list[str] | None = None) -> LinkedInPostFinding | None:
    url = post_url(item)
    if not url:
        return None
    content = text_clean(item.get("content") or item.get("text") or item.get("description"))
    author_name, author_url, author_info = author_fields(item)
    haystack = "\n".join([content, author_name, url]).lower()
    if excluded(haystack, exclusions or []):
        return None
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
    likes, comments, shares = engagement(item)
    max_eng = max(v or 0 for v in [likes, comments, shares])
    if max_eng >= 10_000:
        score += 12
    elif max_eng >= 1_000:
        score += 7
    elif max_eng >= 100:
        score += 4
    score = min(score, 100)
    severity = "high" if score >= 70 else "medium" if score >= 35 else "low"
    return LinkedInPostFinding(url=url, post_id=str(item.get("id") or item.get("entityId") or ""), author_name=author_name, author_url=author_url, author_info=author_info, content=content, matched_terms=matched, score=score, severity=severity, posted_at=posted_at(item), likes=likes, comments=comments, shares=shares)


def state_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def mark_seen(path: Path, urls: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = state_seen(path)
    seen.update(urls)
    path.write_text(json.dumps(sorted(seen), indent=2))


def save_run(sample_dir: Path, run: dict, items: list[dict], query: str, max_posts: int, max_charge_usd: float, posted_limit: str) -> tuple[Path, Path]:
    sample_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    items_path = sample_dir / f"{ts}_scheduled_items.json"
    run_path = sample_dir / f"{ts}_scheduled_run.json"
    items_path.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    safe_run = {k: run.get(k) for k in ["id", "actId", "status", "statusMessage", "startedAt", "finishedAt", "defaultDatasetId", "usage", "usageTotalUsd", "chargedEventCounts", "accountedChargedEventCounts", "stats", "exitCode"]}
    safe_run["input_used"] = {"searchQueries": [query], "maxPosts": max_posts, "postedLimit": posted_limit, "sortBy": "date", "profileScraperMode": "short", "scrapeReactions": False, "scrapeComments": False, "maxTotalChargeUsd": max_charge_usd}
    run_path.write_text(json.dumps(safe_run, indent=2, ensure_ascii=False))
    return run_path, items_path


def fmt_num(n: int | None) -> str:
    return "unavailable" if n is None else f"{n:,}"


def excerpt(text: str, limit: int = 520) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_report(brand_name: str, query: str, run: dict, findings: list[LinkedInPostFinding], new_findings: list[LinkedInPostFinding], run_path: Path, items_path: Path) -> str:
    now = datetime.now(ZoneInfo("America/New_York"))
    start = now - timedelta(hours=6)
    lines = [
        f"{brand_name} LinkedIn Posts Apify Monitor", "",
        f"Window checked: {start:%Y-%m-%d %I:%M %p ET} – {now:%Y-%m-%d %I:%M %p ET}",
        f"Discovery query: {query}", f"Actor: {APIFY_LINKEDIN_ACTOR_DISPLAY}",
        f"Run status: {run.get('status')}", f"Apify reported usage: ${float(run.get('usageTotalUsd') or 0):.4f}",
        f"Dataset items returned: {run.get('itemCount', 'see audit file')}",
        f"Reportable items before dedupe: {len(findings)}", f"New items after dedupe: {len(new_findings)}",
        f"Run ID: {run.get('id')}", f"Dataset ID: {run.get('defaultDatasetId')}", "",
    ]
    if new_findings:
        lines.append("New reportable LinkedIn posts:")
        for idx, f in enumerate(sorted(new_findings, key=lambda x: x.score, reverse=True), start=1):
            lines.extend([
                f"{idx}. [{f.severity.upper()} | score {f.score}] {f.author_name}",
                f"   URL: {f.url}", f"   Author URL: {f.author_url or 'unavailable'}",
                f"   Author info: {f.author_info or 'unavailable'}", f"   Posted: {f.posted_at or 'unavailable'}",
                f"   Engagement: likes {fmt_num(f.likes)} | comments {fmt_num(f.comments)} | shares {fmt_num(f.shares)}",
                f"   Matched terms: {', '.join(f.matched_terms) if f.matched_terms else '(none)'}",
                f"   Content: {excerpt(f.content) if f.content else '(no content text available)'}", "",
            ])
    else:
        lines.extend(["No new medium/high-risk LinkedIn posts after scoring and dedupe.", "Neutral or off-topic brand mentions are saved in raw audit files but suppressed from alerts.", ""])
    lines.extend([
        "Cost guardrails:", "- maxPosts and maxTotalChargeUsd set explicitly every run", "- postedLimit=24h and sortBy=date", "- profileScraperMode=short", "- reaction/comment/profile enrichment disabled", "",
        "Limitations:", "- Apify may miss restricted, deleted, or non-indexed LinkedIn activity.", "- LinkedIn search can include phrase false positives; scoring/exclusions suppress neutral posts.", "- Already-seen URLs/post IDs are excluded from repeat alerts only after a send-mode run.", "",
        "Local audit files:", f"- {run_path}", f"- {items_path}",
    ])
    return "\n".join(lines)


def send_email(to: list[str], cc: list[str], subject: str, body: str) -> str:
    message = "From: Hermes Reports <hermes-agent@trybemedia.com>\n" + f"To: {', '.join(to)}\n" + f"Cc: {', '.join(cc)}\n" + f"Subject: {subject}\n\n{body}\n"
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="apify-linkedin-monitor-email-", suffix=".txt") as f:
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


def run_monitor(config: dict, brand_id: str, query: str, state_dir: Path, sample_dir: Path, max_posts: int, max_charge_usd: float, send: bool, posted_limit: str = "24h") -> tuple[str, list[LinkedInPostFinding], Path, Path]:
    brand = get_brand(config, brand_id)
    run, items = run_actor(query, max_posts, max_charge_usd, posted_limit=posted_limit)
    run["itemCount"] = len(items)
    run_path, items_path = save_run(sample_dir, run, items, query, max_posts, max_charge_usd, posted_limit)
    scored = [f for item in items if (f := classify_item(item, brand.get("negative_keywords", []), brand.get("exclusions", [])))]
    reportable = [f for f in scored if f.score >= 35]
    state_path = state_dir / f"{brand['id']}_linkedin_posts_seen.json"
    seen = state_seen(state_path)
    new_findings = [f for f in reportable if f.url not in seen and (f.post_id not in seen if f.post_id else True)]
    body = render_report(brand["name"], query, run, reportable, new_findings, run_path, items_path)
    now = datetime.now(ZoneInfo("America/New_York"))
    subject_suffix = "No Negative Threats" if not new_findings else f"{len(new_findings)} Potential Negative Items"
    subject = f"{brand['name']} LinkedIn Posts Apify Monitor — {subject_suffix} — {now:%Y-%m-%d %I:%M %p ET}"
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
    parser.add_argument("--max-charge-usd", type=float, default=0.05)
    parser.add_argument("--posted-limit", default="24h")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--sample-dir", default=str(DEFAULT_SAMPLE_DIR))
    parser.add_argument("--send", action="store_true", help="Send report email and mark reported URLs/post IDs as seen")
    args = parser.parse_args(argv)
    body, findings, _run_path, _items_path = run_monitor(load_config(Path(args.config)), args.brand_id, args.query, Path(args.state_dir), Path(args.sample_dir), args.max_posts, args.max_charge_usd, args.send, args.posted_limit)
    print(body)
    print(f"\n---\nNew reportable findings: {len(findings)}")
    print(f"Email sent: {'yes' if args.send else 'no (dry run)'}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
