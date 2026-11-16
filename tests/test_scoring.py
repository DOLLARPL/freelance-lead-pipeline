"""Offline tests for scoring: buckets, strict response validation and the
simple ODRZUĆ audit. The scorer has no keyword/blacklist rejection logic:
buckets come only from the injected model response (OpenRouter) or from a
stable hash (deterministic offline double-run aid).
"""

from __future__ import annotations

import json

import pytest

from freelance_pipeline.models import JobLead
from freelance_pipeline.openrouter import (
    InvalidScoreResponse,
    OpenRouterScorer,
    ScoreError,
    build_score_prompt,
)
from freelance_pipeline.scoring import (
    MAX_LISTING_AGE_DAYS,
    DeterministicScorer,
    ScoreBucket,
    ScoredLead,
    apply_user_policy,
    render_rejected_audit,
)


def make_lead(source: str = "useme", source_id: str = "1", title: str = "Zlecenie AI") -> JobLead:
    return JobLead(
        source=source,
        source_id=source_id,
        url=f"https://{source}.x/o/{source_id}",
        canonical_url=f"https://{source}.x/o/{source_id}",
        title=title,
    )


class FakePoster:
    """Minimal ``requests.Session.post`` stand-in recording payloads."""

    def __init__(self, payload_text: str | None = None, *, status: int = 200, error: Exception | None = None) -> None:
        self.text = payload_text if isinstance(payload_text, str) else json.dumps(payload_text)
        self.status_code = status
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs) -> FakePoster:
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        self.status_code = self.status_code
        return self

    def json(self):
        return json.loads(self.text)


def chat_envelope(content: str) -> str:
    return json.dumps({"choices": [{"message": {"content": content}}]})


def ok_scorer(api_key: str = "k-test") -> OpenRouterScorer:
    return OpenRouterScorer(api_key=api_key)


def leads():
    return [
        make_lead("useme", "1", "Prosta integracja"),
        make_lead("nofluff", "alpha", "Python integracje API"),
    ]


def good_items():
    return [
        {"lead_key": 0, "score": 92, "bucket": "TOP", "reason": "Dobrze pasuje do profilu."},
        {"lead_key": 1, "score": 41, "bucket": "MOŻE", "reason": "Średnie dopasowanie."},
    ]


# ---------- bucket model ----------


def test_bucket_enum_covers_required_members():
    assert {b.value for b in ScoreBucket} == {"TOP", "MOŻE", "ODRZUĆ", "ZAMIEN"}


# ---------- ScoredLead validation ----------


def test_scored_lead_accepts_boundaries_0_and_100():
    lead = make_lead()
    ScoredLead(lead, 0, ScoreBucket.ODRZUC, "brak")
    ScoredLead(lead, 100, ScoreBucket.TOP, "świetne")


def test_scored_lead_rejects_out_of_range():
    for bad in (-1, 101):
        with pytest.raises(ValueError):
            ScoredLead(make_lead(), bad, ScoreBucket.TOP, "x")


def test_scored_lead_rejects_non_int_score():
    for bad in (True, 55.5, "92"):
        with pytest.raises((TypeError, ValueError)):
            ScoredLead(make_lead(), bad, ScoreBucket.TOP, "x")


def test_scored_lead_rejects_blank_rationale():
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            ScoredLead(make_lead(), 50, ScoreBucket.MOZE, bad)


def test_scored_lead_rejects_too_long_rationale():
    with pytest.raises(ValueError):
        ScoredLead(make_lead(), 50, ScoreBucket.MOZE, "r" * 501)


# ---------- deterministic offline scorer ----------


def test_deterministic_scorer_returns_one_per_lead_with_valid_fields():
    result = DeterministicScorer().score(leads())
    assert len(result) == 2
    for s in result:
        assert 0 <= s.score <= 100
        assert s.bucket in ScoreBucket
        assert s.rationale.strip()


def test_deterministic_scorer_is_reproducible():
    scorer = DeterministicScorer()
    assert [s.bucket for s in scorer.score(leads())] == [s.bucket for s in scorer.score(leads())]
    assert [s.score for s in scorer.score(leads())] == [s.score for s in scorer.score(leads())]


def test_deterministic_scorer_ignores_lead_text():
    """No keyword rejection: rewriting the title/description cannot change buckets."""
    plain = make_lead("useme", "k", "Zwykły awans")
    fancy = make_lead("useme", "k", "666 bodhisattwa sygnał trade")
    assert DeterministicScorer().score([plain])[0].bucket == DeterministicScorer().score([fancy])[0].bucket


def test_deterministic_scorer_empty_input():
    assert DeterministicScorer().score([]) == []


def test_user_policy_rejects_senior_roles_even_if_model_liked_them():
    lead = make_lead("justjoinit", "senior-1", "Senior DevOps Engineer")
    scored = apply_user_policy([ScoredLead(lead, 95, ScoreBucket.TOP, "technologie pasują")])
    assert scored[0].bucket is ScoreBucket.ODRZUC
    assert scored[0].score == 0


def test_user_policy_rejects_lead_developer_role():
    lead = make_lead("justjoinit", "lead-1", "Lead n8n Developer")
    scored = apply_user_policy([ScoredLead(lead, 95, ScoreBucket.TOP, "etat")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_client_order_about_lead_generation():
    """„Lead Generation” to temat zlecenia, nie stopień stanowiska (bug 2026-09-19)."""
    for title in (
        "Automatyzacja lead generation w n8n",
        "Lead Generation & CRM sync automation for our sales team",
        "Lead triage automation — n8n + Sheets",
        "n8n lead→Sheets→Slack pipeline for our sales team",
        "CRM sync, lead intake and internal tools",
    ):
        lead = make_lead("useme", title, title)
        scored = apply_user_policy([ScoredLead(lead, 80, ScoreBucket.TOP, "zlecenie")])
        assert scored[0].bucket is ScoreBucket.TOP, title


def test_user_policy_rejects_automation_provider_advertisement():
    lead = make_lead("useme", "provider-1", "Automatyzacje n8n dla firm")
    from dataclasses import replace

    lead = replace(lead, description="Oferuję usługi automatyzacji i szukam klientów.")
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "n8n")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_client_automation_project():
    lead = make_lead("useme", "client-1", "Integracja CRM z formularzem")
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "pasuje")])
    assert scored[0].bucket is ScoreBucket.TOP


# ---------- OpenRouter strict validation ----------


def test_openrouter_happy_path_scores_in_input_order():
    poster = FakePoster(chat_envelope(json.dumps(good_items())))
    scorer = OpenRouterScorer(api_key="sk-test", session=poster)
    result = scorer.score(leads())
    assert [(s.score, s.bucket.value) for s in result] == [(92, "TOP"), (41, "MOŻE")]
    assert result[0].lead.source_id == "1"


def test_openrouter_distinguishes_same_source_id_across_sources():
    same_ids = [make_lead("useme", "42"), make_lead("n8ndevs", "42")]
    items = [
        {"lead_key": 0, "score": 90, "bucket": "TOP", "reason": "Pierwsza."},
        {"lead_key": 1, "score": 60, "bucket": "MOŻE", "reason": "Druga."},
    ]
    poster = FakePoster(chat_envelope(json.dumps(items)))

    result = OpenRouterScorer(api_key="k", session=poster).score(same_ids)

    assert [(item.lead.source, item.score) for item in result] == [("useme", 90), ("n8ndevs", 60)]


def test_openrouter_scores_large_input_in_bounded_batches():
    class SequencePoster:
        def __init__(self):
            self.calls = []
            self.responses = [
                [{"lead_key": 0, "score": 80, "bucket": "TOP", "reason": "A"},
                 {"lead_key": 1, "score": 60, "bucket": "MOŻE", "reason": "B"}],
                [{"lead_key": 0, "score": 20, "bucket": "ODRZUĆ", "reason": "C"}],
            ]

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakePoster(chat_envelope(json.dumps(self.responses[len(self.calls) - 1])))

    poster = SequencePoster()
    batch = [make_lead("useme", str(i)) for i in range(1, 4)]

    result = OpenRouterScorer(api_key="k", session=poster, batch_size=2).score(batch)

    assert [item.lead.source_id for item in result] == ["1", "2", "3"]
    assert len(poster.calls) == 2
    assert all("response_format" in call[1]["json"] for call in poster.calls)


