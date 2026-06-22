import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from brand_monitor import (
    Finding,
    build_queries,
    classify_finding,
    DedupeStore,
    render_report,
    load_config,
    _ResultParser,
    should_attempt_video_enrichment,
    enrich_findings_with_transcripts,
)


def test_build_queries_include_platform_site_and_negative_terms():
    brand = {
        "name": "Flock Safety",
        "canonical_terms": ["Flock Safety", "Flock LPR"],
        "negative_keywords": ["privacy", "lawsuit"],
    }

    queries = build_queries(brand, "facebook")

    assert any("site:facebook.com" in q for q in queries)
    assert any('"Flock Safety"' in q and "privacy" in q for q in queries)
    assert any('"Flock LPR"' in q and "lawsuit" in q for q in queries)


def test_classify_finding_scores_high_for_legal_privacy_terms():
    finding = Finding(
        platform="facebook",
        brand="Flock Safety",
        url="https://facebook.com/example",
        title="Flock Safety lawsuit over privacy",
        snippet="Residents complain about surveillance and unconstitutional license plate readers",
        source_query="q",
    )

    scored = classify_finding(finding, ["privacy", "lawsuit", "surveillance"])

    assert scored.risk_score >= 70
    assert scored.severity == "high"
    assert "privacy" in scored.matched_terms
    assert "lawsuit" in scored.matched_terms


def test_dedupe_store_filters_seen_urls(tmp_path):
    store = DedupeStore(tmp_path / "seen.json")
    first = Finding(platform="instagram", brand="Brand", url="https://instagram.com/p/1", title="a", snippet="b", source_query="q")
    second = Finding(platform="instagram", brand="Brand", url="https://instagram.com/p/2", title="a", snippet="b", source_query="q")

    new_items = store.filter_new([first, second])
    store.mark_seen(new_items)
    repeat_items = store.filter_new([first, second])

    assert [item.url for item in new_items] == ["https://instagram.com/p/1", "https://instagram.com/p/2"]
    assert repeat_items == []
    assert json.loads((tmp_path / "seen.json").read_text()) == ["https://instagram.com/p/1", "https://instagram.com/p/2"]


def test_render_report_includes_no_threat_when_empty():
    body = render_report("Flock Safety", ["facebook", "instagram"], [], "2026-06-16 04:00 PM ET", "2026-06-16 10:00 PM ET")

    assert "No Negative Threats" in body
    assert "Flock Safety" in body
    assert "facebook, instagram" in body


def test_load_config_contains_flock_safety(tmp_path):
    cfg = tmp_path / "brands.json"
    cfg.write_text(json.dumps({"brands": [{"id": "flock_safety", "name": "Flock Safety"}]}))

    loaded = load_config(cfg)

    assert loaded["brands"][0]["id"] == "flock_safety"


def test_bing_parser_extracts_platform_results():
    html = '''
    <li class="b_algo"><h2><a href="https://www.instagram.com/p/example/">Flock Safety privacy concern</a></h2>
    <div class="b_caption"><p>Residents complain about Flock Safety privacy.</p></div></li>
    '''

    parsed = _ResultParser.parse_bing(html, "instagram", "Flock Safety", "query", 5)

    assert len(parsed) == 1
    assert parsed[0].url == "https://www.instagram.com/p/example/"
    assert parsed[0].title == "Flock Safety privacy concern"


def test_classify_finding_uses_transcript_text_for_risk_score():
    finding = Finding(
        platform="instagram",
        brand="Flock Safety",
        url="https://www.instagram.com/reel/example/",
        title="Short reel",
        snippet="",
        source_query="q",
        media_type="video",
        transcript="These Flock cameras are surveillance and a privacy lawsuit waiting to happen.",
        transcript_status="transcribed",
    )

    scored = classify_finding(finding, ["surveillance", "privacy", "lawsuit"])

    assert scored.severity == "high"
    assert "surveillance" in scored.matched_terms
    assert "privacy" in scored.matched_terms


def test_render_report_includes_transcript_excerpt_when_available():
    finding = Finding(
        platform="instagram",
        brand="Flock Safety",
        url="https://www.instagram.com/reel/example/",
        title="Short reel",
        snippet="Caption mentions Flock Safety",
        source_query="q",
        media_type="video",
        transcript="Flock Safety cameras are creepy surveillance tools.",
        transcript_status="transcribed",
        matched_terms=("surveillance",),
        risk_score=72,
        severity="high",
        reason="Matched negative terms: surveillance",
    )

    body = render_report("Flock Safety", ["instagram"], [finding], "start", "end")

    assert "Media type: video" in body
    assert "Transcript status: transcribed" in body
    assert "Transcript excerpt: Flock Safety cameras are creepy surveillance tools." in body


def test_should_attempt_video_enrichment_only_for_video_like_public_urls():
    assert should_attempt_video_enrichment(Finding(platform="instagram", brand="B", url="https://www.instagram.com/reel/abc/", title="", snippet="", source_query=""))
    assert should_attempt_video_enrichment(Finding(platform="facebook", brand="B", url="https://www.facebook.com/watch/?v=123", title="", snippet="", source_query=""))
    assert not should_attempt_video_enrichment(Finding(platform="instagram", brand="B", url="https://www.instagram.com/profile/", title="", snippet="", source_query=""))


def test_enrich_findings_with_transcripts_uses_injected_transcriber():
    finding = Finding(platform="instagram", brand="Flock Safety", url="https://www.instagram.com/reel/abc/", title="", snippet="", source_query="")

    enriched = enrich_findings_with_transcripts([finding], transcriber=lambda url: "negative privacy transcript", max_items=1)

    assert enriched[0].media_type == "video"
    assert enriched[0].transcript_status == "transcribed"
    assert enriched[0].transcript == "negative privacy transcript"
