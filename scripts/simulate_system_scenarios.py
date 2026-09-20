#!/usr/bin/env python3
"""Offline 15-scenario system simulation (no broker, no live LLM).

Drives identification router + allow-table, synthetic judgment labels, and a
scripted weekly/advise agent under varied regimes and scores. Historical
backtesting remains out of scope; this is a behavior/regression harness.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import tests.factories as f  # noqa: E402
import tests.test_identification as tid  # noqa: E402

from trading.ai.advise import run_advise_agent  # noqa: E402
from trading.ai.loop import run_weekly_agent  # noqa: E402
from trading.ai.ports import LlmToolCall, LlmTurn  # noqa: E402
from trading.ai.tools import ToolContext  # noqa: E402
from trading.analytics.judgment import label_signal  # noqa: E402
from trading.config import (  # noqa: E402
    load_agent_config,
    load_agent_config_text,
    load_evaluation_config,
)
from trading.domain.clock import FrozenClock  # noqa: E402
from trading.domain.contracts import AdviceStance, StructureChoice  # noqa: E402
from trading.domain.contracts.identification import (  # noqa: E402
    MacroStatus,
    MarketState,
    TrendState,
    VolatilityState,
)
from trading.domain.enums import (  # noqa: E402
    FamilyStance,
    ProposalType,
    Recommendation,
)
from trading.domain.ids import SequentialIdFactory  # noqa: E402
from trading.domain.primitives import Currency, Money  # noqa: E402
from trading.identification import (  # noqa: E402
    allowed_families_for,
    load_identification_policy,
    route_nifty_options,
)
from trading.identification.config import IdentificationPolicy  # noqa: E402

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
POLICY: IdentificationPolicy = load_identification_policy(
    ROOT / "config" / "identification.yaml"
)
EVAL = load_evaluation_config(ROOT / "config" / "evaluation.yaml")
AGENT_DISABLED = load_agent_config(ROOT / "config" / "agent.yaml").config
AGENT_ENABLED = load_agent_config_text(
    (ROOT / "config" / "agent.yaml")
    .read_text(encoding="utf-8")
    .replace("enabled: false", "enabled: true")
).config


@dataclass(frozen=True)
class Scenario:
    name: str
    trend: TrendState
    volatility: VolatilityState
    iv_percentile: Decimal | None
    iv_rv_ratio: Decimal | None
    event_state: str
    macro_status: MacroStatus
    long_score: str
    debit_score: str
    cooldown: bool = False
    correlated: bool = False
    mae: str | None = None
    mfe: str | None = None
    agent_emits: str = "ABSTAIN"  # ABSTAIN | REVISE | ADVISE_DEBIT


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    paper_winner: str | None
    failed_gates: tuple[str, ...]
    shadows: tuple[str, ...]
    allowed_families: tuple[str, ...]
    judgment_should_enter: bool | None
    weekly_disabled: str
    weekly_scripted: str
    advise_disabled: str
    advise_scripted: str
    notes: str


class ScriptedLlm:
    def __init__(self, turns: list[LlmTurn]) -> None:
        self._turns = list(turns)

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LlmTurn:
        if not self._turns:
            return LlmTurn(text="done", tool_calls=(), input_tokens=1, output_tokens=1)
        return self._turns.pop(0)


class _Boom:
    def complete(self, messages: object, tools: object) -> LlmTurn:
        raise AssertionError("disabled agent must not call model")


def _scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(
            "S01_up_low_iv_long_wins",
            TrendState.UP,
            VolatilityState.NORMAL,
            Decimal("30"),
            Decimal("1.0"),
            "NORMAL",
            MacroStatus.MISSING,
            "0.90",
            "0.70",
            mae="40",
            mfe="250",
            agent_emits="REVISE",
        ),
        Scenario(
            "S02_up_high_iv_debit_wins",
            TrendState.UP,
            VolatilityState.EXPANDING,
            Decimal("75"),
            Decimal("1.4"),
            "NORMAL",
            MacroStatus.MISSING,
            "0.70",
            "0.90",
            mae="60",
            mfe="220",
            agent_emits="ADVISE_DEBIT",
        ),
        Scenario(
            "S03_down_low_iv_long_wins",
            TrendState.DOWN,
            VolatilityState.NORMAL,
            Decimal("25"),
            Decimal("0.9"),
            "NORMAL",
            MacroStatus.NEUTRAL,
            "0.88",
            "0.65",
            mae="50",
            mfe="180",
            agent_emits="REVISE",
        ),
        Scenario(
            "S04_down_high_iv_debit_wins",
            TrendState.DOWN,
            VolatilityState.EXPANDING,
            Decimal("80"),
            Decimal("1.5"),
            "CAUTION",
            MacroStatus.MISSING,
            "0.60",
            "0.85",
            mae="90",
            mfe="200",
            agent_emits="ADVISE_DEBIT",
        ),
        Scenario(
            "S05_range_high_iv_multileg_allowed",
            TrendState.RANGE,
            VolatilityState.COMPRESSED,
            Decimal("82"),
            Decimal("1.2"),
            "NORMAL",
            MacroStatus.MISSING,
            "0.55",
            "0.70",
            mae="120",
            mfe="80",
            agent_emits="ABSTAIN",
        ),
        Scenario(
            "S06_mixed_trend_data_gap",
            TrendState.MIXED,
            VolatilityState.NORMAL,
            Decimal("40"),
            Decimal("1.0"),
            "NORMAL",
            MacroStatus.MISSING,
            "0.80",
            "0.80",
            agent_emits="ABSTAIN",
        ),
        Scenario(
            "S07_macro_conflict_blackout",
            TrendState.UP,
            VolatilityState.NORMAL,
            Decimal("35"),
            Decimal("1.0"),
            "NORMAL",
            MacroStatus.CONFLICT,
            "0.90",
            "0.80",
            agent_emits="ABSTAIN",
        ),
        Scenario(
            "S08_cooldown_blocks_entry",
            TrendState.UP,
            VolatilityState.NORMAL,
            Decimal("30"),
            Decimal("1.0"),
            "NORMAL",
            MacroStatus.MISSING,
            "0.90",
            "0.70",
            cooldown=True,
            agent_emits="ABSTAIN",
        ),
        Scenario(
            "S09_missing_iv_warmup",
            TrendState.UP,
            VolatilityState.NORMAL,
            None,
            None,
            "NORMAL",
            MacroStatus.MISSING,
            "0.90",
            "0.70",
            agent_emits="ABSTAIN",
        ),
        Scenario(
            "S10_event_block_new_allow_empty",
            TrendState.UP,
            VolatilityState.NORMAL,
            Decimal("30"),
            Decimal("1.0"),
            "BLOCK_NEW",
            MacroStatus.MISSING,
            "0.90",
            "0.70",
            agent_emits="ABSTAIN",
        ),
        Scenario(
            "S11_score_tie_no_clear_winner",
            TrendState.UP,
            VolatilityState.NORMAL,
            Decimal("30"),
            Decimal("1.0"),
            "NORMAL",
            MacroStatus.MISSING,
            "0.80",
            "0.80",
            agent_emits="ABSTAIN",
        ),
        Scenario(
            "S12_correlated_exposure",
            TrendState.UP,
            VolatilityState.NORMAL,
            Decimal("30"),
            Decimal("1.0"),
            "NORMAL",
            MacroStatus.MISSING,
            "0.90",
            "0.70",
            correlated=True,
            mae="40",
            mfe="200",
            agent_emits="REVISE",
        ),
        Scenario(
            "S13_thin_mfe_should_pass",
            TrendState.UP,
            VolatilityState.NORMAL,
            Decimal("30"),
            Decimal("1.0"),
            "NORMAL",
            MacroStatus.MISSING,
            "0.90",
            "0.70",
            mae="180",
            mfe="20",
            agent_emits="ABSTAIN",
        ),
        Scenario(
            "S14_caution_mid_iv_debit_pref",
            TrendState.UP,
            VolatilityState.NORMAL,
            Decimal("55"),
            Decimal("1.2"),
            "CAUTION",
            MacroStatus.ALIGNED,
            "0.72",
            "0.86",
            mae="70",
            mfe="190",
            agent_emits="ADVISE_DEBIT",
        ),
        Scenario(
            "S15_unknown_vol_still_routes",
            TrendState.DOWN,
            VolatilityState.UNKNOWN,
            Decimal("28"),
            Decimal("1.0"),
            "NORMAL",
            MacroStatus.MISSING,
            "0.91",
            "0.66",
            mae="45",
            mfe="210",
            agent_emits="REVISE",
        ),
    )


def _market_for(scenario: Scenario) -> MarketState:
    overrides: dict[str, object] = {
        "trend": scenario.trend,
        "volatility": scenario.volatility,
        "event_state": scenario.event_state,
        "macro_status": scenario.macro_status,
        "iv_percentile": scenario.iv_percentile,
        "iv_rv_ratio": scenario.iv_rv_ratio,
    }
    return tid._market(**overrides)


def _tools(clock: FrozenClock) -> ToolContext:
    return ToolContext(
        clock=clock,
        id_factory=SequentialIdFactory(clock.instant),
        evaluation=EVAL,
        cohort=f.long_option_cohort_package(),
    )


def _weekly_turn(kind: str) -> LlmTurn:
    evidence = f.evidence()
    if kind == "REVISE":
        proposal = {
            "proposal_type": ProposalType.STRATEGY_FAMILY.value,
            "scope": "weekly/families",
            "valid_until": (NOW + timedelta(days=7)).isoformat(),
            "recommendation": Recommendation.REVISE.value,
            "confidence": "0.62",
            "evidence": [evidence.model_dump(mode="json")],
            "family_actions": [
                {
                    "strategy_id": "positional_long_option",
                    "stance": FamilyStance.ENABLE.value,
                }
            ],
            "narrative": "sim: paper expectancy positive under low-IV trend",
        }
    else:
        proposal = {
            "proposal_type": ProposalType.STRATEGY_FAMILY.value,
            "scope": "weekly/families",
            "valid_until": (NOW + timedelta(days=7)).isoformat(),
            "recommendation": Recommendation.ABSTAIN.value,
            "confidence": "0.20",
            "evidence": [evidence.model_dump(mode="json")],
            "family_actions": [],
            "narrative": "sim: insufficient comparable evidence",
            "missing_data": ["insufficient_evidence"],
        }
    return LlmTurn(
        text="",
        tool_calls=(
            LlmToolCall(
                call_id="1",
                name="emit_proposal",
                arguments={"proposal": proposal},
            ),
        ),
        input_tokens=10,
        output_tokens=10,
    )


def _advise_turn(kind: str) -> LlmTurn:
    if kind == "ADVISE_DEBIT":
        advice = {
            "preferred_structure": StructureChoice.DEBIT_SPREAD.value,
            "stance": AdviceStance.PAPER.value,
            "confidence": "0.55",
            "alternatives_ranked": [
                {
                    "structure": StructureChoice.POSITIONAL_LONG_OPTION.value,
                    "score": "0.40",
                    "why": "sim lower-ranked long option",
                }
            ],
            "do_not_trade_if": [],
            "market_summary": {"sim": True},
        }
    else:
        advice = {
            "preferred_structure": StructureChoice.PASS.value,
            "stance": AdviceStance.PASS.value,
            "confidence": "0.10",
            "alternatives_ranked": [],
            "do_not_trade_if": ["sim insufficient evidence"],
            "market_summary": {"sim": True},
        }
    return LlmTurn(
        text="",
        tool_calls=(
            LlmToolCall(
                call_id="1",
                name="emit_advice",
                arguments={"advice": advice},
            ),
        ),
        input_tokens=8,
        output_tokens=8,
    )


def _judgment_label(scenario: Scenario) -> bool | None:
    if scenario.mae is None or scenario.mfe is None:
        return None
    signal = type(
        "Sig",
        (),
        {
            "declined": False,
            "mae": Money.of(Decimal(scenario.mae), Currency.INR),
            "mfe": Money.of(Decimal(scenario.mfe), Currency.INR),
            "lots": 1,
        },
    )()
    charges = EVAL.config.fill_model.charges_per_lot.require("sim")
    return label_signal(
        signal,  # type: ignore[arg-type]
        charges_per_lot=charges,
        thresholds=EVAL.config.judgment,
    )


def run_scenario(scenario: Scenario) -> ScenarioResult:
    market = _market_for(scenario)
    long = tid._bound("positional_long_option", scenario.long_score)
    debit = tid._bound("debit_spread", scenario.debit_score)
    route, _ops = route_nifty_options(
        market,
        long_option=long,
        debit_spread=debit,
        policy=POLICY,
        cooldown_active=scenario.cooldown,
        existing_correlated_exposure=scenario.correlated,
    )
    allowed = tuple(sorted(allowed_families_for(market, POLICY)))
    judgment = _judgment_label(scenario)

    clock = FrozenClock(NOW)
    boom: Any = _Boom()

    weekly_off = run_weekly_agent(
        llm=boom,
        config=AGENT_DISABLED,
        tools=_tools(clock),
        prompt="sim weekly",
    )
    emit = scenario.agent_emits
    weekly_kind = "REVISE" if emit == "REVISE" else "ABSTAIN"
    weekly_on = run_weekly_agent(
        llm=ScriptedLlm([_weekly_turn(weekly_kind)]),
        config=AGENT_ENABLED,
        tools=_tools(clock),
        prompt="sim weekly",
    )
    advise_off = run_advise_agent(
        llm=boom,
        config=AGENT_DISABLED,
        tools=_tools(clock),
        prompt="sim advise",
    )
    advise_kind = "ADVISE_DEBIT" if emit == "ADVISE_DEBIT" else "ABSTAIN"
    advise_on = run_advise_agent(
        llm=ScriptedLlm([_advise_turn(advise_kind)]),
        config=AGENT_ENABLED,
        tools=_tools(clock),
        prompt="sim advise",
    )

    notes: list[str] = []
    if route.paper_winner is None:
        notes.append("router_abstain")
    if judgment is False:
        notes.append("ex_post_should_pass")
    if judgment is True:
        notes.append("ex_post_should_enter")
    if not allowed:
        notes.append("allow_table_empty")

    return ScenarioResult(
        name=scenario.name,
        paper_winner=route.paper_winner,
        failed_gates=tuple(route.failed_gate_ids),
        shadows=tuple(route.shadow_alternatives),
        allowed_families=allowed,
        judgment_should_enter=judgment,
        weekly_disabled=weekly_off.recommendation.value,
        weekly_scripted=weekly_on.recommendation.value,
        advise_disabled=advise_off.preferred_structure.value,
        advise_scripted=advise_on.preferred_structure.value,
        notes=",".join(notes) or "ok",
    )


def main() -> int:
    results = [run_scenario(s) for s in _scenarios()]

    out_dir = ROOT / "data" / "simulations"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"system_scenarios_{stamp}.json"
    payload = {
        "generated_at": stamp,
        "scenario_count": len(results),
        "results": [asdict(r) for r in results],
    }
    json_path.write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )

    print(f"scenarios={len(results)} report={json_path}")
    header = (
        f"{'name':<36} {'winner':<24} {'judg':<6} {'wk_off':<8} {'wk_on':<8} "
        f"{'ad_off':<22} {'ad_on':<22} notes"
    )
    print(header)
    for r in results:
        print(
            f"{r.name:<36} {r.paper_winner!s:<24} "
            f"{r.judgment_should_enter!s:<6} {r.weekly_disabled:<8} "
            f"{r.weekly_scripted:<8} {r.advise_disabled:<22} "
            f"{r.advise_scripted:<22} {r.notes}"
        )
        if r.failed_gates:
            print(f"  gates={list(r.failed_gates)} allowed={list(r.allowed_families)}")

    winners = sum(1 for r in results if r.paper_winner)
    abstains = sum(1 for r in results if r.paper_winner is None)
    print(
        f"\nsummary: routed={winners} router_abstain={abstains} "
        f"weekly_disabled_all_abstain="
        f"{all(r.weekly_disabled == 'ABSTAIN' for r in results)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
