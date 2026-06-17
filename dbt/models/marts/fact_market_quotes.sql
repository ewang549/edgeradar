-- Mart: fact_market_quotes
-- The unified, cross-platform quote fact. One row per (source, market_id, outcome,
-- snapshot_ts). Every source adapter funnels into this single grain, so all
-- downstream analytics (divergence, weather edge, scoring) read from here.
--
-- event_id (the canonical cross-platform event) is intentionally absent until
-- Phase 4 builds dim_event via entity resolution; it will be joined on later.

{{ config(materialized = "table") }}

select * from {{ ref("stg_manifold") }}
union all
select * from {{ ref("stg_kalshi") }}
union all
select * from {{ ref("stg_polymarket") }}
