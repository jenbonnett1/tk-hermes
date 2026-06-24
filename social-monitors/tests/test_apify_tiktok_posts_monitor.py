import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from apify_tiktok_posts_monitor import (
    APIFY_TIKTOK_ACTOR_DISPLAY,
    classify_item,
    created_at_from_item,
    render_report,
    save_run,
    state_seen,
    mark_seen,
)

BRAND = {
    "id": "flock_safety",
    "name": "Flock Safety",
    "canonical_terms": ["Flock Safety", "Flock cameras"],
    "negative_keywords": ["privacy", "surveillance", "license plate reader", "police", "tracking"],
    "exclusions": [],
}


def test_classify_item_normalizes_tiktok_schema_and_scores_brand_video():
    item = {
        "id": "7340000000000000000",
        "desc": "Flock Safety license plate reader cameras raise privacy and surveillance concerns with local police tracking.",
        "author": {"uniqueId": "localwatch", "nickname": "Local Watch", "followerCount": 120000},
        "stats": {"playCount": 260000, "diggCount": 12000, "commentCount": 450, "shareCount": 1500},
        "createTime": 1719853200,
        "video": {"duration": 31.4},
    }

    finding = classify_item(item, BRAND)

    assert finding is not None
    assert finding.url == "https://www.tiktok.com/@localwatch/video/7340000000000000000"
    assert finding.author_handle == "localwatch"
    assert finding.author_name == "Local Watch"
    assert finding.views == 260000
    assert finding.likes == 12000
    assert finding.comments == 450
    assert finding.shares == 1500
    assert finding.followers == 120000
    assert finding.duration == 31.4
    assert {"privacy", "surveillance", "license plate reader", "police", "tracking"}.issubset(
        {t.lower() for t in finding.matched_terms}
    )
    assert finding.score >= 70
    assert finding.severity == "high"


def test_classify_item_filters_generic_flock_or_safety_false_positive():
    generic_item = {
        "url": "https://www.tiktok.com/@birdwatch/video/1",
        "desc": "A flock of birds reminded everyone safety first while wearing safety glasses.",
        "author": "birdwatch",
    }

    assert classify_item(generic_item, BRAND) is None


def test_created_at_handles_epoch_seconds_in_eastern_time():
    assert "ET" in created_at_from_item({"createTime": 1719853200})


def test_render_report_includes_tiktok_guardrails_and_finding_details(tmp_path):
    item = {
        "url": "https://www.tiktok.com/@localwatch/video/734",
        "desc": "Flock Safety cameras and privacy concerns around police surveillance.",
        "author": {"uniqueId": "localwatch", "nickname": "Local Watch"},
        "stats": {"playCount": 50000, "commentCount": 100},
    }
    finding = classify_item(item, BRAND)
    run = {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "dataset1", "usageTotalUsd": 0.03}

    body = render_report(
        "Flock Safety",
        "Flock Safety",
        run,
        [item],
        [finding],
        [finding],
        tmp_path / "run.json",
        tmp_path / "items.json",
        10,
        0.05,
        "THIS_WEEK",
    )

    assert "TikTok Apify Pilot" in body
    assert APIFY_TIKTOK_ACTOR_DISPLAY in body
    assert "maxItems=10" in body
    assert "no comments scrape" in body
    assert "no video download/mirroring" in body
    assert finding.url in body
    assert "Matched terms:" in body


def test_state_seen_and_mark_seen_round_trip(tmp_path):
    path = tmp_path / "seen.json"
    assert state_seen(path) == set()
    mark_seen(path, ["url-1", "url-2"])
    assert state_seen(path) == {"url-1", "url-2"}


def test_save_run_writes_sanitized_tiktok_run_metadata(tmp_path):
    run = {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "dataset1", "usageTotalUsd": 0.02, "secret": "omit-me"}
    run_path, items_path = save_run(tmp_path, run, [{"url": "https://tiktok.com/x"}], "Flock Safety", 10, 0.05, "THIS_WEEK")
    saved_run = json.loads(run_path.read_text())

    assert saved_run["id"] == "run1"
    assert saved_run["input_used"]["maxItems"] == 10
    assert saved_run["input_used"]["endPage"] == 1
    assert saved_run["input_used"]["maxTotalChargeUsd"] == 0.05
    assert "secret" not in saved_run
    assert json.loads(items_path.read_text()) == [{"url": "https://tiktok.com/x"}]
