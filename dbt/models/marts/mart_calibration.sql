-- Mart (eval): calibration table. For each predicted-probability bucket, the mean
-- predicted probability vs the realized hit rate. A well-calibrated signal has
-- realized_rate ~= predicted_mean. The honest test of whether the edge is real.

{{ config(materialized = "table", tags = ["eval"]) }}

select
    prob_bucket,
    count(*)                            as n,
    round(avg(predicted_prob_side), 4)  as predicted_mean,
    round(avg(hit), 4)                  as realized_rate
from {{ ref("mart_signal_scores") }}
group by prob_bucket
order by prob_bucket
