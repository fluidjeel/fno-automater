"""Freeze PAPER-003 cohort packages from session cycle outcomes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from trading.domain.contracts import (
    CohortPackage,
    CohortSignal,
    ExperimentDefinition,
    TradeIntent,
)
from trading.domain.contracts.order import OrderCommand
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import ExecutionMode, ReasonCode, RiskAction
from trading.domain.primitives import Money
from trading.runtime.paper_runner import PaperCycleResult, PaperStrategyOutcome

__all__ = ["experiment_id_for", "persist_cohorts"]


def experiment_id_for(prefix: str, strategy_id: str, as_of: datetime) -> str:
    """Freeze one experiment identity per strategy ISO week."""
    iso = as_of.isocalendar()
    short = _short_strategy(strategy_id)
    return f"{prefix}-{short}-{iso.year}W{iso.week:02d}"


def persist_cohorts(
    results: tuple[PaperCycleResult, ...],
    *,
    output_dir: Path,
    prefix: str,
    as_of: datetime,
    observation_start: datetime,
    capital_limit: Money,
    risk_policy_version: str,
    fill_model_version: str,
    code_version: str,
    feature_set_version: str,
) -> tuple[Path, ...]:
    """Write one CohortPackage JSON per strategy. Empty cohorts are skipped."""
    output_dir.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[CohortSignal]] = {}
    versions: dict[str, tuple[str, str, str, ExecutionMode]] = {}
    for result in results:
        for outcome in result.outcomes:
            grouped.setdefault(outcome.strategy_id, []).extend(
                _signals_for(outcome, as_of=as_of)
            )
            setup = outcome.setup_features
            versions[outcome.strategy_id] = (
                outcome.strategy_version,
                "legacy" if setup is None else setup.identification_rule_version,
                "legacy" if setup is None else setup.router_version,
                outcome.execution_mode,
            )
    paths: list[Path] = []
    for strategy_id, signals in grouped.items():
        if not signals:
            continue
        experiment_id = experiment_id_for(prefix, strategy_id, observation_start)
        aligned = tuple(_align_experiment(signal, experiment_id) for signal in signals)
        version_tuple = versions.get(
            strategy_id,
            (strategy_id, "legacy", "legacy", ExecutionMode.PAPER),
        )
        (
            strategy_version,
            identification_version,
            router_version,
            execution_mode,
        ) = version_tuple
        experiment = ExperimentDefinition(
            experiment_id=experiment_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            parameter_version="paper-session-v1",
            execution_mode=execution_mode,
            started_at=observation_start,
            capital_limit=(
                Money.zero(capital_limit.currency)
                if execution_mode is ExecutionMode.SHADOW
                else capital_limit
            ),
            parameters_frozen=True,
            feature_set_version=feature_set_version,
            risk_policy_version=risk_policy_version,
            fill_model_version=fill_model_version,
            code_version=code_version,
            identification_rule_version=identification_version,
            router_version=router_version,
        )
        package = CohortPackage(
            experiment=experiment,
            signals=aligned,
            observation_start=observation_start,
            observation_end=as_of,
        )
        path = output_dir / f"{experiment_id}.json"
        path.write_text(package.model_dump_json(indent=2), encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def _align_experiment(signal: CohortSignal, experiment_id: str) -> CohortSignal:
    intent = signal.intent
    if intent is None or intent.experiment_id == experiment_id:
        return signal
    return signal.model_copy(
        update={"intent": intent.model_copy(update={"experiment_id": experiment_id})}
    )


def _signals_for(
    outcome: PaperStrategyOutcome, *, as_of: datetime
) -> list[CohortSignal]:
    signals: list[CohortSignal] = []
    if not outcome.intents:
        reason = (
            outcome.rejection_reasons[0]
            if outcome.rejection_reasons
            else ReasonCode.INSTRUMENT_UNKNOWN
        )
        signals.append(
            CohortSignal(
                signal_id=f"{outcome.snapshot_id}-blocked",
                snapshot_id=outcome.snapshot_id,
                created_at=as_of,
                declined=True,
                rejection_reason=reason,
                entry_quote=_first_quote(outcome),
                decision_quotes=dict(outcome.decision_quotes),
                setup_features=outcome.setup_features,
                route_decision=outcome.route_decision,
                executed=outcome.executed,
            )
        )
        return signals
    for index, intent in enumerate(outcome.intents):
        decision = outcome.decisions[index] if index < len(outcome.decisions) else None
        event = (
            outcome.order_events[index] if index < len(outcome.order_events) else None
        )
        if decision is not None and decision.action is RiskAction.REJECT:
            reason = (
                decision.reason_codes[0]
                if decision.reason_codes
                else ReasonCode.RISK_LIMIT_TRADE
            )
            signals.append(
                _emitted(
                    intent,
                    snapshot_id=outcome.snapshot_id,
                    risk_action=decision.action,
                    risk_reasons=decision.reason_codes,
                    rejection_reason=reason,
                    outcome=outcome,
                )
            )
            continue
        command = event.command if event is not None else None
        strict_fill_verdict = event.strict_fill_verdict if event is not None else None
        signals.append(
            _emitted(
                intent,
                snapshot_id=outcome.snapshot_id,
                risk_action=decision.action if decision is not None else None,
                risk_reasons=decision.reason_codes if decision is not None else (),
                entry_command=command,
                strict_fill_verdict=strict_fill_verdict,
                outcome=outcome,
            )
        )
    return signals


def _emitted(
    intent: TradeIntent,
    *,
    snapshot_id: str,
    risk_action: RiskAction | None,
    risk_reasons: tuple[ReasonCode, ...],
    rejection_reason: ReasonCode | None = None,
    entry_command: OrderCommand | None = None,
    strict_fill_verdict: ReasonCode | None = None,
    outcome: PaperStrategyOutcome,
) -> CohortSignal:
    quotes = dict(outcome.decision_quotes)
    primary_symbol = intent.legs[0].contract.symbol
    return CohortSignal(
        signal_id=intent.intent_id,
        snapshot_id=snapshot_id,
        created_at=intent.created_at,
        declined=False,
        rejection_reason=rejection_reason,
        intent=intent,
        risk_action=risk_action,
        risk_reasons=risk_reasons,
        entry_quote=quotes.get(primary_symbol),
        decision_quotes=quotes,
        entry_command=entry_command,
        strict_fill_verdict=strict_fill_verdict,
        lots=1,
        regime=(
            None
            if outcome.setup_features is None
            else outcome.setup_features.trend.value
        ),
        days_to_expiry=(
            None if outcome.setup_features is None else outcome.setup_features.dte
        ),
        setup_features=outcome.setup_features,
        route_decision=outcome.route_decision,
        executed=outcome.executed,
    )


def _first_quote(outcome: PaperStrategyOutcome) -> MarketQuote | None:
    return None if not outcome.decision_quotes else outcome.decision_quotes[0][1]


def _short_strategy(strategy_id: str) -> str:
    mapping = {
        "positional_long_option": "LO",
        "debit_spread": "DS",
        "defined_risk_multileg": "ML",
        "cas_microstructure": "CAS",
        "commodity_futures_trend": "CF",
    }
    return mapping.get(strategy_id, strategy_id[:8].upper())
