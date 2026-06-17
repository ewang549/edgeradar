-- Singular test: every non-null match confidence must lie in [0,1].
-- (Singletons carry NULL confidence, which is allowed.) Passes when zero rows.

select map_key, match_confidence
from {{ ref("stg_event_map") }}
where match_confidence is not null
  and (match_confidence < 0 or match_confidence > 1)
