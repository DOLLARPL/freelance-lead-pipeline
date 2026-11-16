from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
    "gbraid",
    "wbraid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
}


def canonicalize_url(url: str) -> str:
    """Strip fragments and tracking params while preserving meaningful ones."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme and not parts.netloc:
        return url

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query.sort()
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))