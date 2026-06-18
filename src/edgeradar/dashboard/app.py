"""EdgeRadar — a READ-ONLY analytics product over the DuckDB warehouse.

A multi-page Streamlit app for reviewing cross-platform prediction-market
mispricings, the data quality underneath them, how well past signals have been
calibrated, and the health of the pipeline itself. It only ever *reads* marts and
Parquet reports — it cannot place a trade, and contains no order-execution path.

Pages (sidebar):
  • Overview            — product framing + top-line KPIs
  • Divergences         — filterable explorer with confidence + edge breakdown
  • Resolution          — how cross-platform events were matched (and near misses)
  • Weather             — NWS forecast vs Kalshi temperature markets + assumptions
  • Calibration         — do the signals actually hold up against settled outcomes?
  • Source health       — freshness / nulls / duplicates / reliability per source
  • System status       — which pipeline stages have produced data

Run via `make dashboard` (http://localhost:8501).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

from edgeradar.analytics import confidence_tier, decompose_edge
from edgeradar.config import get_settings

st.set_page_config(page_title="EdgeRadar", layout="wide", page_icon="📡")

SETTINGS = get_settings()
DATA_ROOT = Path(SETTINGS.data_root)
MARTS = DATA_ROOT / "marts"


# --------------------------------------------------------------------------- #
# Data access (cached, fail-soft)
# --------------------------------------------------------------------------- #


@st.cache_data(ttl=30)
def query(sql: str) -> pd.DataFrame:
    """Run a read-only query against the warehouse; empty frame if it's not ready."""
    try:
        con = duckdb.connect(SETTINGS.duckdb_path, read_only=True)
    except Exception:
        return pd.DataFrame()
    try:
        return con.sql(sql).df()
    except Exception:
        return pd.DataFrame()
    finally:
        con.close()


