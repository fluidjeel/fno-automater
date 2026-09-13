"""Config-driven keyword mapping from permitted snippets to event categories."""

from __future__ import annotations

from dataclasses import dataclass

from trading.news.config import NewsSubsystemConfig, NewsTaxonomyRule
from trading.news.contracts import NewsItem

__all__ = ["TaxonomyMatch", "classify_taxonomy"]


@dataclass(frozen=True, slots=True)
class TaxonomyMatch:
    """One configured event category and the terms that caused the match."""

    rule: NewsTaxonomyRule
    matched_keywords: tuple[str, ...]


def classify_taxonomy(
    item: NewsItem,
    config: NewsSubsystemConfig,
) -> tuple[TaxonomyMatch, ...]:
    """Return configured matches in stable order without inferring direction."""
    text = f"{item.headline} {item.snippet}".casefold()
    matches: list[TaxonomyMatch] = []
    for rule in config.taxonomy:
        keywords = tuple(
            keyword for keyword in rule.keywords if keyword.casefold() in text
        )
        if keywords:
            matches.append(TaxonomyMatch(rule=rule, matched_keywords=keywords))
    return tuple(matches)
