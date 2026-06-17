-- Mart (eval): every resolved signal scored against its actual outcome —
-- whether the implied side won (hit) and the hypothetical PnL net of fees.
-- Produced by `edgeradar evaluate`; this model exposes + ranks it.

{{ config(materialized = "table", tags = ["eval"]) }}

select * from read_parquet('{{ var("signal_scores_path") }}')
order by edge_net desc
