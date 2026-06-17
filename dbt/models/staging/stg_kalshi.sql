-- Staging: Kalshi quotes.
-- Same shape as stg_manifold; filtered to source='kalshi'. implied_prob here is
-- the YES bid/ask midpoint computed by the adapter; illiquid markets arrive as
-- NaN and are converted to NULL.

with raw as (

    select *
    from read_parquet('{{ var("clean_glob") }}', union_by_name = true)
    where source = 'kalshi'

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