@st.cache_data(ttl=30)
def read_parquet(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(p)
    except Exception:
        return pd.DataFrame()


def empty_state(message: str, command: str | None = None) -> None:
    """A friendly, consistent empty state instead of a raw error."""
    st.info(message)
    if command:
        st.code(command, language="bash")


def section(title: str, help_text: str | None = None) -> None:
    st.subheader(title)
    if help_text:
        st.caption(help_text)


# --------------------------------------------------------------------------- #
# Shared header
# --------------------------------------------------------------------------- #

st.sidebar.title("📡 EdgeRadar")
st.sidebar.caption("Read-only mispricing analytics")
PAGE = st.sidebar.radio(
    "Page",
    [
        "Overview",
        "Divergences",
        "Resolution",
        "Weather",
        "Calibration",
        "Source health",
        "System status",
    ],
    label_visibility="collapsed",
)
st.sidebar.divider()
st.sidebar.success("🔒 Read-only — never places orders")
st.sidebar.caption(
    "A signal is a prompt to look, not advice. Trust it only after the calibration "
    "page holds up across many resolved events."
)


def kpis() -> dict:
    """Cheap top-line counts pulled once for the Overview + status pages."""
    q = read_parquet(str(MARTS / "data_quality.parquet"))
    if q.empty:
        q = read_parquet(str(DATA_ROOT / "quality" / "data_quality.parquet"))
    div = query("select * from mart_divergence")
    events = query("select n_sources from dim_event")
    quotes = query("select count(*) as n from fact_market_quotes")
    calib = read_parquet(str(MARTS / "market_calibration.parquet"))
    last_ingest = None
    if not q.empty and "last_snapshot" in q:
        last_ingest = pd.to_datetime(q["last_snapshot"], utc=True, errors="coerce").max()
    return {
        "n_sources": int(q["source"].nunique()) if not q.empty else 0,
        "n_quotes": int(quotes["n"].iloc[0]) if not quotes.empty else 0,
        "n_events": int(len(events)) if not events.empty else 0,
        "n_cross": int((events["n_sources"] > 1).sum()) if not events.empty else 0,
        "n_signals": int(div["is_signal"].sum()) if "is_signal" in div else 0,
        "last_ingest": last_ingest,
        "eval_n": int(len(calib)) if not calib.empty else 0,
        "quality": q,
    }


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


def page_overview() -> None:
    st.title("EdgeRadar")
    st.caption(
        "A streaming data platform that watches the same real-world events across "
        "Kalshi, Manifold, Polymarket and sportsbooks, and surfaces where one venue "
        "disagrees with the consensus — *after* fees and uncertainty. Built for "
        "review and evaluation, not execution."
    )
    k = kpis()

    c1, c2, c3 = st.columns(3)
    c1.metric("Data sources", k["n_sources"])
    c2.metric("Live quotes", f"{k['n_quotes']:,}")
    c3.metric("Cross-platform events", k["n_cross"])
    c4, c5, c6 = st.columns(3)
    c4.metric("Flagged divergences", k["n_signals"])
    c5.metric("Evaluated outcomes", k["eval_n"])
    if k["last_ingest"] is not None and not pd.isna(k["last_ingest"]):
        age_min = (pd.Timestamp.now(tz="UTC") - k["last_ingest"]).total_seconds() / 60
        c6.metric("Last ingest", f"{age_min:.0f} min ago")
    else:
        c6.metric("Last ingest", "—")

    if k["n_quotes"] == 0:
        st.divider()
        empty_state(
            "No data yet. Build the offline demo (uses bundled sample responses, no "
            "network) to populate every page:",
            "make demo",
        )
        return

    st.divider()
    section(
        "How to read this",
        "Each layer is more conservative than the last — this is the core idea of the project.",
    )
    st.markdown(
        "- **Raw edge** — how far a venue's price sits from the consensus of the others.\n"
        "- **Fee-adjusted edge** — raw edge minus the modeled cost to trade "
        "(Kalshi fee + half-spread). Most raw gaps die here.\n"
        "- **Uncertainty-adjusted edge** — fee-adjusted edge minus how much the "
        "platforms disagree with each other. A gap means little when the consensus "
        "is itself noisy.\n"
        "- **Confidence tier** — high / medium / low, from how many independent "
        "platforms priced the event and how tightly they agree."
    )


def page_divergences() -> None:
    st.title("Divergence explorer")
    div = query("select * from mart_divergence")
    if div.empty or "edge_net" not in div:
        empty_state(
            "No divergences yet. Populate the warehouse first:",
            "make demo   # or: make ingest && make resolve && make dbt",
        )
        return

    # ---- filters ---------------------------------------------------------- #
    with st.container():
        f1, f2, f3, f4 = st.columns([2, 2, 2, 3])
        cats = ["(all)"] + sorted(div["category"].dropna().unique().tolist())
        cat = f1.selectbox("Category", cats)
        tiers = f2.multiselect(
            "Confidence", ["high", "medium", "low"], default=["high", "medium", "low"]
        )
        min_edge = f3.slider("Min fee-adjusted edge", 0.0, 0.20, 0.0, 0.01)
        search = f4.text_input("Search title", "")

    view = div.copy()
    if cat != "(all)":
        view = view[view["category"] == cat]
    if tiers:
        view = view[view["confidence_tier"].isin(tiers)]
    view = view[view["edge_net"] >= min_edge]
    if search.strip():
        view = view[view["canonical_title"].str.contains(search.strip(), case=False, na=False)]

    sort_col = "uncertainty_adj_edge" if "uncertainty_adj_edge" in view else "edge_net"
    view = view.sort_values(sort_col, ascending=False)

    st.caption(
        f"{len(view)} row(s) shown · {int(view['is_signal'].sum())} above the signal "
        f"threshold · sorted by {sort_col.replace('_', ' ')}"
    )

    if view.empty:
        empty_state("No divergences match these filters. Widen them to see more.")
        return

    tier_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}
    display = view.assign(
        confidence=view["confidence_tier"].map(lambda t: f"{tier_emoji.get(t, '⚪')} {t}")
    )
    st.dataframe(
        display[
            [
                "canonical_title",
                "source",
                "confidence",
                "implied_prob",
                "consensus",
                "dispersion",
                "trade_cost",
                "edge_net",
                "uncertainty_adj_edge",
                "side_hint",
            ]
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "canonical_title": "event",
            "implied_prob": st.column_config.NumberColumn("price", format="%.3f"),
            "consensus": st.column_config.NumberColumn(format="%.3f"),
            "dispersion": st.column_config.NumberColumn(format="%.3f"),
            "trade_cost": st.column_config.NumberColumn("cost", format="%.3f"),
            "edge_net": st.column_config.NumberColumn("fee-adj edge", format="%.3f"),
            "uncertainty_adj_edge": st.column_config.NumberColumn("unc-adj edge", format="%.3f"),
        },
    )

    section("Top signals — explained", "Where each apparent edge comes from, and what it survives.")
    for _, r in view.head(5).iterrows():
        conf = confidence_tier(int(r.get("n_sources", 0)), float(r.get("dispersion", 0.0)))
        b = decompose_edge(
            float(r["implied_prob"]),
            float(r["consensus"]),
            float(r.get("trade_cost", 0.0)),
            float(r.get("dispersion", 0.0)),
        )
        title = f"{tier_emoji.get(conf.tier, '⚪')} {r['canonical_title']} — {r['source']}"
        with st.expander(title):
            st.write(
                f"**{r['source']}** prices this at **{r['implied_prob']:.3f}** vs a "
                f"consensus of **{r['consensus']:.3f}** ({r['side_hint']})."
            )
            st.write(
                f"- Raw edge: `{b.raw_edge:.3f}`\n"
                f"- After trading cost ({b.trade_cost:.3f}): `{b.fee_adjusted_edge:.3f}`"
                f" {'✅ survives' if b.survives_costs else '❌ gone'}\n"
                f"- After uncertainty ({b.dispersion:.3f} dispersion): "
                f"`{b.uncertainty_adj_edge:.3f}`"
                f" {'✅ survives' if b.survives_uncertainty else '❌ gone'}"
            )
            st.write("**Confidence: " + conf.tier + "** — " + "; ".join(conf.reasons))


