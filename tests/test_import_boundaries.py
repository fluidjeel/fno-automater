"""Architecture dependency rules, enforced by AST inspection.

ARCHITECTURE.md: "Domain and calculations import no infrastructure." A comment
cannot enforce that, so it is a test.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
DOMAIN = SRC / "trading" / "domain"

# The domain layer may depend on these and nothing else.
DOMAIN_ALLOWED_THIRD_PARTY = frozenset({"pydantic"})

# Infrastructure that must never appear in the domain layer, listed explicitly
# so a failure message names the violated rule rather than just a module.
FORBIDDEN_IN_DOMAIN = frozenset(
    {
        "asyncio",
        "http",
        "httpx",
        "logging",
        "os",
        "pathlib",
        "redis",
        "requests",
        "socket",
        "sqlite3",
        "subprocess",
        "urllib",
        "duckdb",
        "polars",
        "pandas",
        "yaml",
        "pydantic_settings",
    }
)

STDLIB = frozenset(sys.stdlib_module_names)


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    """Top-level package name of every import in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        # level > 0 is a relative import; ruff TID252 bans those outright.
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
        ):
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", _python_files(DOMAIN), ids=lambda p: p.name)
def test_domain_imports_only_stdlib_and_pydantic(path: Path) -> None:
    """Invariant: the domain layer has no infrastructure dependency."""
    offenders = {
        root
        for root in _imported_roots(path)
        if root != "trading"
        and root not in STDLIB
        and root not in DOMAIN_ALLOWED_THIRD_PARTY
    }
    assert not offenders, (
        f"{path.relative_to(SRC)} imports non-domain packages {sorted(offenders)}. "
        f"Domain code may import only the standard library and "
        f"{sorted(DOMAIN_ALLOWED_THIRD_PARTY)}; put side effects behind a port."
    )


@pytest.mark.parametrize("path", _python_files(DOMAIN), ids=lambda p: p.name)
def test_domain_avoids_infrastructure_modules(path: Path) -> None:
    """Invariant: no I/O, no ambient config, no network in the domain layer."""
    offenders = _imported_roots(path) & FORBIDDEN_IN_DOMAIN
    assert not offenders, (
        f"{path.relative_to(SRC)} imports infrastructure {sorted(offenders)}. "
        "The domain layer must stay pure and side-effect free."
    )


def test_domain_layer_is_not_empty() -> None:
    """Guard: the boundary tests above pass trivially on an empty tree."""
    assert _python_files(DOMAIN), "no domain modules found; boundary tests are vacuous"


def test_no_module_reads_ambient_time_or_randomness() -> None:
    """Invariant 21: decisions are reproducible, so time and IDs are injected."""
    banned = {
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "time.time",
        "random.random",
        "uuid.uuid1",
        "uuid.uuid4",
    }
    # Wall time is read only inside the clock module.
    exempt = {DOMAIN / "clock.py"}
    violations: list[str] = []
    for path in _python_files(SRC):
        if path in exempt:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = ast.unparse(node.func)
            # Match on the trailing attribute pair, e.g. "datetime.now" in
            # "dt.datetime.now" or "datetime.datetime.now".
            tail = ".".join(call.split(".")[-2:])
            if tail in banned:
                violations.append(f"{path.relative_to(SRC)}:{node.lineno} calls {call}")
    assert not violations, (
        "ambient time or randomness reached production code: "
        + "; ".join(violations)
        + ". Inject Clock or IdFactory instead."
    )


ANALYTICS = SRC / "trading" / "analytics"
AI = SRC / "trading" / "ai"
FORBIDDEN_ANALYTICS_THIRD_PARTY = frozenset(
    {
        "httpx",
        "requests",
        "sqlite3",
        "redis",
        "duckdb",
        "pandas",
        "polars",
        "fyers_apiv3",
    }
)
LIVE_PATH_PREFIXES = (
    "trading.broker",
    "trading.oms",
    "trading.storage",
    "trading.safety",
    "trading.trade",
    "trading.risk",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
        ):
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", _python_files(ANALYTICS), ids=lambda p: p.name)
def test_analytics_stays_off_the_live_path(path: Path) -> None:
    """Layer 4 jobs must not import broker, OMS or storage."""
    imported = _imported_modules(path)
    live = {
        module
        for module in imported
        for prefix in LIVE_PATH_PREFIXES
        if module == prefix or module.startswith(f"{prefix}.")
    }
    assert not live, f"{path.name} imports live-path modules {sorted(live)}"
    third_party = {
        module.split(".")[0]
        for module in imported
        if module.split(".")[0] in FORBIDDEN_ANALYTICS_THIRD_PARTY
    }
    assert not third_party, f"{path.name} imports {sorted(third_party)}"


@pytest.mark.parametrize("path", _python_files(AI), ids=lambda p: p.name)
def test_agent_stays_off_the_live_path(path: Path) -> None:
    """The weekly agent must not import broker, OMS or the trading store."""
    imported = _imported_modules(path)
    live = {
        module
        for module in imported
        for prefix in LIVE_PATH_PREFIXES
        if module == prefix or module.startswith(f"{prefix}.")
    }
    assert not live, f"{path.name} imports live-path modules {sorted(live)}"
