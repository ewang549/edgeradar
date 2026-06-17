"""Discord alerter (read-only).

Reads the divergence + weather signal marts from the warehouse, keeps those above
an edge threshold, and posts a short digest to a Discord webhook. It NEVER places
orders — it notifies a human, who decides. The read-only guardrail is asserted at
runtime.

Usage:
    edgeradar alert            # post to the configured webhook
    edgeradar alert --dry-run  # print the message instead of sending
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from edgeradar.config import get_settings

DISCORD_MAX_CHARS = 1900  # keep under Discord's 2000-char content limit


@dataclass
class Alertable:
    kind: str  # 'divergence' | 'weather'
    label: str  # human description
    edge_net: float
    detail: str


def _query(con: duckdb.DuckDBPyConnection, sql: str):
    try:
        return con.sql(sql).df()
    except Exception:
        return None


def load_alertable(con: duckdb.DuckDBPyConnection, min_edge: float) -> list[Alertable]:
    """Collect above-threshold signals from both signal marts."""
    out: list[Alertable] = []

    div = _query(
        con,
        f"""
        select canonical_title, source, implied_prob, consensus, edge_net, side_hint
        from mart_divergence
        where is_signal and edge_net >= {min_edge}
        order by edge_net desc
        """,
    )
    if div is not None:
        for _, r in div.iterrows():
            out.append(
                Alertable(
                    kind="divergence",
                    label=str(r["canonical_title"])[:70],
                    edge_net=float(r["edge_net"]),
                    detail=(
                        f"{r['source']} {float(r['implied_prob']):.2f} vs consensus "
                        f"{float(r['consensus']):.2f} ({r['side_hint']})"
                    ),
                )
            )

    wx = _query(
        con,
        f"""
        select title, forecast_prob, kalshi_prob, edge_net
        from mart_weather_edge
        where is_signal and edge_net >= {min_edge}
        order by edge_net desc
        """,
    )
    if wx is not None:
        for _, r in wx.iterrows():
            out.append(
                Alertable(
                    kind="weather",
                    label=str(r["title"])[:70],
                    edge_net=float(r["edge_net"]),
                    detail=(
                        f"forecast {float(r['forecast_prob']):.2f} vs kalshi "
                        f"{float(r['kalshi_prob']):.2f}"
                    ),
                )
            )

    out.sort(key=lambda a: a.edge_net, reverse=True)
    return out


def format_message(signals: list[Alertable], min_edge: float) -> str:
    """Render the Discord message body (read-only digest)."""
    if not signals:
        return f"EdgeRadar: no signals above edge {min_edge:.2f}."
    header = f"**EdgeRadar — {len(signals)} signal(s) above edge {min_edge:.2f}**"
    lines = [f"{header} (review only, no trades)"]
    for s in signals:
        lines.append(f"• [{s.kind}] {s.label} — edge_net {s.edge_net:+.3f} ({s.detail})")
    msg = "\n".join(lines)
    return msg[:DISCORD_MAX_CHARS]


def post_discord(webhook_url: str, content: str) -> int:
    """POST the message to a Discord webhook; returns the HTTP status code."""
    import httpx

    resp = httpx.post(webhook_url, json={"content": content}, timeout=15.0)
    return resp.status_code


def run_alert(*, min_edge: float | None = None, dry_run: bool = False) -> tuple[int, str]:
    """Build and (optionally) send the alert. Returns (n_signals, message)."""
    settings = get_settings()
    # Hard guardrail: this path must never be able to trade.
    if settings.enable_order_execution:
        raise RuntimeError(
            "Refusing to run: order execution must stay disabled (read-only system)."
        )

    edge = settings.alert_min_edge if min_edge is None else min_edge
    con = duckdb.connect(settings.duckdb_path, read_only=True)
    try:
        signals = load_alertable(con, edge)
    finally:
        con.close()
    message = format_message(signals, edge)

    if dry_run or not settings.discord_webhook_url:
        print(message if dry_run else "(no DISCORD_WEBHOOK_URL set — printing instead)\n" + message)
    else:
        status = post_discord(settings.discord_webhook_url, message)
        print(f"posted {len(signals)} signal(s) to Discord (HTTP {status}).")
    return len(signals), message
