"""M2 Mode A (live) vs Mode B (shadow early-exit) comparative study."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from trading.config.m2_ab_study import LoadedM2AbStudyConfig, load_m2_ab_study_config
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import ModeId, TradeState
from trading.domain.primitives import Currency, Money
from trading.portfolio.campaign_drawdown import trade_accounting
from trading.portfolio.fill_ledger import index_fill_charges, index_order_events
from trading.trade.exits import strategy_unrealized_pnl
from trading.trade.m2_pre_close_early_exit import evaluate_m2_pre_close_early_exit

if TYPE_CHECKING:
    from trading.runtime.paper_runner import PaperRunner

__all__ = ["M2AbStudy"]


@dataclass
class _ShadowB:
    trade_id: str
    intent: TradeIntent
    max_loss: Money
    entry_at: datetime
    entry_price: Decimal
    qty: int
    symbol: str


@dataclass
class M2AbStudy:
    """Log Mode A from live closes; simulate Mode B with optional pre-close exit."""

    _loaded: LoadedM2AbStudyConfig
    _root: Path
    _shadow_b: dict[str, _ShadowB] = field(default_factory=dict)
    _logged_closed_a: set[str] = field(default_factory=set)
    _b_early_exited: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def open(cls, root: Path) -> M2AbStudy:
        loaded = load_m2_ab_study_config(root / "config/m2_ab_study.yaml")
        return cls(_loaded=loaded, _root=root)

    @property
    def enabled(self) -> bool:
        return self._loaded.config.enabled

    @property
    def results_dir(self) -> str:
        return self._loaded.config.results_dir

    def _results_dir(self) -> Path:
        path = self._root / self._loaded.config.results_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _append(self, filename: str, record: dict[str, object]) -> None:
        path = self._results_dir() / filename
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def run_cycle(
        self,
        runner: PaperRunner,
        snapshots: dict[str, FeatureSnapshot],
        *,
        now: datetime,
    ) -> None:
        """Sync shadow state, evaluate Mode B early exit, flush Mode A closes."""
        if not self.enabled:
            return
        cfg = self._loaded.config
        for position in runner.trade_manager.list_positions():
            if position.state is not TradeState.OPEN:
                continue
            mode = position.mode_id
            if mode is not ModeId.M2_DIRECTIONAL:
                continue
            book = runner._open_book.get(position.trade_id)  # noqa: SLF001
            if book is None:
                continue
            intent, risk = book
            if position.trade_id in self._shadow_b:
                continue
            leg = position.legs[0]
            max_loss = risk.recalculated_max_loss
            if max_loss is None:
                continue
            self._shadow_b[position.trade_id] = _ShadowB(
                trade_id=position.trade_id,
                intent=intent,
                max_loss=max_loss,
                entry_at=position.opened_at or now,
                entry_price=leg.average_entry_price.value,
                qty=leg.quantity_contracts,
                symbol=leg.contract.symbol,
            )
            self._append(
                cfg.comparison_file,
                {
                    "event": "study_entry_sync",
                    "trade_id": position.trade_id,
                    "at": now.isoformat(),
                    "symbol": leg.contract.symbol,
                },
            )

        for trade_id, shadow in list(self._shadow_b.items()):
            snap = snapshots.get(shadow.symbol)
            if snap is None:
                continue
            leg_snaps_map = {}
            for leg in shadow.intent.legs:
                if leg.contract.symbol in snapshots:
                    leg_snaps_map[leg.leg_id] = snapshots[leg.contract.symbol]
            position = runner.trade_manager.get_position(trade_id)
            if position is None or position.state is not TradeState.OPEN:
                continue
            pnl = strategy_unrealized_pnl(position, shadow.intent, leg_snaps_map)
            if pnl is None:
                continue
            should_exit, detail = evaluate_m2_pre_close_early_exit(
                unrealized_pnl=pnl,
                max_loss=shadow.max_loss,
                now=now,
                config=cfg,
            )
            if not should_exit:
                continue
            bid = snap.market.bid
            exit_px = bid.value if bid is not None else shadow.entry_price
            gross = (exit_px - shadow.entry_price) * Decimal(shadow.qty)
            self._append(
                cfg.mode_b_results_file,
                {
                    "trade_id": trade_id,
                    "mode": "B",
                    "entry_at": shadow.entry_at.isoformat(),
                    "exit_at": now.isoformat(),
                    "exit_reason": "EARLY_PRE_CLOSE",
                    "exit_detail": detail,
                    "gross_pnl_inr": str(gross.quantize(Decimal("0.01"))),
                    "mode_a_still_open": True,
                },
            )
            b_rec = {
                "trade_id": trade_id,
                "exit_at": now.isoformat(),
                "mode_b_gross_inr": str(gross.quantize(Decimal("0.01"))),
                "detail": detail,
            }
            self._b_early_exited[trade_id] = b_rec
            self._append(
                cfg.comparison_file,
                {"event": "mode_b_early_exit", **b_rec},
            )
            del self._shadow_b[trade_id]

        self._flush_mode_a_closes(runner, now=now)

    def _flush_mode_a_closes(self, runner: PaperRunner, *, now: datetime) -> None:
        cfg = self._loaded.config
        store = runner._services.store  # noqa: SLF001
        orders = index_order_events(store)
        charges = index_fill_charges(store)
        from trading.config.risk_policy import load_risk_policy

        charges_per_lot = load_risk_policy(
            self._root / "config/risk.yaml"
        ).config.charges_per_lot.to_money()
        for record in store.list_position_lifecycle():
            if record.state is not TradeState.CLOSED:
                continue
            mode = record.mode_id or record.position.mode_id
            if mode is not ModeId.M2_DIRECTIONAL:
                continue
            if record.trade_id in self._logged_closed_a:
                continue
            closed_at = record.as_of
            if closed_at is None:
                continue
            acct = trade_accounting(record, orders, charges, charges_per_lot=charges_per_lot)
            exit_reason = self._infer_exit_reason(record)
            self._append(
                cfg.mode_a_results_file,
                {
                    "trade_id": record.trade_id,
                    "mode": "A",
                    "entry_at": (record.position.opened_at or closed_at).isoformat(),
                    "exit_at": closed_at.isoformat(),
                    "exit_reason": exit_reason,
                    "net_pnl_inr": str(acct.realized_net.amount),
                },
            )
            if record.trade_id in self._shadow_b:
                shadow = self._shadow_b.pop(record.trade_id)
                self._append(
                    cfg.mode_b_results_file,
                    {
                        "trade_id": record.trade_id,
                        "mode": "B",
                        "entry_at": shadow.entry_at.isoformat(),
                        "exit_at": closed_at.isoformat(),
                        "exit_reason": "MIRRORED_MODE_A",
                        "exit_detail": f"no early exit; A exited: {exit_reason}",
                        "net_pnl_inr": str(acct.realized_net.amount),
                        "mode_a_exit_reason": exit_reason,
                    },
                )
                self._append(
                    cfg.comparison_file,
                    {
                        "event": "aligned_close",
                        "trade_id": record.trade_id,
                        "mode_a_net_inr": str(acct.realized_net.amount),
                        "mode_b_same_as_a": True,
                    },
                )
            elif record.trade_id in self._b_early_exited:
                b_early = self._b_early_exited.pop(record.trade_id)
                self._append(
                    cfg.comparison_file,
                    {
                        "event": "divergent_close",
                        "trade_id": record.trade_id,
                        "mode_a_net_inr": str(acct.realized_net.amount),
                        "mode_a_exit_reason": exit_reason,
                        "mode_b_early_gross_inr": b_early.get("mode_b_gross_inr"),
                        "mode_b_exited_at": b_early.get("exit_at"),
                    },
                )
            else:
                self._append(
                    cfg.comparison_file,
                    {
                        "event": "mode_a_close",
                        "trade_id": record.trade_id,
                        "mode_a_net_inr": str(acct.realized_net.amount),
                        "mode_a_exit_reason": exit_reason,
                    },
                )
            self._logged_closed_a.add(record.trade_id)

    @staticmethod
    def _infer_exit_reason(record: PositionLifecycleRecord) -> str:
        carries = record.carry_records or ()
        if carries and carries[-1].action.value == "CARRY_REJECTED":
            return "CARRY_GATE_FLATTEN"
        for leg in record.position.legs:
            if leg.state is TradeState.CLOSED:
                return "LAYER2_OR_DISCOVERY_EXIT"
        return "OTHER"
