import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from apify_linkedin_posts_monitor import (
    APIFY_LINKEDIN_ACTOR_DISPLAY,
    classify_item,
    render_report,
    save_run,
    state_seen,
    mark_seen,
)

SAMPLE = ROOT / "samples" / "apify" / "flock_safety_linkedin" / "20260622_192348_pilot_items.json"


def load_sample_items():
    return json.loads(SAMPLE.read_text())


def test_classify_item_suppresses_neutral_flock_phrase_when_excluded():
    item = load_sample_items()[0]

    finding = classify_item(item, ["privacy", "surveillance", "police", "tracking"], ["backyard chicken", "poultry", "coop"])

    assert finding is None


def test_classify_item_scores_linkedin_privacy_surveillance_post():
    item = {
        "id": "123",
        "linkedinUrl": "https://www.linkedin.com/posts/example_flock-safety-privacy-activity-123",
        "content": "Flock Safety license plate reader deployment raises privacy and surveillance concerns for residents.",
        "author": {"name": "Civil Liberties Group", "linkedinUrl": "https://www.linkedin.com/company/civil", "info": "10,000 followers"},
        "postedAt": {"date": "2026-06-22T17:00:00.000Z"},
        "engagement": {"likes": 150, "comments": 12, "shares": 3},
    }

    finding = classify_item(item, ["privacy", "surveillance", "license plate reader", "police", "tracking"], [])

    assert finding is not None
    assert finding.url.startswith("https://www.linkedin.com/")
    assert finding.post_id == "123"
    assert finding.author_name == "Civil Liberties Group"
    assert {"privacy", "surveillance", "license plate reader"}.intersection({t.lower() for t in finding.matched_terms})
    assert finding.score >= 35
    assert finding.severity in {"medium", "high"}


def test_render_report_includes_linkedin_cost_guardrails_and_finding_details(tmp_path):
    item = {
        "id": "123",
        "linkedinUrl": "https://www.linkedin.com/posts/example_123",
        "content": "Flock Safety tracking and surveillance concerns are growing.",
        "author": {"name": "Reporter", "linkedinUrl": "https://www.linkedin.com/in/reporter"},
        "engagement": {"likes": 5, "comments": 1, "shares": 0},
    }
    finding = classify_item(item, ["surveillance", "tracking"], [])
    run = {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "dataset1", "usageTotalUsd": 0.01, "itemCount": 1}

    body = render_report("Flock Safety", '"Flock Safety"', run, [finding], [finding], tmp_path / "run.json", tmp_path / "items.json")

    assert "LinkedIn Posts Apify Monitor" in body
    assert APIFY_LINKEDIN_ACTOR_DISPLAY in body
    assert "profileScraperMode=short" in body
    assert "reaction/comment/profile enrichment disabled" in body
    assert finding.url in body
    assert "Matched terms:" in body


def test_state_seen_and_mark_seen_round_trip(tmp_path):
    path = tmp_path / "seen.json"
    assert state_seen(path) == set()
    mark_seen(path, ["url-1", "post-1"])
    assert state_seen(path) == {"url-1", "post-1"}


def test_save_run_writes_sanitized_run_metadata(tmp_path):
    run = {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "dataset1", "usageTotalUsd": 0.01, "omit": "me"}
    run_path, items_path = save_run(tmp_path, run, [{"linkedinUrl": "https://linkedin.com/x"}], '"Flock Safety"', 5, 0.03, "24h")
    saved_run = json.loads(run_path.read_text())
    assert saved_run["id"] == "run1"
    assert saved_run["input_used"]["maxPosts"] == 5
    assert saved_run["input_used"]["scrapeComments"] is False
    assert "omit" not in saved_run
    assert json.loads(items_path.read_text()) == [{"linkedinUrl": "https://linkedin.com/x"}]
