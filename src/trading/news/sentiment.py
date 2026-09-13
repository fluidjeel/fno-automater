"""Replaceable sentiment classifiers; no implicit neutral fallback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from importlib import import_module
from typing import Protocol, cast

from trading.news.contracts import SentimentLabel, SentimentProbabilities

__all__ = [
    "Classification",
    "DeterministicTestClassifier",
    "FinBertClassifier",
    "SentimentClassifier",
    "UnavailableSentimentClassifier",
]


@dataclass(frozen=True, slots=True)
class Classification:
    label: SentimentLabel
    probabilities: SentimentProbabilities | None
    model_version: str


class SentimentClassifier(Protocol):
    @property
    def model_version(self) -> str: ...
    def classify(self, texts: tuple[str, ...]) -> tuple[Classification, ...]: ...


class _TransformersModule(Protocol):
    pipeline: Callable[..., object]


class UnavailableSentimentClassifier:
    model_version = "unavailable"

    def classify(self, texts: tuple[str, ...]) -> tuple[Classification, ...]:
        return tuple(
            Classification(SentimentLabel.UNKNOWN, None, self.model_version)
            for _ in texts
        )


class DeterministicTestClassifier:
    """Explicit test-only lexical classifier with stable, documented rules."""

    model_version = "deterministic-test-v1"
    positive_terms = frozenset({"surge", "beat", "growth", "approved", "record"})
    negative_terms = frozenset({"fall", "miss", "default", "downgrade", "crisis"})

    def classify(self, texts: tuple[str, ...]) -> tuple[Classification, ...]:
        results: list[Classification] = []
        for text in texts:
            words = set(text.casefold().split())
            pos, neg = (
                bool(words & self.positive_terms),
                bool(words & self.negative_terms),
            )
            if pos == neg:
                probs = SentimentProbabilities(
                    positive=Decimal("0.1"),
                    negative=Decimal("0.1"),
                    neutral=Decimal("0.8"),
                )
                label = SentimentLabel.NEUTRAL
            elif pos:
                probs = SentimentProbabilities(
                    positive=Decimal("0.8"),
                    negative=Decimal("0.1"),
                    neutral=Decimal("0.1"),
                )
                label = SentimentLabel.POSITIVE
            else:
                probs = SentimentProbabilities(
                    positive=Decimal("0.1"),
                    negative=Decimal("0.8"),
                    neutral=Decimal("0.1"),
                )
                label = SentimentLabel.NEGATIVE
            results.append(Classification(label, probs, self.model_version))
        return tuple(results)


class FinBertClassifier:
    """Lazy local-only FinBERT adapter; it never downloads model weights."""

    def __init__(
        self, model_path: str, *, batch_size: int = 16, max_tokens: int = 512
    ) -> None:
        self.model_path = model_path
        self.batch_size = batch_size
        self.max_tokens = max_tokens
        self._pipeline: object | None = None
        self._failure_reason = ""

    @property
    def model_version(self) -> str:
        if self._failure_reason:
            return f"unavailable:finbert-local:{self._failure_reason}"
        return f"finbert-local:{self.model_path}"

    @property
    def availability_reason(self) -> str:
        return self._failure_reason

    def _unknown(self, texts: tuple[str, ...]) -> tuple[Classification, ...]:
        return tuple(
            Classification(SentimentLabel.UNKNOWN, None, self.model_version)
            for _ in texts
        )

    def classify(self, texts: tuple[str, ...]) -> tuple[Classification, ...]:
        if self._pipeline is None:
            try:
                transformers = cast(_TransformersModule, import_module("transformers"))
                pipeline = transformers.pipeline
                if not callable(pipeline):
                    raise RuntimeError("transformers.pipeline is not callable")
                self._pipeline = pipeline(
                    "text-classification",
                    model=self.model_path,
                    tokenizer=self.model_path,
                    device=-1,
                    local_files_only=True,
                    truncation=True,
                    max_length=self.max_tokens,
                    top_k=None,
                )
            except Exception as exc:
                self._failure_reason = f"MODEL_UNAVAILABLE:{type(exc).__name__}"
                return self._unknown(texts)
        results: list[Classification] = []
        run = self._pipeline
        if not callable(run):
            raise RuntimeError("FinBERT pipeline is not callable")
        try:
            for offset in range(0, len(texts), self.batch_size):
                raw_batch = run(list(texts[offset : offset + self.batch_size]))
                results.extend(self._convert_batch(raw_batch))
        except Exception as exc:
            self._failure_reason = f"INFERENCE_FAILED:{type(exc).__name__}"
            return self._unknown(texts)
        return tuple(results)

    def _convert_batch(self, raw_batch: object) -> list[Classification]:
        results: list[Classification] = []
        for raw in cast(list[list[dict[str, object]]], raw_batch):
            scores = {
                str(item["label"]).casefold(): Decimal(str(item["score"]))
                for item in raw
            }
            positive, negative = (
                scores.get("positive", Decimal(0)),
                scores.get("negative", Decimal(0)),
            )
            neutral = scores.get("neutral", Decimal(0))
            total = positive + negative + neutral
            if total <= 0:
                results.append(
                    Classification(SentimentLabel.UNKNOWN, None, self.model_version)
                )
                continue
            normalized_positive = positive / total
            normalized_negative = negative / total
            probs = SentimentProbabilities(
                positive=normalized_positive,
                negative=normalized_negative,
                neutral=Decimal(1) - normalized_positive - normalized_negative,
            )
            label = max(
                (
                    (positive, SentimentLabel.POSITIVE),
                    (negative, SentimentLabel.NEGATIVE),
                    (neutral, SentimentLabel.NEUTRAL),
                ),
                key=lambda pair: pair[0],
            )[1]
            results.append(Classification(label, probs, self.model_version))
        return results
