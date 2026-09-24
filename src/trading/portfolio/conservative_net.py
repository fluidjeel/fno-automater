"""Conservative net P&L selection for Layer 2 loss enforcement."""

from __future__ import annotations

from trading.domain.primitives import Money, Rounding

__all__ = [
    "confirmed_net",
    "conservative_realized_net",
    "estimated_net",
]


def confirmed_net(gross: Money, confirmed_charges: Money) -> Money:
    """Broker-confirmed net after durable per-fill charges."""
    return (gross - confirmed_charges).quantized(Rounding.HALF_EVEN)


def estimated_net(gross: Money, estimated_charges: Money) -> Money:
    """Model-derived net; not broker-confirmed."""
    return (gross - estimated_charges).quantized(Rounding.HALF_EVEN)


def conservative_realized_net(
    gross: Money,
    *,
    confirmed_charges: Money,
    estimated_charges: Money,
) -> Money:
    """Lower net for enforcement so understated fees cannot mask a breach.

    Daily-loss, campaign-loss and mode-risk limits use this value.
    """
    return min(
        confirmed_net(gross, confirmed_charges),
        estimated_net(gross, estimated_charges),
    )
