"""EdgeRadar dashboard — a READ-ONLY view over the DuckDB warehouse.

Panels: divergence leaderboard, per-event cross-platform prices, weather edge, and
the evaluation/calibration report. It only reads marts; it cannot place trades.

Run via `make dashboard` (http://localhost:8501).
"""

from __future__ import annotations

import duckdb
import pandas as pd
import streamlit as st

from edgeradar.config import get_settings


@st.cache_data(ttl=30)
def _query(sql: str) -> pd.DataFrame:
    settings = get_settings()
    con = duckdb.connect(settings.duckdb_path, read_only=True)
    try:
        return con.sql(sql).df()
    except Exception as exc:  # table may not exist yet
        return pd.DataFrame({"info": [f"not available yet: {exc}"]})


st.set_page_config(page_title="EdgeRadar", layout="wide")
st.title("EdgeRadar")
st.caption(
    "Read-only cross-platform mispricing monitor. Signals are for human review only — "
    "this tool never places orders. A signal is not advice; trust it only after the "
    "calibration report holds up across many resolved events."
)

div = _query("select * from mart_divergence order by edge_net desc")
wx = _query("select * from mart_weather_edge order by edge_net desc")
scores = _query("select * from mart_signal_scores")
calib = _query("select * from mart_calibration order by prob_bucket")

# --- top-line metrics --------------------------------------------------------
c1, c2, c3 = st.columns(3)
c1.metric("Divergence signals", int(div["is_signal"].sum()) if "is_signal" in div else 0)
c2.metric("Weather signals", int(wx["is_signal"].sum()) if "is_signal" in wx else 0)
if "hit" in scores and len(scores):
    c3.metric("Hit rate (resolved)", f"{scores['hit'].mean():.0%}")
else:
    c3.metric("Hit rate (resolved)", "—")

tab_div, tab_event, tab_wx, tab_eval, tab_cal = st.tabs(
    [
        "Divergence leaderboard",
        "Per-event prices",
        "Weather edge",
        "Evaluation",
        "Market calibration",
    ]
)

with tab_div:
    st.subheader("Divergence leaderboard (net of trading cost)")
    if "edge_net" in div:
        st.dataframe(
            div[
                [
                    "canonical_title",
                    "source",
                    "implied_prob",
                    "consensus",
                    "deviation",
                    "trade_cost",
                    "edge_net",
                    "is_signal",
                    "side_hint",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Run `make ingest` → `make resolve` → `make dbt` to populate.")

with tab_event:
    st.subheader("Cross-platform prices for one event")
    fqe = _query(
        "select event_id, canonical_title, source, implied_prob, title "
        "from fact_quotes_with_event where event_id is not null order by event_id"
    )
    if "event_id" in fqe and len(fqe):
        labels = fqe.drop_duplicates("event_id").set_index("event_id")["canonical_title"].to_dict()
        choice = st.selectbox("Event", list(labels), format_func=lambda e: labels.get(e, e))
        st.dataframe(
            fqe[fqe["event_id"] == choice][["source", "implied_prob", "title"]],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No resolved events yet.")

with tab_wx:
    st.subheader("Weather edge — NWS forecast vs Kalshi temperature markets")
    if "edge_net" in wx:
        st.dataframe(
            wx[
                [
                    "location",
                    "title",
                    "forecast_prob",
                    "kalshi_prob",
                    "trade_cost",
                    "edge_net",
                    "is_signal",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Run `make weather` to populate.")

with tab_eval:
    st.subheader("Evaluation — does the edge survive scoring?")
    if "hit" in scores and len(scores):
        cc1, cc2 = st.columns(2)
        tradeable = scores[scores["tradeable"]] if "tradeable" in scores else scores
        cc1.metric(
            "Net PnL (tradeable, per contract)",
            f"{tradeable['pnl_net'].sum():+.3f}"
            if "pnl_net" in tradeable and len(tradeable)
            else "—",
        )
        cc2.metric("Resolved signals", len(scores))
        st.markdown("**Calibration — predicted vs realized**")
        if "predicted_mean" in calib:
            st.dataframe(calib, use_container_width=True, hide_index=True)
            chart = calib.rename(columns={"prob_bucket": "predicted bucket"}).set_index(
                "predicted bucket"
            )
            st.bar_chart(chart[["predicted_mean", "realized_rate"]])
        st.dataframe(
            scores[
                [
                    "signal_type",
                    "market_id",
                    "side",
                    "platform_prob",
                    "predicted_prob_side",
                    "outcome",
                    "hit",
                    "pnl_net",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "A few samples prove nothing — calibration is meaningful only across many events."
        )
    else:
        st.info("Run `make evaluate` to score logged signals against outcomes.")

with tab_cal:
    st.subheader("Market calibration — settled Kalshi markets (closing price vs reality)")
    from pathlib import Path

    cal_path = Path(get_settings().data_root) / "marts" / "market_calibration.parquet"
    if not cal_path.exists():
        st.info("Run `make backfill` to score already-settled markets and populate this.")
    else:
        mc = pd.read_parquet(cal_path)
        if mc.empty:
            st.info("No settled markets scored yet. Run `make backfill`.")
        else:
            brier = float(((mc["predicted"] - mc["outcome"]) ** 2).mean())
            acc = float((((mc["predicted"] >= 0.5).astype(int)) == mc["outcome"]).mean())
            m1, m2 = st.columns(2)
            m1.metric("Markets scored", len(mc))
            m2.metric("Brier (lower=better)", f"{brier:.3f}")
            st.caption(f"Favorite accuracy: {acc:.1%}")

            mc = mc.copy()
            mc["bucket"] = (mc["predicted"] * 10).round() / 10
            curve = (
                mc.groupby("bucket")
                .agg(
                    n=("outcome", "size"),
                    predicted=("predicted", "mean"),
                    realized=("outcome", "mean"),
                )
                .reset_index()
            )
            st.markdown("**Calibration curve** (predicted vs realized; equal = well-calibrated)")
            st.line_chart(curve.set_index("bucket")[["predicted", "realized"]])

            st.markdown(
                "**By market type** (realized < predicted in cheap buckets = longshot bias)"
            )
            grp = (
                mc.groupby("group")
                .agg(
                    n=("outcome", "size"),
                    brier=(
                        "predicted",
                        lambda s: float(((s - mc.loc[s.index, "outcome"]) ** 2).mean()),
                    ),
                )
                .reset_index()
            )
            st.dataframe(grp, use_container_width=True, hide_index=True)
            st.caption(
                "Read-only analysis. A favorite-longshot bias is a known market "
                "effect, not a guaranteed edge."
            )
