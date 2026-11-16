from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from freelance_pipeline.adapters import SourceAdapter
from freelance_pipeline.sources.facebook import FacebookGroups
from freelance_pipeline.sources.justjoinit import JustJoinITJobs
from freelance_pipeline.sources.make import MakeHireHelp
from freelance_pipeline.sources.n8n import N8NCommunityJobs
from freelance_pipeline.sources.n8ndevs import N8NDevsJobs
from freelance_pipeline.sources.nofluffjobs import NoFluffJobsJobs
from freelance_pipeline.sources.oferteo import OFERTEO_PRODUCTION_CATEGORY_PATHS, OferteoJobs
from freelance_pipeline.sources.oferteo_mail import OferteoMailJobs
from freelance_pipeline.sources.rocketjobs import RocketJobsJobs
from freelance_pipeline.sources.useme import PRODUCTION_CATEGORY_PATHS, UsemeJobs

# Every adapter stays registered so a source can be switched back on without
# touching adapter code. The production *active* set is the four community/
# freelance sources where real client work actually lives (HANDOFF, etap 1);
# the job-board adapters below (justjoinit, nofluffjobs, rocketjobs) are kept
# in code as inactive, and so is facebook_groups: stage 2 only registers it
# (HANDOFF "Etap 2 (adapter FB)"), while stages 3–4 decide about calibration,
# quiet mode and the chat-row limit before it can join ACTIVE_SOURCES.
DEFAULT_ADAPTERS: dict[str, type[SourceAdapter]] = {
    "facebook_groups": FacebookGroups,
    "justjoinit": JustJoinITJobs,
    "n8n_community_jobs": N8NCommunityJobs,
    "make_hire_help": MakeHireHelp,
    "nofluffjobs": NoFluffJobsJobs,
    "n8ndevs": N8NDevsJobs,
    # Oferteo joined 2026-09-25 as a read-only radar over its **public** lists.
    # Contact is pay-per-lead (points), so the adapter never reveals anything and
    # the lead carries contact_requires_points=True; the value it adds is that
    # Jan sees which zapytania exist before deciding to spend points on any.
    "oferteo": OferteoJobs,
    # Oferteo z maila (2026-10-28): źródło prawdziwych zapytań z pełnym briefem, bo mail
    # ``powiadomienie@oferteo.pl`` niesie zakres, miasto, termin i numer zapytania zanim
    # ktokolwiek wyda punkty na kontakt. Publiczna lista (``oferteo``) zostaje jako radar.
    "oferteo_mail": OferteoMailJobs,
    "useme": UsemeJobs,
    "rocketjobs": RocketJobsJobs,
}

# Active production set, in report order. Since 2026-10-07 this is the Polish
# set only: Jan's decision D1 (PLAN-DOPRACOWANIE-2026-10 §2) — the three English
# community sources were switched off (huge competition, no realistic chance of
# winning the job) and Oferteo left the cron path too, because its contact is
# pay-per-lead and spending points is Jan's manual call, not a cron's. The
# English adapters and the Oferteo adapter stay registered and buildable, so
# rollback is one line here plus one line in scripts/run_production.sh.
# facebook_groups joined 2026-09-21 (stage 4 of the FB plan): Jan wants the group
# leads on Telegram, not in a file he never opens; the per-source row cap that
# once limited them was removed on 2026-09-22 and the noise is handled by the
# seller-voice rules in scoring.py instead. All six configured groups are `lang: pl`.
ACTIVE_SOURCES: tuple[str, ...] = (
    "useme",
    "facebook_groups",
)

# Registered but not collected in production (job boards: employment ads).
INACTIVE_SOURCES: tuple[str, ...] = tuple(
    name for name in DEFAULT_ADAPTERS if name not in ACTIVE_SOURCES
)

# Per-source options the production set needs. Only Useme carries any: it is the
# Polish board and stage 5b (2026-09-21) widened it from one category to five, so
# the adapter default stays the single-lane behaviour the offline fixtures and
# the adapter tests rely on. Adding a source option anywhere else is one line
# here, not a code change.
ACTIVE_SOURCE_KWARGS: dict[str, dict[str, Any]] = {
    "oferteo": {"category_paths": OFERTEO_PRODUCTION_CATEGORY_PATHS},
    "useme": {"category_paths": PRODUCTION_CATEGORY_PATHS},
}


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Declarative config describing one source adapter instantiation."""

    name: str
    base_url: str | None = None
    timeout: float = 15.0
    max_pages: int | None = None
    user_agent: str | None = None
    session: Any = field(default=None, repr=False, compare=False)

    @property
    def kwargs(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_pages": self.max_pages,
            "user_agent": self.user_agent,
            "session": self.session,
        }


class AdapterRegistry:
    """Minimal name -> adapter-factory registry (no orchestration or scoring)."""

    def __init__(self, adapters: dict[str, type[SourceAdapter]] | None = None) -> None:
        self._adapters = dict(adapters) if adapters is not None else dict(DEFAULT_ADAPTERS)

    def names(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def build(self, name: str, **kwargs: Any) -> SourceAdapter:
        try:
            factory = self._adapters[name]
        except KeyError:
            raise KeyError(f"unknown source adapter: {name!r}") from None
        return factory(**kwargs)

    def build_all(self, **kwargs: Any) -> dict[str, SourceAdapter]:
        return {name: factory(**kwargs) for name, factory in self._adapters.items()}


default_registry = AdapterRegistry(DEFAULT_ADAPTERS)


def build_adapter(name: str, **kwargs: Any) -> SourceAdapter:
    """Convenience constructor backed by the default registry."""
    return default_registry.build(name, **kwargs)


def build_active_adapters(
    registry: AdapterRegistry | None = None, **kwargs: Any
) -> dict[str, SourceAdapter]:
    """Build only the active production set (:data:`ACTIVE_SOURCES`).

    Inactive job-board adapters stay registered and individually buildable
    (``build_adapter``) so they can be re-enabled without code changes, but a
    default production run never collects from them. :data:`ACTIVE_SOURCE_KWARGS`
    supplies the per-source options (only Useme has any today); caller keywords
    are still passed to every adapter, exactly as before, and a caller keyword
    with the same name wins for that source.
    """
    reg = default_registry if registry is None else registry
    return {
        name: reg.build(name, **{**ACTIVE_SOURCE_KWARGS.get(name, {}), **kwargs})
        for name in ACTIVE_SOURCES
    }


__all__ = [
    "ACTIVE_SOURCES",
    "ACTIVE_SOURCE_KWARGS",
    "DEFAULT_ADAPTERS",
    "INACTIVE_SOURCES",
    "AdapterRegistry",
    "SourceConfig",
    "build_active_adapters",
    "build_adapter",
    "default_registry",
]