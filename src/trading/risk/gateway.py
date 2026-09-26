"""TradeIntent to RiskDecision orchestration. Invariant 4."""

from __future__ import annotations

import functools
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum, unique
from typing import TypedDict

from trading.broker.ports import MarginPreviewPort
from trading.config.discovery import DiscoveryConfig
from trading.config.loader import LoadedConfig
from trading.config.risk_policy import LoadedRiskPolicy, RiskPolicyConfig
from trading.config.schema import RiskLimits
from trading.domain.clock import Clock
from trading.domain.contracts.exposure import ExposureReport
from trading.domain.contracts.instrument import InstrumentSpec
from trading.domain.contracts.intent import IntentLeg, TradeIntent
from trading.domain.contracts.mode_policy import ModesConfig, load_modes_config
from trading.domain.contracts.paper_data import PaperDataRequirements
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.contracts.risk import ApprovedLeg, LegQuoteRef, RiskDecision
from trading.domain.contracts.sizing import SizingRequest
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import (
    DataQuality,
    EntryProfile,
    InstrumentKind,
    ModeId,
    ReasonCode,
    ReservationState,
    RiskAction,
    Side,
    Trigger,
)
from trading.domain.ids import IdFactory
from trading.domain.primitives import Lots, LotSize, Money, Percent, Rounding
from trading.news.contracts import EventRiskState, EventRiskStatus, NewsQuality
from trading.portfolio.campaign_drawdown import CampaignLedger
from trading.research.registry import is_experimental_off_strict_book
from trading.risk.discovery_sizing import (
    apply_discovery_lots,
    collect_strict_would_block,
    discovery_guide_budget,
    evaluate_discovery_hard_limits,
    rescale_approved_legs,
)
from trading.risk.gate_profile import is_soft, partition_reasons
from trading.risk.limits import (
    build_sizing_limits,
    evaluate_campaign_limit,
    evaluate_exposure_limits,
    evaluate_pre_trade_limits,
    project_post_trade_exposure,
)
from trading.risk.mode_ledger import FourModeBook
from trading.risk.reservation import CapitalReservationService
from trading.risk.sizing.butterfly import (
    ButterflyLegs,
    LongButterflySizingEngine,
    is_long_butterfly,
)
from trading.risk.sizing.commodity_future import (
    CommodityFutureSizingEngine,
    is_commodity_future,
)
from trading.risk.sizing.credit_spread import (
    CreditSpreadSizingEngine,
    credit_spread_legs,
    is_credit_spread,
)
from trading.risk.sizing.debit_spread import (
    DebitSpreadSizingEngine,
    is_debit_spread,
    spread_legs,
)
from trading.risk.sizing.iron_butterfly import (
    IronButterflyLegs,
    IronButterflySizingEngine,
    is_iron_butterfly,
)
from trading.risk.sizing.iron_condor import (
    IronCondorLegs,
    IronCondorSizingEngine,
    is_iron_condor,
)
from trading.risk.sizing.long_option import LongOptionSizingEngine, LotBounds
from trading.risk.sizing.long_volatility import (
    LongStraddleSizingEngine,
    LongStrangleSizingEngine,
    is_long_straddle,
    is_long_strangle,
)
from trading.risk.snapshot_bundle import validate_leg_snapshot_bundle
from trading.safety.paper_data import (
    PaperDataInputs,
    assess_paper_data,
    strict_would_block_p0,
)

__all__ = ["RiskGateway", "RiskGatewayRequest"]


class _SizingError(ValueError):
    """Quotes or geometry could not be sized."""


@unique
class _StructureKind(StrEnum):
    LONG_OPTION = "LONG_OPTION"
    DEBIT_SPREAD = "DEBIT_SPREAD"
    CREDIT_SPREAD = "CREDIT_SPREAD"
    IRON_CONDOR = "IRON_CONDOR"
    IRON_BUTTERFLY = "IRON_BUTTERFLY"
    LONG_BUTTERFLY = "LONG_BUTTERFLY"
    LONG_STRADDLE = "LONG_STRADDLE"
    LONG_STRANGLE = "LONG_STRANGLE"
    COMMODITY_FUTURE = "COMMODITY_FUTURE"


@dataclass(frozen=True, slots=True)
class _SizingOutcome:
    approved_lots: int
    bounds: LotBounds
    cost_per_lot: Money
    recalculated_max_loss: Money
    estimated_margin: Money
    approved_legs: tuple[ApprovedLeg, ...]
    net_delta_delta: int


@dataclass(frozen=True, slots=True)
class RiskGatewayRequest:
    """Inputs required to evaluate one intent."""

    intent: TradeIntent
    feature_snapshot: FeatureSnapshot
    portfolio_snapshot: PortfolioSnapshot
    instrument: InstrumentSpec
    leg_snapshots: Mapping[str, FeatureSnapshot] = field(default_factory=dict)
    # None means the caller supplied no reviewed event state. A strategy that
    # requires a clear blackout must then fail closed.
    event_risk_state: EventRiskState | None = None
    paper_requirements: PaperDataRequirements | None = None
    broker_state_ok: bool = True
    campaign_id: str | None = None
    # ADESK-A4: when supplied, PART 6 hard caps run after pre-trade limits.
    exposure_report: ExposureReport | None = None


class _SnapshotAudit(TypedDict):
    decision_snapshot_id: str
    decision_timestamp: datetime
    leg_quotes: tuple[LegQuoteRef, ...]


@functools.lru_cache(maxsize=1)
def _get_cached_modes_config() -> ModesConfig | None:
    try:
        return load_modes_config()
    except Exception:
        return None


