"""Operator view of family G1/G2 gates and session stances."""

from __future__ import annotations

from trading.domain.contracts.cycle_evidence import FamilyOperatorRow
from trading.domain.contracts.mode_policy import ModesConfig, load_modes_config
from trading.domain.enums import ExecutionMode, FamilyId, ModeId, ReviewSlotId
from trading.domain.family_gates import G1_EXCEEDS_BUDGET_FAMILIES, G2_UNPROVEN_FAMILIES
from trading.research.registry import family_research_status
from trading.runtime.paper_session import PaperSessionConfig

__all__ = ["build_family_operator_view", "build_four_mode_allocations"]


def build_four_mode_allocations(
    modes_config: ModesConfig | None = None,
) -> tuple[dict[str, str], ...]:
    """Return mode_id, share, and reference capital share for the operator view."""
    config = modes_config or load_modes_config()
    rows: list[dict[str, str]] = []
    for mode_id, policy in config.modes.items():
        rows.append(
            {
                "mode_id": mode_id.value,
                "capital_share": str(policy.capital_share),
                "allowed_families": ",".join(
                    family.value for family in policy.allowed_families
                ),
            }
        )
    return tuple(rows)


def build_family_operator_view(
    session_config: PaperSessionConfig,
    *,
    modes_config: ModesConfig | None = None,
    next_review_slot: ReviewSlotId | None = ReviewSlotId.NSE_AFTERNOON,
) -> tuple[FamilyOperatorRow, ...]:
    """Build G1/G2/stance rows for dashboard and CLI."""
    config = modes_config or load_modes_config()
    family_to_mode: dict[str, ModeId] = {}
    for mode_id, policy in config.modes.items():
        for family in policy.allowed_families:
            family_to_mode[family.value] = mode_id

    rows: list[FamilyOperatorRow] = []
    seen: set[str] = set()
    for family in FamilyId:
        seen.add(family.value)
        stance = session_config.strategy_stances.get(family.value)
        rows.append(
            _row_for_family(
                family.value,
                mode_id=family_to_mode.get(family.value),
                stance=stance,
                next_review_slot=next_review_slot,
            )
        )
    for strategy_id, stance in session_config.strategy_stances.items():
        if strategy_id in seen:
            continue
        rows.append(
            _row_for_family(
                strategy_id,
                mode_id=family_to_mode.get(strategy_id),
                stance=stance,
                next_review_slot=next_review_slot,
            )
        )
    return tuple(sorted(rows, key=lambda item: item.family_id))


def _row_for_family(
    family_id: str,
    *,
    mode_id: ModeId | None,
    stance: ExecutionMode | None,
    next_review_slot: ReviewSlotId | None,
) -> FamilyOperatorRow:
    return FamilyOperatorRow(
        family_id=family_id,
        mode_id=mode_id,
        session_stance=stance,
        research_status=family_research_status(family_id, session_stance=stance).value,
        g1_blocked=family_id in G1_EXCEEDS_BUDGET_FAMILIES,
        g2_proven=family_id not in G2_UNPROVEN_FAMILIES,
        next_review_slot=next_review_slot.value if next_review_slot else None,
    )
