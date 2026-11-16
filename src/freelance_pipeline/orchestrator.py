from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from freelance_pipeline.models import JobLead

Collector = Callable[[object], Sequence[JobLead]]


def _default_collect(source: object) -> list[JobLead]:
    """Collect from a duck-typed adapter with a ``collect()`` method."""
    return list(source.collect())  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class SourceResult:
    """One source's collection outcome: its leads or the failure it caused.

    A failed source keeps an empty ``leads`` bucket so no partial payload can
    ever be mistaken for a successful collection. This object carries no state
    store and performing/committing seen-markers is never its business; the
    orchestrator performs no state mutation whatsoever.
    """

    source: str
    leads: tuple[JobLead, ...] = ()
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def error_message(self) -> str | None:
        return str(self.error) if self.error is not None else None


@dataclass(frozen=True, slots=True)
class CollectionRun:
    """The full, isolation-respecting outcome of of collecting all sources.

    One broken source is reported through :attr:`errors` and never aborts the
    other collections, so nothing is lost and nothing is silently marked seen.
    """

    results: tuple[SourceResult, ...]

    @property
    def successes(self) -> tuple[SourceResult, ...]:
        return tuple(r for r in self.results if r.ok)

    @property
    def failures(self) -> tuple[SourceResult, ...]:
        return tuple(r for r in self.results if not r.ok)

    @property
    def successes_sources(self) -> tuple[str, ...]:
        return tuple(r.source for r in self.successes)

    @property
    def failed_sources(self) -> tuple[str, ...]:
        return tuple(r.source for r in self.failures)

    @property
    def error_messages(self) -> dict[str, str]:
        return {r.source: r.error_message for r in self.failures if r.error_message is not None}

    @property
    def all(self) -> tuple[JobLead, ...]:
        """Every lead from every successful source, in register order."""
        return tuple(lead for result in self.successes for lead in result.leads)

    @property
    def is_ok(self) -> bool:
        return not self.failures


def collect_all(
    adapters: Mapping[str, object],
    *,
    collector: Collector | None = None,
) -> CollectionRun:
    """Run every source once, isolating each failure, with no side effects.

    ``collector`` defaults to ``source.collect()`` and is injectable so a wall
    clock timeout or retry policy can wrap every source uniformly. Exceptions
    are captured per source and returned, never re-raised, so a hly broken
    source cannot abort the whole run.
    """
    collect: Collector = collector if collector is not None else _default_collect
    results: list[SourceResult] = []
    for name, source in adapters.items():
        try:
            leads = tuple(collect(source))
        except Exception as exc:  # noqa: BLE001 - intentional failure isolation
            results.append(SourceResult(source=name, error=exc))
        else:
            results.append(SourceResult(source=name, leads=leads))
    return CollectionRun(results=tuple(results))


__all__ = [
    "CollectionRun",
    "Collector",
    "SourceResult",
    "collect_all",
]