class RiskGateway:
    """Deterministic pre-trade gate: size, limit-check, reserve, decide."""

    def __init__(
        self,
        *,
        account_config: LoadedConfig,
        risk_policy: LoadedRiskPolicy,
        reservation_service: CapitalReservationService,
        margin_preview: MarginPreviewPort,
        clock: Clock,
        id_factory: IdFactory,
        nifty_only_execution: bool = True,
        modes_config: ModesConfig | None = None,
        mode_book: FourModeBook | None = None,
    ) -> None:
        self._account_config = account_config
        self._risk_policy = risk_policy
        self._reservations = reservation_service
        self._margin_preview = margin_preview
        self._clock = clock
        self._ids = id_factory
        self._nifty_only_execution = nifty_only_execution
        self._modes_config = modes_config or _get_cached_modes_config()
        if mode_book is not None:
            self._mode_book: FourModeBook | None = mode_book
        elif self._modes_config is not None:
            self._mode_book = FourModeBook(modes_config=self._modes_config)
        else:
            self._mode_book = None
        self._mode_fill_recorded: set[str] = set()
        self._mode_close_recorded: set[str] = set()
        self._campaign_ledger: CampaignLedger | None = None
        self._long_option_sizer = LongOptionSizingEngine()
        self._debit_spread_sizer = DebitSpreadSizingEngine()
        self._credit_spread_sizer = CreditSpreadSizingEngine()
        self._iron_condor_sizer = IronCondorSizingEngine()
        self._iron_butterfly_sizer = IronButterflySizingEngine()
        self._long_butterfly_sizer = LongButterflySizingEngine()
        self._long_straddle_sizer = LongStraddleSizingEngine()
        self._long_strangle_sizer = LongStrangleSizingEngine()
        self._commodity_future_sizer = CommodityFutureSizingEngine()

    @property
    def mode_book(self) -> FourModeBook | None:
        """Four-mode capital book, if configured."""
        return self._mode_book

    def set_campaign_ledger(self, campaign_ledger: CampaignLedger | None) -> None:
        """Attach the durable campaign ledger used for roll loss caps."""
        self._campaign_ledger = campaign_ledger

    def note_mode_fill(
        self,
        mode_id: ModeId,
        *,
        margin: Money,
        premium: Money | None = None,
        trade_id: str | None = None,
    ) -> None:
        """Move a mode reservation into filled open risk after entry complete.

        Idempotent per ``trade_id`` so duplicate fill events never double-count.
        """
        if self._mode_book is None:
            return
        if trade_id is not None and trade_id in self._mode_fill_recorded:
            return
        zero = Money.zero(margin.currency)
        self._mode_book.record_fill(
            mode_id, margin=margin, premium=premium if premium is not None else zero
        )
        if trade_id is not None:
            self._mode_fill_recorded.add(trade_id)
            self._mode_close_recorded.discard(trade_id)

    def note_mode_close(
        self,
        mode_id: ModeId,
        amount: Money,
        *,
        trade_id: str | None = None,
    ) -> None:
        """Release mode open risk after a position fully closes."""
        if self._mode_book is None:
            return
        if trade_id is not None and trade_id in self._mode_close_recorded:
            return
        self._mode_book.release_open_risk(mode_id, amount)
        if trade_id is not None:
            self._mode_close_recorded.add(trade_id)
            self._mode_fill_recorded.discard(trade_id)

    def note_mode_release(
        self, mode_id: ModeId, amount: Money, *, trade_id: str | None = None
    ) -> None:
        """Release a pending mode reservation after reject/abort/failed prefix."""
        if self._mode_book is None:
            return
        if trade_id is not None and trade_id in self._mode_fill_recorded:
            # Already moved into margin; abort path should use note_mode_close.
            self.note_mode_close(mode_id, amount, trade_id=trade_id)
            return
        self._mode_book.release_reservation(mode_id, amount)

    def evaluate(self, request: RiskGatewayRequest) -> RiskDecision:
        """Return an approval with reserved capital or a machine-readable rejection."""
        now = self._clock.now_utc()
        intent = request.intent
        portfolio = request.portfolio_snapshot
        feature = request.feature_snapshot
        policy = self._risk_policy.config
        account_risk = self._account_config.config.risk
        pre_trade = portfolio.exposure
        audit = _primary_audit(intent, feature, request.leg_snapshots, now)

        if not intent.is_live_at(now):
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.DECISION_EXPIRED,),
                decided_at=now,
                audit=audit,
            )
        if intent.snapshot_id != feature.snapshot_id:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.SNAPSHOT_MISMATCH,),
                decided_at=now,
                audit=audit,
            )
        if not feature.permits_new_exposure:
            code = _quality_reason(feature.quality.state)
            return self._reject(
                intent,
                portfolio,
                reason_codes=(code,),
                decided_at=now,
                audit=audit,
            )

        discovery_config = (
            self._mode_book.discovery_config if self._mode_book is not None else None
        )
        entry_profile = (
            EntryProfile.DISCOVERY
            if discovery_config is not None
            else EntryProfile.STRICT
        )
        early_strict_would_block: list[ReasonCode] = []
        constraint_reason = _constraint_reason(request, now=now)
        if constraint_reason is not None:
            if is_soft(constraint_reason, entry_profile, discovery_config):
                early_strict_would_block.append(constraint_reason)
            else:
                return self._reject(
                    intent,
                    portfolio,
                    reason_codes=(constraint_reason,),
                    decided_at=now,
                    audit=audit,
                )

        if self._nifty_only_execution and (
            request.instrument.underlying != "NIFTY"
            or request.instrument.instrument_kind is not InstrumentKind.OPTION
        ):
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.NON_NIFTY_EXECUTION_REJECTED,),
                decided_at=now,
                audit=audit,
            )

        structure = _detect_structure(request)
        if structure is None:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.INSTRUMENT_UNKNOWN,),
                decided_at=now,
                audit=audit,
            )

        if is_experimental_off_strict_book(intent.family_id):
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.CALENDAR_EXPERIMENTAL_OFF_STRICT_BOOK,),
                decided_at=now,
                audit=audit,
            )

        if intent.mode_id is not None:
            if intent.family_id is None:
                return self._reject(
                    intent,
                    portfolio,
                    reason_codes=(ReasonCode.MODE_FAMILY_NOT_PERMITTED,),
                    decided_at=now,
                    audit=audit,
                )
            modes_cfg = self._modes_config or _get_cached_modes_config()
            if modes_cfg is not None and intent.mode_id in modes_cfg.modes:
                allowed_families = {
                    f.value if hasattr(f, "value") else str(f)
                    for f in modes_cfg.modes[intent.mode_id].allowed_families
                }
                if intent.family_id not in allowed_families:
                    return self._reject(
                        intent,
                        portfolio,
                        reason_codes=(ReasonCode.MODE_FAMILY_NOT_PERMITTED,),
                        decided_at=now,
                        audit=audit,
                    )
            if intent.mode_id == ModeId.M3_TACTICAL_POSITIONAL and (
                structure is _StructureKind.LONG_OPTION
                or len(intent.legs) == 1
                or intent.family_id in {"long_call", "long_put"}
            ):
                return self._reject(
                    intent,
                    portfolio,
                    reason_codes=(ReasonCode.MODE_FAMILY_NOT_PERMITTED,),
                    decided_at=now,
                    audit=audit,
                )
        if any(
            leg.side is Side.SELL for leg in intent.legs
        ) and not _short_is_admissible(structure, policy):
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.RISK_LIMIT_TRADE,),
                applied_limits=("naked_short_disabled",),
                decided_at=now,
                audit=audit,
            )
        if _is_multi_leg(structure):
            spread_reason = _multi_leg_spread_reason(
                request.leg_snapshots,
                intent.entry_policy.max_spread,
            )
        else:
            spread_reason = _spread_reason(feature, intent.entry_policy.max_spread)
        if spread_reason is not None:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(spread_reason,),
                decided_at=now,
                audit=audit,
            )
        instrument_reason = _instrument_reason(request.instrument, structure)
        if instrument_reason is not None:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(instrument_reason,),
                decided_at=now,
                audit=audit,
            )
        if _is_multi_leg(structure):
            bundle = validate_leg_snapshot_bundle(
                intent,
                request.leg_snapshots,
                now=now,
                freshness=self._account_config.config.freshness,
            )
            audit = _SnapshotAudit(
                decision_snapshot_id=bundle.decision_snapshot_id,
                decision_timestamp=bundle.decision_timestamp,
                leg_quotes=bundle.leg_quotes,
            )
            if bundle.reason is not None:
                return self._reject(
                    intent,
                    portfolio,
                    reason_codes=(bundle.reason,),
                    decided_at=now,
                    audit=audit,
                )

        if intent.mode_id is None and not policy.has_allocation(intent.strategy_id):
            # Fail closed: build_sizing_limits raises for an unlisted strategy,
            # and a configuration gap must surface as a machine-readable
            # rejection rather than an exception out of the decision path.
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.CAPITAL_UNAVAILABLE,),
                applied_limits=("no_strategy_allocation",),
                decided_at=now,
                audit=audit,
            )

        mode_ledger = None
        if intent.mode_id is not None and self._mode_book is not None:
            mode_ledger = self._mode_book.get_ledger(intent.mode_id)

        limits = build_sizing_limits(
            portfolio,
            account_risk,
            policy,
            intent.strategy_id,
            config_version=self._account_config.version,
            mode_id=intent.mode_id,
            modes_config=self._modes_config,
            mode_ledger=mode_ledger,
            discovery_config=discovery_config,
        )
        sizing_request = SizingRequest(
            request_id=self._ids.new_id("SIZE-REQ"),
            intent=intent,
            feature_snapshot=feature,
            portfolio_snapshot=portfolio,
            limits=limits,
            requested_at=now,
        )
        try:
            sizing = _compute_sizing_outcome(
                structure,
                feature,
                sizing_request,
                request,
                policy=policy,
                account_risk=account_risk,
                long_option_sizer=self._long_option_sizer,
                debit_spread_sizer=self._debit_spread_sizer,
                credit_spread_sizer=self._credit_spread_sizer,
                iron_condor_sizer=self._iron_condor_sizer,
                iron_butterfly_sizer=self._iron_butterfly_sizer,
                long_butterfly_sizer=self._long_butterfly_sizer,
                long_straddle_sizer=self._long_straddle_sizer,
                long_strangle_sizer=self._long_strangle_sizer,
                commodity_future_sizer=self._commodity_future_sizer,
                margin_preview=self._margin_preview,
                account_id=self._account_config.config.account_id,
                preview_request_id=self._ids.new_id("MARGIN-PREV"),
            )
        except _SizingError:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.PRICE_UNAVAILABLE,),
                decided_at=now,
                audit=audit,
            )
        discovery_active = (
            discovery_config is not None
            and intent.mode_id is not None
            and mode_ledger is not None
        )
        if sizing.approved_lots <= 0 and not discovery_active:
            reason = _zero_lot_reason(sizing.bounds, mode_id=intent.mode_id)
            if intent.mode_id is not None and (
                sizing.bounds.risk_lots <= 0
                or sizing.recalculated_max_loss > limits.max_loss_per_trade
            ):
                reason = ReasonCode.MIN_LOT_EXCEEDS_BUDGET
            return self._reject(
                intent,
                portfolio,
                reason_codes=(reason,),
                decided_at=now,
                audit=audit,
            )
        if sizing.cost_per_lot.is_zero or sizing.cost_per_lot.is_negative:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.SIZE_BELOW_MINIMUM,),
                decided_at=now,
                audit=audit,
            )

        paper_block, paper_soft = _paper_p0_reason(
            request,
            now=now,
            margin=sizing.estimated_margin,
            entry_profile=entry_profile,
            discovery_config=discovery_config,
        )
        early_strict_would_block.extend(paper_soft)
        if paper_block:
            return self._reject(
                intent,
                portfolio,
                reason_codes=paper_block,
                decided_at=now,
                audit=audit,
            )

        strict_would_block: tuple[ReasonCode, ...] = ()
        approval_reasons: list[ReasonCode] = []
        if (
            discovery_config is not None
            and intent.mode_id is not None
            and mode_ledger is not None
        ):
            mode_id = intent.mode_id
            guide = discovery_guide_budget(mode_ledger, discovery_config, mode_id)
            discovery_sized = apply_discovery_lots(
                cost_per_lot=sizing.cost_per_lot,
                guide=guide,
            )
            if discovery_sized.approved_lots <= 0:
                return self._reject(
                    intent,
                    portfolio,
                    reason_codes=(ReasonCode.SIZE_BELOW_MINIMUM,),
                    decided_at=now,
                    audit=audit,
                )
            old_lots = sizing.approved_lots if sizing.approved_lots > 0 else 1
            margin_per_lot = sizing.estimated_margin / old_lots
            sizing = _SizingOutcome(
                approved_lots=discovery_sized.approved_lots,
                bounds=sizing.bounds,
                cost_per_lot=sizing.cost_per_lot,
                recalculated_max_loss=discovery_sized.recalculated_max_loss,
                estimated_margin=(
                    margin_per_lot * discovery_sized.approved_lots
                ).quantized(Rounding.CEILING),
                approved_legs=rescale_approved_legs(
                    sizing.approved_legs,
                    discovery_sized.approved_lots,
                )
                if sizing.approved_legs
                else sizing.approved_legs,
                net_delta_delta=sizing.net_delta_delta,
            )
            limit_check = evaluate_discovery_hard_limits(
                cost_per_lot=sizing.cost_per_lot,
                recalculated_max_loss=sizing.recalculated_max_loss,
                approved_lots=sizing.approved_lots,
                mode_ledger=mode_ledger,
                discovery=discovery_config,
                mode_id=mode_id,
            )
            if not limit_check.passed:
                return self._reject(
                    intent,
                    portfolio,
                    reason_codes=limit_check.reason_codes,
                    applied_limits=limit_check.applied_limits,
                    decided_at=now,
                    audit=audit,
                )
            strict_would_block = collect_strict_would_block(
                intent,
                portfolio,
                account_risk,
                policy,
                limits,
                recalculated_max_loss=sizing.recalculated_max_loss,
                approved_lots=sizing.approved_lots,
                mode_ledger=mode_ledger,
                modes_config=self._modes_config,
                mode_book=self._mode_book,
                exposure_report=request.exposure_report,
                campaign_id=request.campaign_id,
                campaign_ledger=self._campaign_ledger,
            )
            approval_reasons = [ReasonCode.OK]
            if discovery_sized.one_lot_over_guide:
                approval_reasons.append(ReasonCode.ONE_LOT_OVER_GUIDE)
        else:
            limit_check = evaluate_pre_trade_limits(
                intent,
                portfolio,
                account_risk,
                policy,
                limits,
                recalculated_max_loss=sizing.recalculated_max_loss,
                approved_lots=sizing.approved_lots,
                mode_ledger=mode_ledger,
                modes_config=self._modes_config,
                mode_book=self._mode_book,
            )
            if not limit_check.passed:
                return self._reject(
                    intent,
                    portfolio,
                    reason_codes=limit_check.reason_codes,
                    applied_limits=limit_check.applied_limits,
                    decided_at=now,
                    audit=audit,
                )

            campaign_check = evaluate_campaign_limit(
                request.campaign_id,
                self._campaign_ledger,
                recalculated_max_loss=sizing.recalculated_max_loss,
            )
            if not campaign_check.passed:
                return self._reject(
                    intent,
                    portfolio,
                    reason_codes=campaign_check.reason_codes,
                    applied_limits=campaign_check.applied_limits,
                    decided_at=now,
                    audit=audit,
                )

            if request.exposure_report is not None:
                exposure_check = evaluate_exposure_limits(
                    request.exposure_report, policy
                )
                if not exposure_check.passed:
                    return self._reject(
                        intent,
                        portfolio,
                        reason_codes=exposure_check.reason_codes,
                        applied_limits=exposure_check.applied_limits,
                        decided_at=now,
                        audit=audit,
                    )
            approval_reasons = list(limit_check.reason_codes)

        decision_id = self._ids.new_id("DEC")
        reservation = self._reservations.try_reserve(
            intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            amount=sizing.recalculated_max_loss,
            margin_available=limits.margin_available,
            risk_decision_id=decision_id,
            mode_id=intent.mode_id,
            idempotency_key=intent.intent_id,
        )
        if reservation.state is not ReservationState.RESERVED:
            reason = (
                reservation.reason_codes[0]
                if reservation.reason_codes
                else ReasonCode.CAPITAL_UNAVAILABLE
            )
            return self._reject(
                intent,
                portfolio,
                reason_codes=(reason,),
                decided_at=now,
                audit=audit,
            )
        if self._mode_book is not None and intent.mode_id is not None:
            global_cap = (
                None
                if discovery_active
                else (
                    policy.max_global_open_risk.to_money()
                    if policy.max_global_open_risk is not None
                    else None
                )
            )
            if not self._mode_book.try_reserve(
                intent.mode_id,
                sizing.recalculated_max_loss,
                global_cap=global_cap,
            ):
                self._reservations.release(
                    reservation.reservation_id,
                    trigger=Trigger.LOCAL_COMMAND,
                )
                return self._reject(
                    intent,
                    portfolio,
                    reason_codes=(ReasonCode.CAPITAL_UNAVAILABLE,),
                    applied_limits=("mode_available_capital", "max_global_open_risk"),
                    decided_at=now,
                    audit=audit,
                )

        post_trade = project_post_trade_exposure(
            portfolio,
            margin_required=sizing.estimated_margin,
            premium_paid=sizing.recalculated_max_loss,
            net_delta_delta=sizing.net_delta_delta,
        )
        action = (
            RiskAction.RESIZE
            if sizing.recalculated_max_loss < intent.requested_risk
            else RiskAction.APPROVE
        )
        expires_at = now + timedelta(seconds=policy.decision_ttl_seconds)
        return RiskDecision(
            decision_id=decision_id,
            intent_id=intent.intent_id,
            correlation_id=intent.correlation_id,
            experiment_id=intent.experiment_id,
            execution_mode=intent.execution_mode,
            policy_version=policy.policy_version,
            config_version=self._account_config.version,
            action=action,
            approved_legs=sizing.approved_legs,
            capital_reservation_id=reservation.reservation_id,
            reserved_capital=reservation.amount,
            recalculated_max_loss=sizing.recalculated_max_loss,
            margin_required=sizing.estimated_margin,
            pre_trade_exposure=pre_trade,
            post_trade_projection=post_trade,
            applied_limits=limit_check.applied_limits,
            reason_codes=tuple(approval_reasons),
            strict_would_block=tuple(
                dict.fromkeys((*early_strict_would_block, *strict_would_block))
            ),
            decided_at=now,
            expires_at=expires_at,
            decision_snapshot_id=audit["decision_snapshot_id"],
            decision_timestamp=audit["decision_timestamp"],
            leg_quotes=audit["leg_quotes"],
        )

    def _reject(
        self,
        intent: TradeIntent,
        portfolio: PortfolioSnapshot,
        *,
        reason_codes: tuple[ReasonCode, ...],
        decided_at: datetime,
        applied_limits: tuple[str, ...] = (),
        audit: _SnapshotAudit | None = None,
    ) -> RiskDecision:
        policy = self._risk_policy.config
        expires_at = decided_at + timedelta(seconds=policy.decision_ttl_seconds)
        extra_snapshot_id = audit["decision_snapshot_id"] if audit is not None else None
        extra_timestamp = audit["decision_timestamp"] if audit is not None else None
        extra_quotes = audit["leg_quotes"] if audit is not None else ()
        return RiskDecision(
            decision_id=self._ids.new_id("DEC"),
            intent_id=intent.intent_id,
            correlation_id=intent.correlation_id,
            experiment_id=intent.experiment_id,
            execution_mode=intent.execution_mode,
            policy_version=policy.policy_version,
            config_version=self._account_config.version,
            action=RiskAction.REJECT,
            pre_trade_exposure=portfolio.exposure,
            applied_limits=applied_limits,
            reason_codes=reason_codes,
            decided_at=decided_at,
            expires_at=expires_at,
            decision_snapshot_id=extra_snapshot_id,
            decision_timestamp=extra_timestamp,
            leg_quotes=extra_quotes,
        )


