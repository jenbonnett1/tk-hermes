# Flock Safety LinkedIn + Reddit Apify Pilot — 2026-06-22

## LinkedIn

Actor: `harvestapi/linkedin-post-search`

Input used for scheduled monitor:

- `searchQueries=["\"Flock Safety\""]`
- `maxPosts=10`
- `postedLimit=24h`
- `sortBy=date`
- `profileScraperMode=short`
- `scrapeReactions=false`
- `scrapeComments=false`
- `maxTotalChargeUsd=0.05`

Pricing observed from Apify Store: pay-per-post, with optional profile/reaction/comment enrichment events. Enrichment is disabled in the scheduled pilot.

Dry run with `maxPosts=5`, `maxTotalChargeUsd=0.03` succeeded, returned 5 items, and produced 2 new reportable findings.

## Reddit

Actor: `harshmaur/reddit-search-scraper`

Input used for scheduled monitor:

- `searchTerms=["\"Flock Safety\""]`
- `searchPosts=true`
- `searchComments=false`
- `searchCommunities=false`
- `searchSort=new`
- `searchTime=day`
- `maxPostsCount=10`
- `crawlCommentsPerPost=false`
- `maxTotalChargeUsd=0.04`

Pricing observed from Apify Store: actor start event plus per-result event. Comments and community search are disabled to avoid multiplying result count and cost.

Dry run with `maxPostsCount=10`, `maxTotalChargeUsd=0.04` succeeded, returned 5 items, and produced 4 new reportable findings.

## Scheduled jobs

- LinkedIn: `run_flock_apify_linkedin_posts_monitor.sh`, every 6 hours, emails in send mode.
- Reddit: `run_flock_apify_reddit_posts_monitor.sh`, every 6 hours, emails in send mode.

Both monitors save raw run metadata and dataset items under `/opt/data/social-monitors/samples/apify/` and dedupe by URL/post ID only after send-mode runs.
