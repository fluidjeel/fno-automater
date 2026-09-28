"""Reference-counted subscription registry for the shared data socket hub."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SubscriptionKey", "SubscriptionRegistry"]


@dataclass(frozen=True, slots=True, order=True)
class SubscriptionKey:
    """One symbol and feed type pair on the shared data socket."""

    symbol: str
    data_type: str


class SubscriptionRegistry:
    """Track owner-tagged subscriptions with incremental refcounting."""

    def __init__(self) -> None:
        self._owners: dict[str, set[SubscriptionKey]] = {}
        self._refcounts: dict[SubscriptionKey, int] = {}

    def subscribe(
        self, owner: str, symbol: str, data_type: str
    ) -> set[SubscriptionKey]:
        """Add one owner subscription; return keys that became newly active."""
        key = SubscriptionKey(symbol=symbol, data_type=data_type)
        owner_keys = self._owners.setdefault(owner, set())
        if key in owner_keys:
            return set()
        owner_keys.add(key)
        count = self._refcounts.get(key, 0) + 1
        self._refcounts[key] = count
        if count == 1:
            return {key}
        return set()

    def unsubscribe(
        self, owner: str, symbol: str, data_type: str
    ) -> set[SubscriptionKey]:
        """Remove one owner subscription; return keys that became inactive."""
        key = SubscriptionKey(symbol=symbol, data_type=data_type)
        owner_keys = self._owners.get(owner)
        if owner_keys is None or key not in owner_keys:
            return set()
        owner_keys.remove(key)
        if not owner_keys:
            self._owners.pop(owner, None)
        count = self._refcounts.get(key, 0) - 1
        if count <= 0:
            self._refcounts.pop(key, None)
            return {key}
        self._refcounts[key] = count
        return set()

    def replace_owner(
        self,
        owner: str,
        desired: set[SubscriptionKey],
    ) -> tuple[set[SubscriptionKey], set[SubscriptionKey]]:
        """Replace an owner's subscriptions; return added and removed active keys."""
        current = set(self._owners.get(owner, set()))
        to_remove = current - desired
        to_add = desired - current
        activated: set[SubscriptionKey] = set()
        deactivated: set[SubscriptionKey] = set()
        for key in to_remove:
            deactivated.update(self.unsubscribe(owner, key.symbol, key.data_type))
        for key in to_add:
            activated.update(self.subscribe(owner, key.symbol, key.data_type))
        return activated, deactivated

    def active_keys(self) -> frozenset[SubscriptionKey]:
        """Return all currently active subscription keys."""
        return frozenset(self._refcounts)

    def keys_for_data_type(self, data_type: str) -> tuple[str, ...]:
        """Return active symbols for one data type."""
        return tuple(
            sorted(key.symbol for key in self._refcounts if key.data_type == data_type)
        )

    def owner_count(self) -> int:
        """Return the number of distinct owners with active subscriptions."""
        return len(self._owners)

    def subscriber_counts(self) -> dict[str, int]:
        """Return per-owner active subscription counts."""
        return {owner: len(keys) for owner, keys in sorted(self._owners.items())}
