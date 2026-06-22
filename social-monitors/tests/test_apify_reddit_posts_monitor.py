import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from apify_reddit_posts_monitor import (
    APIFY_REDDIT_ACTOR_DISPLAY,
    classify_item,
    render_report,
    save_run,
    state_seen,
    mark_seen,
)

SAMPLE = ROOT / "samples" / "apify" / "flock_safety_reddit" / "20260622_192356_pilot_items.json"


def load_sample_items():
    return json.loads(SAMPLE.read_text())


def test_classify_item_normalizes_reddit_schema_and_scores_alpr_post():
    item = load_sample_items()[0]

    finding = classify_item(item, ["privacy", "surveillance", "license plate reader", "police", "tracking"], [])

    assert finding is not None
    assert finding.url.startswith("https://www.reddit.com/")
    assert finding.post_id
    assert finding.subreddit == "r/loveland"
    assert finding.author_name
    assert {"police", "tracking", "alpr"}.intersection({t.lower() for t in finding.matched_terms})
    assert finding.score >= 35
    assert finding.severity in {"medium", "high"}


def test_classify_item_suppresses_non_post_and_neutral_posts_below_threshold():
    comment = {"dataType": "comment", "url": "https://reddit.com/comment", "body": "Flock Safety privacy"}
    assert classify_item(comment, ["privacy"], []) is None

    post = {
        "dataType": "post",
        "postUrl": "https://www.reddit.com/r/example/comments/abc/test/",
        "parsedId": "abc",
        "title": "Flock Safety helped recover a stolen vehicle",
        "body": "Local residents thanked the team for the support.",
        "communityName": "r/example",
        "upVotes": 2,
        "commentsCount": 0,
    }
    finding = classify_item(post, ["privacy", "surveillance", "lawsuit"], [])
    assert finding is not None
    assert finding.score < 35
    assert finding.severity == "low"


def test_render_report_includes_reddit_cost_guardrails_and_finding_details(tmp_path):
    item = load_sample_items()[0]
    finding = classify_item(item, ["privacy", "surveillance", "license plate reader", "police", "tracking"], [])
    run = {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "dataset1", "usageTotalUsd": 0.02, "itemCount": 1}

    body = render_report("Flock Safety", '"Flock Safety"', run, [finding], [finding], tmp_path / "run.json", tmp_path / "items.json")

    assert "Reddit Posts Apify Monitor" in body
    assert APIFY_REDDIT_ACTOR_DISPLAY in body
    assert "searchComments/searchCommunities disabled" in body
    assert "crawlCommentsPerPost disabled" in body
    assert finding.url in body
    assert "Matched terms:" in body


def test_state_seen_and_mark_seen_round_trip(tmp_path):
    path = tmp_path / "seen.json"
    assert state_seen(path) == set()
    mark_seen(path, ["url-1", "post-1"])
    assert state_seen(path) == {"url-1", "post-1"}


def test_save_run_writes_sanitized_run_metadata(tmp_path):
    run = {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "dataset1", "usageTotalUsd": 0.02, "omit": "me"}
    run_path, items_path = save_run(tmp_path, run, [{"postUrl": "https://reddit.com/x"}], '"Flock Safety"', 5, 0.04, "day")
    saved_run = json.loads(run_path.read_text())
    assert saved_run["id"] == "run1"
    assert saved_run["input_used"]["maxPostsCount"] == 5
    assert saved_run["input_used"]["searchComments"] is False
    assert "omit" not in saved_run
    assert json.loads(items_path.read_text()) == [{"postUrl": "https://reddit.com/x"}]
