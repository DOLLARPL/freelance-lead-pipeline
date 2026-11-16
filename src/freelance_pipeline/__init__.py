"""Freelance Pipeline v2 — core schema and plumbing."""

from freelance_pipeline.adapters import SourceAdapter
from freelance_pipeline.http import (
    DEFAULT_USER_AGENT,
    SourceError,
    SourceHTTPError,
    SourceParseError,
    SourceTimeoutError,
)
from freelance_pipeline.migration import (
    LegacyMigrationError,
    migrate_legacy_seen,
)
from freelance_pipeline.models import JobLead
from freelance_pipeline.registry import (
    ACTIVE_SOURCES,
    AdapterRegistry,
    SourceConfig,
    build_active_adapters,
    build_adapter,
    default_registry,
)
from freelance_pipeline.sources import (
    MakeHireHelp,
    MakeHireHelpError,
    N8NCommunityJobs,
    N8NCommunityJobsError,
    N8NDevsError,
    N8NDevsJobs,
    UsemeError,
    UsemeJobs,
)
from freelance_pipeline.state import SeenStore
from freelance_pipeline.urls import canonicalize_url

__all__ = [
    "ACTIVE_SOURCES",
    "DEFAULT_USER_AGENT",
    "AdapterRegistry",
    "JobLead",
    "LegacyMigrationError",
    "MakeHireHelp",
    "MakeHireHelpError",
    "N8NCommunityJobs",
    "N8NCommunityJobsError",
    "N8NDevsError",
    "N8NDevsJobs",
    "SeenStore",
    "SourceAdapter",
    "SourceConfig",
    "SourceError",
    "SourceHTTPError",
    "SourceParseError",
    "SourceTimeoutError",
    "UsemeError",
    "UsemeJobs",
    "build_active_adapters",
    "build_adapter",
    "canonicalize_url",
    "default_registry",
    "migrate_legacy_seen",
]