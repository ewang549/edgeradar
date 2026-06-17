-- Mart: mart_weather_edge
-- Reads the weather-edge table produced by `edgeradar weather`: for each Kalshi
-- temperature market, the NWS-forecast-implied probability vs Kalshi's price, and
-- the edge net of trading cost. Ranked by net edge. Review aid only.

{{ config(materialized = "table") }}

select
    location,
    market_id,
    title,
    direction,
    threshold_f,
    forecast_date,
    forecast_high_f,
    sigma_f,
    forecast_prob,
    kalshi_prob,
    trade_cost,
    edge_gross,
    edge_net,
    is_signal
from read_parquet('{{ var("weather_edge_path") }}')
order by edge_net desc