def _leg_template_lots(approved_lots: int) -> int:
    """Build leg templates for discovery when strict sizing returned zero lots."""
    return approved_lots if approved_lots > 0 else 1


def _detect_structure(request: RiskGatewayRequest) -> _StructureKind | None:
    intent = request.intent
    if is_iron_condor(intent):
        return _StructureKind.IRON_CONDOR
    if is_iron_butterfly(intent):
        return _StructureKind.IRON_BUTTERFLY
    if is_long_butterfly(intent):
        return _StructureKind.LONG_BUTTERFLY
    if is_long_straddle(intent):
        return _StructureKind.LONG_STRADDLE
    if is_long_strangle(intent):
        return _StructureKind.LONG_STRANGLE
    if is_debit_spread(intent):
        return _StructureKind.DEBIT_SPREAD
    if is_credit_spread(intent):
        return _StructureKind.CREDIT_SPREAD
    if is_commodity_future(intent, request.instrument):
        return _StructureKind.COMMODITY_FUTURE
    if (
        len(intent.legs) == 1
        and intent.legs[0].side is Side.BUY
        and request.instrument.instrument_kind is InstrumentKind.OPTION
    ):
        return _StructureKind.LONG_OPTION
    return None


def _paper_p0_reason(
    request: RiskGatewayRequest,
    *,
    now: datetime,
    margin: Money,
    entry_profile: EntryProfile,
    discovery_config: DiscoveryConfig | None,
) -> tuple[tuple[ReasonCode, ...], tuple[ReasonCode, ...]]:
    """Return hard P0 blockers and DISCOVERY strict_would_block shadows."""
    requirements = request.paper_requirements
    if requirements is None:
        return (), ()
    if request.leg_snapshots:
        snapshots = tuple(request.leg_snapshots.values())
    else:
        snapshots = (request.feature_snapshot,)
    assessment = assess_paper_data(
        requirements,
        PaperDataInputs(
            now=now,
            snapshots=snapshots,
            event_risk=request.event_risk_state,
            portfolio=request.portfolio_snapshot,
            broker_state_ok=request.broker_state_ok,
            margin_confirmed=not margin.is_zero,
            margin_required=margin,
            instruments=_paper_instruments(request),
        ),
        entry_profile=entry_profile,
        discovery_config=discovery_config,
    )
    p0_soft = strict_would_block_p0(
        assessment,
        entry_profile=entry_profile,
        discovery_config=discovery_config,
    )
    if assessment.p0_ok:
        return (), p0_soft
    hard, soft = partition_reasons(
        assessment.p0_reason_codes or (ReasonCode.DATA_GAP,),
        entry_profile,
        discovery_config,
    )
    return hard, (*p0_soft, *soft)


