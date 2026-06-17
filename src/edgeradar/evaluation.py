"""Evaluation / backtest — the credibility core.

A pretty dashboard proves nothing. The only way to know whether a flagged "edge"
is real is to record every signal *at the moment it fires* (with the prices then),
wait for the event to resolve, and score what actually happened. This module does
exactly that:

* ``log_signals`` reads the current signal marts (divergence + weather) from the
  warehouse and appends them to an append-only ``signal_log`` — capturing the
  prices and the implied/forecast probabilities at signal time. Idempotent on a
  signal key, so re-running the same snapshot doesn't double-count.
* ``load_resolutions`` reads known outcomes (``seeds/resolutions.csv`` for the
  demo; live, these come from Kalshi's settled ``result`` / NWS observations).
* ``score_signals`` joins the two and computes, per signal: the side it implied,
  whether that side won (``hit``), the model's predicted probability for that side
  (for calibration), and the hypothetical PnL **net of fees** — counted only for
  the *tradeable* (Kalshi) side, since Manifold is play money.

Honesty notes baked in: PnL is only summed over tradeable signals; calibration
buckets predicted probability against realized outcomes; nothing here suggests
acting before the numbers hold up across many events.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd

from edgeradar.config import get_settings

SIGNAL_LOG_NAME = "signal_log.parquet"
SIGNAL_SCORES_NAME = "signal_scores.parquet"

# Stable schemas so the Parquet files always exist (even empty), letting the dbt
# eval models read them without special-casing a missing file.
SIGNAL_LOG_COLUMNS = [
    "signal_key",
    "signal_type",
    "event_id",
    "market_id",
    "source",
    "side",
    "platform_prob",
    "reference_prob",
    "predicted_prob_side",
    "edge_net",
    "trade_cost",
    "tradeable",
    "signal_ts",
    "title",
]
SIGNAL_SCORES_COLUMNS = SIGNAL_LOG_COLUMNS + ["outcome", "hit", "pnl_net", "prob_bucket"]


def _signal_key(signal_type: str, market_id: str, signal_ts: str, side: str) -> str:
    return hashlib.md5(f"{signal_type}|{market_id}|{signal_ts}|{side}".encode()).hexdigest()[:16]


def _marts_dir(data_root: str | None) -> Path:
    return Path(data_root or get_settings().data_root) / "marts"


def log_signals(*, data_root: str | None = None, duckdb_path: str | None = None) -> pd.DataFrame:
    """Append currently-flagged signals (with prices at signal time) to the signal_log.

    Reads mart_divergence and mart_weather_edge from the warehouse. Returns the
    full (deduped) signal_log DataFrame.
    """
    settings = get_settings()
    db = duckdb_path or settings.duckdb_path
    con = duckdb.connect(db, read_only=True)

    rows: list[dict] = []
    try:
        div = con.sql("select * from mart_divergence where is_signal").df()
    except Exception:
        div = pd.DataFrame()
    try:
        wx = con.sql("select * from mart_weather_edge where is_signal").df()
    except Exception:
        wx = pd.DataFrame()
    con.close()

    for _, r in div.iterrows():
        # deviation < 0 => market underprices YES vs consensus => implied YES side.
        side = "YES" if r["deviation"] < 0 else "NO"
        consensus = float(r["consensus"])
        predicted = consensus if side == "YES" else 1.0 - consensus
        signal_ts = str(pd.to_datetime(r["snapshot_ts"], utc=True))
        rows.append(
            {
                "signal_type": "divergence",
                "event_id": r["event_id"],
                "market_id": r["market_id"],
                "source": r["source"],
                "side": side,
                "platform_prob": float(r["implied_prob"]),
                "reference_prob": consensus,
                "predicted_prob_side": predicted,
                "edge_net": float(r["edge_net"]),
                "trade_cost": float(r["trade_cost"]),
                "tradeable": r["source"] == "kalshi",
                "signal_ts": signal_ts,
                "title": r["title"],
            }
        )

    for _, r in wx.iterrows():
        side = "YES" if float(r["forecast_prob"]) > float(r["kalshi_prob"]) else "NO"
        fc = float(r["forecast_prob"])
        predicted = fc if side == "YES" else 1.0 - fc
        rows.append(
            {
                "signal_type": "weather",
                "event_id": None,
                "market_id": r["market_id"],
                "source": "kalshi",
                "side": side,
                "platform_prob": float(r["kalshi_prob"]),
                "reference_prob": fc,
                "predicted_prob_side": predicted,
                "edge_net": float(r["edge_net"]),
                "trade_cost": float(r["trade_cost"]),
                "tradeable": True,
                "signal_ts": str(r["forecast_date"]),
                "title": r["title"],
            }
        )

    new = pd.DataFrame(rows)
    if not new.empty:
        new["signal_key"] = [
            _signal_key(r.signal_type, r.market_id, r.signal_ts, r.side) for r in new.itertuples()
        ]

    out_dir = _marts_dir(data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / SIGNAL_LOG_NAME

    if log_path.exists():
        existing = pd.read_parquet(log_path)
        combined = pd.concat([existing, new], ignore_index=True) if not new.empty else existing
    else:
        combined = new

    if combined is None or combined.empty:
        combined = pd.DataFrame(columns=SIGNAL_LOG_COLUMNS)
    else:
        combined = combined.drop_duplicates(subset=["signal_key"], keep="first")
    # Always persist (stable schema) so the dbt eval models can read it.
    combined.reindex(columns=SIGNAL_LOG_COLUMNS).to_parquet(log_path, index=False)
    return combined


def load_resolutions(path: str | Path = "seeds/resolutions.csv") -> pd.DataFrame:
    """Read known outcomes: columns market_id, outcome (1=YES happened, 0=NO)."""
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=["market_id", "outcome"])
    df = pd.read_csv(p)
    df = df[["market_id", "outcome"]].dropna()
    df["outcome"] = df["outcome"].astype(int)
    return df


@dataclass
class ScoreSummary:
    n_signals_logged: int
    n_resolved: int
    hit_rate: float | None
    n_tradeable_resolved: int
    pnl_net_total: float | None
    mean_edge_net: float | None
    calibration: list[dict] = field(default_factory=list)


def _pnl_net(side: str, p_yes: float, outcome: int, trade_cost: float) -> float:
    """Per-contract PnL (dollars) of taking `side` at YES-price p_yes, net of cost."""
    if side == "YES":
        payoff = (1.0 - p_yes) if outcome == 1 else (-p_yes)
    else:  # NO at price (1 - p_yes)
        p_no = 1.0 - p_yes
        payoff = (1.0 - p_no) if outcome == 0 else (-p_no)
    return payoff - trade_cost


def score_signals(
    *, data_root: str | None = None, resolutions_path: str | Path = "seeds/resolutions.csv"
) -> tuple[pd.DataFrame, ScoreSummary]:
    """Join the signal_log to resolutions and score it. Writes signal_scores.parquet."""
    out_dir = _marts_dir(data_root)
    log_path = out_dir / SIGNAL_LOG_NAME
    log = pd.read_parquet(log_path) if log_path.exists() else pd.DataFrame()

    resolutions = load_resolutions(resolutions_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_path = out_dir / SIGNAL_SCORES_NAME

    scored = (
        log.merge(resolutions, on="market_id", how="inner")
        if not log.empty and not resolutions.empty
        else pd.DataFrame()
    )
    if not scored.empty:
        scored["hit"] = (
            ((scored["side"] == "YES") & (scored["outcome"] == 1))
            | ((scored["side"] == "NO") & (scored["outcome"] == 0))
        ).astype(int)
        scored["pnl_net"] = [
            _pnl_net(r.side, r.platform_prob, int(r.outcome), float(r.trade_cost))
            for r in scored.itertuples()
        ]
        # calibration bucket on the model's predicted probability for the side
        scored["prob_bucket"] = (scored["predicted_prob_side"] * 5).round() / 5  # 0.0,0.2,...,1.0
        scored.reindex(columns=SIGNAL_SCORES_COLUMNS).to_parquet(scores_path, index=False)
    else:
        pd.DataFrame(columns=SIGNAL_SCORES_COLUMNS).to_parquet(scores_path, index=False)

    tradeable = scored[scored["tradeable"]] if not scored.empty else scored
    calib = []
    if not scored.empty:
        for bucket, grp in scored.groupby("prob_bucket"):
            calib.append(
                {
                    "prob_bucket": float(bucket),
                    "n": int(len(grp)),
                    "predicted_mean": round(float(grp["predicted_prob_side"].mean()), 4),
                    "realized_rate": round(float(grp["hit"].mean()), 4),
                }
            )

    summary = ScoreSummary(
        n_signals_logged=int(len(log)),
        n_resolved=int(len(scored)),
        hit_rate=round(float(scored["hit"].mean()), 4) if not scored.empty else None,
        n_tradeable_resolved=int(len(tradeable)),
        pnl_net_total=round(float(tradeable["pnl_net"].sum()), 4) if not tradeable.empty else None,
        mean_edge_net=round(float(scored["edge_net"].mean()), 4) if not scored.empty else None,
        calibration=calib,
    )
    return scored, summary
