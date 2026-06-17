"""Dagster asset graph for evaluation.

Dagster models work as a graph of *assets* (data artifacts) with dependencies, so
you get a visual lineage and one-click re-materialization in the UI. Here the graph
is small and honest:

    signal_log  ->  scored_signals  ->  eval_dbt_models

`signal_log` snapshots the currently-flagged signals; `scored_signals` joins them
to known outcomes and scores hit rate / calibration / net PnL; `eval_dbt_models`
materializes the dbt presentation tables (`mart_signal_scores`, `mart_calibration`).

Launch the UI with `make dagster` (http://localhost:3000), or materialize headless
with `dagster asset materialize -m edgeradar.orchestration.definitions --select '*'`.
"""

from __future__ import annotations

# NOTE: deliberately no `from __future__ import annotations` here — Dagster
# introspects the `context` parameter annotation at runtime, and PEP-563 string
# annotations would break that resolution.
import subprocess

from dagster import Definitions, asset

from edgeradar.evaluation import log_signals, score_signals

# `context` is left unannotated below: Dagster introspects this parameter at
# runtime and only accepts its own context types or a blank annotation.


@asset(description="Snapshot currently-flagged signals into the append-only signal_log.")
def signal_log(context) -> int:
    log = log_signals()
    context.add_output_metadata({"n_signals_logged": len(log)})
    return len(log)


@asset(deps=[signal_log], description="Join the signal_log to outcomes and score it.")
def scored_signals(context) -> dict:
    _, summary = score_signals()
    meta = {
        "n_resolved": summary.n_resolved,
        "hit_rate": summary.hit_rate if summary.hit_rate is not None else float("nan"),
        "net_pnl_tradeable": summary.pnl_net_total
        if summary.pnl_net_total is not None
        else float("nan"),
    }
    context.add_output_metadata(meta)
    return meta


@asset(deps=[scored_signals], description="Build the dbt eval presentation tables.")
def eval_dbt_models(context) -> None:
    # Materialize only the eval-tagged dbt models (the parquet they read now exists).
    result = subprocess.run(
        ["dbt", "build", "--select", "tag:eval", "--project-dir", "dbt", "--profiles-dir", "dbt"],
        capture_output=True,
        text=True,
    )
    context.log.info(result.stdout[-2000:] if result.stdout else "(no dbt output)")
    if result.returncode != 0:
        raise RuntimeError(f"dbt eval build failed:\n{result.stderr[-2000:]}")


defs = Definitions(assets=[signal_log, scored_signals, eval_dbt_models])
