"""Subprocess worker: reopen persisted PAPER state after an OS-level restart."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from tests.paper_positional_sim.harness import SimWorld
from trading.domain.clock import FrozenClock


def main(argv: list[str]) -> int:
    """Load store+broker from disk, recover, persist, and exit."""
    job_path = Path(argv[1])
    result_path = Path(argv[2])
    payload = json.loads(job_path.read_text(encoding="utf-8"))
    clock = FrozenClock(datetime.fromisoformat(payload["clock"]))
    workdir = Path(payload["store_path"]).parent
    world = SimWorld(workdir, clock=clock)
    restored = list(world.runner.last_recovery.restored_trade_ids)
    blocked = world.runner.last_recovery.entries_blocked
    world.close()
    result_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "restored_trade_ids": restored,
                "entries_blocked": blocked,
            }
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