def _paper_instruments(request: RiskGatewayRequest) -> dict[str, InstrumentSpec]:
    mapped = {
        request.instrument.trading_symbol: request.instrument,
        request.feature_snapshot.contract.symbol: request.instrument,
    }
    for snapshot in request.leg_snapshots.values():
        mapped[snapshot.contract.symbol] = request.instrument
    return mapped


def _constraint_reason(
    request: RiskGatewayRequest,
    *,
    now: datetime,
) -> ReasonCode | None:
    """Revalidate strategy entry constraints against decision-time evidence."""
    constraints = request.intent.constraints
    if constraints.require_event_blackout_clear:
        event_risk = request.event_risk_state
        if (
            event_risk is None
            or event_risk.scope not in {request.intent.underlying, "GLOBAL"}
            or now < event_risk.as_of
            or now >= event_risk.expires_at
            or event_risk.quality_state is not NewsQuality.VALID
            or event_risk.state not in {EventRiskStatus.NORMAL, EventRiskStatus.CAUTION}
        ):
            return ReasonCode.EVENT_BLACKOUT

    if request.leg_snapshots:
        snapshots = tuple(request.leg_snapshots.values())
    else:
        snapshots = (request.feature_snapshot,)
    for snapshot in snapshots:
        derivatives = snapshot.derivatives
        if derivatives is None:
            return ReasonCode.INSTRUMENT_UNKNOWN
        if derivatives.days_to_expiry < constraints.min_days_to_expiry:
            return ReasonCode.CONTRACT_EXPIRED
        minimum_oi = constraints.min_open_interest
        if minimum_oi is not None and (
            derivatives.open_interest is None or derivatives.open_interest < minimum_oi
        ):
            return ReasonCode.DEPTH_INSUFFICIENT
    return None


