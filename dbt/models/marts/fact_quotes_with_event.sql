-- Mart: fact_market_quotes joined to its resolved event_id.
-- This is what Phase 5 reads: every quote tagged with the canonical event it
-- belongs to, so quotes from different platforms for the same event line up.
-- LEFT join so quotes without a resolved event (e.g. before `resolve` runs)
-- still appear, with a NULL event_id.

{{ config(materialized = "view") }}

select
    q.*,
    m.event_id,
    m.canonical_title,
    m.category
from {{ ref("fact_market_quotes") }} q
left join {{ ref("stg_event_map") }} m
    on q.source = m.source
   and q.market_id = m.market_id
