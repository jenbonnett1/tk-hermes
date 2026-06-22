# Apify Actor Evaluation for TK Social Monitoring

Date checked: 2026-06-18 UTC via public Apify Store API. Prices can change; verify actor pricing before enabling scheduled runs.

## Cost-control assumptions

- No `APIFY_TOKEN` is configured yet, so this is a public-store evaluation only; no paid runs were executed.
- Use Apify initially as an enrichment/discovery supplement, not as the only monitor.
- Every scheduled run should set both a result cap and a charge cap where supported: `maxItems` / actor-specific `limit`, plus Apify `maxTotalChargeUsd` or equivalent run option.
- Start with one brand and one platform; run manually before cron.
- Persist actor run IDs, dataset IDs, item counts, and estimated cost in monitor logs/reports.

## Recommended shortlist

### Instagram Reels / video

**Recommended first test:** `apify/instagram-reel-scraper`

Why:
- Official Apify actor, very high usage: ~7.8M total runs; ~1.35M successful public runs in last 30 days at lookup time.
- Description explicitly includes Instagram reels, captions, timestamp, transcript, hashtags, mentions, tagged users, comments, likes, shares, views, duration, and downloaded video.
- Pricing found:
  - reel item: about `$0.0026` on free tier / `$0.0023` bronze, lower on higher tiers.
  - actor start: about `$0.001` to `$0.006` depending tier/version.
  - shares-count add-on: about `$0.007` free tier per reel.
  - transcript add-on: about `$0.048` free tier per started minute per reel.
  - video-download add-on: about `$0.020` free tier per started MB downloaded per reel.

Cost guidance:
- Do **not** download every video. Video download can dominate cost quickly: 10 MB ~= `$0.20` on free-tier pricing before any other costs.
- Prefer caption/metadata/comment scan first.
- Use transcript add-on only for high-signal candidates if it avoids downloading the full video.
- For our current 6-hour pilot, initial cap should be something like 5-10 reels/run and at most 1-2 transcripts/run.

**Alternative keyword-discovery actor:** `patient_discovery/instagram-search-reels`

Why:
- Keyword reels search, no login/no cookie according to description.
- Pricing found: actor start `$0.002`; result item about `$0.0025` free tier, discounted on paid tiers.
- Good for discovery, but smaller footprint and no reviews at lookup time compared with official Apify actor.

### LinkedIn

**Recommended first test:** `harvestapi/linkedin-post-search`

Why:
- No cookies/account required according to actor description.
- High usage: ~2.4M total runs; ~720k successful public runs in last 30 days at lookup time.
- Good fit for brand/company keyword post search.
- Pricing found:
  - post: about `$0.002` free/bronze, lower on some tiers.
  - 0-result query: `$0.001`.
  - optional main profile enrichment: about `$0.002` each.
  - optional full profile enrichment: about `$0.004` each.
  - optional reactions/comments: about `$0.002` per item.

Cost guidance:
- Start with posts only; do not fetch reactions/comments/profiles by default.
- Enrich comments/profile only on medium/high-risk candidate posts.
- Watch out for 0-result query costs if running many brand/keyword combinations.

**Company-specific companion:** `harvestapi/linkedin-company-posts`

Use for known company pages/official accounts, not broad brand mention discovery.
Pricing found similar to post search: about `$0.002` per post, optional reactions/comments at about `$0.002` each, 0-result URL charge `$0.001`.

### Reddit

**Recommended cost-first test:** `harshmaur/reddit-search-scraper`

Why:
- Explicitly marketed for keyword/brand monitoring.
- Pricing found: actor start `$0.02`; result saved `$0.0015`; title says from `$1.50/1k`.
- Newer/smaller actor at lookup time, so test reliability before depending on it.

**Alternative:** `practicaltools/reddit-keyword-monitor`

Why:
- Built for keyword/mention monitoring and filtering.
- Pricing found: actor start `$0.03`; result item `$0.0045` after listed price change.
- More expensive per result than `harshmaur`, but may have better filtering.

**Higher-cost richer scraper:** `brilliant_gum/reddit-scraper`

Pricing found: actor start `$0.005`; post `$0.008`; comment `$0.006`; profile `$0.008`. Use only if we need richer Reddit post/comment detail and cheaper actors miss context.

## Pilot recommendation

1. Add Apify adapter support behind an explicit config flag, but do not schedule it until an `APIFY_TOKEN` is configured.
2. Pilot Instagram first for Flock Safety because it complements the current FB/IG monitor and directly addresses reels/video.
3. First paid run: `apify/instagram-reel-scraper`, 5 reels max, no video download, no transcript; inspect schema and quality.
4. Second run: same actor, only 1 selected reel with transcript add-on or video download depending actor input options.
5. Add LinkedIn `harvestapi/linkedin-post-search` as a separate pilot with 10 posts max and no comments/profiles.
6. Add Reddit `harshmaur/reddit-search-scraper` with 25 results max and narrow negative keyword terms.
7. Cron only after 2-3 manual runs have acceptable precision and measured cost.

## Monthly cost guardrail examples

If run every 6 hours (about 120 runs/month):

- Instagram metadata only: 5 reels/run * `$0.0026` ~= `$1.56/month` plus start charges.
- Instagram with one 1-minute transcript/run: 120 * `$0.048` ~= `$5.76/month` plus reel/start charges.
- Instagram with one 10 MB video download/run: 120 * 10 * `$0.020` ~= `$24/month` plus reel/start charges. Avoid as default.
- LinkedIn posts only: 10 posts/run * `$0.002` * 120 ~= `$2.40/month`, plus 0-result/start charges.
- Reddit search, 25 results/run at `$0.0015` * 120 ~= `$4.50/month`, plus `$2.40/month` start charges at `$0.02/run`.

These are rough actor-charge examples only; Apify platform/plan pricing and actor pricing can change.
