-- Staging: the entity-resolution output (market -> event_id) produced by
-- `edgeradar resolve`. One row per resolved market. NaN confidences (singletons)
-- are normalized to NULL.

with raw as (

    select * from read_parquet('{{ var("event_map_path") }}')

)

select
    md5(source || '|' || market_id)                               as map_key,
    source,
    market_id,
    title,
    category,
    event_id,
    canonical_title,
    match_method,
    case when match_confidence is null or isnan(match_confidence)
         then null else match_confidence end                      as match_confidence
from raw
