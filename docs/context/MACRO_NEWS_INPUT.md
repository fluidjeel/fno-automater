# Macro News Input

The data pipeline reads append-only JSON Lines from the `macro_news.input_file`
setting in `config/data_pipeline.yaml`. Each line is a structured classification
from a trusted, timestamped source; free-text news and model-generated live
sentiment are not accepted by this pipeline.

Required fields are `event_id`, `source`, `scope`, `published_at`, `received_at`,
`sentiment`, `impact`, `confidence` and `evidence_ref`. Timestamps must include a
timezone. `scope` is an underlying name such as `NIFTY`, or `GLOBAL`. Sentiment is
`-1` (bearish), `0` (neutral) or `1` (bullish); impact and confidence are decimal
values between zero and one.

Example file (copy to `data/macro_news.jsonl`):

`config/macro_news.jsonl.example`

Validate before a live fetch:

```bash
cp config/macro_news.jsonl.example data/macro_news.jsonl
uv run trading data news validate
```

At each market-data capture, the pipeline excludes records that were not yet
published and received, fall outside the configured age window, or do not match
the underlying/global scope. Remaining records receive linear time decay using
the configured half-life. Their impact and confidence weight a bounded mean
sentiment score. The score, parameters' version, and full structured evidence
are embedded in the canonical event so replay reproduces the same factor.

No records means no macro-news feature in the snapshot; it is not interpreted as
neutral sentiment. Current age and decay settings are initial research values,
not validated production thresholds. This JSONL contract remains the simple,
pre-classified input used by the market-data pipeline. The separate evidence
collection and advisory subsystem is described in [NEWS_SUBSYSTEM.md](NEWS_SUBSYSTEM.md).
