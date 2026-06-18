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
import json
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


RESOLUTIONS_AUTO_NAME = "resolutions_auto.csv"


def load_resolutions(
    path: str | Path = "seeds/resolutions.csv", *, data_root: str | None = None
) -> pd.DataFrame:
    """Known outcomes (market_id, outcome). Merges the manual seed file with the
    auto-resolved outcomes (data/marts/resolutions_auto.csv); auto wins on conflict."""
    frames = []
    seed = Path(path)
    if seed.exists():
        frames.append(pd.read_csv(seed)[["market_id", "outcome"]])
    auto = _marts_dir(data_root) / RESOLUTIONS_AUTO_NAME
    if auto.exists():
        frames.append(pd.read_csv(auto)[["market_id", "outcome"]])
    if not frames:
        return pd.DataFrame(columns=["market_id", "outcome"])
    df = pd.concat(frames, ignore_index=True).dropna()
    df["outcome"] = df["outcome"].astype(int)
    # auto file is appended last, so keep="last" lets it win over a stale seed row
    return df.drop_duplicates(subset=["market_id"], keep="last")


@dataclass
class BackfillSummary:
    n_markets: int
    accuracy: float | None  # fraction where the favored side (p>=0.5) was correct
    brier: float | None  # mean squared error of the closing price vs outcome
    calibration: list[dict] = field(default_factory=list)
    by_group: list[dict] = field(default_factory=list)  # per market-type breakdown


# Coarse market-type buckets, keyed off the Kalshi ticker prefix.
_GROUP_KEYWORDS: dict[str, tuple[str, ...]] = {
    "crypto": ("BTC", "ETH", "DOGE", "XRP", "SOL", "ADA", "LTC", "CRYPTO"),
    "sports": (
        "NBA",
        "MLB",
        "NFL",
        "NHL",
        "WC",
        "GAME",
        "SOCCER",
        "UFC",
        "TENNIS",
        "NCAA",
        "EPL",
        "NASCAR",
        "GOLF",
        "PGA",
    ),
    "weather": ("HIGH", "LOW", "TEMP", "WEATHER", "RAIN", "SNOW"),
}


def market_group(ticker: str) -> str:
    """Coarse market type from a Kalshi ticker (crypto / sports / weather / other)."""
    t = ticker.upper()
    for group, keywords in _GROUP_KEYWORDS.items():
        if any(k in t for k in keywords):
            return group
    return "other"


