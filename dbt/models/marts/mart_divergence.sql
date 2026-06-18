-- Mart: mart_divergence
-- For each market in a cross-platform event, how far its implied probability sits
-- from the consensus of the OTHER platforms (leave-one-out, so a market isn't
-- compared to itself), and whether that gap survives the cost to trade.
--
--   deviation              = implied_prob - consensus
--   edge_net               = |deviation| - trade_cost          (cost-aware edge)
--   dispersion             = stddev of platform prices for the event (disagreement)
--   uncertainty_adj_edge   = edge_net - dispersion             (penalize noisy consensus)
--   confidence_tier        = high / medium / low (more platforms + tight agreement = higher)
--   is_signal              = edge_net > divergence_min_edge
--
-- Ranked by uncertainty-adjusted edge. REVIEW aid only; `side_hint` notes direction.
-- Nothing here is acted on automatically.

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
        count(*)                       as n_markets,
        count(distinct source)         as n_sources,
        sum(implied_prob)              as sum_p,
        coalesce(stddev_pop(implied_prob), 0.0) as dispersion
    from q
    group by event_id
),

joined as (
    select
        q.*,
        ev.n_sources,
        ev.n_markets,
        ev.dispersion,
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
    n_sources,
    n_markets,
    implied_prob,
    consensus,
    round(dispersion, 4)                                           as dispersion,
    implied_prob - consensus                                       as deviation,
    abs(implied_prob - consensus)                                  as abs_deviation,
    trade_cost,
    abs(implied_prob - consensus) - trade_cost                     as edge_net,
    -- Penalize the edge by how much the platforms disagree (noisy consensus).
    abs(implied_prob - consensus) - trade_cost - dispersion        as uncertainty_adj_edge,
    (abs(implied_prob - consensus) - trade_cost) > {{ var("divergence_min_edge") }} as is_signal,
    case
        when n_sources >= 3 and dispersion < 0.05 then 'high'
        when n_sources >= 2 and dispersion < 0.10 then 'medium'
        else 'low'
    end                                                            as confidence_tier,
    case when implied_prob > consensus
         then 'priced rich here vs consensus'
         else 'priced cheap here vs consensus' end                 as side_hint
from joined
order by uncertainty_adj_edge desc
