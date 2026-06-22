# Flock Safety Facebook Apify Pilot — 2026-06-22

## Goal

Test whether Apify can replace the weak Facebook-post search-index MVP for Flock Safety brand-risk monitoring, with strict cost caps.

## Candidate actors inspected

### `powerai/facebook-post-search-scraper`

- Purpose: keyword-based Facebook post search.
- Pricing observed via Apify Store API:
  - Actor start: `$0.09`
  - Dataset item: `$0.00999` per result
- Input fields observed from public actor page:
  - `query` required
  - `recent_posts` optional
  - `location_uid` optional
  - `start_date` / `end_date` optional
  - `maxResults` optional, minimum accepted value appears to be `10`

Test results:

1. `maxResults=5`, `maxTotalChargeUsd=0.06`
   - Failed validation before run: `input.maxResults must be >= 10`.
   - No useful dataset.
2. `maxResults=10`, `maxTotalChargeUsd=0.12`
   - Run status: `ABORTED`
   - Reported usage: `$0.09`
   - Dataset items: `0`
   - Status message: aborted after reaching max cost.

Conclusion: not recommended for our low-budget Facebook pilot because the fixed start charge is high and it aborted before producing items under a modest cap.

### `scrapeforge/facebook-search-posts`

- Purpose: Facebook search for posts/pages/groups/people/videos/events by keyword.
- Pricing observed via Apify Store API:
  - Actor start: `$0.00005`
  - Dataset item: `$0.00259` per result
- Input fields observed from public actor page:
  - `query`
  - `search_type`, default/search value `posts`
  - `max_results`
  - `recent_posts`
  - optional date/location filters

Test input:

```json
{
  "query": "\"Flock Safety\"",
  "search_type": "posts",
  "max_results": 5,
  "recent_posts": true
}
```

Run options:

```text
maxTotalChargeUsd=0.03
```

Result:

- Actor: `scrapeforge/facebook-search-posts`
- Status: `SUCCEEDED`
- Run ID: `eZ5pECDDjMqK8zbud`
- Dataset ID: `ng9OYw5bIk1sd8nag`
- Items returned: `5`
- Reported immediate usage: `$0.00005`
- Note: pricing indicates up to about `$0.013` for 5 dataset items if item events are charged/finalized separately.

Saved files:

```text
/opt/data/social-monitors/samples/apify/flock_safety_facebook_posts/20260622_190541_scrapeforge_run.json
/opt/data/social-monitors/samples/apify/flock_safety_facebook_posts/20260622_190541_scrapeforge_items.json
```

## Returned schema highlights

Top-level item keys included:

```text
url, post_id, author, author_title, message, message_rich, timestamp,
reactions, reactions_count, reshare_count, comments_count,
video, video_files, video_thumbnail, video_view_count,
image, external_url, attached_post_url, associated_group
```

This is a much better normalized-record fit than the old search-index MVP.

## Relevance notes from 5-result test

The results were substantially more relevant than the MVP. Examples included:

1. Loveland Police Department post mentioning a victim/report context and Flock Safety camera evidence.
2. Thinking Humanity post about a Virginia engineer charged with destroying Flock Safety AI-powered license plate reader cameras, with privacy/surveillance framing.
3. Hashem Al-Ghaili post about public battle over AI-powered surveillance cameras and Flock Safety license plate readers.
4. Morgan Nick Foundation post mentioning Flock camera/license-plate evidence in a missing-person case.
5. Weird World post about destruction of Flock Safety cameras and ALPR/privacy concerns.

Risk signal quality:

- Items 2, 3, and 5 are clearly PR/reputation-risk relevant.
- Item 4 is not negative but is brand-relevant in a public-safety context.
- Item 1 may be operational/public-safety context but not obviously negative toward Flock Safety.

## Recommendation

Use `scrapeforge/facebook-search-posts` as the first Facebook Apify adapter candidate.

Initial production/pilot settings:

```text
query="Flock Safety"
search_type=posts
max_results=10
maxTotalChargeUsd=0.05
recent_posts=true
```

Adapter should:

1. Deduplicate by `url` or `post_id`.
2. Normalize `author.name`, `author.url`, `message`, `timestamp`, `reactions_count`, `comments_count`, `reshare_count`, `video_view_count`, and media fields.
3. Score message text with the existing risk terms.
4. Include neutral brand-relevant posts at low severity only in audit/debug, not email by default.
5. Email medium/high findings only, with a heartbeat/no-threat report if desired.
6. Keep run ID, dataset ID, item count, and usage/cost in each report.

Avoid `powerai/facebook-post-search-scraper` for now unless we need to compare quality with a larger budget cap.
