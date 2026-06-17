-- Mart: mart_divergence
-- For each market in a cross-platform event, how far its implied probability sits
-- from the consensus of the OTHER platforms (leave-one-out, so a market isn't
-- compared to itself), and whether that gap survives the cost to trade.
--
--   deviation  = implied_prob - consensus
--   edge_net   = |deviation| - trade_cost     (the honest, cost-aware edge)
--   is_signal  = edge_net > divergence_min_edge
--
-- Ranked by edge_net. This is a REVIEW aid, not advice: `side_hint` only notes
-- which direction the gap points. Nothing here is acted on automatically.

{{ config(materialized = "table") }}

with latest as (
    select
        *,
        row_number() over (partition by source, market_id order by snapshot_ts desc) as rn
    from {{ ref("fact_quotes_with_event") }}
    where event_id is not null
      and implied_prob is not null
),

q as (
    select
        event_id, canonical_title, category, source, market_id, title,
        implied_prob, snapshot_ts,
        coalesce(trade_cost, 0.0) as trade_cost
    from latest
    where rn = 1
),

ev as (
    select
        event_id,
        count(*)                as n_markets,
        count(distinct source)  as n_sources,
        sum(implied_prob)       as sum_p
    from q
    group by event_id
),

joined as (
    select
        q.*,
        ev.n_sources,
        (ev.sum_p - q.implied_prob) / nullif(ev.n_markets - 1, 0) as consensus
    from q
    join ev using (event_id)
    where ev.n_sources > 1
)

select
    event_id,
    canonical_title,
    category,
    source,
    market_id,
    title,
    snapshot_ts,
    implied_prob,
    consensus,
    implied_prob - consensus                                       as deviation,
    abs(implied_prob - consensus)                                  as abs_deviation,
    trade_cost,
    abs(implied_prob - consensus) - trade_cost                     as edge_net,
    (abs(implied_prob - consensus) - trade_cost) > {{ var("divergence_min_edge") }} as is_signal,
    case when implied_prob > consensus
         then 'priced rich here vs consensus'
         else 'priced cheap here vs consensus' end                 as side_hint
from joined
order by edge_net desc
