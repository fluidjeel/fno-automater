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
    violations: list[str] = []
    for path in _python_files(SRC):
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
