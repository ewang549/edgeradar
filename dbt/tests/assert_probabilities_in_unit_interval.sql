-- Singular data test: every non-null probability must lie strictly inside (0,1).
-- NULLs are allowed (illiquid markets have no defined probability). The test
-- passes when this query returns zero rows.

select
    quote_key,
    'implied_prob' as column_name,
    implied_prob   as value
from {{ ref("fact_market_quotes") }}
where implied_prob is not null
  and (implied_prob <= 0 or implied_prob >= 1)

union all

select
    quote_key,
    'fee_adj_prob' as column_name,
    fee_adj_prob   as value
from {{ ref("fact_market_quotes") }}
where fee_adj_prob is not null
  and (fee_adj_prob <= 0 or fee_adj_prob >= 1)
