"""Helpers for India VIX resolution."""

from __future__ import annotations

from pathlib import Path

from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.identification.config import IdentificationPolicy

__all__ = ["resolve_vix_symbol"]


def resolve_vix_symbol(
    policy: IdentificationPolicy,
    *,
    instrument_root: Path,
) -> str:
    """Return the configured VIX symbol after instrument-master verification.

    Fail closed when the configured symbol is absent from the master so we never
    invent a Fyers ticker.
    """
    store = InstrumentSpecStore(instrument_root)
    spec = store.find(policy.vix_symbol)
    if spec is None:
        raise ValueError(
            f"identification.vix_symbol {policy.vix_symbol!r} not found in "
            f"instrument master at {instrument_root}"
        )
    return spec.trading_symbol
