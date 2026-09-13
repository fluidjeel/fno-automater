# News, macro and sentiment subsystem

## Runtime boundary

The subsystem is a Layer 1 evidence producer. `trading news collect` polls the
sources listed in `config/news.yaml`; `snapshot` persists normalized items,
event clusters, a sentiment snapshot and advisory event-risk states as JSONL.
`status`, `latest`, `demo`, `schedule`, and `proposal` are local operations. The
weekly proposal generator currently abstains. No command can place or modify an
order, close a position, or change strategy configuration. Existing Fyers
market-data collection does not depend on this subsystem.

Install the regular collector dependencies with `uv sync --extra news --group
dev`. FinBERT is a separate optional extra (`uv sync --extra sentiment --group
dev`) and requires a model already present locally; the adapter sets
`local_files_only=True`. When it is not configured, sentiment probabilities are
absent, direction is uncertain, and confidence is zero. The deterministic
lexical classifier exists only for tests.

## Sources and limits

- GDELT DOC API: public article metadata discovery at
  `https://api.gdeltproject.org/api/v2/doc/doc`. Retain attribution and original
  links; do not republish full article text. See [GDELT API overview](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
  and [GDELT terms](https://gdeltproject.org/about.html).
- RBI RSS: official releases and notifications; configured feeds are
  `https://rbi.org.in/pressreleases_rss.xml` and
  `https://rbi.org.in/notifications_rss.xml`. See the [RBI RSS directory](https://www.rbi.org.in/Scripts/rss.aspx).
- SEBI RSS: `https://www.sebi.gov.in/sebirss.xml`; see [SEBI RSS](https://www.sebi.gov.in/rss.html).
- NSE circular feed: `https://feeds.feedburner.com/nseindia/circulars`; see [NSE RSS](https://www.nseindia.com/static/rss-feed).
- PIB releases: `https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=1`; see [PIB RSS](https://www.pib.gov.in/ViewRss.aspx?lang=1&reg=1).
- FRED requires `FRED_API_KEY`. The configured DGS10 observations use initial
  release output, but the API observation date is not treated as an actual news
  release time. See [FRED observation docs](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)
  and [FRED terms](https://fred.stlouisfed.org/docs/api/terms_of_use.html).
- EIA requires `EIA_API_KEY` and is currently a disabled-until-configured
  adapter: configure a concrete API v2 route and series before enabling it. See
  [EIA API docs](https://www.eia.gov/opendata/documentation.php).
- Google News RSS is optional and disabled by default.

RSS/GDELT content is metadata plus a bounded snippet. Respect each provider's
current terms, request limits, attribution requirements and retention policy.
The config is a research profile: source credibility weights, event taxonomy,
impact scores and event-risk thresholds are not historically calibrated. A
`BLOCK_NEW_ENTRIES` or `MARKET_EMERGENCY` value is advisory evidence only and
requires a separately reviewed Layer 2 contract before any strategy consumes it.

## Commands

```bash
uv run trading news demo
uv run trading news collect
uv run trading news snapshot
uv run trading news status
uv run trading news latest
uv run trading news proposal
uv run trading news schedule
```

`collect` and `snapshot` perform one bounded cycle. `backfill` repeats the same
source-window cycle; it does not crawl arbitrary provider history. `schedule`
prints the configured Asia/Kolkata windows; use an external cron/systemd unit to
invoke `snapshot`. No daemon is installed automatically. The collector has
bounded retries, per-source polling intervals, response-size limits and
circuit cooldowns. Missing API keys, inaccessible feeds and parse errors appear
as source health statuses and do not interrupt market-data ingestion.
