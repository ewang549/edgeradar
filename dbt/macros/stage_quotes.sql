{#
    stage_quotes(source)

    Shared staging logic for every market source. Reads the clean Parquet lake,
    filters to one source, builds the surrogate `quote_key` from the natural key
    (source, market_id, outcome, snapshot_ts), and converts NaN floats to NULL so
    downstream NULL semantics are clean. Every per-source staging model is a
    one-liner calling this macro, which keeps the four sources perfectly in sync
    (so the fact_market_quotes union never drifts) and removes ~120 lines of copy-paste.
#}
{% macro stage_quotes(source) %}

with raw as (

    select *
    from read_parquet('{{ var("clean_glob") }}', union_by_name = true)
    where source = '{{ source }}'

)

select
    md5(
        source || '|' || market_id || '|' || outcome || '|' || cast(snapshot_ts as varchar)
    )                                                              as quote_key,
    source,
    market_id,
    outcome,
    title,
    cast(price as double)                                          as price,
    case when implied_prob is null or isnan(implied_prob)
         then null else implied_prob end                          as implied_prob,
    case when fee_adj_prob is null or isnan(fee_adj_prob)
         then null else fee_adj_prob end                          as fee_adj_prob,
    case when spread is null or isnan(spread)
         then null else spread end                                as spread,
    case when trade_cost is null or isnan(trade_cost)
         then null else trade_cost end                            as trade_cost,
    coalesce(price_is_stale, false)                                as price_is_stale,
    cast(snapshot_ts as timestamp)                                as snapshot_ts,
    cast(close_ts as timestamp)                                   as close_ts
from raw

{% endmacro %}
