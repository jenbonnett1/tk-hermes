#!/usr/bin/env python3
"""Generate Flock Safety general sentiment platform reports from latest monitor audit data.

This script is intentionally separate from the existing negative/risk monitors. It
keeps the current jobs untouched while producing a second report stream that covers
positive, neutral, mixed, and negative public/social mentions where the underlying
platform monitor saved raw Apify items.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

CONFIG_PATH = Path("/opt/data/social-monitors/config/brands.json")
STATE_DIR = Path("/opt/data/social-monitors/state/sentiment")
HIMALAYA_BIN = Path("/opt/data/profiles/tk/home/.local/bin/himalaya")
HIMALAYA_CONFIG = Path("/opt/data/profiles/tk/home/.config/himalaya/config.toml")

PLATFORM_SAMPLE_DIRS = {
    "facebook": Path("/opt/data/social-monitors/samples/apify/flock_safety_facebook_posts"),
    "linkedin": Path("/opt/data/social-monitors/samples/apify/flock_safety_linkedin"),
    "reddit": Path("/opt/data/social-monitors/samples/apify/flock_safety_reddit"),
    "instagram": Path("/opt/data/social-monitors/samples/apify/flock_safety_instagram_reels"),
    "tiktok": Path("/opt/data/social-monitors/samples/apify/flock_safety_tiktok"),
}

PLATFORM_LABELS = {
    "facebook": "Facebook",
    "linkedin": "LinkedIn",
    "reddit": "Reddit",
    "instagram": "Instagram/Reels",
    "tiktok": "TikTok",
}

POSITIVE_TERMS = {
    "safe", "safer", "safety", "solved", "solve", "helped", "helpful", "protect", "protected",
    "prevention", "prevent", "recovered", "recovery", "arrest", "arrested", "apprehended",
    "success", "effective", "valuable", "support", "thank", "thanks", "great", "good",
    "excellent", "love", "recommend", "community safety", "public safety", "crime reduction",
    "reduce crime", "reduced crime", "found", "located", "partnership", "grateful", "kudos",
}

NEGATIVE_TERMS = {
    "privacy", "surveillance", "lawsuit", "unconstitutional", "illegal", "creepy", "tracking",
    "spying", "spy", "warrantless", "boycott", "complaint", "scam", "dangerous", "concern",
    "concerns", "civil liberties", "mass surveillance", "orwellian", "vandalized", "destroying",
    "destroyed", "security failure", "breach", "abuse", "racist", "bias", "false positive",
}

BRAND_CONTEXT_TERMS = {
    "flock safety", "flocksafety", "flock cameras", "flock camera", "flock lpr",
    "flock license plate", "flock license plate reader", "flock license plate readers",
    "flock alpr", "flock"  # retained only when paired with safety/camera terms by source monitors
}

EXCLUSIONS = {"flock of birds", "flock wallpaper", "flock fabric", "safety glasses"}


@dataclass
class SentimentFinding:
    platform: str
    url: str
    author: str
    text: str
    created_at: str
    engagement: int
    sentiment_score: int
    sentiment_label: str
    positive_terms: list[str]
    negative_terms: list[str]


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def latest_items_file(platform: str) -> Path | None:
    sample_dir = PLATFORM_SAMPLE_DIRS[platform]
    if not sample_dir.exists():
        return None
    files = sorted(sample_dir.glob("*_scheduled_items.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("text") or value.get("content") or value.get("message") or json.dumps(value, ensure_ascii=False)
    return re.sub(r"\s+", " ", str(value)).strip()


def nested(item: dict[str, Any], path: str) -> Any:
    cur: Any = item
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def intish(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = value.replace(",", "").strip()
        if digits.isdigit():
            return int(digits)
    return 0


def first_int(item: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = nested(item, key) if "." in key else item.get(key)
        parsed = intish(value)
        if parsed:
            return parsed
    return 0


def text_for(platform: str, item: dict[str, Any]) -> str:
    if platform == "facebook":
        return clean_text(item.get("message") or item.get("message_rich") or item.get("description"))
    if platform == "linkedin":
        return clean_text(item.get("content") or item.get("text") or item.get("description"))
    if platform == "reddit":
        return clean_text(item.get("body") or item.get("text") or item.get("selftext") or item.get("title"))
    if platform == "instagram":
        caption = item.get("caption")
        if isinstance(caption, dict):
            return clean_text(caption.get("text"))
        return clean_text(item.get("caption") or item.get("title") or item.get("text"))
    if platform == "tiktok":
        return clean_text(item.get("desc") or item.get("description") or item.get("text"))
    return clean_text(item)


def url_for(platform: str, item: dict[str, Any]) -> str:
    if platform == "instagram":
        code = item.get("code") or item.get("shortCode") or item.get("id")
        return str(item.get("url") or (f"https://www.instagram.com/reel/{code}/" if code else ""))
    if platform == "linkedin":
        return str(item.get("linkedinUrl") or item.get("shareLinkedinUrl") or item.get("url") or "")
    if platform == "tiktok":
        url = str(item.get("url") or "")
        if url:
            return url
        video_id = str(item.get("id") or nested(item, "video.id") or "")
        author = author_for(platform, item).lstrip("@")
        return f"https://www.tiktok.com/@{author}/video/{video_id}" if video_id and author else ""
    if platform == "reddit":
        permalink = item.get("permalink")
        if permalink and str(permalink).startswith("/"):
            return "https://www.reddit.com" + str(permalink)
    return str(item.get("url") or item.get("post_url") or item.get("postUrl") or item.get("link") or item.get("permalink") or "")


def author_for(platform: str, item: dict[str, Any]) -> str:
    if platform == "facebook":
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        return str(author.get("name") or item.get("author_title") or "unknown")
    if platform == "linkedin":
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        return str(author.get("name") or item.get("authorName") or "unknown")
    if platform == "instagram":
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        return "@" + str(user.get("username") or item.get("username") or item.get("ownerUsername") or "unknown").lstrip("@")
    if platform == "tiktok":
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        return "@" + str(author.get("uniqueId") or item.get("authorMeta.name") or item.get("author") or "unknown").lstrip("@")
    if platform == "reddit":
        return str(item.get("author") or item.get("username") or item.get("subreddit") or "unknown")
    return "unknown"


def created_for(platform: str, item: dict[str, Any]) -> str:
    if platform == "linkedin":
        posted = item.get("postedAt") if isinstance(item.get("postedAt"), dict) else {}
        return str(posted.get("date") or posted.get("postedAgoText") or item.get("createdAt") or "")
    raw = item.get("timestamp") or item.get("createdAt") or item.get("created_utc") or item.get("createTime") or item.get("taken_at") or item.get("taken_at_timestamp") or ""
    if isinstance(raw, (int, float)) and raw:
        try:
            return datetime.fromtimestamp(float(raw), tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p ET")
        except Exception:
            return str(raw)
    return str(raw)


def engagement_for(platform: str, item: dict[str, Any]) -> int:
    if platform == "facebook":
        return max(first_int(item, "reactions_count", "reactionCount", "reactionsCount"), first_int(item, "comments_count", "commentCount"), first_int(item, "reshare_count", "shareCount"), first_int(item, "video_view_count", "videoViewCount"))
    if platform == "linkedin":
        return max(first_int(item, "engagement.likes", "likes", "likesCount"), first_int(item, "engagement.comments", "comments", "commentsCount"), first_int(item, "engagement.shares", "shares", "sharesCount"))
    if platform == "reddit":
        return max(first_int(item, "score", "upvotes"), first_int(item, "num_comments", "comments"))
    if platform == "instagram":
        return max(first_int(item, "ig_play_count", "play_count", "video_play_count", "videoPlayCount"), first_int(item, "like_count", "likesCount"), first_int(item, "comment_count", "commentsCount"), first_int(item, "share_count", "sharesCount"))
    if platform == "tiktok":
        return max(first_int(item, "stats.playCount", "playCount", "views"), first_int(item, "stats.diggCount", "stats.likeCount", "likes"), first_int(item, "stats.commentCount", "comments"), first_int(item, "stats.shareCount", "shares"))
    return 0


def has_brand_context(text: str, url: str) -> bool:
    haystack = f"{text} {url}".lower()
    if any(ex in haystack for ex in EXCLUSIONS):
        return False
    return any(term in haystack for term in BRAND_CONTEXT_TERMS)


def classify(text: str, engagement: int) -> tuple[int, str, list[str], list[str]]:
    haystack = text.lower()
    pos = sorted({term for term in POSITIVE_TERMS if term in haystack})
    neg = sorted({term for term in NEGATIVE_TERMS if term in haystack})
    raw = 50 + min(35, len(pos) * 12) - min(40, len(neg) * 14)
    if pos and not neg:
        raw += 5
    if neg and not pos:
        raw -= 5
    if pos and neg:
        raw -= 2
    if engagement >= 100_000:
        raw += 3 if raw >= 50 else -3
    elif engagement >= 10_000:
        raw += 2 if raw >= 50 else -2
    score = max(1, min(100, raw))
    label = "positive" if score >= 67 else "negative" if score <= 34 else "mixed/neutral"
    return score, label, pos, neg


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def mark_seen(path: Path, urls: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = load_seen(path)
    seen.update(urls)
    path.write_text(json.dumps(sorted(seen), indent=2))


def excerpt(text: str, limit: int = 430) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def weighted_platform_score(findings: list[SentimentFinding]) -> int:
    if not findings:
        return 50
    total = 0.0
    weight_sum = 0.0
    for finding in findings:
        weight = 1 + min(5, finding.engagement / 1000)
        total += finding.sentiment_score * weight
        weight_sum += weight
    return max(1, min(100, round(total / weight_sum)))


def render(platform: str, items_path: Path | None, findings: list[SentimentFinding], new_findings: list[SentimentFinding]) -> tuple[str, str]:
    now = datetime.now(ZoneInfo("America/New_York"))
    start = now - timedelta(hours=6)
    label = PLATFORM_LABELS[platform]
    overall = weighted_platform_score(findings)
    pos_count = sum(1 for f in findings if f.sentiment_label == "positive")
    neg_count = sum(1 for f in findings if f.sentiment_label == "negative")
    neutral_count = len(findings) - pos_count - neg_count
    subject = f"Flock Safety {label} General Sentiment — Score {overall}/100 — {now:%Y-%m-%d %I:%M %p ET}"
    lines = [
        f"Flock Safety {label} General Sentiment Report",
        "",
        f"Window checked: {start:%Y-%m-%d %I:%M %p ET} – {now:%Y-%m-%d %I:%M %p ET}",
        f"Overall sentiment score: {overall}/100 (100 = fully positive, 1 = strongly negative)",
        f"Mentions classified from latest audit data: {len(findings)} total | {pos_count} positive | {neutral_count} mixed/neutral | {neg_count} negative",
        f"New items after sentiment-report dedupe: {len(new_findings)}",
        f"Source audit file: {items_path or 'No source audit file found yet'}",
        "",
    ]
    if new_findings:
        lines.append("Most notable new mentions:")
        ranked = sorted(new_findings, key=lambda f: (abs(f.sentiment_score - 50), f.engagement), reverse=True)[:10]
        for idx, f in enumerate(ranked, start=1):
            lines.extend([
                f"{idx}. [{f.sentiment_label.upper()} | sentiment {f.sentiment_score}/100 | engagement {f.engagement:,}] {f.author}",
                f"   URL: {f.url or 'unavailable'}",
                f"   Created: {f.created_at or 'unavailable'}",
                f"   Positive cues: {', '.join(f.positive_terms) if f.positive_terms else '(none detected)'}",
                f"   Negative cues: {', '.join(f.negative_terms) if f.negative_terms else '(none detected)'}",
                f"   Text: {excerpt(f.text) if f.text else '(no text available)'}",
                "",
            ])
    else:
        lines.extend([
            "No new brand-relevant items in the latest saved audit data for this sentiment stream.",
            "The negative/risk monitor remains unchanged and still runs separately.",
            "",
        ])
    lines.extend([
        "Notes:",
        "- This is a broad sentiment read, not just a negative-threat filter.",
        "- Scores are rule-based and directional; the Executive Summary job synthesizes cross-platform context separately.",
        "- This report uses latest saved public/search-visible platform audit data and inherits the same platform coverage limitations.",
    ])
    return subject, "\n".join(lines)


def send_email(to: list[str], cc: list[str], subject: str, body: str) -> None:
    cc_header = f"Cc: {', '.join(cc)}\n" if cc else ""
    message = "From: Hermes Reports <hermes-agent@trybemedia.com>\n" + f"To: {', '.join(to)}\n" + cc_header + f"Subject: {subject}\n\n{body}\n"
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="flock-sentiment-email-", suffix=".txt") as f:
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


def run(platform: str, send: bool) -> str:
    config = load_config()
    items_path = latest_items_file(platform)
    raw_items: list[dict[str, Any]] = []
    if items_path:
        raw = json.loads(items_path.read_text())
        raw_items = raw if isinstance(raw, list) else []
    findings: list[SentimentFinding] = []
    for item in raw_items:
        text = text_for(platform, item)
        url = url_for(platform, item)
        if not has_brand_context(text, url):
            continue
        engagement = engagement_for(platform, item)
        score, sentiment_label, pos, neg = classify(text, engagement)
        findings.append(SentimentFinding(platform=platform, url=url, author=author_for(platform, item), text=text, created_at=created_for(platform, item), engagement=engagement, sentiment_score=score, sentiment_label=sentiment_label, positive_terms=pos, negative_terms=neg))
    state_path = STATE_DIR / f"flock_safety_{platform}_sentiment_seen.json"
    seen = load_seen(state_path)
    new_findings = [f for f in findings if f.url and f.url not in seen]
    subject, body = render(platform, items_path, findings, new_findings)
    if send:
        recipients = config.get("report_recipients", {})
        send_email(recipients.get("to", []), recipients.get("cc", []), subject, body)
        mark_seen(state_path, [f.url for f in new_findings if f.url])
    print(body)
    print(f"\n---\nEmail sent: {'yes' if send else 'no (dry run)'}")
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=sorted(PLATFORM_SAMPLE_DIRS))
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    run(args.platform, args.send)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
