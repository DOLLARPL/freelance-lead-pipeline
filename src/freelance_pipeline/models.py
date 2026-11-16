from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _freeze(value: Any) -> Any:
    """Recursively convert a JSON-compatible value into an immutable, hashable one."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(
                f"raw_data must contain only JSON-like values, got {type(value).__name__}"
            )
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return FrozenList(value)
    raise TypeError(f"raw_data must contain only JSON-like values, got {type(value).__name__}")


def _unfreeze(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return value.to_dict()
    if isinstance(value, FrozenList):
        return value.to_list()
    return value


class FrozenDict(Mapping):
    """Immutable, hashable, dict-comparable view of a JSON object."""

    __slots__ = ("_items",)

    def __init__(self, data: Mapping) -> None:
        items = []
        for key, value in data.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"raw_data object keys must be str, got {type(key).__name__}"
                )
            items.append((key, _freeze(value)))
        object.__setattr__(self, "_items", tuple(sorted(items)))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __getitem__(self, key: Any) -> Any:
        for k, v in self._items:
            if k == key:
                return v
        raise KeyError(key)

    def __iter__(self):
        return (k for k, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self) == dict(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._items)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict(self)!r})"

    def to_dict(self) -> dict:
        return {k: _unfreeze(v) for k, v in self._items}


class FrozenList(Sequence):
    """Immutable, hashable, list-comparable view of a JSON array."""

    __slots__ = ("_items",)

    def __init__(self, data) -> None:
        object.__setattr__(self, "_items", tuple(_freeze(v) for v in data))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __getitem__(self, index):
        return self._items[index]

    def __len__(self) -> int:
        return len(self._items)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FrozenList):
            return self._items == other._items
        if isinstance(other, list):
            return list(self._items) == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._items)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({list(self)!r})"

    def to_list(self) -> list:
        return [_unfreeze(v) for v in self._items]


def _required_str(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str, got {type(value).__name__}")
    if not value.strip():
        raise ValueError(f"{name} must not be empty or whitespace")
    return value


def _optional_str(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str or None, got {type(value).__name__}")
    return value


def _aware_datetime(name: str, value: Any, *, required: bool = False) -> datetime | None:
    if value is None:
        if required:
            raise TypeError(f"{name} is required")
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(value).__name__}")
    try:
        offset = value.utcoffset()
    except Exception:  # noqa: BLE001 - tzinfo may raise anything; surface as ValueError
        raise ValueError(f"{name} must be timezone-aware") from None
    if offset is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _budget(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"budget must be a finite nonnegative number, got {type(value).__name__}")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"budget must be finite and nonnegative, got {value!r}")
    return value


def _skills(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise TypeError("skills must be an iterable of str, not a bare string")
    try:
        items = tuple(value)
    except TypeError:
        raise TypeError(f"skills must be an iterable of str, got {type(value).__name__}") from None
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"skills items must be str, got {type(item).__name__}")
    return items


def _raw_data(value: Any) -> FrozenDict | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"raw_data must be a mapping, got {type(value).__name__}")
    return _freeze(value)


@dataclass(frozen=True, slots=True)
class JobLead:
    """A validated, dependency-free lead harvested from a freelance source."""

    source: str
    source_id: str
    url: str
    canonical_url: str = ""
    title: str = ""
    description: str = ""
    company_or_author: str | None = None
    published_at: datetime | None = None
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    budget: float | None = None
    currency: str | None = None
    contract_type: str | None = None
    work_mode: str | None = None
    location: str | None = None
    skills: tuple[str, ...] = ()
    language: str | None = None
    status: str | None = None
    raw_data: FrozenDict | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _required_str("source", self.source))
        object.__setattr__(self, "source_id", _required_str("source_id", self.source_id))
        object.__setattr__(self, "url", _required_str("url", self.url))
        object.__setattr__(self, "canonical_url", _optional_str("canonical_url", self.canonical_url))
        object.__setattr__(self, "title", _optional_str("title", self.title))
        object.__setattr__(self, "description", _optional_str("description", self.description))
        object.__setattr__(self, "company_or_author", _optional_str("company_or_author", self.company_or_author))
        object.__setattr__(self, "published_at", _aware_datetime("published_at", self.published_at))
        object.__setattr__(self, "collected_at", _aware_datetime("collected_at", self.collected_at, required=True))
        object.__setattr__(self, "budget", _budget(self.budget))
        object.__setattr__(self, "currency", _optional_str("currency", self.currency))
        object.__setattr__(self, "contract_type", _optional_str("contract_type", self.contract_type))
        object.__setattr__(self, "work_mode", _optional_str("work_mode", self.work_mode))
        object.__setattr__(self, "location", _optional_str("location", self.location))
        object.__setattr__(self, "skills", _skills(self.skills))
        object.__setattr__(self, "language", _optional_str("language", self.language))
        object.__setattr__(self, "status", _optional_str("status", self.status))
        object.__setattr__(self, "raw_data", _raw_data(self.raw_data))