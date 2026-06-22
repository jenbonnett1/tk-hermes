import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from apify_facebook_posts_monitor import (
    APIFY_FACEBOOK_ACTOR_DISPLAY,
    classify_item,
    render_report,
    save_run,
    state_seen,
    mark_seen,
)

SAMPLE = ROOT / "samples" / "apify" / "flock_safety_facebook_posts" / "20260622_190541_scrapeforge_items.json"


def load_sample_items():
    return json.loads(SAMPLE.read_text())


def test_classify_item_normalizes_scrapeforge_schema_and_scores_privacy_post():
    items = load_sample_items()
    item = next(i for i in items if "privacy" in i.get("message", "").lower() or "surveillance" in i.get("message", "").lower())

    finding = classify_item(item, ["privacy", "surveillance", "license plate reader", "police", "tracking"])

    assert finding is not None
    assert finding.url.startswith("https://www.facebook.com/")
    assert finding.post_id
    assert finding.author_name
    assert "privacy" in [t.lower() for t in finding.matched_terms] or "surveillance" in [t.lower() for t in finding.matched_terms]
    assert finding.score >= 35
    assert finding.severity in {"medium", "high"}


def test_classify_item_keeps_neutral_brand_mention_below_report_threshold():
    item = {
        "url": "https://www.facebook.com/example/posts/1",
        "post_id": "1",
        "author": {"name": "Local Police", "url": "https://www.facebook.com/local"},
        "message": "Flock Safety helped locate a missing vehicle and everyone is safe.",
        "timestamp": 1782147079,
    }

    finding = classify_item(item, ["privacy", "surveillance", "lawsuit"])

    assert finding is not None
    assert finding.score < 35
    assert finding.severity == "low"


def test_render_report_includes_apify_cost_guardrails_and_finding_details(tmp_path):
    item = load_sample_items()[2]
    finding = classify_item(item, ["privacy", "surveillance", "license plate reader"])
    run = {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "dataset1", "usageTotalUsd": 0.01, "itemCount": 1}

    body = render_report(
        "Flock Safety",
        '"Flock Safety"',
        run,
        [finding],
        [finding],
        tmp_path / "run.json",
        tmp_path / "items.json",
    )

    assert "Facebook Posts Apify Monitor" in body
    assert APIFY_FACEBOOK_ACTOR_DISPLAY in body
    assert "max_results and maxTotalChargeUsd" in body
    assert finding.url in body
    assert "Matched terms:" in body


def test_state_seen_and_mark_seen_round_trip(tmp_path):
    path = tmp_path / "seen.json"

    assert state_seen(path) == set()
    mark_seen(path, ["url-1", "post-1"])

    assert state_seen(path) == {"url-1", "post-1"}


def test_save_run_writes_sanitized_run_metadata(tmp_path):
    run = {
        "id": "run1",
        "status": "SUCCEEDED",
        "defaultDatasetId": "dataset1",
        "usageTotalUsd": 0.01,
        "unneededLargeField": "omit me",
    }

    run_path, items_path = save_run(tmp_path, run, [{"url": "https://facebook.com/x"}], '"Flock Safety"', 5, 0.03)

    saved_run = json.loads(run_path.read_text())
    saved_items = json.loads(items_path.read_text())
    assert saved_run["id"] == "run1"
    assert saved_run["input_used"]["max_results"] == 5
    assert "unneededLargeField" not in saved_run
    assert saved_items == [{"url": "https://facebook.com/x"}]
