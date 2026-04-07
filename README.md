# MetaGatherer — FB Ads Library Winning Product Scraper

Finds winning ecommerce products by mining the Facebook Ads Library API.

## How It Works

1. **Seed keywords** — starts with "buy now", "shop now", "free shipping", etc.
2. **BFS expansion** — extracts product keywords from discovered ads and searches those too (up to configurable depth).
3. **Page filtering** — keeps only pages with **10–2000 followers** (small/medium dropshippers scaling up).
4. **Product clustering** — groups ads from the same page by keyword overlap to identify single-product campaigns.
5. **Winning threshold** — a cluster must have **12+ active ads** started within the last 7 days (configurable).
6. **Shop Now detection** — checks ad copy and captions for Shop Now / Buy Now phrasing.
7. **Shopify verification** — checks the page's linked website via HTTP headers and HTML signals.
8. **Output** — Rich terminal table + CSV export.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure your token
cp .env.example .env
# Edit .env and paste your FB_ACCESS_TOKEN

# 3. Run
python main.py
```

## Getting a Facebook Access Token

1. Go to [https://developers.facebook.com/tools/explorer/](https://developers.facebook.com/tools/explorer/)
2. Select or create an app (any type — "Consumer" works).
3. Click **Generate Access Token**.
4. Add permissions: `ads_read`, `pages_read_engagement`
5. Copy the token into your `.env` file as `FB_ACCESS_TOKEN`.

> Tokens expire after ~60 days. Use the [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/) to extend them.

## Usage

```
python main.py [OPTIONS]

Options:
  --token TEXT              FB access token [env: FB_ACCESS_TOKEN]
  -c, --countries TEXT      Target country codes (repeatable, default: US)
  -d, --days INTEGER        Lookback window in days [default: 7]
  --min-ads INTEGER         Min active ads per product cluster [default: 12]
  --min-followers INTEGER   Min page followers [default: 10]
  --max-followers INTEGER   Max page followers [default: 2000]
  -k, --keywords TEXT       Extra seed keywords (repeatable)
  --max-keywords INTEGER    Max keywords to search total [default: 30]
  --keyword-depth INTEGER   BFS depth for keyword expansion [default: 3]
  --video-only              Only show pages with video ads
  --require-shop-now        Require Shop Now CTA in ad copy [default: on]
  --no-require-shop-now     Disable Shop Now requirement
  -o, --output PATH         Output CSV path (auto-named if omitted)
  --no-csv                  Skip CSV export
  --api-pages INTEGER       API result pages per keyword [default: 3]
  -v, --verbose             Debug logging
  -h, --help                Show this message and exit.
```

## Example Commands

```bash
# Standard scan (US, 7-day window, 12 min ads)
python main.py

# Aggressive — UK, 3-day window, 15 min ads, video only
python main.py -c GB -d 3 --min-ads 15 --video-only

# Add niche keywords as seeds
python main.py -k "posture corrector" -k "knee brace" -k "back pain relief"

# Wider net — up to 5000 followers, no Shop Now requirement
python main.py --max-followers 5000 --no-require-shop-now

# Multiple countries
python main.py -c US -c CA -c AU -d 7 --output winners.csv
```

## Output Columns (CSV)

| Column | Description |
|--------|-------------|
| `page_name` | Facebook page name |
| `page_id` | Facebook page ID |
| `page_followers` | Page follower/fan count |
| `page_url` | Linked website URL |
| `page_category` | FB page category |
| `winning_ad_count` | Ads in the winning cluster (within window) |
| `total_page_ads_seen` | All active ads seen for this page |
| `has_shop_now_cta` | Shop Now phrasing detected in ad copy |
| `is_shopify_store` | Whether the linked URL is a Shopify store |
| `shopify_detection_reason` | How Shopify was (or wasn't) detected |
| `video_ads_present` | Any video ads in the cluster |
| `ad_start_dates` | Dates ads started (within window) |
| `earliest_ad_start` | Oldest ad start date |
| `latest_ad_start` | Newest ad start date |
| `sample_ad_title` | Title of the first ad in the cluster |
| `sample_ad_body` | Body copy of the first ad |
| `snapshot_url` | FB Ads Library snapshot link |
| `publisher_platforms` | facebook, instagram, audience_network, etc. |
| `keywords_matched` | Which search terms found this page |
| `languages` | Languages the ad runs in |
| `impressions_range` | Estimated impression range |
| `spend_range` | Estimated spend range |
| `currency` | Currency for spend |

## Limitations & Notes

- **Follower counts** require the `pages_read_engagement` permission on your token.
- **Shop Now button type** is not directly exposed by the Ads Library API; detection is based on ad copy text signals.
- **Destination URLs** are not always available in the API; Shopify detection falls back to the page's linked website.
- The Ads Library API has **rate limits** — the tool automatically retries with exponential backoff.
- Results are sorted by: most ads → video first → Shopify first.
- Use `-d 3` for a tighter recency window (last 3 days) to find the hottest products.
