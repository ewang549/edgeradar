-- Staging (eval): the append-only signal log written by `edgeradar log-signals`.
-- Tagged 'eval' so the core `make dbt` build never depends on it (the file only
-- exists after the evaluation step runs).

{{ config(materialized = "view", tags = ["eval"]) }}

select * from read_parquet('{{ var("signal_log_path") }}')
