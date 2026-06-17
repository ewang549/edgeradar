# Manual override seeds

## `event_overrides.csv`

Human-maintained corrections for entity resolution (Phase 4). Each row asserts a
relationship between two markets that **overrides** the automatic fuzzy matcher:

| column        | meaning                                  |
|---------------|------------------------------------------|
| `source_a`    | platform slug of market A (e.g. `kalshi`)|
| `market_id_a` | market A's native id                     |
| `source_b`    | platform slug of market B                |
| `market_id_b` | market B's native id                     |
| `relation`    | `match` (force same event) or `block` (force different) |

A `match` row groups the two markets with confidence 1.0 even if the fuzzy score
is low; a `block` row keeps them apart even if the fuzzy score is high. Rows that
reference market ids not present in the data are simply ignored, so the two
`EXAMPLE_*` rows shipped here are inert placeholders that just show the format —
replace them with real ids as you review proposed matches (`make resolve`).

## `resolutions.csv`

Known event outcomes used to score logged signals (Phase 6):

| column      | meaning                                       |
|-------------|-----------------------------------------------|
| `market_id` | the market's native id                        |
| `outcome`   | `1` if YES happened, `0` if NO                |
| `note`      | free-text note (ignored by the scorer)        |

For the demo these are filled in by hand for the sample markets. Live, they'd be
populated from Kalshi's settled `result` field and NWS observed highs (a future
automation; the scorer just reads this table).