def page_resolution() -> None:
    st.title("Event resolution workbench")
    section(
        "Cross-platform matching",
        "How EdgeRadar decides two differently-worded markets describe the same event.",
    )
    em = read_parquet(str(MARTS / "event_map.parquet"))
    if em.empty:
        empty_state("No event map yet. Run resolution:", "make demo   # or: make resolve")
        return

    sizes = em.groupby("event_id")["source"].nunique()
    cross_ids = sizes[sizes > 1].index.tolist()
    cross = em[em["event_id"].isin(cross_ids)]

    c1, c2, c3 = st.columns(3)
    c1.metric("Markets mapped", len(em))
    c2.metric("Events", em["event_id"].nunique())
    c3.metric("Cross-platform events", len(cross_ids))

    if cross_ids:
        labels = (
            cross.drop_duplicates("event_id").set_index("event_id")["canonical_title"].to_dict()
        )
        choice = st.selectbox(
            "Inspect a matched event", cross_ids, format_func=lambda e: labels.get(e, e)
        )
        members = cross[cross["event_id"] == choice]
        st.dataframe(
            members[["source", "title", "match_method", "match_confidence"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "match_confidence": st.column_config.NumberColumn("confidence", format="%.2f")
            },
        )
    else:
        empty_state("No cross-platform events matched in the current data.")

    section("Near misses", "Pairs scored just under the match threshold — surfaced for review.")
    pairs = read_parquet(str(MARTS / "candidate_pairs.parquet"))
    if pairs.empty:
        st.caption("No candidate pairs recorded (single-source data, or nothing near the cutoff).")
    else:
        near = pairs[pairs["decision"] == "no-match"] if "decision" in pairs else pairs
        if near.empty:
            st.caption("No near-miss pairs — every scored pair was a clear match or non-match.")
        else:
            st.dataframe(
                near[["category", "confidence", "title_a", "title_b"]].head(50),
                use_container_width=True,
                hide_index=True,
                column_config={"confidence": st.column_config.NumberColumn(format="%.2f")},
            )
            st.caption(
                "To force or forbid a match, edit `seeds/event_overrides.csv` and re-run "
                "resolution. Overrides always win over the fuzzy score."
            )


def page_weather() -> None:
    st.title("Weather forecast module")
    section(
        "NWS forecast vs Kalshi temperature markets",
        "A worked example: turning a physical forecast into a probability, then a price compare.",
    )
    wx = read_parquet(str(MARTS / "weather_edge.parquet"))
    if wx.empty:
        wx = query("select * from mart_weather_edge")
    if wx.empty or "edge_net" not in wx:
        empty_state("No weather edges yet. Run:", "make demo   # or: make weather")
        return

    with st.expander("Model assumptions (read this before trusting a number)"):
        st.markdown(
            "- We treat the NWS daily-high forecast as the mean of a **Normal** "
            "distribution over the actual high.\n"
            "- The standard deviation (σ) is **calibrated empirically** from resolved "
            "outcomes (`make` → `calibrate-sigma`), not guessed.\n"
            "- `forecast_prob` = P(high ≥ threshold) under that Normal. It is compared "
            "to the Kalshi market price, then charged the trading cost.\n"
            "- This only runs on genuine temperature markets (a guard rejects "
            "sports/other questions that mention weather words)."
        )

    wanted = [
        "location",
        "title",
        "forecast_prob",
        "kalshi_prob",
        "trade_cost",
        "edge_net",
        "is_signal",
    ]
    cols = [c for c in wanted if c in wx]
    st.dataframe(
        wx.sort_values("edge_net", ascending=False)[cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "forecast_prob": st.column_config.NumberColumn("forecast P", format="%.3f"),
            "kalshi_prob": st.column_config.NumberColumn("market P", format="%.3f"),
            "trade_cost": st.column_config.NumberColumn("cost", format="%.3f"),
            "edge_net": st.column_config.NumberColumn("net edge", format="%.3f"),
        },
    )


def page_calibration() -> None:
    st.title("Calibration & evaluation")
    section(
        "Do the signals actually hold up?",
        "The honest part: scoring predictions against settled outcomes. Small samples mean little.",
    )

    mc = read_parquet(str(MARTS / "market_calibration.parquet"))
    if mc.empty:
        empty_state(
            "No settled-outcome calibration yet. Score already-settled Kalshi markets:",
            "make backfill",
        )
        return

    brier = float(((mc["predicted"] - mc["outcome"]) ** 2).mean())
    acc = float((((mc["predicted"] >= 0.5).astype(int)) == mc["outcome"]).mean())
    c1, c2, c3 = st.columns(3)
    c1.metric("Markets scored", f"{len(mc):,}")
    c2.metric("Brier score", f"{brier:.3f}", help="Lower is better. 0.25 = no skill.")
    c3.metric("Favorite accuracy", f"{acc:.1%}")

    mc = mc.copy()
    mc["bucket"] = (mc["predicted"] * 10).round() / 10
    curve = (
        mc.groupby("bucket")
        .agg(n=("outcome", "size"), predicted=("predicted", "mean"), realized=("outcome", "mean"))
        .reset_index()
    )
    section("Calibration curve", "Predicted vs realized. On the diagonal = perfectly calibrated.")
    st.line_chart(curve.set_index("bucket")[["predicted", "realized"]])
    st.dataframe(curve, use_container_width=True, hide_index=True)

    if "group" in mc:
        section(
            "By market type",
            "Realized below predicted in cheap buckets = favorite-longshot bias.",
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

    st.warning(
        "Limitations: this scores *market closing prices* against outcomes (a market-wide "
        "calibration study), which is context — not a backtest of EdgeRadar's own live "
        "signals. Forward signal scoring accumulates as logged signals resolve over time. "
        "A favorite-longshot bias is a known effect, not a guaranteed, tradeable edge."
    )


def page_source_health() -> None:
    st.title("Source health & data quality")
    section(
        "Can you trust the data underneath?",
        "Freshness, completeness, duplicates and a blended reliability grade per source.",
    )
    q = read_parquet(str(DATA_ROOT / "quality" / "data_quality.parquet"))
    if q.empty:
        empty_state("No quality report yet. Generate one:", "make demo   # or: edgeradar quality")
        return

    grade_emoji = {"A": "🟢", "B": "🟢", "C": "🟡", "D": "🟠", "F": "🔴"}
    show = q.assign(grade=q["reliability_grade"].map(lambda g: f"{grade_emoji.get(g, '⚪')} {g}"))
    st.dataframe(
        show[
            [
                "source",
                "grade",
                "reliability_score",
                "n_quotes",
                "n_markets",
                "age_minutes",
                "null_rate",
                "duplicate_rate",
                "prob_violations",
                "issues",
            ]
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "reliability_score": st.column_config.ProgressColumn(
                "reliability", min_value=0, max_value=100, format="%.0f"
            ),
            "age_minutes": st.column_config.NumberColumn("age (min)", format="%.0f"),
            "null_rate": st.column_config.NumberColumn(format="%.2f"),
            "duplicate_rate": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    if "generated_at" in q and len(q):
        ts = pd.to_datetime(q["generated_at"].iloc[0], utc=True, errors="coerce")
        if not pd.isna(ts):
            st.caption(f"Report generated {ts:%Y-%m-%d %H:%M UTC}.")
    st.caption(
        "Note: the offline `make demo` data carries a fixed timestamp, so every "
        "source reads as 'stale' here — that's the freshness check working, not a "
        "bug. Run `make refresh` against live data to see real freshness grades."
    )


def page_system_status() -> None:
    st.title("System status")
    section(
        "Pipeline stages", "Which stages have produced data. Green = ready, grey = not run yet."
    )

    checks = [
        ("Warehouse (DuckDB)", Path(SETTINGS.duckdb_path).exists()),
        ("Clean lake (quotes)", not query("select 1 from fact_market_quotes limit 1").empty),
        ("dbt marts (divergence)", not query("select 1 from mart_divergence limit 1").empty),
        ("Entity resolution", (MARTS / "event_map.parquet").exists()),
        ("Weather edge", (MARTS / "weather_edge.parquet").exists()),
        ("Data-quality report", (DATA_ROOT / "quality" / "data_quality.parquet").exists()),
        ("Signal log", (MARTS / "signal_log.parquet").exists()),
        ("Calibration (settled)", (MARTS / "market_calibration.parquet").exists()),
    ]
    rows = [{"stage": name, "status": "✅ ready" if ok else "⬜ not run"} for name, ok in checks]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    ready = sum(1 for _, ok in checks if ok)
    st.caption(f"{ready}/{len(checks)} stages have produced data.")
    if ready < len(checks):
        st.code("make demo   # build everything offline from sample data", language="bash")

    st.divider()
    st.caption(
        f"Warehouse: `{SETTINGS.duckdb_path}` · order execution: "
        f"`{SETTINGS.enable_order_execution}` (must be False)."
    )


PAGES = {
    "Overview": page_overview,
    "Divergences": page_divergences,
    "Resolution": page_resolution,
    "Weather": page_weather,
    "Calibration": page_calibration,
    "Source health": page_source_health,
    "System status": page_system_status,
}

PAGES[PAGE]()