def _is_defined_risk(structure: _StructureKind) -> bool:
    return structure in {
        _StructureKind.DEBIT_SPREAD,
        _StructureKind.CREDIT_SPREAD,
        _StructureKind.IRON_CONDOR,
        _StructureKind.IRON_BUTTERFLY,
        _StructureKind.LONG_BUTTERFLY,
        _StructureKind.LONG_STRADDLE,
        _StructureKind.LONG_STRANGLE,
    }


def _short_is_admissible(structure: _StructureKind, policy: RiskPolicyConfig) -> bool:
    """Whether an intent carrying a SELL leg may be admitted.

    Two ways a short is allowed, and nothing else:

      - its worst case is capped by construction (spreads, iron condor); or
      - it is a single-leg commodity future whose loss is bounded by the same
        mandatory protective stop that sized it, so the position cannot lose
        more than the risk already approved for it. This is a policy switch,
        off by default, and it admits that one structure only.

    Naked short options are never admissible on either path.
    """
    if _is_defined_risk(structure):
        return True
    return (
        structure is _StructureKind.COMMODITY_FUTURE
        and policy.allow_stop_bounded_futures_short
    )


def _is_multi_leg(structure: _StructureKind) -> bool:
    return structure in {
        _StructureKind.DEBIT_SPREAD,
        _StructureKind.CREDIT_SPREAD,
        _StructureKind.IRON_CONDOR,
        _StructureKind.IRON_BUTTERFLY,
        _StructureKind.LONG_BUTTERFLY,
        _StructureKind.LONG_STRADDLE,
        _StructureKind.LONG_STRANGLE,
    }


