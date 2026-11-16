from __future__ import annotations

from freelance_pipeline.http import SourceError

from .discourse import DiscourseCategoryAdapter


class N8NCommunityJobsError(SourceError):
    """Raised when the n8n Community Jobs source cannot be collected."""


class N8NCommunityJobs(DiscourseCategoryAdapter):
    """Specialist adapter for the public n8n community "Jobs" category (RSS).

    The RSS feed is the most stable public representation of the category and
    includes the full post body in one bounded request per page.
    """

    source = "n8n_community_jobs"
    error_cls = N8NCommunityJobsError
    base_url = "https://community.n8n.io"
    category_slug = "jobs"
    feed_format = "rss"
    max_pages = 1