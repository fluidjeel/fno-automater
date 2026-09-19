"""Deterministic market-state, contract-binding and structure routing."""

from trading.identification.binders import (
    BoundCandidates,
    bind_debit_spread,
    bind_long_option,
)
from trading.identification.config import (
    IdentificationPolicy,
    load_identification_policy,
)
from trading.identification.macro import publish_macro_assessment
from trading.identification.market_state import build_market_state
from trading.identification.vix import resolve_vix_symbol
from trading.identification.router import RoutedOpportunity, route_nifty_options

__all__ = [
    "BoundCandidates",
    "IdentificationPolicy",
    "RoutedOpportunity",
    "bind_debit_spread",
    "bind_long_option",
    "build_market_state",
    "resolve_vix_symbol",
    "load_identification_policy",
    "publish_macro_assessment",
    "route_nifty_options",
]