def test_openrouter_missing_api_key_raises_without_key_value(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    scorer = OpenRouterScorer(api_key=None, session=FakePoster(""))
    with pytest.raises(ScoreError) as excinfo:
        scorer.score(leads())
    assert "OPENROUTER_API_KEY" in str(excinfo.value)
    assert "sk-test" not in str(excinfo.value)


def test_openrouter_http_error_raises_score_error():
    poster = FakePoster("", status=429)
    scorer = OpenRouterScorer(api_key="k", session=poster)
    with pytest.raises(ScoreError, match="HTTP"):
        scorer.score(leads())


def test_openrouter_transport_error_raises_score_error():
    poster = FakePoster("", error=RuntimeError("net down"))
    scorer = OpenRouterScorer(api_key="k", session=poster)
    with pytest.raises(ScoreError, match="net down"):
        scorer.score(leads())


def test_openrouter_wrong_envelope_raises():
    # Not a chat.completions envelope at all.
    scorer = OpenRouterScorer(api_key="k", session=FakePoster(json.dumps({"error": "x"})))
    with pytest.raises(InvalidScoreResponse):
        scorer.score(leads())


def test_openrouter_content_must_be_json_array():
    scorer = OpenRouterScorer(api_key="k", session=FakePoster(chat_envelope("not json")))
    with pytest.raises(InvalidScoreResponse):
        scorer.score(leads())
    scorer = OpenRouterScorer(api_key="k", session=FakePoster(chat_envelope('{"scores": []}')))
    with pytest.raises(InvalidScoreResponse):
        scorer.score(leads())


def test_openrouter_object_wrapped_in_markdown_is_accepted():
    fenced = "```json\n" + json.dumps(good_items()) + "\n```"
    scorer = OpenRouterScorer(api_key="k", session=FakePoster(chat_envelope(fenced)))

    result = scorer.score(leads())

    assert len(result) == 2


def test_openrouter_count_mismatch_is_rejected():
    scorer = OpenRouterScorer(
        api_key="k", session=FakePoster(chat_envelope(json.dumps(good_items()[:1])))
    )
    with pytest.raises(InvalidScoreResponse):
        scorer.score(leads())


def test_openrouter_unknown_source_id_is_rejected():
    items = good_items()
    items[0]["lead_key"] = 99
    scorer = OpenRouterScorer(api_key="k", session=FakePoster(chat_envelope(json.dumps(items))))
    with pytest.raises(InvalidScoreResponse):
        scorer.score(leads())


def test_openrouter_out_of_range_score_is_rejected():
    items = good_items()
    items[0]["score"] = 150
    scorer = OpenRouterScorer(api_key="k", session=FakePoster(chat_envelope(json.dumps(items))))
    with pytest.raises(InvalidScoreResponse):
        scorer.score(leads())


def test_openrouter_float_integral_score_is_coerced():
    items = good_items()
    items[0]["score"] = 92.0
    scorer = OpenRouterScorer(api_key="k", session=FakePoster(chat_envelope(json.dumps(items))))
    result = scorer.score(leads())
    assert result[0].score == 92


def test_openrouter_unknown_bucket_is_rejected():
    items = good_items()
    items[0]["bucket"] = "KONIECZNIE"
    scorer = OpenRouterScorer(api_key="k", session=FakePoster(chat_envelope(json.dumps(items))))
    with pytest.raises(InvalidScoreResponse):
        scorer.score(leads())


def test_openrouter_blank_rationale_is_rejected():
    items = good_items()
    items[0]["reason"] = "   "
    scorer = OpenRouterScorer(api_key="k", session=FakePoster(chat_envelope(json.dumps(items))))
    with pytest.raises(InvalidScoreResponse):
        scorer.score(leads())


def test_openrouter_empty_input_does_not_touch_network():
    scorer = OpenRouterScorer(api_key=None)
    assert scorer.score([]) == []


def test_scorer_never_leaks_api_key_into_transport_exception():
    poster = FakePoster("", error=RuntimeError("Bearer sk-test-wrong3"))
    scorer = OpenRouterScorer(api_key="sk-secret", session=poster)
    with pytest.raises(ScoreError) as excinfo:
        scorer.score(leads())
    text = str(excinfo.value)
    assert "sk-secret" not in text


# ---------- simple ODRZUĆ audit ----------


def test_rejected_audit_lists_only_odrzuc():
    one = ScoredLead(make_lead(source_id="j"), 90, ScoreBucket.TOP, "tak")
    rejected = ScoredLead(make_lead(source_id="k"), 12, ScoreBucket.ODRZUC, "nie pasuje")
    maybe = ScoredLead(make_lead(source_id="l"), 44, ScoreBucket.MOZE, "może")
    text = render_rejected_audit([one, rejected, maybe])
    assert "ODRZUĆ" in text
    assert "nie pasuje" in text
    assert "k" in text
    assert "TOP" not in text
    assert "MOŻE" not in text


# ---------- provider-advertisement policy (EN + PL) ----------


def _with_description(lead: JobLead, description: str) -> JobLead:
    from dataclasses import replace

    return replace(lead, description=description)


def test_user_policy_rejects_for_hire_repair_advertisement():
    lead = _with_description(
        make_lead("n8n_community_jobs", "p1", "[For Hire] Fix one broken n8n workflow — US$40 fixed"),
        "I'll troubleshoot one contained fault in an existing n8n workflow: a failing node, "
        "webhook/API call, data mapping or output-format issue.",
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "naprawa n8n, pasuje")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_available_for_hire_service_pitch():
    lead = _with_description(
        make_lead("n8n_community_jobs", "p2", "Available for hire : Production AI Agents & n8n Automations"),
        "I am a full-stack developer available for freelance projects and contract work. "
        "My services include AI agents, lead capture and CRM sync.",
    )
    scored = apply_user_policy([ScoredLead(lead, 88, ScoreBucket.TOP, "AI agents")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_first_person_service_pitch_without_for_hire_marker():
    lead = _with_description(
        make_lead("make_hire_help", "p3", "Small Make scenario repairs from EUR 25"),
        "We offer small, fixed-scope Make repairs handled in writing: mapping, filters and "
        "data formatting for your existing scenario.",
    )
    scored = apply_user_policy([ScoredLead(lead, 85, ScoreBucket.TOP, "naprawa Make")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_platinum_partner_advertisement():
    lead = _with_description(
        make_lead("make_hire_help", "p4", "Make Platinum Partner | Help with Complex Make Scenarios"),
        "We're Msquare Automation. We've been building on Make back when it was still called Integromat.",
    )
    scored = apply_user_policy([ScoredLead(lead, 82, ScoreBucket.TOP, "partner Make")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_first_person_i_help_businesses_pitch():
    """Live false TOP 2026-09-19: „Small-business workflow automation | X Studio”."""
    lead = _with_description(
        make_lead(
            "make_hire_help",
            "p5",
            "Small-business workflow automation, integrations & practical AI | MethodMade Studio",
        ),
        "I'm Amber Dunn, founder of MethodMade Studio. I help small businesses untangle "
        "workflows that have gradually become a mix of spreadsheets and mailboxes.",
    )
    scored = apply_user_policy([ScoredLead(lead, 80, ScoreBucket.TOP, "workflow automation")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_i_run_a_lab_pitch():
    """Live false TOP 2026-09-19 (2nd run): „LEVDELUS LAB — one small Make workflow fix”."""
    lead = _with_description(
        make_lead("make_hire_help", "p6", "LEVDELUS LAB — one small Make workflow fix, US$30 fixed"),
        "Hi — I run LEVDELUS LAB, a small automation lab. I take one bounded Make workflow "
        "problem at a time: broken scenario, webhook/API connection, Google Sheets mapping.",
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "naprawa Make")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_i_build_scenarios_for_clients_pitch():
    lead = _with_description(
        make_lead("make_hire_help", "p7", "Make.com automation for SMEs in Switzerland & DACH"),
        "I'm Wilhelm, founder of core.iq GmbH. I build and maintain Make scenarios for small "
        "and mid-sized businesses, mostly order flows and Shopify integrations.",
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "Make dla SME")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_client_who_is_a_founder_of_a_restaurant():
    lead = _with_description(
        make_lead("useme", "c3", "Automatyzacja zamówień w restauracji"),
        "Jestem właścicielem małej restauracji i szukam kogoś, kto połączy formularz "
        "zamówień z arkuszem oraz wyśle potwierdzenia do klientów.",
    )
    scored = apply_user_policy([ScoredLead(lead, 85, ScoreBucket.TOP, "integracja")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_rejects_i_take_on_fixed_price_pitch():
    """Live false TOP 2026-09-19 (3rd run): „Make + n8n automation debugging — fixed price”."""
    lead = _with_description(
        make_lead("make_hire_help", "p8", "Make + n8n automation debugging — diagnose first, fixed price"),
        "I take on small, bounded automation problems in Make (and n8n), and I start with a "
        "diagnosis rather than quoting the whole build.",
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "debugowanie Make")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_client_looking_for_a_freelancer():
    """A client asking for a freelancer must never be read as a provider ad."""
    lead = _with_description(
        make_lead("n8n_community_jobs", "c1", "N8n Freelancer needed for workflow"),
        "I've been playing around n8n for an urgent project of mine, but I'm afraid I'm stuck. "
        "I'm happy to pay for help at this point. It's a simple workflow but I can't finish it.",
    )
    scored = apply_user_policy([ScoredLead(lead, 92, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_keeps_client_integration_order():
    lead = _with_description(
        make_lead("useme", "c2", "Integracja API systemu hotelowego z Google BigQuery"),
        "Szukamy programisty, który przygotuje skrypt cyklicznie pobierający dane z REST API "
        "i zapisujący je do BigQuery.",
    )
    scored = apply_user_policy([ScoredLead(lead, 70, ScoreBucket.MOZE, "integracja")])
    assert scored[0].bucket is ScoreBucket.MOZE


# ---------- live misses 2026-09-20: „free help” / „I run X and help” pitches ----------
# Real bodies from the 2026-09-20 05:30 run; all five came back as TOP 100 and
# were delivered to Telegram. Bodies are trimmed only in length.


def test_user_policy_rejects_free_help_pitch():
    """Live TOP 2026-09-20: „FREE Help for Make.com Users Facing Errors…”."""
    lead = _with_description(
        make_lead(
            "make_hire_help",
            "p9",
            "FREE Help for Make.com Users Facing Errors, Broken Scenarios or Complex Workflows",
        ),
        "Hello MAKERS, Let's MAKE chaos into system! I am offering LIFETIME FREE HELP for anyone "
        "stuck with Make.com scenarios or automation workflows. I can review your setup and "
        "suggest fixes or a cleaner workflow design. Feel free to DM me or share your scenario "
        "here anytime and I will take a look and help where I can.",
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "help z Make")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_i_run_a_business_and_help_with_make_pitch():
    """Live TOP 2026-09-20: „Need help with a Make scenario or workflow automation?”."""
    lead = _with_description(
        make_lead(
            "make_hire_help",
            "p10",
            "Need help with a Make scenario or workflow automation?",
        ),
        "Hi everyone, I'm Dr Tanvi, a PhD in Business / IT. I run Tuesday Wizards and help "
        "businesses get more value from Make by turning manual processes into simple automations. "
        "Most of the work I do is helping people design or fix Make scenarios. If you are "
        "currently trying to automate something and feel stuck, I'm happy to take a look and talk "
        "through possible options. You can book a strategy session here if you'd like to go "
        "through your setup together.",
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "pomoc z Make")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_automation_developer_with_calendly_pitch():
    """Live TOP 2026-09-20: „Filevine and Legal Case Management Automations with Make.com”."""
    lead = _with_description(
        make_lead(
            "make_hire_help",
            "p11",
            "Filevine and Legal Case Management Automations with Make.com",
        ),
        "Hi everyone, I'm an automation developer focused on building integrations and workflows "
        "for law firms and legal operations teams using Make.com and related tools. Typical "
        "projects I support include automating legal intake and case creation and fixing existing "
        "automations that aren't behaving reliably. If you're working on Filevine or other "
        "law-oriented automations, feel free to reply or find a time on my Calendly.",
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "automatyzacje dla kancelarii")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_i_automate_your_wordpress_lifecycle_pitch():
    """Live TOP 2026-09-20: „WordPress Content Automation, SEO & Social Media Publishing”."""
    lead = _with_description(
        make_lead(
            "make_hire_help",
            "p12",
            "WordPress Content Automation, SEO & Social Media Publishing",
        ),
        "I automate your entire WordPress content lifecycle with Make: automatic publishing, SEO "
        "optimisation, internal linking, tag management and multi-platform social media "
        "auto-sharing. I build Make.com automation systems that handle everything for you. What I "
        "can do for you: automatic publications, auto-publish posts from Google Docs, Airtable or "
        "Notion, draft or direct publish with featured image, slug, categories and tags.",
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "automatyzacja WordPress")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_client_forum_question_without_a_pitch():
    """Live TOP 2026-09-20: „Run with existing data but different” — realne pytanie klienta."""
    lead = _with_description(
        make_lead("make_hire_help", "c4", "Run with existing data but different"),
        "Hey guys. Is there a way to run previous webhooks but with different data? That is, with "
        "the option of running the same webhook but just changing its phone number? What happens "
        "today is that I have to go to external systems like Postman and it's super inefficient.",
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "pytanie klienta o Make")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_keeps_client_offering_a_budget():
    """A client stating his own budget („I'm offering 500 USD”) is not a provider ad."""
    lead = _with_description(
        make_lead("n8n_community_jobs", "c5", "Looking for an n8n developer for a lead intake flow"),
        "We're looking for someone who can build a lead intake and booking workflow in n8n. I'm "
        "offering 500 USD for the whole build if it covers everything we discussed.",
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


# ---------- money: hourly rates and fixed budgets ----------


def _rated(lead: JobLead, *, low: float, high: float, unit: str, currency: str = "USD") -> JobLead:
    from dataclasses import replace

    return replace(
        lead,
        budget=low,
        currency=currency,
        raw_data={"rate_min": low, "rate_max": high, "rate_unit": unit},
    )


def test_user_policy_rejects_joke_hourly_rate():
    """Jan 2026-09-19: „$15 za godzinę to żart”."""
    lead = _rated(make_lead("n8ndevs", "rate-1", "AI Automation LLM - Prototype Development"),
                  low=15, high=15, unit="hour")
    scored = apply_user_policy([ScoredLead(lead, 80, ScoreBucket.TOP, "MVP agenta AI")])
    assert scored[0].bucket is ScoreBucket.ODRZUC
    assert "USD/h" in scored[0].rationale and "zł/h" in scored[0].rationale


def test_user_policy_demotes_marginal_hourly_rate_from_top():
    """24–30 USD/h to powyżej progu (23), ale poniżej komfortu (38)."""
    lead = _rated(make_lead("n8ndevs", "rate-2", "AI Engineer - Document Automation"),
                  low=24, high=30, unit="hour")
    scored = apply_user_policy([ScoredLead(lead, 80, ScoreBucket.TOP, "PoC dla konsultingu")])
    assert scored[0].bucket is ScoreBucket.MOZE
    assert scored[0].score == 80  # demoted, not deleted
    assert "poniżej komfortowej" in scored[0].rationale


def test_user_policy_keeps_good_hourly_rate_in_top():
    lead = _rated(make_lead("n8ndevs", "rate-3", "Real Estate Underwriting System"),
                  low=19, high=40, unit="hour")
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "konkretne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_demotes_small_fixed_project_from_top():
    lead = _rated(make_lead("n8ndevs", "rate-4", "Fix Reproducible n8n Bug"),
                  low=49, high=49, unit="project", currency="EUR")
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "naprawa buga")])
    assert scored[0].bucket is ScoreBucket.MOZE
    assert "niski ticket" in scored[0].rationale


def test_user_policy_keeps_meaningful_fixed_project_in_top():
    lead = _rated(make_lead("n8ndevs", "rate-5", "OpenAI Assistant API integration"),
                  low=250, high=250, unit="project")
    scored = apply_user_policy([ScoredLead(lead, 85, ScoreBucket.TOP, "integracja chatbota")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_leaves_pln_budget_without_rate_metadata_alone():
    """Useme budgets carry no hourly/project pill — never money-gate them."""
    lead = make_lead("useme", "pln-1", "Automatyzacja faktur")
    from dataclasses import replace

    lead = replace(lead, budget=200.0, currency="PLN")
    scored = apply_user_policy([ScoredLead(lead, 95, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_rejects_employment_wording_in_the_ad_itself():
    """Live 2026-09-19: TOP 100 with a rationale saying „to oferta pracy”."""
    lead = make_lead(
        "n8n_community_jobs",
        "emp-2",
        "Hiring: Automation Developer (n8n, Make.com, GoHighLevel) - full-time, remote from India",
    )
    from dataclasses import replace

    lead = replace(
        lead,
        description="Full-time, permanent, remote. Salary range INR 9.6-13.2L per annum.",
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "technologie pasują")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_salary_vocabulary_without_full_time_title():
    lead = make_lead("useme", "emp-3", "Automatyk do biura")
    from dataclasses import replace

    lead = replace(lead, description="Oferujemy umowę o pracę i stałą pensję miesięczną.")
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "automatyzacja biura")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_part_time_client_gig():
    """„Part-time help wanted” to zlecenie od klienta, nie etat."""
    lead = make_lead("make_hire_help", "pt-1", "Part-time help needed with our Make scenarios")
    from dataclasses import replace

    lead = replace(
        lead,
        description="We need someone a few hours a week to keep our Make scenarios working.",
    )
    scored = apply_user_policy([ScoredLead(lead, 85, ScoreBucket.TOP, "drobne wsparcie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_rejects_employment_contract_type_from_the_source():
    """Live 2026-09-19: „Full-Time AI Automation Engineer — $80-$100/hr” came out as MOŻE."""
    lead = make_lead("n8ndevs", "emp-1", "Full-Time AI Automation Engineer - $80-$100/hr")
    from dataclasses import replace

    lead = replace(
        lead,
        contract_type="Employment · Full-time",
        budget=80.0,
        currency="USD",
        raw_data={"rate_min": 80.0, "rate_max": 100.0, "rate_unit": "hour"},
    )
    scored = apply_user_policy([ScoredLead(lead, 70, ScoreBucket.MOZE, "wysoka stawka")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_freelance_contract_type():
    lead = make_lead("n8ndevs", "free-1", "n8n + AI Agent Developer for Production Systems")
    from dataclasses import replace

    lead = replace(lead, contract_type="Freelance")
    scored = apply_user_policy([ScoredLead(lead, 85, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_rejects_third_person_provider_ad():
    """Live miss 2026-09-19: n8ndevs rewrites provider ads into the third person."""
    lead = make_lead("n8ndevs", "ahmed-1", "n8n Production Systems - Real-Time Alerts & Integrations")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "Ahmed builds n8n systems for production. His systems monitor ERP databases, "
            "pushing Telegram alerts within three seconds. Services offered include: one "
            "workflow for $95, complete production build at $250 fixed. He is available "
            "remotely from Turkey."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 70, ScoreBucket.TOP, "alerty i integracje")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_client_who_is_described_third_person():
    """Klient opisany w trzeciej osobie przez feed nie może odpaść."""
    lead = make_lead("n8ndevs", "client-9", "Automate Gmail Responses for Fitness Business")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "A small fitness studio receives 5-10 emails daily with common questions. "
            "They are looking for a solution to automatically respond to these inquiries."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "automatyzacja maili")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_rejects_pilot_partnership_where_the_author_works():
    """R11, live 2026-09-19 — Jan's decision: „Odrzuć”."""
    lead = make_lead("n8n_community_jobs", "pilot-1", "Looking for 2-3 agencies running AI agents")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "I built the policy layer I keep going on about in the agent safety threads. "
            "It works. So I'm looking for two or three agencies running AI agents in "
            "production for clients. What you get: I do the integration myself. Not a docs "
            "link and good luck — I map your actions, write the policy and verify it."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "pilotaż dla agencji")])
    assert scored[0].bucket is ScoreBucket.ODRZUC
    assert "self" in scored[0].rationale or "autor sam" in scored[0].rationale


def test_user_policy_rejects_available_for_freelance_projects_pitch():
    """Live 2026-09-19 (last run): „…developer available for freelance projects” in TOP."""
    lead = make_lead("n8ndevs", "avail-1", "n8n AI Automation Developer - Lead Generation & Workflow Integrations")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "Experienced AI automation developer available for freelance projects. "
            "I build AI agents and automate workflows that enhance lead generation."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 80, ScoreBucket.TOP, "AI agent + lead gen")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_client_asking_for_an_agency_to_do_the_work():
    """A client wanting an agency to build FOR HIM must survive the new rule."""
    lead = make_lead("useme", "c5", "Szukamy agencji do wdrożenia automatyzacji w naszym sklepie")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "Prowadzimy sklep i szukamy wykonawcy, który zbuduje nam integrację z "
            "magazynem i wyśle potwierdzenia zamówień. Budżet 4000 PLN."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_rejects_full_stack_engineer_pitching_services():
    """Live/golden miss 2026-09-19: „I'm a full-stack engineer who can build…”."""
    lead = make_lead("make_hire_help", "p9", "Full-Stack Engineer for APIs, Backends & Custom Software")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "I'm a full-stack engineer who can build the custom software around your "
            "automation when Make alone isn't enough. I can help with custom REST APIs, "
            "webhooks, OAuth and PostgreSQL."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "custom software wokół Make")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_a_client_who_happens_to_be_a_developer():
    """A client may be a developer themselves — self-description alone is no reject."""
    lead = make_lead("useme", "c4", "Automatyzacja raportów sprzedażowych")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "Jestem developerem, ale nie mam czasu tego robić sam. Potrzebuję kogoś, kto "
            "połączy nasze API z arkuszem i wyśle raport codziennie rano."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 85, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_keeps_agency_that_is_buying():
    """R5, live 2026-09-19: NUBO is an agency LOOKING FOR a builder — a client."""
    lead = make_lead("n8n_community_jobs", "nubo-1", "[ESPAÑOL] Looking for an n8n + AI Automation Builder")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "We're NUBO, a small AI implementation agency building AI agents and automations. "
            "We're looking for an experienced n8n builder to work with us on an ongoing, "
            "project-by-project basis as our workload grows."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "agencja szuka wykonawcy")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_still_rejects_agency_selling_services():
    lead = make_lead("make_hire_help", "agency-1", "Make.com automation for SMEs")
    from dataclasses import replace

    lead = replace(
        lead,
        description="We are an automation agency. We help businesses streamline operations. Get in touch.",
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "Make dla SME")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


# ---------- prompt calibration ----------


def test_score_prompt_embeds_the_few_shot_examples_for_every_bucket():
    prompt = build_score_prompt(leads())
    assert "few-shot" in prompt.lower()
    examples = prompt[prompt.index("Przykłady poprawnych ocen") : prompt.index("Odpowiedz wyłącznie")]
    # 2026-09-21: three Polish examples joined the calibration set, one of them
    # for the new ZAMIEN bucket (automate instead of hiring a person).
    assert examples.count('"bucket"') == 16
    assert examples.count('"ODRZUĆ"') == 7
    assert examples.count('"TOP"') == 8
    assert examples.count('"ZAMIEN"') == 1
    assert "Zapytanie:" in prompt
    assert '"lead_key": 0' in prompt


def test_default_batch_size_is_ten():
    assert OpenRouterScorer(api_key="k").batch_size == 10


# ---------- malformed-payload recovery ----------


def test_openrouter_retries_a_malformed_payload_once_and_recovers():
    """Live 2026-09-19: one truncated response lost a whole run."""

    class OnceBrokenPoster:
        def __init__(self):
            self.calls = 0

        def post(self, url, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakePoster(chat_envelope('{"broken"'))
            return FakePoster(chat_envelope(json.dumps(good_items())))

    poster = OnceBrokenPoster()
    result = OpenRouterScorer(api_key="k", session=poster).score(leads())

    assert poster.calls == 2
    assert [(entry.lead.source_id, entry.bucket.value) for entry in result] == [
        ("1", "TOP"),
        ("alpha", "MOŻE"),
    ]


def test_openrouter_still_fails_when_every_attempt_is_malformed():
    poster = FakePoster(chat_envelope("not json"))
    with pytest.raises(InvalidScoreResponse):
        OpenRouterScorer(api_key="k", session=poster, max_attempts=3).score(leads())
    assert len(poster.calls) == 3


# ---------- portfolio gap (backlog PoC) ----------


def test_scored_lead_rejects_an_overlong_gap():
    with pytest.raises(ValueError):
        ScoredLead(make_lead(), 80, ScoreBucket.TOP, "powód", "x" * 300)


def test_scored_lead_treats_blank_gap_as_none():
    assert ScoredLead(make_lead(), 80, ScoreBucket.TOP, "powód", "   ").gap is None


def test_user_policy_keeps_the_gap_when_it_demotes_and_rejects():
    from dataclasses import replace

    lead = replace(
        make_lead("n8ndevs", "gap-1", "Fix Reproducible n8n Bug"),
        budget=49.0,
        currency="EUR",
        raw_data={"rate_min": 49.0, "rate_max": 49.0, "rate_unit": "project"},
    )
    demoted = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "naprawa", "brak case study n8n fix")])
    assert demoted[0].bucket is ScoreBucket.MOZE
    assert demoted[0].gap == "brak case study n8n fix"

    provider = replace(make_lead("make_hire_help", "gap-2", "Make.com automation for SMEs"), description="We are an automation agency. Get in touch.")
    rejected = apply_user_policy([ScoredLead(provider, 90, ScoreBucket.TOP, "Make", "brak case study Make")])
    assert rejected[0].bucket is ScoreBucket.ODRZUC
    assert rejected[0].gap == "brak case study Make"


def test_score_prompt_carries_the_live_portfolio_and_the_gap_field():
    prompt = build_score_prompt(leads())
    assert "PORTFOLIO JANA" in prompt
    assert "portfolio_gap" in prompt
    assert "jnxautomation.pl" in prompt


# ---------- wymagany język obcy (decyzja Jana 2026-09-19) ----------


def test_user_policy_rejects_a_required_foreign_language():
    lead = make_lead("n8n_community_jobs", "lang-1", "[ESPAÑOL] Looking for an n8n builder")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "We're NUBO, a small AI implementation agency. Fluent Spanish is required, as "
            "most of our projects are with Spanish-speaking clients. English sufficient "
            "to work with technical documentation."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "agencja szuka wykonawcy")])
    assert scored[0].bucket is ScoreBucket.ODRZUC
    assert "języka" in scored[0].rationale


def test_user_policy_rejects_polish_requirement_for_a_foreign_language():
    lead = make_lead("useme", "lang-2", "Automatyzacja dla klienta niemieckiego")
    from dataclasses import replace

    lead = replace(lead, description="Wymagana biegła znajomość języka niemieckiego w mowie i piśmie.")
    scored = apply_user_policy([ScoredLead(lead, 80, ScoreBucket.TOP, "automatyzacja")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_english_or_polish_requirement():
    lead = make_lead("useme", "lang-3", "Automatyzacja raportów")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "Wymagana dobra znajomość języka angielskiego (dokumentacja) i polskiego. "
            "Klient pracuje w Warszawie."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 85, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_keeps_an_offer_that_only_mentions_foreign_clients():
    """„Klienci mówią po hiszpańsku, ale praca po angielsku” to nie bloker."""
    lead = make_lead("n8n_community_jobs", "lang-4", "Automatyzacja obsługi klientów z Hiszpanii")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "We serve Spanish-speaking customers, but the whole project is delivered in "
            "English — no Spanish needed on your side."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 85, ScoreBucket.TOP, "zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_hourly_floor_and_comfort_come_from_pln():
    from freelance_pipeline.scoring import COMFORT_HOURLY_PLN, MIN_HOURLY_PLN

    assert MIN_HOURLY_PLN == 90.0
    assert COMFORT_HOURLY_PLN == 150.0


def test_user_policy_rejects_vendor_marketing_question_title():
    """Golden reject-42 (EISI SOFT): „Do you want to automate processes? We are your choice”."""
    lead = make_lead("make_hire_help", "vendor-q", "Do you want to automate processes? We are your choice")
    from dataclasses import replace

    lead = replace(
        lead,
        description=(
            "At EISI SOFT we live at the intersection of this can be automated and why has "
            "not anyone done this before. We have been in the software development business "
            "for years."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 80, ScoreBucket.MOZE, "automatyzacja procesów")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_a_client_question_title_without_a_pitch():
    lead = make_lead("useme", "client-q", "Chcesz zautomatyzować wystawianie faktur?")
    from dataclasses import replace

    lead = replace(
        lead,
        description="Prowadzę biuro rachunkowe i szukam wykonawcy, który połączy nasze narzędzia.",
    )
    scored = apply_user_policy([ScoredLead(lead, 85, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


# ---------- polskie zlecenia: zakres, nie słowo „automatyzacja” (2026-09-21) ----------


CODEX_TEXT = (
    "Poszukuje kogoś kto pomoże mi/skonfiguruje za mnie wszystko. Potrzebuje łącznik w "
    "konfiguracji Codexa z moim serwerem i domeną aby działała strona zrobiona w Site GPT "
    "oraz przedstawi jak działać aby każde zmiany na stronie publikować na bieżąco. "
    "Potrzebuje domenę skonfigurować aby mi skrzynki e-mail nie sparaliżowało. Dodatkowo: "
    "pomóc wdrożyć ścieżkę rozbudowy strony www i automatyzacja bloga firmowego."
)

POS_TEXT = (
    "Senior POS Developer / Android Integration – React, SUNMI, fiskalizacja, offline. "
    "Szukam doświadczonego programisty do dalszego rozwoju własnego systemu POS dla "
    "gastronomii. Zakres prac: natywna warstwa Android dla urządzeń SUNMI, integracja z "
    "drukarkami fiskalnymi Posnet / Novitus, obsługa SDK producentów urządzeń."
)


def test_user_policy_keeps_a_polish_configuration_commission():
    """Werdykt Jana 2026-09-21: konfiguracja hostingu + Codex + Site GPT = keep.

    The old model rationale („zadanie administracyjne, nie automatyzacja”) is
    exactly what this test locks out: the policy must not reject a Polish
    commission for lacking the word „automatyzacja”.
    """
    from dataclasses import replace

    lead = replace(
        make_lead("useme", "pl-1", "Konfiguracja: Hosting z Codex i Site GPT [Dla nie technicznej osoby]"),
        description=CODEX_TEXT,
        company_or_author="Adam",
    )
    scored = apply_user_policy([ScoredLead(lead, 82, ScoreBucket.TOP, "klient nietechniczny, zakres jasny")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_rejects_native_hardware_work_out_of_scope():
    """Werdykt Jana 2026-09-21: aplikacja POS = reject (Android, drukarki, SDK)."""
    from dataclasses import replace

    lead = replace(
        make_lead("useme", "pl-2", "Aplikacja pos dla gastronomii"),
        description=POS_TEXT,
    )
    scored = apply_user_policy([ScoredLead(lead, 20, ScoreBucket.ODRZUC, "poza zakresem")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_a_landing_page_commission_is_not_rejected_by_the_policy():
    """A fresh landing-page ad stays in play; Jan's Perca „reject” came from age."""
    from dataclasses import replace

    lead = replace(
        make_lead("useme", "pl-3", "Firma Perca zleci wykonanie Landing page na swoją stronę"),
        description=(
            "Firma Perca jest producentem stołów, krzeseł i małej tapicerki klasy premium. "
            "Poszukujemy specjalisty do wykonania landing pagea na naszą stronę."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 75, ScoreBucket.TOP, "strona w zakresie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_device_work_is_rejected_even_when_the_model_hedges_to_moze():
    """Golden „Aplikacja pos dla gastronomii”: model wrote „poza zakresem”, gave MOŻE."""
    from dataclasses import replace

    lead = replace(make_lead("useme", "pl-2b", "Aplikacja pos dla gastronomii"), description=POS_TEXT)
    scored = apply_user_policy([ScoredLead(lead, 50, ScoreBucket.MOZE, "może wykraczać")])
    assert scored[0].bucket is ScoreBucket.ODRZUC
    assert "urządzeniami" in scored[0].rationale


def test_device_rule_does_not_touch_browser_automation():
    """Guard against false positives: Playwright work is software, not hardware."""
    from dataclasses import replace

    lead = replace(
        make_lead("n8ndevs", "pl-2c", "Proof of Concept: n8n & Claude Browser Automation"),
        description=(
            "A developer is needed to create a proof of concept workflow in n8n utilizing "
            "Claude and a browser automation tool like Playwright, exporting the data to "
            "Google Sheets."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "PoC")])
    assert scored[0].bucket is ScoreBucket.TOP


# ---------- reklamy wykonawców złapane na żywo 2026-09-21 (przebieg kontrolny) ----------


def test_third_person_provider_ad_is_rejected():
    """„Khadijat … is looking for freelance projects … What I work with …”."""
    from dataclasses import replace

    lead = replace(
        make_lead("n8ndevs", "seller-1", "AI Automation & n8n Workflow Builder - Freelance Opportunities"),
        description=(
            "Khadijat, an AI Automation Specialist and n8n workflow builder, is looking for "
            "freelance projects, contract work, and remote collaborations. With expertise in "
            "building automation systems, Khadijat helps businesses reduce repetitive tasks. "
            "What I work with: n8n workflow automation, AI integrations, OpenAI API. "
            "Portfolio: Visit here for examples of previous systems built."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "konkretne usługi")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_company_services_ad_is_rejected():
    """„RAR 121 can help. We specialize in … We offer services including …”."""
    from dataclasses import replace

    lead = replace(
        make_lead("n8ndevs", "seller-2", "RAR 121 - Business Process Improvement and Automation Services"),
        description=(
            "If your business processes aren't functioning optimally, RAR 121 can help. We "
            "specialize in identifying inefficiencies, such as repetitive tasks, disjointed "
            "workflows, or lost data opportunities. We offer services including business "
            "process automation, n8n workflow design, lead management and API integrations."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "firma szuka wykonawcy")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_first_person_automation_pitch_is_rejected():
    """„I build AI-powered WhatsApp automation … I can provide a live demo”."""
    from dataclasses import replace

    lead = replace(
        make_lead(
            "make_hire_help",
            "seller-3",
            "I build AI-powered WhatsApp lead qualification automations for real estate businesses",
        ),
        description=(
            # Exactly what the Make adapter hands the model: an excerpt truncated
            # at „… hand qualified leads &hellip;”, so the ad must be caught on its
            # title, not on the sentences that never arrive.
            "Need to automate your real estate enquiries on WhatsApp? \n"
            "I build AI-powered WhatsApp automation for real estate businesses that helps "
            "respond to enquiries, qualify potential buyers and renters, and hand qualified "
            "leads &hellip;"
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "automatyzacja WhatsApp")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_a_client_looking_for_freelance_help_is_not_rejected():
    """Guard: buyer language protects a real commission from the new patterns."""
    from dataclasses import replace

    lead = replace(
        make_lead("n8ndevs", "seller-4", "We are looking for freelance help with an n8n workflow"),
        description=(
            "We are looking for freelance help: our order confirmations are typed by hand "
            "into the ERP and we want that automated. We have a budget and a clear scope."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 85, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_a_client_asking_someone_to_build_for_their_business_is_not_rejected():
    """Guard for the „I build … for businesses” pattern: a buyer is never hit."""
    from dataclasses import replace

    lead = replace(
        make_lead("make_hire_help", "seller-5", "Szukamy wykonawcy do automatyzacji WhatsApp"),
        description=(
            "We are looking for someone to build AI-powered WhatsApp automation for our "
            "business: enquiries come in all day and nobody qualifies them. Budget is agreed."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


# ---------- reguła wieku ogłoszenia (decyzja Jana 2026-09-21) ----------


def test_user_policy_rejects_a_listing_that_is_ten_days_old():
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    lead = replace(
        make_lead("useme", "age-1", "Firma Perca zleci wykonanie Landing page na swoją stronę"),
        description="Poszukujemy specjalisty do wykonania landing pagea na naszą stronę.",
        published_at=datetime.now(UTC) - timedelta(days=MAX_LISTING_AGE_DAYS),
    )
    scored = apply_user_policy([ScoredLead(lead, 88, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.ODRZUC
    assert "10 dni" in scored[0].rationale and "przebrana" in scored[0].rationale


def test_user_policy_leaves_a_fresh_listing_alone():
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    lead = replace(
        make_lead("useme", "age-2", "Firma Perca zleci wykonanie Landing page na swoją stronę"),
        description="Poszukujemy specjalisty do wykonania landing pagea na naszą stronę.",
        published_at=datetime.now(UTC) - timedelta(days=2),
    )
    scored = apply_user_policy([ScoredLead(lead, 88, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_age_rule_is_silent_when_the_source_has_no_date():
    """Useme prints the publication date on the offer page, not in the listing."""
    lead = make_lead("useme", "age-3", "Firma Perca zleci wykonanie Landing page")
    scored = apply_user_policy([ScoredLead(lead, 88, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP
    assert scored[0].lead.published_at is None


# ---------- czwarty kubełek: ZAMIEN (automatyzacja zamiast zatrudnienia) ----------


HIRING_REPETITIVE_TEXT = (
    "Poszukuję pracownika na kilka godzin dziennie. Codziennie przychodzi około 40 faktur "
    "na maila i trzeba je przepisywać do naszego programu księgowego oraz wypełniać arkusze "
    "z rozliczeniami. Praca jest stała i powtarzalna."
)


def test_zamien_survives_for_a_hiring_ad_about_repetitive_work():
    from dataclasses import replace

    lead = replace(
        make_lead("useme", "z-1", "Szukam osoby do przepisywania danych z faktur"),
        description=HIRING_REPETITIVE_TEXT,
    )
    scored = apply_user_policy([ScoredLead(lead, 61, ScoreBucket.ZAMIEN, "da się zastąpić automatyzacją")])
    assert scored[0].bucket is ScoreBucket.ZAMIEN
    assert scored[0].score == 61
    assert scored[0].rationale == "da się zastąpić automatyzacją"


def test_zamien_beats_the_employment_rule_because_that_is_the_whole_point():
    """„umowa o pracę” na stanowisku do przepisywania faktur to właśnie ZAMIEN."""
    from dataclasses import replace

    lead = replace(
        make_lead("useme", "z-2", "Szukam pracownika, umowa o pracę"),
        description=(
            "Szukam pracownika na umowę o pracę, pełny etat, do przepisywania danych z "
            "faktur do arkusza. Praca biurowa, powtarzalna."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 55, ScoreBucket.ZAMIEN, "praca powtarzalna")])
    assert scored[0].bucket is ScoreBucket.ZAMIEN


def test_zamien_is_never_downgraded_by_the_money_rule():
    from dataclasses import replace

    lead = replace(
        make_lead("useme", "z-3", "Szukam osoby do wprowadzania danych"),
        description=HIRING_REPETITIVE_TEXT,
        raw_data={"rate_min": 10.0, "rate_max": 15.0, "rate_unit": "hour"},
        currency="USD",
    )
    scored = apply_user_policy([ScoredLead(lead, 50, ScoreBucket.ZAMIEN, "praca powtarzalna")])
    assert scored[0].bucket is ScoreBucket.ZAMIEN


def test_zamien_without_repetitive_work_falls_back_to_reject():
    """Real case: „Tester manualny…” (golden reject-80) must not become a signal."""
    from dataclasses import replace

    lead = replace(
        make_lead("useme", "z-4", "Tester manualny do testowania serwisu aukcyjnego"),
        description=(
            "Poszukuję testera manualnego do przetestowania rozwijanego serwisu aukcyjnego. "
            "Testy będą prowadzone w środowisku testowym: tworzenie i dokumentowanie "
            "scenariuszy testowych."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 58, ScoreBucket.ZAMIEN, "etat")])
    assert scored[0].bucket is ScoreBucket.ODRZUC
    assert "pracy powtarzalnej" in scored[0].rationale


def test_zamien_from_a_provider_ad_is_rejected():
    from dataclasses import replace

    lead = replace(
        make_lead("n8ndevs", "z-5", "I automate your data entry"),
        description=(
            "I run a small automation lab and help businesses. I automate your data entry "
            "and I answer your emails. Reach out and book a call."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.ZAMIEN, "usługi")])
    assert scored[0].bucket is ScoreBucket.ODRZUC
    assert "reklama wykonawcy" in scored[0].rationale


def test_zamien_requiring_a_foreign_language_is_rejected():
    from dataclasses import replace

    lead = replace(
        make_lead("useme", "z-6", "Szukam osoby do przepisywania faktur"),
        description=(
            "Szukam pracownika do przepisywania danych z faktur. Wymagana biegła znajomość "
            "języka niemieckiego w mowie i piśmie."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 55, ScoreBucket.ZAMIEN, "praca powtarzalna")])
    assert scored[0].bucket is ScoreBucket.ODRZUC
    assert "języka" in scored[0].rationale


def test_zamien_rows_stay_out_of_the_rejected_audit():
    from dataclasses import replace

    lead = replace(make_lead("useme", "z-7", "Szukam osoby do wprowadzania danych"), description=HIRING_REPETITIVE_TEXT)
    audit = render_rejected_audit([ScoredLead(lead, 61, ScoreBucket.ZAMIEN, "automatyzacja")])
    assert audit == ""


# ------------------------------------------------------------------- wzorce grupowe FB
# Etap 3 kalibracji (2026-09-21): 97 czystych postów z grup Facebooka, werdykty Jana
# przyjęte przez agenta. Te testy pinują granicę między reklamą wykonawcy a klientem
# w polskich grupach — w szczególności to, czego reguły ŚWIADOMIE nie łapią.

def test_polish_seller_forms_are_rejected():
    """Reklamy wykonawcy z polskich grup: „Oferuję…”, „Tworzymy…”, „szukam zleceń”."""
    cases = {
        "p-1": "Szukasz usprawnień dla swojego biznesu? Oferuję kompleksowe automatyzacje — od audytu po gotowe rozwiązanie. CRM, agenci, chatboty.",
        "p-2": "Prowadzisz małą firmę i tonie w arkuszach? Podejmuję się jednorazowych zleceń freelance. Mogę pomóc w zakresie: Excel, raportowanie danych, fakturowanie.",
        "p-3": "Potrzebujesz strony internetowej dla swojej firmy? Tworzymy nowoczesne strony. Co oferujemy? Profesjonalne realizacje.",
        "p-4": "Witam, projektuję domy prefabrykowane, szukam zleceń cała Polska, zapraszam do kontaktu.",
        "p-5": "TANIE STRONY INTERNETOWE – OD 300 ZŁ! Strona wizytówka 300 zł.",
        "p-6": "Montuję krótkie i długie treści pod social media, moje portfolio poniżej.",
    }
    for source_id, text in cases.items():
        lead = make_lead("facebook_groups", source_id, text)
        scored = apply_user_policy([ScoredLead(lead, 85, ScoreBucket.TOP, "wygląda na zlecenie")])
        assert scored[0].bucket is ScoreBucket.ODRZUC, text


def test_group_noise_is_rejected():
    """Administracyjne powitania i zaproszenia na webinary: to nie ogłoszenie."""
    cases = {
        "noise-1": "Powitajmy nowych członków naszej grupy! Katarzyna Zielińska, Adam Nowak, Jan Kowalski.",
        "noise-2": "Czy Twój sklep jest gotowy na Black Friday? Już jutro o 11:00 widzimy się na bezpłatnym webinarze.",
        "noise-3": "Prosta wizytówka Twojej firmy? Koszt wykonania: 1500 zł. Mam wolne miejsce na realizację.",
        "noise-4": "one year Claude ai max plan 20x anyone need?",
    }
    for source_id, text in cases.items():
        lead = make_lead("facebook_groups", source_id, text)
        scored = apply_user_policy([ScoredLead(lead, 70, ScoreBucket.MOZE, "mgliste")])
        assert scored[0].bucket is ScoreBucket.ODRZUC, text


def test_facebook_post_without_client_intent_is_rejected():
    """Bramka treści dla grup FB (2026-09-25): post bez śladu intencji klienta to nie lead.

    Wszystkie teksty poniżej są zmierzone na produkcyjnym payloadzie z przebiegu 25.09,
    gdzie wchodziły do TOP ze score 100/100, bo polityka nie miała żadnej bramki treści
    dla postów z grup (sonda: `scratch/fb-filter-probe.py`, 49 z 61 postów przechodziło).
    """
    cases = {
        "slop-1": "Dziś pierwszy dzień jesieni! 🍁🍂 To znak, że powoli wchodzimy w czas najbardziej intensywnych działań biznesowych.",
        "slop-2": "Zapraszam do kontaktu 📞💼📭 NES New Era Service",
        "slop-3": "Cześć, mam na imię Magdalena.",
        "slop-4": "Czwartek wspomnień 📷 Skomentuj ze zdjęciem przedstawiającym ...",
        "slop-5": 'Trwa** promocja **na kurs** "Accounting English - język angielski w księgowości"**',
        "slop-6": "Choinki, lampki, bombki, skrzaty, torby prezentowe: jedna dostawa, jedna faktura",
        "slop-7": "Dzień dobry, proszę o poradę w sprawie spółki z o.o. Z rachunku bankowego spółki opłacane były ogłoszenia na OLX.",
        "slop-8": "Orientuje się ktoś jakie są szacunkowe koszty wdrożenia lokalnego AI w firmie, bez kosztu serwera z GPU.",
        "slop-9": "PrestaShop 9.1.5 Jaki najlepszy rozmiar zdjęcia do karty produktu? I w jakim formacie?",
        "slop-10": "Wcześniej Allegro International nadawałem elegancko InPost a teraz mi zmienili i wszystko musze nadawac AllegroOne które nawet nie obejmuje mojego kodu pocztowego.",
    }
    for source_id, text in cases.items():
        lead = make_lead("facebook_groups", source_id, text)
        scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "model dał TOP")])
        assert scored[0].bucket is ScoreBucket.ODRZUC, text
        assert "intencji klienta" in scored[0].rationale, text


def test_facebook_client_intent_phrases_survive_the_content_gate():
    """Prawdziwe zlecenia z grup FB mają ślad intencji klienta, więc bramka ich nie rusza.

    Teksty z przebiegu 25.09 (grupy: „Szukam programisty”, „Zlecenia programistyczne”,
    „Wordpress | E-commerce | Apki”, „Allegro: grupa wsparcia sprzedawców”).
    """
    cases = {
        "keep-poszukuje": (
            "Poszukuję osoby/firmy do zbudowania platformy do zarządzania kosztami energii "
            "z wykorzystaniem elementów prognozowania z AI. Proszę o kontakt na PRIV. "
            "Prześlę szczegółowy brief."
        ),
        "keep-szukam-programisty": (
            "Cześć, szukam programisty / developera do rozwijania projektu startupowego B2B "
            "SaaS. Mamy już działające MVP i chcemy przejść do kolejnego etapu rozwoju."
        ),
        "keep-poszukuje-pomocy": (
            "Witam , poszukuje pomocy informatyka lub kogoś kto zna się na programach , które "
            "automatycznie rezerwują sloty wjazdu na Transporeon. Bardzo proszę o pomoc."
        ),
        "keep-kto-poleci": (
            "Hej, kto poleci sprawdzonego wykonawcę do integracji sklepu z Allegro? "
            "Potrzebuję kogoś na już."
        ),
        "keep-prosze-o-oferty": (
            "Prowadzimy sprzedaż na Allegro. Szukam kogoś kto sprawdzi nam konto, gdzie "
            "tracimy pieniądze i jak ograniczyć koszty. Proszę o oferty."
        ),
    }
    for source_id, text in cases.items():
        lead = make_lead("facebook_groups", source_id, text)
        scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "zlecenie")])
        assert scored[0].bucket is ScoreBucket.TOP, text


def test_content_gate_does_not_touch_other_sources():
    """Bramka treści działa wyłącznie na grupach FB: listingi źródłowe mają własne dane.

    Ogłoszenie z Useme bez słowa „szukam” musi zostać w TOP — tam budżet, widełki i typ
    umowy są metadanymi, a nie domysłem z postu.
    """
    lead = make_lead(
        "useme",
        "useme-144956",
        "Rozbudowane wyszukiwanie do istniejącego serwisu b2b",
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_client_calling_himself_poszukuje_is_protected():
    """„Poszukuję” to głos klienta: bramka znała tylko „poszukujemy” i zabijała zlecenia.

    Realny przypadek z grupy Social Media - ZLECĘ/WYKONAM: klient nie potrafi sam
    rozwiązać problemów z kontem Meta Business Suite i szuka specjalisty.
    """
    lead = make_lead(
        "facebook_groups",
        "keep-4",
        "Social Media - ZLECĘ / WYKONAM: Hej, poszukuję specjalisty który pomoże mi "
        "z ustawieniami kont w Meta Business Suite",
    )
    scored = apply_user_policy([ScoredLead(lead, 80, ScoreBucket.TOP, "zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_private_and_portfolio_are_not_standalone_triggers():
    """„na priv”, „portfolio”, „w komentarzu” używa też klient — nie mogą odrzucać same."""
    from dataclasses import replace

    keep = replace(
        make_lead(
            "facebook_groups",
            "keep-8",
            "PrestaShop Polska: Poszukuję do opieki technicznej do sklepu",
        ),
        description=(
            "Szukam opieki technicznej nad sklepem: aktualizacje, naprawa błędów, instalacja "
            "modułów. Proszę o oferty z orientacyjnymi cenami na priv."
        ),
    )
    scored = apply_user_policy([ScoredLead(keep, 75, ScoreBucket.TOP, "zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP

    client_portfolios = replace(
        make_lead(
            "facebook_groups",
            "keep-5",
            "Hej, szukam osoby do zrobienia kontentu restauracji w Katowicach",
        ),
        description=(
            "Hej, szukam osoby do zrobienia kontentu restauracji tj (rolki i zdjęcia) "
            "w Katowicach. Podsyłajcie swoje portfolio :)"
        ),
    )
    scored = apply_user_policy([ScoredLead(client_portfolios, 40, ScoreBucket.MOZE, "poza zakresem")])
    assert scored[0].bucket is ScoreBucket.MOZE  # MOŻE od modelu, nie ODRZUĆ od reguły


def test_supplier_and_product_ads_are_rejected():
    """Żywe false TOPy z pierwszego dostarczenia FB na Telegram (2026-09-21).

    Wszystkie trzy wiersze model dał raz jako TOP 100/100, a przy powtórnym pomiarze
    jako MOŻE/ODRZUĆ — dlatego reklama produktu ma własną, deterministyczną regułę.
    """
    cases = {
        "sup-1": (
            "Folia stretch 51 cm, hurtowo prosto od importera. Cena od 22,59 zł netto "
            "za rolkę. Do ręcznego owijania palet i zabezpieczania przesyłek."
        ),
        "sup-2": (
            "Twoja oferta z płatnościami i panelem klienta na Dashy. Klient kupuje usługę "
            "i od razu dostaje własny panel z chatem, plikami, fakturą i zadaniami. "
            "Wszystko w jednym miejscu, zacznij za darmo"
        ),
        "sup-3": (
            "Masz firmę, ale Twoja strona internetowa nie robi już dobrego wrażenia? "
            "Tworzę nowoczesne strony internetowe, które pomagają zdobywać klientów. "
            "Jeśli chcesz zobaczyć moje realizacje, zajrzyj tutaj."
        ),
    }
    for source_id, text in cases.items():
        lead = make_lead("facebook_groups", source_id, text)
        scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "model dał TOP")])
        assert scored[0].bucket is ScoreBucket.ODRZUC, text


def test_a_client_selling_on_allegro_is_not_a_product_ad():
    """Bramka klienta chroni: „sprzedaję na Allegro i szukam osoby do konta” to zlecenie."""
    from dataclasses import replace

    lead = replace(
        make_lead(
            "facebook_groups",
            "keep-allegro",
            "Allegro: grupa wsparcia sprzedawców: Prowadzimy sprzedaż na Allegro",
        ),
        description=(
            "Prowadzimy sprzedaż na Allegro. Szukam kogoś kto sprawdzi nam konto, gdzie "
            "tracimy pieniądze i jak ograniczyć koszty."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 80, ScoreBucket.TOP, "zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_group_name_in_the_title_does_not_disable_the_anti_ad_rules():
    """Regresja na żywy błąd 2026-09-21: nazwa grupy FB zatruwała bramkę klienta.

    Tytuł leada z Facebooka to „<nazwa grupy>: <pierwsze zdanie>”, a same grupy
    nazywają się „Szukam wykonawcy” / „Szukam programisty”. Bramka `_BUYER_SEARCH_RE`
    widziała więc „szukam wykonawcy” w KAŻDYM poście z tych grup i wyłączała reguły
    antyreklamowe — przez to „TANIE STRONY INTERNETOWE – OD 300 ZŁ” weszło do TOP.
    """
    from dataclasses import replace

    lead = replace(
        make_lead(
            "facebook_groups",
            "tania-witryna",
            "Zlecenia programistyczne | Szukam wykonawcy: TANIE STRONY INTERNETOWE – OD 300 ZŁ!",
        ),
        description=(
            "TANIE STRONY INTERNETOWE – OD 300 ZŁ! Potrzebujesz prostej i profesjonalnej "
            "strony internetowej dla swojej firmy? STRONA WIZYTÓWKA – 300 ZŁ."
        ),
        raw_data={"group_name": "Zlecenia programistyczne | Szukam wykonawcy"},
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "model dał TOP")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_singular_self_promotion_forms_are_rejected():
    """„Zajmuję się sprzedażą na Allegro… Pomagam firmom” i „Szukam Media Buyera”."""
    cases = {
        "zajmuje": (
            "📈 Zajmuję się sprzedażą na Allegro i innych marketplace. Pomagam firmom "
            "zwiększać sprzedaż, poprawiać wyniki kont i lepiej wykorzystywać potencjał ofert."
        ),
        "media-buyer": (
            "Szukam Media Buyera / eksperta e-commerce do mocnego projektu dropshippingowego. "
            "Ty prowadzisz sklep i marketing (TikTok/Meta); ja finansuję testy i budżet."
        ),
    }
    for source_id, text in cases.items():
        lead = make_lead("facebook_groups", source_id, text)
        scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "model dał TOP")])
        assert scored[0].bucket is ScoreBucket.ODRZUC, text


def test_client_asking_for_an_audit_is_a_job_not_marketing():
    """„Sprawdźcie, gdzie tracimy pieniądze” to zlecenie na audyt (jest w cenniku Jana)."""
    from dataclasses import replace

    lead = replace(
        make_lead("facebook_groups", "audyt-allegro", "Allegro: grupa wsparcia sprzedawców"),
        description=(
            "Prowadzimy sprzedaż na Allegro. Szukam kogoś kto sprawdzi nam konto, gdzie "
            "tracimy pieniądze, jak ograniczyć koszty i co robimy źle."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "model dał TOP")])
    assert scored[0].bucket is ScoreBucket.TOP


# ---------- live misses 2026-09-23: MOŻE-bucket provider ads ----------
# Three seller ads that reached the 05:30 run in the MOŻE bucket (file only, not the
# chat) with real bodies from their threads.


def test_user_policy_rejects_available_pilot_review_ad():
    """„[Available] Fixed-scope n8n reliability review — €49 pilot” (Samantha100)."""
    from dataclasses import replace

    lead = replace(
        make_lead(
            "n8n_community_jobs",
            "sam-1",
            "[Available] Fixed-scope n8n reliability review — €49 pilot, no production access",
        ),
        description=(
            "I’m opening one pilot slot for a written reliability review of one critical path "
            "in an existing n8n workflow. This is not a full implementation service or a "
            "promise to repair every node. Fixed pilot price: €49. The review uses redacted "
            "materials only: workflow JSON; expected and observed behaviour; one synthetic or "
            "anonymised execution trace."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 70, ScoreBucket.MOZE, "usługa serwisowa")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_i_fix_the_operational_problems_ad():
    """„I fix the operational problems around Make + Airtable” (clearmerit)."""
    from dataclasses import replace

    lead = replace(
        make_lead(
            "make_hire_help",
            "cm-1",
            "I fix the operational problems around Make + Airtable — duplicates, bad updates, "
            "retries & handoffs",
        ),
        description=(
            "A Make scenario can run successfully and still create operational problems. That "
            "operational reliability layer is where I focus. My strongest current hands-on "
            "automation work is n8n + Airtable. I run CLEAR MERIT’s own Internal / Working Demo "
            "using Gmail → n8n → Airtable with duplicate prevention, record lookup, conditional "
            "branching and human approval."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 70, ScoreBucket.MOZE, "usługa serwisowa")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_rejects_free_diagnosis_lead_magnet():
    """„Got a broken n8n workflow? Give me one real problem…” (hugo_acevedo)."""
    from dataclasses import replace

    lead = replace(
        make_lead(
            "n8n_community_jobs",
            "hugo-1",
            "Got a broken n8n workflow? Give me one real problem and I’ll try to diagnose it",
        ),
        description=(
            "I keep seeing people stuck on n8n problems where the workflow looks almost right, "
            "but something small is breaking the whole automation. Give me one real n8n problem "
            "you’re currently stuck on. If you can describe the problem here, I’ll take a look "
            "and try to identify the likely cause and the next thing I would test."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 75, ScoreBucket.MOZE, "darmowa diagnoza")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_user_policy_keeps_client_asking_to_fix_a_broken_scenario():
    """Klient: „we're looking for a freelancer to fix it” — nigdy nie reklama."""
    from dataclasses import replace

    lead = replace(
        make_lead("make_hire_help", "client-fix", "My Make scenario broke, need someone to fix it"),
        description=(
            "We have a Make scenario that failed after a mapping change and we're looking for a "
            "freelancer to fix it. We can share the workflow JSON; no production access needed "
            "at first."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "realne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_user_policy_rejects_offer_help_monitoring_ad():
    """„[Offer Help] Diagnosis + silent-regression monitoring…” — TOP 100, 2026-09-24."""
    from dataclasses import replace

    lead = replace(
        make_lead(
            "make_hire_help",
            "tom-1",
            "[Offer Help] Diagnosis + silent-regression monitoring for Make scenarios that run "
            "green with wrong output (async, fixed price)",
        ),
        description=(
            "I diagnose scenarios that break, and I monitor the ones that keep succeeding while "
            "the result stops being correct. Diagnosis: you send a blueprint export plus execution "
            "history, I return a written report within 48-72h. Fixed price, async, no calls, I "
            "never touch your live scenario. DM me here with a screenshot and I will start with a "
            "free 3-point mini-read, capped at 20 minutes. This is my one listing in this category."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "monitoring Make")])
    assert scored[0].bucket is ScoreBucket.ODRZUC


def test_oferteo_service_title_is_rejected():
    """Oferteo: sam tytuł, brak opisu, płatny kontakt — serwis leci do ODRZUĆ.

    Cztery tytuły z przebiegu 2026-09-25 dostawały 100/100, bo model nie miał
    czego czytać poza tytułem, a Jan za każdy odblokowany kontakt płaci punkty.
    Pierwsza wersja reguły tylko degradowała je do MOŻE; decyzja Jana z
    2026-09-25 po zobaczeniu raportu: serwis komputerowy, sieci, odzyskiwanie
    danych i druk 3D to nie nasza robota, więc nie ma ich nigdzie.
    """
    titles = (
        "Zapytanie o odzyskanie danych, zdjęcia, telefon",
        "Potrzebna naprawa komputera, problem z internetem, siecią Wi-Fi lub LAN",
        "Szukam serwisu komputerowego",
        "Zapytanie dla specjalisty OD sieci komputerowych",
        "Zapytanie dla specjalisty OD sieci komputerów",
        # Warianty z żywej listy 2026-09-25 — pierwsza wersja wzorca je gubiła.
        "Potrzebuję drukowania 3D, 11-20szt.",
        "Zlecenie na drukowanie 3D części zamiennej, 6-10szt.",
        "Zapytanie o odzyskanie danych, SMS-y, telefon",
        "Potrzebna naprawa laptopa, błędy systemu, problem z internetem, siecią Wi-Fi lub LAN",
        "Potrzebuję internetu stacjonarnego, prędkość 1 Gb/s lub więcej",
        "Szukam dostawcy internetu",
        "Zapytanie o dostawcę internetu",
        "Zapytanie o konfigurację komputera, konfigurację sieci, Instalację oprogramowania",
    )
    for index, title in enumerate(titles):
        lead = make_lead("oferteo", f"of-{index}", title)
        scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "serwis sprzętu")])
        assert scored[0].bucket is ScoreBucket.ODRZUC, title


def test_oferteo_integration_title_keeps_top():
    """Integracje systemów IT i outsourcing zostają w TOP — to nie serwis."""
    titles = (
        "Zapytanie dla specjalisty OD integracji systemów IT",
        "Zapytanie o wykonanie strony www",
        "Zlecenia na modyfikację dedykowanego oprogramowania",
    )
    for index, title in enumerate(titles):
        lead = make_lead("oferteo", f"of-keep-{index}", title)
        scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "integracja")])
        assert scored[0].bucket is ScoreBucket.TOP, title


def test_oferteo_service_rule_does_not_touch_other_sources():
    """Reguła jest celowo wąska: tylko Oferteo, gdzie widać sam tytuł."""
    lead = make_lead("useme", "us-1", "Zapytanie o odzyskanie danych, zdjęcia, telefon")
    scored = apply_user_policy([ScoredLead(lead, 100, ScoreBucket.TOP, "serwis sprzętu")])
    assert scored[0].bucket is ScoreBucket.TOP


def test_oferteo_service_rule_rejects_in_every_bucket():
    """Nie tylko TOP: serwis komputerowy nie dostaje drugiej szansy w MOŻE."""
    for bucket, score in ((ScoreBucket.MOZE, 50), (ScoreBucket.ZAMIEN, 60)):
        lead = make_lead("oferteo", f"of-{bucket.value}", "Potrzebna naprawa komputera, siecią Wi-Fi")
        scored = apply_user_policy([ScoredLead(lead, score, bucket, "serwis sprzętu")])
        assert scored[0].bucket is ScoreBucket.ODRZUC, bucket.value


# ---------- Jan 2026-10-27: klient, który wprost nie chce automatyzacji ----------
# „Wprowadzanie 750 zderzaków do BaseLinker”: „Nie interesują mnie skrypty AI ani
# automaty – branża wymaga ludzkiego oka.” Takiego klienta się nie przekonuje.


def test_client_refusing_automation_is_rejected_in_every_bucket():
    from dataclasses import replace

    lead = replace(
        make_lead(
            "useme",
            "146286",
            "Wprowadzanie 750 zderzaków do BaseLinker i Allegro (RĘCZNIE)",
        ),
        description=(
            "Szukam PERFEKCJONISTY do ręcznego wprowadzenia około 750 używanych zderzaków "
            "i spoilerów samochodowych do Katalogu BaseLinker oraz jako szkice na Allegro. "
            "Nie interesują mnie skrypty AI ani automaty – branża wymaga ludzkiego oka."
        ),
    )
    for bucket in (ScoreBucket.TOP, ScoreBucket.MOZE, ScoreBucket.ZAMIEN):
        scored = apply_user_policy([ScoredLead(lead, 100, bucket, "model dał wysoką ocenę")])
        assert scored[0].bucket is ScoreBucket.ODRZUC
        assert "nie chce" in scored[0].rationale


def test_automation_wording_that_is_not_a_refusal_survives():
    from dataclasses import replace

    lead = replace(
        make_lead("useme", "147000", "Automatyzacja wystawiania ofert na Allegro"),
        description=(
            "Chcę narzędzie bez AI: proste reguły, które wystawiają oferty z pliku CSV. "
            "Szukam automatyzacji, która zdejmie ze mnie ręczne klikanie."
        ),
    )
    scored = apply_user_policy([ScoredLead(lead, 90, ScoreBucket.TOP, "konkretne zlecenie")])
    assert scored[0].bucket is ScoreBucket.TOP
