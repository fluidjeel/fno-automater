"""Regime-aware sizing for directional conviction trades.

Applies regime and conviction multipliers to the base risk-lots term while
preserving all hard caps (capital, margin, portfolio limits, liquidity).
The multiplier only adjusts the risk-lots term; it can never breach the
hard envelope computed by the existing min() formula.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading.broker.ports import (
    MarginPreviewLeg,
    MarginPreviewPort,
    MarginPreviewRequest,
)
from trading.config.risk_policy import RiskPolicyConfig
from trading.config.schema import RiskLimits
from trading.domain.contracts.common import ContractRef
from trading.domain.contracts.instrument import InstrumentSpec
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.contracts.sizing import SizingRequest
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import InstrumentKind, Side, SizingBindingConstraint
from trading.domain.primitives import Lots, LotSize, Money, Percent, Price, Rounding
from trading.risk.limits import (
    floor_divide_money,
    open_trade_slots,
    premium_budget_used,
    underlying_exposure_for,
)

__all__ = [
    "DirectionalConvictionSizingEngine",
    "DirectionalConvictionSizingResult",
    "RegimeAwareLotBounds",
]

_ZERO = Decimal(0)
_ONE = Decimal(1)

_CONSTRAINT_ORDER: tuple[SizingBindingConstraint, ...] = (
    SizingBindingConstraint.RISK,
    SizingBindingConstraint.CAPITAL,
    SizingBindingConstraint.MARGIN,
    SizingBindingConstraint.PORTFOLIO_LIMIT,
    SizingBindingConstraint.LIQUIDITY,
)


@dataclass(frozen=True, slots=True)
class RegimeAwareLotBounds:
    """Individual terms in the min() sizing formula with regime adjustment."""

    base_risk_lots: int
    regime_multiplier: Decimal
    conviction_multiplier: Decimal
    adjusted_risk_lots: int
    capital_lots: int
    margin_lots: int
    portfolio_limit_lots: int
    liquidity_lots: int

    @property
    def approved_lots(self) -> int:
        return min(
            self.adjusted_risk_lots,
            self.capital_lots,
            self.margin_lots,
            self.portfolio_limit_lots,
            self.liquidity_lots,
        )

    def binding_constraint(self, approved_lots: int) -> SizingBindingConstraint:
        mapping = {
            SizingBindingConstraint.RISK: self.adjusted_risk_lots,
            SizingBindingConstraint.CAPITAL: self.capital_lots,
            SizingBindingConstraint.MARGIN: self.margin_lots,
            SizingBindingConstraint.PORTFOLIO_LIMIT: self.portfolio_limit_lots,
            SizingBindingConstraint.LIQUIDITY: self.liquidity_lots,
        }
        for constraint in _CONSTRAINT_ORDER:
            if mapping[constraint] == approved_lots:
                return constraint
        return SizingBindingConstraint.RISK


@dataclass(frozen=True, slots=True)
class DirectionalConvictionSizingResult:
    """Sized outcome with regime-aware adjustment, before capital reservation."""

    bounds: RegimeAwareLotBounds
    approved_lots: int
    cost_per_lot: Money
    estimated_margin: Money
    recalculated_max_loss: Money
    entry_price: Price
    lot_size: LotSize
    premium_per_lot: Money
    margin_per_lot: Money


def _conviction_to_multiplier(conviction_score: Decimal) -> Decimal:
    """Map conviction score (0-100) to a sizing multiplier.

    65-75 (MODERATE): 0.7x — cautious entry
    75-85 (STRONG):   1.0x — full base size
    85+   (VERY_STRONG): 1.3x — enhanced position
    """
    if conviction_score < Decimal("65"):
        return _ZERO  # should not be called below entry threshold
    if conviction_score < Decimal("75"):
        return Decimal("0.7")
    if conviction_score < Decimal("85"):
        return _ONE
    return Decimal("1.3")


class DirectionalConvictionSizingEngine:
    """Size premium-paid options with regime and conviction adjustments.

    The regime multiplier and conviction multiplier adjust only the risk-lots
    term. All hard caps (capital, margin, portfolio, liquidity) are never
    overridden. The final lots = min(adjusted_risk_lots, capital, margin,
    portfolio, liquidity).
    """

    def size(
        self,
        request: SizingRequest,
        instrument: InstrumentSpec,
        policy: RiskPolicyConfig,
        margin_preview: MarginPreviewPort,
        *,
        account_id: str,
        account_risk: RiskLimits,
        preview_request_id: str,
        regime_multiplier: Decimal = _ONE,
        conviction_score: Decimal = Decimal("75"),
    ) -> DirectionalConvictionSizingResult:
        """Compute lots from risk x regime x conviction, capped by hard limits."""
        intent = request.intent
        if len(intent.legs) != 1:
            raise ValueError("directional conviction sizing supports exactly one leg")
        leg = intent.legs[0]
        if leg.side is not Side.BUY:
            raise ValueError("directional conviction sizing requires a BUY leg")
        if instrument.instrument_kind is not InstrumentKind.OPTION:
            raise ValueError(
                "directional conviction sizing requires an OPTION instrument"
            )

        feature = request.feature_snapshot
        entry_price = self._entry_price(feature)
        lot_size = LotSize(instrument.lot_size)
        currency = request.limits.max_loss_per_trade.currency
        one_lot = Lots(1).to_quantity(lot_size)
        premium_per_lot = entry_price.notional(one_lot, currency)
        slippage = Percent.from_fraction(policy.slippage_buffer_fraction).of(
            premium_per_lot
        )
        charges = policy.charges_per_lot.to_money()
        cost_per_lot = (premium_per_lot + slippage + charges).quantized(
            Rounding.CEILING
        )

        # Base risk lots (same as LongOptionSizingEngine)
        base_risk_lots = floor_divide_money(
            request.limits.max_loss_per_trade, cost_per_lot
        )

        # Apply regime and conviction multipliers to the risk term only
        conviction_mult = _conviction_to_multiplier(conviction_score)
        combined_multiplier = regime_multiplier * conviction_mult
        adjusted_risk_lots = max(0, int(Decimal(base_risk_lots) * combined_multiplier))

        # Hard caps — never overridden by multipliers
        capital_budget = min(
            request.limits.strategy_allocation_remaining,
            request.limits.margin_available,
            request.limits.daily_loss_remaining,
            key=lambda money: money.amount,
        )
        capital_lots = floor_divide_money(capital_budget, cost_per_lot)
        margin_lots, margin_per_lot = self._margin_lots(
            margin_preview,
            account_id=account_id,
            preview_request_id=preview_request_id,
            contract=leg.contract,
            lot_size=lot_size,
            margin_available=request.limits.margin_available,
        )
        portfolio_limit_lots = self._portfolio_limit_lots(
            request,
            policy,
            cost_per_lot,
            premium_per_lot,
            account_risk=account_risk,
            feature=feature,
            lot_size=lot_size,
        )
        liquidity_lots = self._liquidity_lots(feature, lot_size)

        bounds = RegimeAwareLotBounds(
            base_risk_lots=base_risk_lots,
            regime_multiplier=regime_multiplier,
            conviction_multiplier=conviction_mult,
            adjusted_risk_lots=adjusted_risk_lots,
            capital_lots=capital_lots,
            margin_lots=margin_lots,
            portfolio_limit_lots=portfolio_limit_lots,
            liquidity_lots=liquidity_lots,
        )
        approved_lots = bounds.approved_lots
        recalculated_max_loss = (cost_per_lot * approved_lots).quantized(
            Rounding.CEILING
        )
        if approved_lots > 0:
            estimated_margin = (margin_per_lot * approved_lots).quantized(
                Rounding.CEILING
            )
        else:
            estimated_margin = Money.zero(currency)

        return DirectionalConvictionSizingResult(
            bounds=bounds,
            approved_lots=approved_lots,
            cost_per_lot=cost_per_lot,
            estimated_margin=estimated_margin,
            recalculated_max_loss=recalculated_max_loss,
            entry_price=entry_price,
            lot_size=lot_size,
            premium_per_lot=premium_per_lot,
            margin_per_lot=margin_per_lot,
        )

    @staticmethod
    def _entry_price(feature: FeatureSnapshot) -> Price:
        ask = feature.market.ask
        if ask is None:
            raise ValueError(
                "ask price is required to size a directional conviction entry"
            )
        return ask

    @staticmethod
    def _margin_lots(
        margin_preview: MarginPreviewPort,
        *,
        account_id: str,
        preview_request_id: str,
        contract: ContractRef,
        lot_size: LotSize,
        margin_available: Money,
    ) -> tuple[int, Money]:
        quantity = Lots(1).to_quantity(lot_size)
        preview = margin_preview.preview_margin(
            MarginPreviewRequest(
                request_id=preview_request_id,
                account_id=account_id,
                legs=(
                    MarginPreviewLeg(
                        contract=contract,
                        side=Side.BUY,
                        quantity_contracts=quantity.contracts,
                    ),
                ),
            )
        )
        if not preview.confirmed or preview.margin_required.is_zero:
            return 0, Money.zero(margin_available.currency)
        margin_per_lot = preview.margin_required.quantized(Rounding.CEILING)
        lots = floor_divide_money(margin_available, margin_per_lot)
        return lots, margin_per_lot

    def _portfolio_limit_lots(
        self,
        request: SizingRequest,
        policy: RiskPolicyConfig,
        cost_per_lot: Money,
        premium_per_lot: Money,
        *,
        account_risk: RiskLimits,
        feature: FeatureSnapshot,
        lot_size: LotSize,
    ) -> int:
        portfolio = request.portfolio_snapshot
        equity = portfolio.exposure.equity
        slots = open_trade_slots(portfolio, account_risk)
        if slots <= 0:
            return 0

        premium_budget = (equity * policy.options_premium_budget_fraction).quantized(
            Rounding.FLOOR
        )
        premium_remaining = premium_budget - premium_budget_used(portfolio)
        premium_lots = floor_divide_money(premium_remaining, cost_per_lot)

        concentration_cap = (
            equity * policy.underlying_concentration_fraction
        ).quantized(Rounding.FLOOR)
        current = underlying_exposure_for(portfolio, request.intent.underlying)
        if current is not None:
            current_notional = current.gross_notional
        else:
            current_notional = Money.zero(equity.currency)
        concentration_remaining = concentration_cap - current_notional
        concentration_lots = floor_divide_money(
            concentration_remaining,
            premium_per_lot,
        )

        delta_lots = self._delta_limit_lots(
            policy,
            feature=feature,
            portfolio=portfolio,
            lot_size=lot_size,
        )
        return min(premium_lots, concentration_lots, delta_lots, slots)

    @staticmethod
    def _delta_limit_lots(
        policy: RiskPolicyConfig,
        *,
        feature: FeatureSnapshot,
        portfolio: PortfolioSnapshot,
        lot_size: LotSize,
    ) -> int:
        derivatives = feature.derivatives
        if derivatives is None or derivatives.greeks is None:
            return 10_000
        delta = derivatives.greeks.delta
        if delta is None or delta == 0:
            return 10_000
        current_delta = int(portfolio.exposure.net_delta)
        headroom = policy.net_delta_limit - abs(current_delta)
        delta_per_lot = abs(int(delta * Decimal(lot_size.contracts_per_lot)))
        if delta_per_lot <= 0:
            return 10_000
        return max(headroom // delta_per_lot, 0)

    @staticmethod
    def _liquidity_lots(feature: FeatureSnapshot, lot_size: LotSize) -> int:
        bid_size = feature.market.bid_size
        ask_size = feature.market.ask_size
        if bid_size is None or ask_size is None:
            return 10_000
        depth = min(bid_size, ask_size)
        return max(depth // lot_size.contracts_per_lot, 0)
