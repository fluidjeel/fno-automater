"""Quality labels on feature blocks (informational in paper)."""

from enum import StrEnum

__all__ = ["QualityFlag"]


class QualityFlag(StrEnum):
    OK = "OK"
    MISSING = "MISSING"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"
