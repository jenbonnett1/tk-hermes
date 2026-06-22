# Flock Safety Instagram Apify Pilot — 2026-06-18

## Goal

Test Apify for Flock Safety Instagram/Reels monitoring while preserving the $5 free-plan credit budget.

## Runs executed

### 1. `apify/instagram-reel-scraper` against official profile

Input summary:

```json
{
  "username": ["flocksafety"],
  "resultsLimit": 5,
  "onlyPostsNewerThan": "30 days",
  "skipPinnedPosts": true,
  "includeDownloadedVideo": false,
  "includeTranscript": false,
  "includeSharesCount": false
}
```

Result:

- Status: `SUCCEEDED`
- Items: 1 error item
- Error: `no_items` / `Empty or private data for provided input`
- Usage reported: `$0.001`
- Notes: Official Flock Safety Instagram profile did not return usable reels from this actor/profile path.

Raw files:

- `/opt/data/social-monitors/samples/apify/flock_safety_instagram_reels/20260618_211045_items.json`
- `/opt/data/social-monitors/samples/apify/flock_safety_instagram_reels/20260618_211045_run.json`

### 2. `patient_discovery/instagram-search-reels` keyword search

Input summary:

```json
{
  "query": "Flock Safety",
  "maxPages": 1
}
```

Run cap:

- `maxTotalChargeUsd=0.05`

Result:

- Status: `SUCCEEDED`
- Items: 12
- Usage reported: `$0.008`
- Charged events reported: actor start events only; dataset items showed as 0 charged in run metadata.
- All 12 results appeared to be negative/risk-relevant based on caption text.
- All 12 included video fields.

Top observed examples:

1. `reasonmagazine` — https://www.instagram.com/reel/DElZ56BSpPD/
   - Plays: 1,948,436
   - Likes: 44,054
   - Comments: 7,066
   - Shares: 37,987
   - Caption terms: mass surveillance, Flock Safety cameras, nationwide database, 1984.

2. `thefreefloridian` — https://www.instagram.com/reel/DWSTGn5Dp0G/
   - Plays: 1,076,389
   - Likes: 86,245
   - Comments: 1,852
   - Shares: 31,218
   - Caption terms: spying, Fourth Amendment, warrantless surveillance, privacy.

3. `argos_investigations` — https://www.instagram.com/reel/DSyOLWREhvG/
   - Plays: 25,216
   - Likes: 500
   - Comments: 41
   - Shares: 1,251
   - Caption terms: cameras, neighborhoods, privacy implications.

4. `reasonmagazine` — https://www.instagram.com/reel/DUrXacqAaJ2/
   - Plays: 302,892
   - Likes: 21,677
   - Comments: 1,095
   - Shares: 11,896
   - Caption terms: 80,000 cameras, surveilling Americans, Orwellian mass surveillance.

5. `dirty.bastards.collective` — https://www.instagram.com/reel/DOtd8DyAJjY/
   - Plays: 687,423
   - Likes: 32,115
   - Comments: 697
   - Shares: 8,137
   - Caption terms: Flock Camera, ALPR, mass surveillance.

Raw/summary files:

- `/opt/data/social-monitors/samples/apify/flock_safety_instagram_reels/20260618_211139_keyword_items.json`
- `/opt/data/social-monitors/samples/apify/flock_safety_instagram_reels/20260618_211139_keyword_run.json`
- `/opt/data/social-monitors/docs/flock-safety-instagram-apify-pilot-2026-06-18.json`

### 3. `apify/instagram-reel-scraper` direct reel enrichment

Input summary:

```json
{
  "username": ["https://www.instagram.com/reel/DElZ56BSpPD/"],
  "resultsLimit": 1,
  "includeDownloadedVideo": false,
  "includeTranscript": false,
  "includeSharesCount": false
}
```

Run cap:

- `maxTotalChargeUsd=0.03`

Result:

- Status: `SUCCEEDED`
- Items: 1
- Usage reported: `$0`
- Returned normalized fields useful for enrichment:
  - `ownerUsername`
  - `caption`
  - `likesCount`
  - `commentsCount`
  - `videoViewCount`
  - `videoPlayCount`
  - `timestamp`
  - `videoUrl`
  - `audioUrl`
  - `latestComments`

Raw files:

- `/opt/data/social-monitors/samples/apify/flock_safety_instagram_reels/20260618_211235_official_direct_reel_items.json`
- `/opt/data/social-monitors/samples/apify/flock_safety_instagram_reels/20260618_211235_official_direct_reel_run.json`

## Takeaways

1. The keyword actor is effective for discovery.
2. The official Apify actor is effective for direct reel enrichment once we already have a reel URL.
3. The profile path for `flocksafety` did not return useful data in this test.
4. We should not enable broad video download or transcript by default while on the free plan.
5. Recommended architecture:
   - Use `patient_discovery/instagram-search-reels` for capped keyword discovery.
   - Deduplicate URLs.
   - Risk-score captions and engagement.
   - Use `apify/instagram-reel-scraper` only on selected high-signal reel URLs for richer metadata and optional video URL/transcription.

## Budget notes

Total reported usage from these pilot runs was approximately `$0.009`.

Recommended manual test limits while on the $5 free-plan credit:

- Keyword discovery: `maxPages=1`, `maxTotalChargeUsd=0.05`.
- Direct enrichment: 1-2 reels/run, `maxTotalChargeUsd=0.03`.
- No broad video downloads.
- No transcript add-on by default.
- No cron until parser/dedupe/reporting are implemented and monthly cost is estimated.