def _fetch_settled_kalshi(pages: int, limit: int = 1000) -> list[dict]:
    """Page through Kalshi's settled-markets feed (most recent first)."""
    import httpx

    settings = get_settings()
    url = f"{settings.kalshi_api_base}/markets"
    out: list[dict] = []
    cursor = None
    for _ in range(max(1, pages)):
        params = {"status": "settled", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        try:
            r = httpx.get(url, params=params, timeout=30.0)
            r.raise_for_status()
            j = r.json()
        except (httpx.HTTPError, ValueError):
            break
        out.extend(j.get("markets", []))
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def backfill_kalshi_calibration(
    *, pages: int = 5, dry_run: bool = False, data_root: str | None = None
) -> BackfillSummary:
    """Score ALREADY-settled Kalshi markets immediately (no waiting for new events).

    Each settled binary market exposes its closing price (last_price_dollars ~ the
    market's implied probability) and its actual result, so we can measure calibration
    right now over a whole day of resolved markets. Also appends the outcomes to
    resolutions_auto.csv so any of our own logged signals on those tickers get scored.
    """
    if dry_run:
        sample = Path("sample_responses/kalshi/settled.json")
        markets = json.loads(sample.read_text()).get("markets", [])
    else:
        markets = _fetch_settled_kalshi(pages)

    rows = []
    for m in markets:
        if m.get("market_type") != "binary":
            continue
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        try:
            p = float(m.get("last_price_dollars"))
            vol = float(m.get("volume_fp", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not (0.0 < p < 1.0) or vol <= 0:  # skip illiquid / degenerate combos
            continue
        rows.append(
            {
                "ticker": str(m["ticker"]),
                "title": m.get("title", ""),
                "group": market_group(str(m["ticker"])),
                "predicted": p,  # closing P(YES)
                "outcome": 1 if result == "yes" else 0,
                "close_time": m.get("close_time"),
            }
        )

    df = pd.DataFrame(rows)
    out_dir = _marts_dir(data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    cols = ["ticker", "title", "group", "predicted", "outcome", "close_time"]
    (df if not df.empty else pd.DataFrame(columns=cols)).to_parquet(
        out_dir / "market_calibration.parquet", index=False
    )

    if df.empty:
        return BackfillSummary(0, None, None, [])

    # Also feed these real outcomes into the auto-resolution store.
    auto_path = out_dir / RESOLUTIONS_AUTO_NAME
    res = df.rename(columns={"ticker": "market_id"})[["market_id", "outcome"]].copy()
    res["source"] = "kalshi"
    if auto_path.exists():
        res = pd.concat([pd.read_csv(auto_path), res], ignore_index=True)
    res.drop_duplicates(subset=["market_id"], keep="last").to_csv(auto_path, index=False)

    accuracy = float((((df["predicted"] >= 0.5).astype(int)) == df["outcome"]).mean())
    brier = float(((df["predicted"] - df["outcome"]) ** 2).mean())
    df["bucket"] = (df["predicted"] * 10).round() / 10
    calib = [
        {
            "prob_bucket": float(b),
            "n": int(len(g)),
            "predicted_mean": round(float(g["predicted"].mean()), 4),
            "realized_rate": round(float(g["outcome"].mean()), 4),
        }
        for b, g in df.groupby("bucket")
    ]

    # Per market-type breakdown, incl. a "longshot overpricing" measure:
    # mean(predicted - outcome) among cheap (<0.25) contracts. Positive => longshots
    # were overpriced (favorite-longshot bias) in that group.
    by_group = []
    for grp, gd in df.groupby("group"):
        acc = float((((gd["predicted"] >= 0.5).astype(int)) == gd["outcome"]).mean())
        br = float(((gd["predicted"] - gd["outcome"]) ** 2).mean())
        cheap = gd[gd["predicted"] < 0.25]
        longshot = (
            round(float((cheap["predicted"] - cheap["outcome"]).mean()), 4)
            if len(cheap) >= 10
            else None
        )
        by_group.append(
            {
                "group": grp,
                "n": int(len(gd)),
                "accuracy": round(acc, 4),
                "brier": round(br, 4),
                "longshot_overpricing": longshot,
            }
        )
    by_group.sort(key=lambda d: d["n"], reverse=True)
    return BackfillSummary(len(df), round(accuracy, 4), round(brier, 4), calib, by_group)


def _resolve_one(source: str, market_id: str) -> tuple[int | None, str]:
    """Fetch the settled outcome for one market.

    Returns (outcome, detail) where outcome is 1=YES, 0=NO, or None if not resolved.
    `detail` is a short human-readable reason (for diagnostics).
    """
    import httpx

    settings = get_settings()
    try:
        if source == "kalshi":
            r = httpx.get(f"{settings.kalshi_api_base}/markets/{market_id}", timeout=20.0)
            if r.status_code != 200:
                return None, f"http {r.status_code}"
            mkt = r.json().get("market") or {}
            result, status = mkt.get("result", ""), mkt.get("status", "")
            if result == "yes":
                return 1, "settled yes"
            if result == "no":
                return 0, "settled no"
            return None, f"not settled (status={status!r}, result={result!r})"
        elif source == "manifold":
            r = httpx.get(f"{settings.manifold_api_base}/market/{market_id}", timeout=20.0)
            if r.status_code != 200:
                return None, f"http {r.status_code}"
            m = r.json()
            if m.get("isResolved") and m.get("resolution") in ("YES", "NO"):
                return (1 if m["resolution"] == "YES" else 0), f"resolved {m['resolution']}"
            return None, f"not resolved (resolution={m.get('resolution')!r})"
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return None, f"error: {type(exc).__name__}"
    return None, "unsupported source"


def auto_resolve(
    *, data_root: str | None = None, dry_run: bool = False, verbose: bool = False
) -> tuple[int, int]:
    """Auto-fetch settled outcomes for logged signals from Kalshi + Manifold.

    Appends newly-resolved (market_id, outcome) to data/marts/resolutions_auto.csv so
    the evaluation loop needs no manual input. Returns (checked, newly_resolved).
    Fail-soft: network errors for a market just leave it unresolved (retried next run).
    """
    out_dir = _marts_dir(data_root)
    log_path = out_dir / SIGNAL_LOG_NAME
    if dry_run or not log_path.exists():
        return 0, 0

    log = pd.read_parquet(log_path)
    if log.empty or "source" not in log.columns:
        return 0, 0

    auto_path = out_dir / RESOLUTIONS_AUTO_NAME
    already = set()
    if auto_path.exists():
        already = set(pd.read_csv(auto_path)["market_id"].astype(str))

    # Only sources whose outcomes we can fetch directly.
    todo = (
        log[log["source"].isin(["kalshi", "manifold"])][["source", "market_id"]]
        .drop_duplicates()
        .to_dict("records")
    )
    checked, new_rows = 0, []
    for row in todo:
        mid = str(row["market_id"])
        if mid in already:
            continue
        checked += 1
        outcome, detail = _resolve_one(row["source"], mid)
        if verbose:
            print(f"    {row['source']}:{mid} -> {detail}")
        if outcome is not None:
            new_rows.append({"market_id": mid, "outcome": outcome, "source": row["source"]})

    if new_rows:
        out_dir.mkdir(parents=True, exist_ok=True)
        new_df = pd.DataFrame(new_rows)
        combined = (
            pd.concat([pd.read_csv(auto_path), new_df], ignore_index=True)
            if auto_path.exists()
            else new_df
        )
        combined.drop_duplicates(subset=["market_id"], keep="last").to_csv(auto_path, index=False)
    return checked, len(new_rows)


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

    resolutions = load_resolutions(resolutions_path, data_root=data_root)
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
