"""EdgeRadar — read-only cross-platform prediction-market mispricing engine.

This package ingests PUBLIC market data, normalizes it to implied probabilities,
resolves which markets refer to the same real-world event, and surfaces
divergences net of fees/spread for a human to review.

It does NOT place orders or execute trades. See ARCHITECTURE.md ("Why read-only").
"""

__version__ = "0.0.1"
