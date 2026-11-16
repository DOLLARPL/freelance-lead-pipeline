
from freelance_pipeline.urls import canonicalize_url


def test_strips_fragment():
    assert canonicalize_url("https://example.com/jobs/1#section") == "https://example.com/jobs/1"


def test_strips_tracking_parameters():
    assert (
        canonicalize_url("https://example.com/jobs/1?utm_source=x&id=5&utm_medium=email")
        == "https://example.com/jobs/1?id=5"
    )


def test_strips_gclid_and_fbclid():
    assert (
        canonicalize_url("https://example.com/jobs/1?gclid=abc&x=1&fbclid=zz")
        == "https://example.com/jobs/1?x=1"
    )


def test_strips_tracking_parameters_case_insensitively():
    assert (
        canonicalize_url(
            "https://example.com/jobs/1?UTM_SOURCE=x&id=5&Utm_Medium=email&GCLID=abc"
        )
        == "https://example.com/jobs/1?id=5"
    )


def test_strips_mixed_case_fbclid():
    assert (
        canonicalize_url("https://example.com/jobs/1?FBCLID=zz&x=1&gClId=qq")
        == "https://example.com/jobs/1?x=1"
    )


def test_preserves_meaningful_query_parameters():
    assert (
        canonicalize_url("https://example.com/jobs/1?page=2&category=it&utm_campaign=c")
        == "https://example.com/jobs/1?category=it&page=2"
    )


def test_preserves_fragment_when_no_tracking_in_preserve_list():
    # fragment removal is unconditional, query preservation is selective
    assert canonicalize_url("https://example.com/jobs?q=python#top") == "https://example.com/jobs?q=python"


def test_returns_input_when_invalid_or_relative():
    assert canonicalize_url("not a url") == "not a url"
    assert canonicalize_url("") == ""


def test_orders_remaining_query_parameters():
    assert canonicalize_url("https://example.com/jobs?b=2&a=1") == "https://example.com/jobs?a=1&b=2"


def test_preserves_query_param_values_that_look_like_tracking_but_are_meaningful():
    assert canonicalize_url("https://example.com/jobs?ref=job-board&id=9") == "https://example.com/jobs?id=9&ref=job-board"