def _instrument_reason(
    instrument: InstrumentSpec,
    structure: _StructureKind,
) -> ReasonCode | None:
    if structure is _StructureKind.COMMODITY_FUTURE:
        if instrument.instrument_kind is not InstrumentKind.FUTURE:
            return ReasonCode.INSTRUMENT_UNKNOWN
        return None
    if instrument.instrument_kind is not InstrumentKind.OPTION:
        return ReasonCode.INSTRUMENT_UNKNOWN
    return None


def _compute_sizing_outcome(
    structure: _StructureKind,
    feature: FeatureSnapshot,
    sizing_request: SizingRequest,
    request: RiskGatewayRequest,
    *,
    policy: RiskPolicyConfig,
    account_risk: RiskLimits,
    long_option_sizer: LongOptionSizingEngine,
    debit_spread_sizer: DebitSpreadSizingEngine,
    credit_spread_sizer: CreditSpreadSizingEngine,
    iron_condor_sizer: IronCondorSizingEngine,
    iron_butterfly_sizer: IronButterflySizingEngine,
    long_butterfly_sizer: LongButterflySizingEngine,
    long_straddle_sizer: LongStraddleSizingEngine,
    long_strangle_sizer: LongStrangleSizingEngine,
    commodity_future_sizer: CommodityFutureSizingEngine,
    margin_preview: MarginPreviewPort,
    account_id: str,
    preview_request_id: str,
) -> _SizingOutcome:
    lot_size = LotSize(request.instrument.lot_size)
    if structure is _StructureKind.DEBIT_SPREAD:
        try:
            spread_sizing = debit_spread_sizer.size(
                sizing_request,
                request.leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = spread_sizing.approved_lots
        approved_legs = _approved_spread_legs(
            request.intent,
            _leg_template_lots(approved_lots),
            spread_sizing.lot_size,
        )
        net_delta_delta = (
            _spread_net_delta_delta(
                request.leg_snapshots,
                approved_lots,
                spread_sizing.lot_size,
            )
            if approved_lots > 0
            else 0
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=spread_sizing.bounds,
            cost_per_lot=spread_sizing.cost_per_lot,
            recalculated_max_loss=spread_sizing.recalculated_max_loss,
            estimated_margin=spread_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=net_delta_delta,
        )

    if structure is _StructureKind.CREDIT_SPREAD:
        try:
            credit_sizing = credit_spread_sizer.size(
                sizing_request,
                request.leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = credit_sizing.approved_lots
        approved_legs = _approved_credit_spread_legs(
            request.intent,
            _leg_template_lots(approved_lots),
            credit_sizing.lot_size,
        )
        net_delta_delta = (
            _spread_net_delta_delta(
                request.leg_snapshots,
                approved_lots,
                credit_sizing.lot_size,
            )
            if approved_lots > 0
            else 0
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=credit_sizing.bounds,
            cost_per_lot=credit_sizing.cost_per_lot,
            recalculated_max_loss=credit_sizing.recalculated_max_loss,
            estimated_margin=credit_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=net_delta_delta,
        )

    if structure is _StructureKind.IRON_CONDOR:
        try:
            condor_sizing = iron_condor_sizer.size(
                sizing_request,
                request.leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = condor_sizing.approved_lots
        approved_legs = _approved_condor_legs(
            condor_sizing.legs,
            _leg_template_lots(approved_lots),
            condor_sizing.lot_size,
        )
        net_delta_delta = (
            _spread_net_delta_delta(
                request.leg_snapshots,
                approved_lots,
                condor_sizing.lot_size,
            )
            if approved_lots > 0
            else 0
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=condor_sizing.bounds,
            cost_per_lot=condor_sizing.cost_per_lot,
            recalculated_max_loss=condor_sizing.recalculated_max_loss,
            estimated_margin=condor_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=net_delta_delta,
        )

    if structure is _StructureKind.IRON_BUTTERFLY:
        try:
            butterfly_sizing = iron_butterfly_sizer.size(
                sizing_request,
                request.leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = butterfly_sizing.approved_lots
        approved_legs = _approved_iron_butterfly_legs(
            butterfly_sizing.legs,
            _leg_template_lots(approved_lots),
            butterfly_sizing.lot_size,
        )
        net_delta_delta = (
            _spread_net_delta_delta(
                request.leg_snapshots,
                approved_lots,
                butterfly_sizing.lot_size,
            )
            if approved_lots > 0
            else 0
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=butterfly_sizing.bounds,
            cost_per_lot=butterfly_sizing.cost_per_lot,
            recalculated_max_loss=butterfly_sizing.recalculated_max_loss,
            estimated_margin=butterfly_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=net_delta_delta,
        )

    if structure is _StructureKind.LONG_BUTTERFLY:
        try:
            fly_sizing = long_butterfly_sizer.size(
                sizing_request,
                request.leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = fly_sizing.approved_lots
        approved_legs = _approved_butterfly_legs(
            fly_sizing.legs,
            _leg_template_lots(approved_lots),
            fly_sizing.lot_size,
        )
        net_delta_delta = (
            _spread_net_delta_delta(
                request.leg_snapshots,
                approved_lots,
                fly_sizing.lot_size,
            )
            if approved_lots > 0
            else 0
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=fly_sizing.bounds,
            cost_per_lot=fly_sizing.cost_per_lot,
            recalculated_max_loss=fly_sizing.recalculated_max_loss,
            estimated_margin=fly_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=net_delta_delta,
        )

    if structure is _StructureKind.LONG_STRADDLE:
        try:
            straddle_sizing = long_straddle_sizer.size(
                sizing_request,
                request.leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = straddle_sizing.approved_lots
        approved_legs = _approved_volatility_legs(
            straddle_sizing.call_leg,
            straddle_sizing.put_leg,
            _leg_template_lots(approved_lots),
            straddle_sizing.lot_size,
        )
        net_delta_delta = (
            _spread_net_delta_delta(
                request.leg_snapshots,
                approved_lots,
                straddle_sizing.lot_size,
            )
            if approved_lots > 0
            else 0
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=straddle_sizing.bounds,
            cost_per_lot=straddle_sizing.cost_per_lot,
            recalculated_max_loss=straddle_sizing.recalculated_max_loss,
            estimated_margin=straddle_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=net_delta_delta,
        )

    if structure is _StructureKind.LONG_STRANGLE:
        try:
            strangle_sizing = long_strangle_sizer.size(
                sizing_request,
                request.leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = strangle_sizing.approved_lots
        approved_legs = _approved_volatility_legs(
            strangle_sizing.call_leg,
            strangle_sizing.put_leg,
            _leg_template_lots(approved_lots),
            strangle_sizing.lot_size,
        )
        net_delta_delta = (
            _spread_net_delta_delta(
                request.leg_snapshots,
                approved_lots,
                strangle_sizing.lot_size,
            )
            if approved_lots > 0
            else 0
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=strangle_sizing.bounds,
            cost_per_lot=strangle_sizing.cost_per_lot,
            recalculated_max_loss=strangle_sizing.recalculated_max_loss,
            estimated_margin=strangle_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=net_delta_delta,
        )

    if structure is _StructureKind.COMMODITY_FUTURE:
        try:
            future_sizing = commodity_future_sizer.size(
                sizing_request,
                request.instrument,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = future_sizing.approved_lots
        leg = request.intent.legs[0]
        template_lots = _leg_template_lots(approved_lots)
        approved_legs = (
            ApprovedLeg(
                leg_id=leg.leg_id,
                lots=Lots(template_lots),
                lot_size=future_sizing.lot_size,
            ),
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=future_sizing.bounds,
            cost_per_lot=future_sizing.cost_per_lot,
            recalculated_max_loss=future_sizing.recalculated_max_loss,
            estimated_margin=future_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=(
                _net_delta_delta(feature, approved_lots, future_sizing.lot_size)
                if approved_lots > 0
                else 0
            ),
        )

    option_sizing = long_option_sizer.size(
        sizing_request,
        request.instrument,
        policy,
        margin_preview,
        account_id=account_id,
        account_risk=account_risk,
        preview_request_id=preview_request_id,
    )
    approved_lots = option_sizing.approved_lots
    leg = request.intent.legs[0]
    template_lots = _leg_template_lots(approved_lots)
    approved_legs = (
        ApprovedLeg(
            leg_id=leg.leg_id,
            lots=Lots(template_lots),
            lot_size=option_sizing.lot_size,
        ),
    )
    return _SizingOutcome(
        approved_lots=approved_lots,
        bounds=option_sizing.bounds,
        cost_per_lot=option_sizing.cost_per_lot,
        recalculated_max_loss=option_sizing.recalculated_max_loss,
        estimated_margin=option_sizing.estimated_margin,
        approved_legs=approved_legs,
        net_delta_delta=(
            _net_delta_delta(feature, approved_lots, option_sizing.lot_size)
            if approved_lots > 0
            else 0
        ),
    )


def _approved_spread_legs(
    intent: TradeIntent,
    approved_lots: int,
    lot_size: LotSize,
) -> tuple[ApprovedLeg, ...]:
    if approved_lots <= 0:
        return ()
    long_leg, short_leg = spread_legs(intent)
    return (
        ApprovedLeg(
            leg_id=long_leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        ),
        ApprovedLeg(
            leg_id=short_leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        ),
    )


def _approved_credit_spread_legs(
    intent: TradeIntent,
    approved_lots: int,
    lot_size: LotSize,
) -> tuple[ApprovedLeg, ...]:
    if approved_lots <= 0:
        return ()
    short_leg, long_leg = credit_spread_legs(intent)
    return (
        ApprovedLeg(
            leg_id=long_leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        ),
        ApprovedLeg(
            leg_id=short_leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        ),
    )


def _approved_iron_butterfly_legs(
    legs: object,
    approved_lots: int,
    lot_size: LotSize,
) -> tuple[ApprovedLeg, ...]:
    if approved_lots <= 0:
        return ()
    if not isinstance(legs, IronButterflyLegs):
        raise TypeError("legs must be IronButterflyLegs")
    return tuple(
        ApprovedLeg(
            leg_id=leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        )
        for leg in (
            legs.long_put,
            legs.long_call,
            legs.short_put,
            legs.short_call,
        )
    )


def _approved_butterfly_legs(
    legs: object,
    approved_lots: int,
    lot_size: LotSize,
) -> tuple[ApprovedLeg, ...]:
    if approved_lots <= 0:
        return ()
    if not isinstance(legs, ButterflyLegs):
        raise TypeError("legs must be ButterflyLegs")
    return tuple(
        ApprovedLeg(
            leg_id=leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        )
        for leg in (
            legs.low_wing,
            legs.high_wing,
            legs.short_body,
        )
    )


def _approved_volatility_legs(
    call_leg: object,
    put_leg: object,
    approved_lots: int,
    lot_size: LotSize,
) -> tuple[ApprovedLeg, ...]:
    if approved_lots <= 0:
        return ()
    if not isinstance(call_leg, IntentLeg) or not isinstance(put_leg, IntentLeg):
        raise TypeError("legs must be IntentLeg")
    return (
        ApprovedLeg(
            leg_id=put_leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        ),
        ApprovedLeg(
            leg_id=call_leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        ),
    )


def _approved_condor_legs(
    legs: object,
    approved_lots: int,
    lot_size: LotSize,
) -> tuple[ApprovedLeg, ...]:
    if approved_lots <= 0:
        return ()
    if not isinstance(legs, IronCondorLegs):
        raise TypeError("legs must be IronCondorLegs")
    return tuple(
        ApprovedLeg(
            leg_id=leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        )
        for leg in (
            legs.long_put,
            legs.short_put,
            legs.long_call,
            legs.short_call,
        )
    )


def _quality_reason(state: DataQuality) -> ReasonCode:
    if state is DataQuality.STALE:
        return ReasonCode.DATA_STALE
    if state is DataQuality.INVALID:
        return ReasonCode.DATA_INVALID
    return ReasonCode.DATA_DEGRADED


def _spread_reason(feature: FeatureSnapshot, max_spread: Percent) -> ReasonCode | None:
    bid = feature.market.bid
    ask = feature.market.ask
    if bid is None or ask is None:
        return ReasonCode.PRICE_UNAVAILABLE
    mid = (bid.value + ask.value) / 2
    if mid <= 0:
        return ReasonCode.PRICE_UNAVAILABLE
    spread_fraction = (ask.value - bid.value) / mid
    if spread_fraction > max_spread.fraction:
        return ReasonCode.SPREAD_TOO_WIDE
    return None


def _zero_lot_reason(bounds: LotBounds, *, mode_id: ModeId | None = None) -> ReasonCode:
    if bounds.risk_lots <= 0 and mode_id is not None:
        return ReasonCode.MIN_LOT_EXCEEDS_BUDGET
    if bounds.margin_lots <= 0:
        return ReasonCode.MARGIN_INSUFFICIENT
    if bounds.capital_lots <= 0:
        return ReasonCode.CAPITAL_UNAVAILABLE
    if bounds.risk_lots <= 0:
        return ReasonCode.RISK_LIMIT_TRADE
    if bounds.portfolio_limit_lots <= 0:
        return ReasonCode.RISK_LIMIT_PORTFOLIO
    if bounds.liquidity_lots <= 0:
        return ReasonCode.DEPTH_INSUFFICIENT
    return ReasonCode.SIZE_BELOW_MINIMUM


def _net_delta_delta(
    feature: FeatureSnapshot,
    approved_lots: int,
    lot_size: LotSize,
) -> int:
    derivatives = feature.derivatives
    contracts = approved_lots * lot_size.contracts_per_lot
    if derivatives is None or derivatives.greeks is None:
        return contracts
    delta = derivatives.greeks.delta
    if delta is None:
        return contracts
    return int(Decimal(delta) * Decimal(contracts))


def _spread_net_delta_delta(
    leg_snapshots: Mapping[str, FeatureSnapshot],
    approved_lots: int,
    lot_size: LotSize,
) -> int:
    contracts = approved_lots * lot_size.contracts_per_lot
    total = Decimal(0)
    for snapshot in leg_snapshots.values():
        derivatives = snapshot.derivatives
        if derivatives is None or derivatives.greeks is None:
            continue
        delta = derivatives.greeks.delta
        if delta is None:
            continue
        total += Decimal(delta) * Decimal(contracts)
    if total == 0:
        return contracts
    return int(total)


def _primary_audit(
    intent: TradeIntent,
    feature: FeatureSnapshot,
    leg_snapshots: Mapping[str, FeatureSnapshot],
    now: datetime,
) -> _SnapshotAudit:
    quotes: list[LegQuoteRef] = []
    for leg in intent.legs:
        snapshot = leg_snapshots.get(leg.leg_id)
        if snapshot is None:
            continue
        quotes.append(
            LegQuoteRef(
                leg_id=leg.leg_id,
                snapshot_id=snapshot.snapshot_id,
                symbol=snapshot.contract.symbol,
                event_time=snapshot.times.event_time,
                calculation_time=snapshot.times.calculation_time,
            )
        )
    if not quotes:
        quotes.append(
            LegQuoteRef(
                leg_id=intent.legs[0].leg_id if intent.legs else "primary",
                snapshot_id=feature.snapshot_id,
                symbol=feature.contract.symbol,
                event_time=feature.times.event_time,
                calculation_time=feature.times.calculation_time,
            )
        )
    return _SnapshotAudit(
        decision_snapshot_id=intent.snapshot_id,
        decision_timestamp=now,
        leg_quotes=tuple(quotes),
    )


def _multi_leg_spread_reason(
    leg_snapshots: Mapping[str, FeatureSnapshot],
    max_spread: Percent,
) -> ReasonCode | None:
    for snapshot in leg_snapshots.values():
        reason = _spread_reason(snapshot, max_spread)
        if reason is not None:
            return reason
    return None
