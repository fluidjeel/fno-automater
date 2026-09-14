"""Deterministic paper broker for offline slice-1 validation."""

from trading.broker.paper.adapter import PaperBroker
from trading.broker.paper.fixtures import PaperBrokerFixtures

__all__ = ["PaperBroker", "PaperBrokerFixtures"]
