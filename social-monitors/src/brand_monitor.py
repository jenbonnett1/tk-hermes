#!/usr/bin/env python3
"""Brand-focused Facebook/Instagram negative-post monitor.

MVP approach: search-index discovery (DuckDuckGo HTML) for public Facebook and
Instagram URLs, rules-based risk scoring, per-brand/platform URL dedupe, and
Himalaya SMTP email reports.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "brands.json"
DEFAULT_STATE_DIR = Path("/opt/data/social-monitors/state")
HIMALAYA_BIN = Path("/opt/data/profiles/tk/home/.local/bin/himalaya")
HIMALAYA_CONFIG = Path("/opt/data/profiles/tk/home/.config/himalaya/config.toml")

NEGATIVE_DEFAULTS = [
    "boycott",
    "scam",
    "lawsuit",
    "sue",
    "privacy",
    "complaint",
    "warning",
    "dangerous",
    "angry",
    "problem",
    "issue",
    "bad experience",
    "avoid",
    "fraud",
]

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
}

PLATFORM_SITES = {
    "facebook": "facebook.com",
    "instagram": "instagram.com",
}


@dataclass(frozen=True)
class Finding:
    platform: str
    brand: str
    url: str
    title: str
    snippet: str
    source_query: str
    discovered_at: str | None = None
    matched_terms: tuple[str, ...] = ()
    risk_score: int = 0
    severity: str = "low"
    reason: str = ""
    media_type: str | None = None
    transcript: str = ""
    transcript_status: str = "not_attempted"


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    return json.loads(Path(path).read_text())


def get_brand(config: dict, brand_id: str) -> dict:
    for brand in config.get("brands", []):
        if brand.get("id") == brand_id or brand.get("name") == brand_id:
            return brand
    raise KeyError(f"Unknown brand: {brand_id}")


def build_queries(brand: dict, platform: str) -> list[str]:
    if platform not in PLATFORM_SITES:
        raise ValueError(f"Unsupported platform: {platform}")
    site = PLATFORM_SITES[platform]
    terms = brand.get("canonical_terms") or [brand["name"]]
    negatives = brand.get("negative_keywords") or NEGATIVE_DEFAULTS
    queries: list[str] = []
    # Chunk negatives to keep query size reasonable for search engines.
    for term in terms:
        for i in range(0, len(negatives), 5):
            chunk = negatives[i : i + 5]
            neg_expr = " OR ".join(f'"{n}"' if " " in n else n for n in chunk)
            queries.append(f'site:{site} "{term}" ({neg_expr})')
    for handle in (brand.get("handles", {}).get(platform) or []):
        queries.append(f'site:{site} "{handle}" (complaint OR scam OR lawsuit OR boycott OR warning)')
    return queries


class _ResultParser:
    """Small, dependency-free DuckDuckGo result parser."""

    RESULT_RE = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'(?:<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>|<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>)(?P<snippet>.*?)</(?:a|div)>',
        re.S | re.I,
    )
    ANCHOR_RE = re.compile(r'<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S | re.I)
    BING_RE = re.compile(
        r'<li[^>]+class="[^"]*b_algo[^"]*"[^>]*>.*?'
        r'<h2[^>]*>\s*<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>\s*</h2>.*?'
        r'(?:<div[^>]+class="[^"]*b_caption[^"]*"[^>]*>\s*<p[^>]*>(?P<snippet>.*?)</p>)?',
        re.S | re.I,
    )

    @staticmethod
    def clean(text: str) -> str:
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", html.unescape(text)).strip()

    @classmethod
    def parse(cls, body: str, platform: str, brand_name: str, query: str, limit: int) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[str] = set()
        for match in cls.RESULT_RE.finditer(body):
            href = html.unescape(match.group("href"))
            url = normalize_result_url(href)
            if PLATFORM_SITES[platform] not in url or url in seen:
                continue
            seen.add(url)
            findings.append(
                Finding(
                    platform=platform,
                    brand=brand_name,
                    url=url,
                    title=cls.clean(match.group("title")),
                    snippet=cls.clean(match.group("snippet")),
                    source_query=query,
                    discovered_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            if len(findings) >= limit:
                break
        # Fallback: collect platform links even if snippets are unavailable.
        if not findings:
            for match in cls.ANCHOR_RE.finditer(body):
                href = html.unescape(match.group("href"))
                url = normalize_result_url(href)
                if PLATFORM_SITES[platform] not in url or url in seen:
                    continue
                seen.add(url)
                findings.append(
                    Finding(
                        platform=platform,
                        brand=brand_name,
                        url=url,
                        title=cls.clean(match.group("title")),
                        snippet="",
                        source_query=query,
                        discovered_at=datetime.now(timezone.utc).isoformat(),
                    )
                )
                if len(findings) >= limit:
                    break
        return findings

    @classmethod
    def parse_bing(cls, body: str, platform: str, brand_name: str, query: str, limit: int) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[str] = set()
        for match in cls.BING_RE.finditer(body):
            url = normalize_result_url(html.unescape(match.group("href")))
            if PLATFORM_SITES[platform] not in url or url in seen:
                continue
            seen.add(url)
            findings.append(
                Finding(
                    platform=platform,
                    brand=brand_name,
                    url=url,
                    title=cls.clean(match.group("title")),
                    snippet=cls.clean(match.group("snippet") or ""),
                    source_query=query,
                    discovered_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            if len(findings) >= limit:
                break
        return findings


def normalize_result_url(href: str) -> str:
    if href.startswith("//duckduckgo.com/l/?") or href.startswith("https://duckduckgo.com/l/?"):
        parsed = urllib.parse.urlparse("https:" + href if href.startswith("//") else href)
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("uddg"):
            return qs["uddg"][0]
    return href


def infer_media_type(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    query = urllib.parse.parse_qs(parsed.query)
    if "instagram.com" in parsed.netloc and ("/reel/" in path or "/tv/" in path):
        return "video"
    if "facebook.com" in parsed.netloc and ("/watch" in path or "/videos/" in path or "/reel/" in path or "v" in query):
        return "video"
    return None


def should_attempt_video_enrichment(finding: Finding) -> bool:
    return infer_media_type(finding.url) == "video"


def _transcript_excerpt(text: str, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def transcribe_video_url(url: str, timeout: int = 240, max_duration_seconds: int = 180) -> str:
    """Best-effort public video transcription.

    Uses yt-dlp only for public media extraction and faster-whisper for local
    speech-to-text. This intentionally avoids logged-in scraping/cookies.
    """
    with tempfile.TemporaryDirectory(prefix="brand-monitor-media-") as tmp:
        outtmpl = str(Path(tmp) / "media.%(ext)s")
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--quiet",
            "--no-warnings",
            "--max-filesize",
            "50M",
            "--match-filter",
            f"duration <= {max_duration_seconds}",
            "-x",
            "--audio-format",
            "wav",
            "-o",
            outtmpl,
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "yt-dlp failed").strip().splitlines()[-1]
            raise RuntimeError(detail)
        wavs = sorted(Path(tmp).glob("*.wav"))
        if not wavs:
            raise RuntimeError("no audio file extracted")
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(f"faster-whisper unavailable: {exc}") from exc
        model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(wavs[0]), beam_size=1, vad_filter=True)
        transcript = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
        if not transcript:
            raise RuntimeError("no speech detected")
        return transcript


def enrich_findings_with_transcripts(
    findings: Iterable[Finding],
    transcriber=transcribe_video_url,
    max_items: int = 2,
) -> list[Finding]:
    enriched: list[Finding] = []
    attempted = 0
    for finding in findings:
        media_type = infer_media_type(finding.url)
        current = replace(finding, media_type=media_type or finding.media_type)
        if media_type == "video" and attempted < max_items:
            attempted += 1
            try:
                transcript = transcriber(finding.url)
                status = "transcribed" if transcript.strip() else "no_speech"
                current = replace(current, transcript=transcript.strip(), transcript_status=status)
            except Exception as exc:
                current = replace(current, transcript="", transcript_status=f"unavailable: {str(exc)[:120]}")
        enriched.append(current)
    return enriched


def search_duckduckgo(query: str, platform: str, brand_name: str, limit: int = 10, timeout: int = 8) -> list[Finding]:
    urls = [
        ("duckduckgo", "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})),
        ("duckduckgo-lite", "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query})),
        ("bing", "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query})),
    ]
    last_error: Exception | None = None
    for engine, url in urls:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HermesBrandMonitor/1.0; +https://trybemedia.com)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec: controlled URL
                body = resp.read().decode("utf-8", errors="replace")
            if engine == "bing":
                parsed = _ResultParser.parse_bing(body, platform, brand_name, query, limit)
            else:
                parsed = _ResultParser.parse(body, platform, brand_name, query, limit)
            return parsed
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"all search engines failed for query {query!r}: {last_error}")


def classify_finding(finding: Finding, negative_keywords: Iterable[str]) -> Finding:
    haystack = f"{finding.title}\n{finding.snippet}\n{finding.transcript}".lower()
    matched = []
    score = 0
    for term in negative_keywords:
        t = term.lower()
        if t in haystack:
            matched.append(term)
            score += 18 if t in HIGH_RISK_TERMS else 10
    if any(t in haystack for t in HIGH_RISK_TERMS):
        score += 25
    if finding.platform == "facebook":
        score += 4
    if "/groups/" in finding.url:
        score += 5
    score = min(score, 100)
    if score >= 70:
        severity = "high"
    elif score >= 35:
        severity = "medium"
    else:
        severity = "low"
    reason = "Matched negative terms: " + ", ".join(matched) if matched else "Weak/ambiguous search-index match"
    return replace(finding, matched_terms=tuple(matched), risk_score=score, severity=severity, reason=reason)


class DedupeStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen = set(json.loads(self.path.read_text()) if self.path.exists() else [])

    def filter_new(self, findings: Iterable[Finding]) -> list[Finding]:
        new: list[Finding] = []
        for finding in findings:
            if finding.url not in self._seen:
                new.append(finding)
        return new

    def mark_seen(self, findings: Iterable[Finding]) -> None:
        for finding in findings:
            self._seen.add(finding.url)
        self.path.write_text(json.dumps(sorted(self._seen), indent=2))


def render_report(brand_name: str, platforms: list[str], findings: list[Finding], start_et: str, end_et: str) -> str:
    header = f"{brand_name} Facebook/Instagram Risk Monitor"
    platform_text = ", ".join(platforms)
    if not findings:
        return (
            f"{header} — No Negative Threats\n\n"
            f"No new meaningful negative Facebook/Instagram search-index results were found for {brand_name}.\n\n"
            f"Platforms checked: {platform_text}\n"
            f"Window checked: {start_et} – {end_et}\n\n"
            "Notes:\n"
            "- This MVP uses search-index discovery for public Facebook/Instagram URLs, so it may not capture private groups, non-indexed posts, or all public activity.\n"
            "- Public short-video transcription is attempted opportunistically when candidate reel/watch URLs are accessible without login; unavailable transcripts are not treated as monitor failures.\n"
            "- Already-seen URLs are excluded from repeat alerts.\n"
        )
    lines = [
        header,
        "",
        f"Window checked: {start_et} – {end_et}",
        f"Platforms checked: {platform_text}",
        "",
        f"Summary: {len(findings)} new potential negative item(s) found.",
        f"Highest severity: {max(findings, key=lambda f: f.risk_score).severity}",
        "",
        "Findings:",
    ]
    for idx, f in enumerate(sorted(findings, key=lambda x: x.risk_score, reverse=True), start=1):
        lines.extend(
            [
                f"{idx}. [{f.severity.upper()} | score {f.risk_score}] {f.platform}",
                f"   Title: {f.title or '(no title)'}",
                f"   URL: {f.url}",
                f"   Media type: {f.media_type or 'unknown'}",
                f"   Snippet: {f.snippet or '(no snippet available)'}",
                f"   Transcript status: {f.transcript_status}",
                f"   Transcript excerpt: {_transcript_excerpt(f.transcript) if f.transcript else '(none available)'}",
                f"   Matched terms: {', '.join(f.matched_terms) if f.matched_terms else '(none)'}",
                f"   Why it matters: {f.reason}",
                f"   Source query: {f.source_query}",
                "",
            ]
        )
    lines.extend(
        [
            "Notes:",
            "- This MVP uses search-index discovery for public Facebook/Instagram URLs, so it may not capture private groups, non-indexed posts, or all public activity.",
            "- Public short-video transcription is attempted opportunistically when candidate reel/watch URLs are accessible without login; unavailable transcripts are not treated as monitor failures.",
            "- Already-seen URLs are excluded from repeat alerts.",
        ]
    )
    return "\n".join(lines)


def send_email(to: list[str], cc: list[str], subject: str, body: str) -> str:
    message = (
        "From: Hermes Reports <hermes-agent@trybemedia.com>\n"
        f"To: {', '.join(to)}\n"
        f"Cc: {', '.join(cc)}\n"
        f"Subject: {subject}\n\n"
        f"{body}\n"
    )
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="brand-monitor-email-", suffix=".txt") as f:
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
    platforms: list[str],
    state_dir: Path,
    max_results: int,
    send: bool,
    max_queries_per_platform: int = 6,
    enrich_media: bool = False,
    max_enrich_items: int = 2,
) -> tuple[str, list[Finding]]:
    brand = get_brand(config, brand_id)
    now = datetime.now(ZoneInfo("America/New_York"))
    start = now - timedelta(hours=6)
    start_text = start.strftime("%Y-%m-%d %I:%M %p ET")
    end_text = now.strftime("%Y-%m-%d %I:%M %p ET")
    raw_findings: list[Finding] = []
    for platform in platforms:
        for query in build_queries(brand, platform)[:max_queries_per_platform]:
            try:
                results = search_duckduckgo(query, platform, brand["name"], limit=max_results)
            except Exception:
                # Search-index providers occasionally block or throttle individual
                # queries. Treat a failed query as no results for this MVP rather
                # than emailing noisy per-query error findings.
                results = []
            raw_findings.extend(results)
            time.sleep(1)  # Be gentle with search endpoints.
    if enrich_media:
        raw_findings = enrich_findings_with_transcripts(raw_findings, max_items=max_enrich_items)
    all_findings = [classify_finding(r, brand.get("negative_keywords", NEGATIVE_DEFAULTS)) for r in raw_findings]
    # Report only medium/high confidence matches; low-confidence search-index hits
    # are ignored until keyword/scoring rules are tuned.
    reportable = [f for f in all_findings if f.risk_score >= 35]
    store = DedupeStore(state_dir / f"{brand['id']}_{'_'.join(platforms)}_seen.json")
    new_reportable = store.filter_new(reportable)
    body = render_report(brand["name"], platforms, new_reportable, start_text, end_text)
    subject_suffix = "No Negative Threats" if not new_reportable else f"{len(new_reportable)} Potential Negative Items"
    subject = f"{brand['name']} Facebook/Instagram Risk Monitor — {subject_suffix} — {end_text}"
    if send:
        recipients = config.get("report_recipients", {})
        send_email(recipients.get("to", []), recipients.get("cc", []), subject, body)
        store.mark_seen(new_reportable)
    return body, new_reportable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--brand-id", required=True)
    parser.add_argument("--platforms", default="facebook,instagram")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--max-queries-per-platform", type=int, default=6)
    parser.add_argument("--enrich-media", action="store_true", help="Opportunistically download/transcribe public short videos before scoring")
    parser.add_argument("--max-enrich-items", type=int, default=2, help="Maximum candidate videos to transcribe per query")
    parser.add_argument("--send", action="store_true", help="Send the report email and mark reported URLs as seen")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    body, findings = run_monitor(
        config,
        args.brand_id,
        platforms,
        Path(args.state_dir),
        args.max_results,
        args.send,
        args.max_queries_per_platform,
        args.enrich_media,
        args.max_enrich_items,
    )
    print(body)
    print(f"\n---\nNew reportable findings: {len(findings)}")
    print(f"Email sent: {'yes' if args.send else 'no (dry run)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
