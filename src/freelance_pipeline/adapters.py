from __future__ import annotations

from typing import Protocol, runtime_checkable

from freelance_pipeline.models import JobLead


@runtime_checkable
class SourceAdapter(Protocol):
    """Contract every source adapter must satisfy."""

    def collect(self) -> list[JobLead]:
        """Fetch leads from the underlying source."""
        ...


def is_source_adapter(obj: object) -> bool:
    """Stricter runtime check than ``isinstance`` against the protocol.

    ``@runtime_checkable`` only verifies attribute presence, so a class whose
    ``collect`` attribute is not callable would still pass ``isinstance``.
    """
    return isinstance(obj, SourceAdapter) and callable(getattr(obj, "collect", None))