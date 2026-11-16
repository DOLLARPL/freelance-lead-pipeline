from __future__ import annotations

from freelance_pipeline.http import SourceError

from .discourse import DiscourseCategoryAdapter


class MakeHireHelpError(SourceError):
    """Raised when the Make Community "Hire Help" source cannot be collected."""


class MakeHireHelp(DiscourseCategoryAdapter):
    """Specialist adapter for the public Make community "Hire a Pro" category (JSON).

    The JSON category API exposes each topic's explicit tags, which map directly
    onto ``skills``, and a plain-text excerpt for ``description``.
    """

    source = "make_hire_help"
    error_cls = MakeHireHelpError
    base_url = "https://community.make.com"
    category_slug = "hire-a-pro"
    feed_format = "json"
    max_pages = 1