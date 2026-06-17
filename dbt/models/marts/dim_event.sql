-- Mart: dim_event
-- The canonical cross-platform event dimension. One row per event_id, aggregating
-- the markets that entity resolution grouped together. n_sources > 1 means the
-- event is priced on more than one platform — exactly the events the divergence
-- engine (Phase 5) compares.

{{ config(materialized = "table") }}

select
    event_id,
    any_value(canonical_title)              as canonical_title,
    any_value(category)                     as category,
    count(*)                                as n_markets,
    count(distinct source)                  as n_sources,
    list(distinct source)                   as sources,
    min(match_confidence)                   as min_match_confidence,
    avg(match_confidence)                   as avg_match_confidence
from {{ ref("stg_event_map") }}
group by event_id
