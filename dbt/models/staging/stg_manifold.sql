-- Staging: Manifold quotes.
-- Reads the clean Parquet lake directly (DuckDB can query Parquet in place),
-- filters to this source, and standardizes types. Float NaN (from illiquid /
-- undefined probabilities written by the adapter) is converted to NULL so
-- downstream NULL semantics are clean.

with raw as (

    select *
    from read_parquet('{{ var("clean_glob") }}', union_by_name = true)
    where source = 'manifold'

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
    cast(snapshot_ts as timestamp)                                as snapshot_ts,
    cast(close_ts as timestamp)                                   as close_ts
from raw
