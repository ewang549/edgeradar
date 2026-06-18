"""Thin command-line entrypoint.

Phase 0 only exposes `version` and `config-check` so you can confirm settings
load correctly. `ingest` is registered but intentionally inert until Phase 1
wires up concrete adapters.
"""

from __future__ import annotations

import argparse
import sys

from edgeradar import __version__
from edgeradar.config import get_settings


def _cmd_version(_: argparse.Namespace) -> int:
    print(f"edgeradar {__version__}")
    return 0


def _cmd_config_check(_: argparse.Namespace) -> int:
    s = get_settings()
    print("Loaded settings (secrets masked):")
    print(f"  minio_endpoint      = {s.minio_endpoint}")
    print(f"  minio_bucket        = {s.minio_bucket}")
    print(f"  duckdb_path         = {s.duckdb_path}")
    print(f"  data_root           = {s.data_root}")
    print(f"  kalshi_api_base     = {s.kalshi_api_base}")
    print(f"  manifold_api_base   = {s.manifold_api_base}")
    print(f"  odds_api_key set?   = {bool(s.odds_api_key)}")
    print(f"  discord webhook set?= {bool(s.discord_webhook_url)}")
    print(f"  order execution     = {s.enable_order_execution}  (must be False)")
    if s.enable_order_execution:
        print(
            "REFUSING: order execution is disabled by design. See ARCHITECTURE.md.", file=sys.stderr
        )
        return 2
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from edgeradar.ingest import run_ingest

    mode = "dry-run (offline, sample data)" if args.dry_run else "live"
    print(f"[ingest] source={args.source!r} mode={mode}")
    try:
        results = run_ingest(args.source, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for r in results:
        print(
            f"  {r.source:<10} raw={r.n_raw:<4} quotes={r.n_quotes:<4} "
            f"-> {r.clean_path or '(no quotes)'}"
        )
    return 0


def _cmd_produce(args: argparse.Namespace) -> int:
    from edgeradar.streaming.producer import produce_source

    mode = "dry-run (offline, sample data)" if args.dry_run else "live"
    print(f"[produce] source={args.source!r} mode={mode}")
    try:
        n = produce_source(args.source, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"  produced {n} message(s) to the raw topic.")
    return 0


def _cmd_consume(args: argparse.Namespace) -> int:
    from edgeradar.streaming.consumer import consume_and_land

    print(f"[consume] draining topic (idle timeout {args.idle_timeout}s)...")
    result = consume_and_land(idle_timeout=args.idle_timeout)
    print(f"  consumed {result.messages} message(s) -> {result.quotes} quote(s)")
    for f in result.files:
        print(f"    wrote {f}")
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    from edgeradar.entity_resolution import resolve

    print(f"[resolve] entity resolution (threshold={args.threshold})")
    res = resolve(threshold=args.threshold)
    if res.event_map.empty:
        print("  no markets found — run `make ingest`/`make consume` first.", file=sys.stderr)
        return 1
    print(
        f"  {len(res.event_map)} markets -> {res.n_events} events "
        f"({res.n_cross_platform} cross-platform; {res.overrides_applied} override(s) applied)"
    )
    print(f"  wrote {res.event_map_path}")

    if not res.candidate_pairs.empty:
        print("\n  Proposed/again-reviewable matches (highest confidence first):")
        cols = ["category", "confidence", "decision", "title_a", "title_b"]
        for _, p in res.candidate_pairs[cols].iterrows():
            print(
                f"    [{p['decision']:<12} {p['confidence']:.2f} {p['category']:<8}] "
                f"{p['title_a'][:38]!r} <> {p['title_b'][:38]!r}"
            )
    print("\n  Cross-platform events:")
    sizes = res.event_map.groupby(["event_id", "canonical_title"])["source"].agg(
        ["nunique", "count"]
    )
    for (eid, title), row in sizes[sizes["nunique"] > 1].iterrows():
        print(f"    {eid}  ({row['count']} markets / {row['nunique']} platforms)  {title[:50]!r}")
    return 0


def _cmd_calibrate_sigma(_: argparse.Namespace) -> int:
    from edgeradar.weather import calibrate_weather_sigma

    sigma, n, written = calibrate_weather_sigma()
    if written:
        print(f"[calibrate-sigma] fitted sigma={sigma} from {n} resolved market(s); saved.")
    else:
        print(
            f"[calibrate-sigma] kept prior sigma={sigma} "
            f"({n} resolved market(s) — need more before refitting)."
        )
    return 0


def _cmd_weather(args: argparse.Namespace) -> int:
    from edgeradar.weather import build_weather_edge

    mode = "dry-run (sample forecast)" if args.dry_run else "live (api.weather.gov)"
    print(f"[weather] building weather edge — {mode}")
    df = build_weather_edge(dry_run=args.dry_run)
    if df.empty:
        print("  no Kalshi temperature markets matched (need data + a recognizable temp market).")
        return 0
    print(f"  {len(df)} temperature market(s) scored:")
    for _, r in df.sort_values("edge_net", ascending=False).iterrows():
        flag = "SIGNAL" if r["is_signal"] else "      "
        print(
            f"    [{flag}] forecast {r['forecast_prob']:.2f} vs kalshi {r['kalshi_prob']:.2f} "
            f"edge_net={r['edge_net']:+.3f}  {str(r['title'])[:46]!r}"
        )
    return 0


def _cmd_log_signals(_: argparse.Namespace) -> int:
    from edgeradar.evaluation import log_signals

    log = log_signals()
    print(f"[log-signals] signal_log now holds {len(log)} signal(s).")
    return 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    from edgeradar.evaluation import backfill_kalshi_calibration

    print("[backfill] scoring already-settled Kalshi markets (instant calibration)...")
    s = backfill_kalshi_calibration(pages=args.pages, dry_run=args.dry_run)
    print(f"  settled markets scored : {s.n_markets}")
    if s.n_markets == 0:
        print("  (no settled markets returned — try more --pages, or check connectivity.)")
        return 0
    print(f"  accuracy (favorite)    : {s.accuracy:.1%}")
    print(f"  Brier score (lower=better): {s.brier}")
    print("  calibration (closing price -> realized):")
    for b in s.calibration:
        print(
            f"    bucket {b['prob_bucket']:.1f}: n={b['n']:<4} "
            f"predicted={b['predicted_mean']} realized={b['realized_rate']}"
        )
    return 0


def _cmd_auto_resolve(args: argparse.Namespace) -> int:
    from edgeradar.evaluation import auto_resolve

    checked, newly = auto_resolve(verbose=getattr(args, "verbose", False))
    print(
        f"[auto-resolve] checked {checked} unresolved market(s); resolved {newly} new outcome(s)."
    )
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from edgeradar.evaluation import auto_resolve, log_signals, score_signals

    log_signals()
    # Autonomously fetch settled outcomes from Kalshi + Manifold (unless --no-resolve).
    if not getattr(args, "no_resolve", False):
        checked, newly = auto_resolve()
        if checked:
            print(f"[evaluate] auto-resolved {newly} newly-settled market(s) of {checked} checked.")
    _, s = score_signals()
    print("[evaluate] signal scoring (honest backtest)")
    print(f"  signals logged       : {s.n_signals_logged}")
    print(f"  resolved & scored    : {s.n_resolved}")
    if s.n_resolved == 0:
        print("  (no resolved outcomes yet — events haven't settled. They'll auto-resolve later.)")
        return 0
    print(f"  hit rate (all)       : {s.hit_rate}")
    print(f"  tradeable resolved   : {s.n_tradeable_resolved}")
    print(f"  net PnL (tradeable)  : {s.pnl_net_total}  (per 1 contract, $ units)")
    print(f"  mean net edge        : {s.mean_edge_net}")
    if s.calibration:
        print("  calibration (predicted -> realized):")
        for b in s.calibration:
            print(
                f"    bucket {b['prob_bucket']:.1f}: n={b['n']} "
                f"predicted={b['predicted_mean']} realized={b['realized_rate']}"
            )
    print("\n  NOTE: a small sample proves nothing. Trust this only across many resolved events.")
    return 0


def _cmd_alert(args: argparse.Namespace) -> int:
    from edgeradar.alerter import run_alert

    n, _ = run_alert(min_edge=args.min_edge, dry_run=args.dry_run)
    print(f"[alert] {n} signal(s) above threshold.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="edgeradar", description="EdgeRadar CLI (read-only).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="Print version.").set_defaults(func=_cmd_version)
    sub.add_parser("config-check", help="Load and print settings.").set_defaults(
        func=_cmd_config_check
    )

    p_ingest = sub.add_parser("ingest", help="Batch: fetch + land Parquet directly (Phase 1).")
    p_ingest.add_argument("--source", default="all", help="Source slug or 'all'.")
    p_ingest.add_argument("--dry-run", action="store_true", help="Use saved sample responses.")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_produce = sub.add_parser("produce", help="Stream: publish raw quotes to the topic (Phase 3).")
    p_produce.add_argument("--source", default="all", help="Source slug or 'all'.")
    p_produce.add_argument("--dry-run", action="store_true", help="Use saved sample responses.")
    p_produce.set_defaults(func=_cmd_produce)

    p_consume = sub.add_parser(
        "consume", help="Stream: normalize topic -> clean Parquet (Phase 3)."
    )
    p_consume.add_argument(
        "--idle-timeout", type=float, default=5.0, help="Stop after this many idle seconds."
    )
    p_consume.set_defaults(func=_cmd_consume)

    p_resolve = sub.add_parser(
        "resolve", help="Entity resolution: group same-event markets (Phase 4)."
    )
    p_resolve.add_argument(
        "--threshold", type=float, default=0.60, help="Fuzzy match confidence cutoff."
    )
    p_resolve.set_defaults(func=_cmd_resolve)

    p_weather = sub.add_parser(
        "weather", help="Weather edge: NWS forecast vs Kalshi temp markets (Phase 5)."
    )
    p_weather.add_argument("--dry-run", action="store_true", help="Use saved sample forecast.")
    p_weather.set_defaults(func=_cmd_weather)

    sub.add_parser(
        "calibrate-sigma", help="Fit the weather forecast sigma from resolved outcomes (Phase 5+)."
    ).set_defaults(func=_cmd_calibrate_sigma)

    sub.add_parser(
        "log-signals", help="Append currently-flagged signals to the signal_log (Phase 6)."
    ).set_defaults(func=_cmd_log_signals)

    p_eval = sub.add_parser(
        "evaluate",
        help="Auto-resolve outcomes + log signals + score them (hit rate, calibration, PnL).",
    )
    p_eval.add_argument(
        "--no-resolve", action="store_true", help="Skip the auto-resolution network step."
    )
    p_eval.set_defaults(func=_cmd_evaluate)

    p_bf = sub.add_parser(
        "backfill",
        help="Score already-settled Kalshi markets now for instant calibration (Phase 6).",
    )
    p_bf.add_argument("--pages", type=int, default=5, help="Pages of settled markets to pull.")
    p_bf.add_argument("--dry-run", action="store_true", help="Use saved sample settled markets.")
    p_bf.set_defaults(func=_cmd_backfill)

    p_ar = sub.add_parser(
        "auto-resolve",
        help="Fetch settled outcomes from Kalshi + Manifold for logged signals (Phase 6).",
    )
    p_ar.add_argument(
        "--verbose", action="store_true", help="Print why each market did/didn't resolve."
    )
    p_ar.set_defaults(func=_cmd_auto_resolve)

    p_alert = sub.add_parser(
        "alert", help="Post above-threshold signals to Discord (read-only) (Phase 7)."
    )
    p_alert.add_argument(
        "--min-edge", type=float, default=None, help="Override alert edge threshold."
    )
    p_alert.add_argument(
        "--dry-run", action="store_true", help="Print the message instead of sending."
    )
    p_alert.set_defaults(func=_cmd_alert)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
