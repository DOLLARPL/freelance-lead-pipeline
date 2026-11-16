from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

from freelance_pipeline.models import JobLead

MAX_RATIONALE_LEN = 500
MAX_GAP_LEN = 240


class ScoreBucket(Enum):
    """The four buckets the pipeline may ever assign to a lead.

    ``ZAMIEN`` is Jan's fourth axis (2026-09-21): an ad where the client is
    looking to HIRE A PERSON for repetitive work that can be automated. Such an
    offer is never a commission to answer as it stands, so it never reaches TOP
    or the TOP report — it gets its own file, its own counter and (Jan,
    2026-09-21) its own second Telegram message, so the idea can be measured
    over several runs.
    """

    TOP = "TOP"
    MOZE = "MOŻE"
    ODRZUC = "ODRZUĆ"
    ZAMIEN = "ZAMIEN"


def _coerce_score(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"score must be an int, got {value!r}")
    if not 0 <= value <= 100:
        raise ValueError(f"score must be within 0..100, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class ScoredLead:
    """A lead plus a strictly validated score and bucket."""

    lead: JobLead
    score: int
    bucket: ScoreBucket
    rationale: str
    # Optional: a capability this offer needs that Jan cannot show yet. Only the
    # portfolio-gap backlog reads it; it never changes the bucket.
    gap: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _coerce_score(self.score))
        if not isinstance(self.bucket, ScoreBucket):
            raise TypeError(f"bucket must be a ScoreBucket, got {type(self.bucket).__name__}")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("rationale must be a non-empty str")
        if len(self.rationale) > MAX_RATIONALE_LEN:
            raise ValueError(
                f"rationale must be at most {MAX_RATIONALE_LEN} chars, got {len(self.rationale)}"
            )
        object.__setattr__(self, "rationale", self.rationale.strip())
        if self.gap is not None:
            if not isinstance(self.gap, str):
                raise TypeError(f"gap must be a str or None, got {type(self.gap).__name__}")
            if len(self.gap) > MAX_GAP_LEN:
                raise ValueError(f"gap must be at most {MAX_GAP_LEN} chars, got {len(self.gap)}")
            object.__setattr__(self, "gap", self.gap.strip() or None)


class Scorer(Protocol):
    """Every scorer must return exactly one ``ScoredLead`` per input lead."""

    def score(self, leads: Sequence[JobLead]) -> list[ScoredLead]:
        ...


# These are deliberately narrow, high-confidence rules. They are not a
# general keyword relevance filter: they only enforce the user's explicit
# exclusions after the model has seen the complete lead.
_SENIOR_TITLE_RE = re.compile(
    r"\b(?:senior|sr\.?|lead(?!\s*(?:[→\-–:/]|gen\w*|triage|nurtur\w*|scor\w*|qualif\w*|"
    r"intake))|principal|staff|head of|director|chief|expert|architect)\b",
    re.IGNORECASE,
)
_AUTOMATOR_POST_RE = re.compile(
    # Polish: the provider speaks about its own offer.
    r"\b(?:szukam\s+zleceń|szukam\s+klient(?:a|ów)|oferuj(?:ę|emy)\s+usługi|"
    r"(?:moje|nasze)\s+usługi|jestem\s+freelancer\w*|jesteśmy\s+agencj\w*)\b|"
    # English: "[For Hire]", "available for hire", "hire me", service pitch.
    r"\b(?:available\s+)?for\s+hire\b|\bhire\s+me\b|"
    r"\bavailable\s+for\s+(?:freelance|contract|projects?|work)\b|"
    r"\b(?:my|our)\s+(?:services|portfolio|rates)\b|"
    r"\b(?:i|we)\s+(?:offer|provide|deliver)\s+"
    r"(?:automation|automations|n8n|make(?:\.com)?|ai|workflow|integrations?)\b|"
    r"\b(?:i|we)\s+(?:build|develop|craft)\s+(?:ai\s+agents?|automat\w+|workflows?|"
    r"integrations?|chatbots?)\b|"
    r"\boffers?\s+(?:small|fixed[- ]scope|free|paid|affordable|professional|one)\b|"
    r"\b(?:i|we)\s+help\s+(?:businesses|business|companies|teams|founders|clients|"
    r"people|brands|startups|smbs?|smes?|creators|others)\b|"
    r"\bfounder\s+of\s+[\w&.\- ]{2,40}?(?:studio|agenc\w+|automations?|solutions|lab|"
    r"consultanc\w+|gmbh|llc|ltd|inc)\b|"
    r"\b(?:i|we)\s+(?:run|operate|own)\s+[\w&.\- ]{2,30}(?:lab|studio|agenc\w+|automation|"
    r"solutions)\b|"
    r"\b(?:i|we)\s+(?:build|maintain|design|develop)\s+(?:and\s+\w+\s+)?"
    r"(?:automations?|workflows?|scenarios?|integrations?|agents?|bots?)\b|"
    r"\b(?:i|we)\s+take\s+(?:on|one|care\s+of)\b|"
    r"\b(?:i|we)\s+(?:am|are|['\u2019]m|['\u2019]re)\s+(?:a|an)?\s*"
    r"(?:freelanc\w+|independent\s+\w+|automation\s+(?:agency|studio|lab|developer)|"
    r"software\s+house)\b|"
    # Company/partner self-positioning.
    r"\bfreelance\s+services\b|"
    r"\b(?:platinum|gold|silver|premium)\s+(?:make|n8n|automation)?\s*partner\b",
    re.IGNORECASE,
)

# Weak seller signal: the nouns alone decide nothing (an agency may be the buyer),
# so they only reject when the post is not asking for someone to work with it.
_AGENCY_NOUN_RE = re.compile(
    r"\b(?:agenc(?:y|ies)|software\s+house|automation\s+(?:studio|lab))\b",
    re.IGNORECASE,
)

# Seller voice as a conjunction: the author calls themselves a professional AND
# offers to do the work. Either half alone is not enough (a client may be a
# developer, and „you can…” is ordinary English).
_SELF_IDENTITY_RE = re.compile(
    r"\b(?:i(?:['\u2019]m| am)|we(?:['\u2019]re| are))\s+(?:a|an)\s+[\w\-/ ]{0,30}?"
    r"(?:engineer|developer|specialist|consultant|architect|expert|builder|"
    r"automator|freelancer)\b",
    re.IGNORECASE,
)
_OFFER_RE = re.compile(
    r"\b(?:i|we)\s+can\s+(?:help|build|design|deliver|take|handle|set\s+up|fix)\b|"
    r"\b(?:available\s+for|reach\s+out|message\s+me|dm\s+me|book\s+a\s+call|"
    r"get\s+in\s+touch|my\s+rates|my\s+availability)\b",
    re.IGNORECASE,
)

# The author does the work for the reader ("I do the integration myself",
# "I map your actions") or frames it as a pilot/partnership they deliver.
# „Partnerstwo" here means the author sells to agencies, not that he buys
# (Jan's decision 2026-09-19: odrzucać takie pilotaże).
_SELLER_DELIVERS_RE = re.compile(
    r"\b(?:i|we)(?:['\u2019]ll| will)?\s+(?:do|handle|take\s+care\s+of)\s+"
    r".{0,25}\b(?:myself|ourselves)\b|"
    r"\b(?:i|we)\s+(?:map|write|build|deliver|install|set\s+up)\s+your\b|"
    r"\bwhat\s+you\s+get\b.{0,120}\b(?:i|we)\b",
    re.IGNORECASE,
)

# n8ndevs rewrites some posts into the third person, so a provider ad can read
# „Ahmed builds n8n systems …, services offered include …, he is available from
# Turkey” (live miss 2026-09-19). Third-person seller voice is its own signal.
_SELLER_THIRD_PERSON_RE = re.compile(
    r"\bservices\s+offered\b|"
    r"\b(?:he|she|they)\s+(?:builds?|offers?|delivers?|provides?|implements?|"
    r"is\s+available|are\s+available|speciali[sz]es)\b|"
    r"\b(?:his|her|their)\s+(?:services|rates|availability|portfolio|workflow\s+packages)\b|"
    r"\bstarting\s+from\s+[$€£]\s?\d",
    re.IGNORECASE,
)

# Client-voice vs seller-voice, take three (live misses 2026-09-20, all TOP 100 on
# make_hire_help): „FREE Help for Make.com Users…”, „Need help with a Make scenario?”,
# „Filevine and Legal Case Management Automations…”. These posts are unambiguous
# seller marketing — they are *not* commissions — because the author describes his
# own services and sends the reader to his own booking link. The existing rules
# missed them: _SELF_IDENTITY_RE asked for „I'm a <role>” plus „I can help”, and
# this voice says „I run X and help…”, „I automate your…”, „typical projects I
# support…”, „find a time on my Calendly”. Since a real client commissioning work
# may still open with „I'm offering 500 USD…”, this rule never fires when the post
# contains buyer-search language (_BUYER_SEARCH_RE).
_PROVIDER_PITCH_RE = re.compile(
    # Free-help / community pitches: the author sells himself, nobody pays anyone.
    r"\b(?:lifetime\s+)?free\s+help\b|"
    r"\boffering\s+(?:lifetime\s+)?free\b|"
    # Seller-to-reader voice: „what I can do for you”, „I automate your…”.
    r"\bwhat\s+i\s+can\s+do\s+for\s+you\b|"
    r"\bi\s+(?:automate|fix|review|optimise|optimize|upgrade|rework|debug)\s+your\b|"
    r"\bi\s+build\s+[\w. ]{0,30}(?:systems?|automations?|scenarios?|workflows?)\s+that\s+handle\b|"
    r"\bi(?:['\u2019]m| am)\s+happy\s+to\s+take\s+a\s+look\b|"
    r"\bi\s+(?:will|can)\s+(?:take\s+a\s+look|review\s+your|diagnose)\b|"
    # Owner of a studio/lab/agency selling to the readers.
    r"\bi\s+run\s+[\w][\w\s'&.\-]{0,40}\band\s+(?:help|build|automate|scale)\b|"
    # „I'm an automation developer focused on…” + „typical projects I support…”.
    r"\bi(?:['\u2019]m| am)\s+an?\s+[\w\- ]{0,30}?(?:developer|consultant|specialist|"
    r"engineer|architect|integrator|automator)\s+(?:focused|speciali[sz]|working|helping|who|based)\b|"
    r"\btypical\s+projects\s+i\s+(?:support|take\s+on|work\s+on|deliver|handle)\b|"
    r"\bmy\s+focus\s+is\s+on\b|"
    # Booking funnel: a client does not send the reader to his own calendar.
    r"\bmy\s+calendly\b|\bcalendly\.com\b|"
    r"\bbook\s+a\s+(?:strategy|discovery|free)\s+(?:session|call|consultation)\b|"
    r"\bfind\s+a\s+time\s+on\s+my\b|"
    # Live misses 2026-09-21 (verification run on a state copy). All three are
    # unambiguous seller ads that dodged the patterns above by being rewritten
    # into the third person or by naming the work instead of the tool:
    #   * „Khadijat, an AI Automation Specialist and n8n workflow builder, is
    #     looking for freelance projects, contract work, and remote
    #     collaborations. … What I work with: … Portfolio: Visit here …”
    #   * „If your business processes aren't functioning optimally, RAR 121 can
    #     help. We specialize in … We offer services including …”
    #   * „I build AI-powered WhatsApp lead qualification automations for real
    #     estate businesses … What the automation can do: … I can provide a live
    #     demo … how I can build and customize a similar system for your …”
    # The buyer-search guard below keeps a real client safe: „we're looking for
    # freelance help with X” carries buyer language and is never hit.
    r"\b(?:is|are)\s+looking\s+for\s+(?:freelance|contract)\b|"
    r"\bwhat\s+i\s+work\s+with\b|"
    r"\bportfolio\s*:\s*visit\b|"
    r"\b(?:i|we)\s+offer\s+services\b|"
    r"\b(?:i|we)\s+speciali[sz]e\s+in\b|"
    r"\b[A-Z][\w&.\- ]{2,30}\s+can\s+help\b|"
    r"\bwhat\s+the\s+automation\s+can\s+do\b|"
    r"\bi\s+can\s+provide\s+a\s+live\s+demo\b|"
    r"\bi\s+can\s+build\s+and\s+customi[sz]e\s+a\s+similar\s+system\b|"
    # Wzorce GRUPOWE po polsku (etap 3 kalibracji, pomiar 97 czystych postów z FB —
    # liczby i przykłady w ETAP3-KALIBRACJA.md). Plan zakładał „PM me”, „check my
    # profile”, „comment below”, Calendly; w polskich grupach te wzorce nie występują
    # ani razu, a reklama wykonawcy wygląda inaczej: „Oferuję kompleksowe automatyzacje”,
    # „Podejmuję się jednorazowych zleceń freelance”, „Tworzymy nowoczesne strony”,
    # „Co oferujemy?”, „moje portfolio poniżej”, „szukam zleceń”, „strony od 300 zł”.
    #
    # CZEGO TU ŚWIADOMIE NIE MA: „na priv”, „portfolio”, „w komentarzu”. Te zwroty są
    # dwukierunkowe — używa ich też KLIENT („proszę o oferty z orientacyjnymi cenami na
    # priv” = keep, „podsyłajcie swoje portfolio” = klient zaprasza wykonawców).
    # Samodzielny wyzwalacz odrzucenia na którymkolwiek z nich zabiłby realne zlecenia.
    r"\boferuj[ęe]\b|\boferujemy\b|\bco\s+oferujemy\b|"
    r"\bpodejmuj[ęe]\s+si[ęe]\b|\btworzymy\b|\bzajmujemy\s+si[ęe]\b|\bwykonam\b|"
    r"\bmoje\s+portfolio\b|\bszukam\s+zlece[ńn]\b|\bprzyjm[ęe]\s+zlecenia\b|"
    r"\bcennik\b|\bod\s+\d+\s*z[łl]\b|\bchcesz\s+zareklamowa[ćc]\b|"
    r"\bkoszt\w*\s*:?\s*\d+\s*z[łl]\b|\bmam\s+wolne\s+(?:miejsce|termin|moce)\b|"
    r"\banyone\s+need\b|"
    # „Tworzę nowoczesne strony…, jeśli chcesz zobaczyć moje realizacje, zajrzyj tutaj"
    # — żywy false TOP z 2026-09-21 (grupa „Szukam programisty"); sam „tworzę" nie
    # wystarcza, bo klient pisze „Tworzę produkt i potrzebuję drugiej pary oczu".
    r"\btworz[ęe]\s+(?:nowoczesne|profesjonalne|strony|aplikacje|sklepy|systemy|realizacje)\b|"
    # „Zajmuję się sprzedażą na Allegro… Pomagam firmom zwiększać sprzedaż" — żywy false
    # TOP z 2026-09-21; reguła miała tylko formę mnogą („zajmujemy się").
    r"\bzajmuj[ęe]\s+si[ęe]\b|"
    # „Szukam Media Buyera / eksperta e-commerce" — klient szukający media buyera to nie
    # zlecenie Jana (prowadzenie kampanii jest poza zakresem), nawet gdy ma budżet.
    r"\bmedia\s+buyer\w*\b|"
    # Same live run, second pass: the Make adapter hands the model a TRUNCATED
    # excerpt („… hand qualified leads &hellip;”), so the phrases above never
    # arrive. What does arrive is the title: „I build AI-powered WhatsApp lead
    # qualification automations for real estate businesses”. Seller framing is
    # „I/we build <work> for <businesses/clients>”; a client writing „we are
    # looking for someone to build …” has „to” before the verb and is never hit
    # (and the buyer-search guard covers the rest).
    r"\b(?:i|we)\s+(?:build|develop|craft)\s+(?:[\w/\-]+\s+){0,2}"
    r"(?:automations?|workflows?|scenarios?|integrations?|chatbots?|bots?|systems?)\s+for\s+"
    r"(?:[\w/\-]+\s+){0,3}(?:business(?:es)?|companies|clients|agenc\w+|founders|teams|smbs?)\b|"
    # Live misses 2026-09-23 (przebieg 05:30, kubełek MOŻE — nie na czacie, ale wciąż
    # zły kubełek). Trzy reklamy wykonawców:
    #   * Samantha100: „[Available] Fixed-scope n8n reliability review — €49 pilot,
    #     no production access” + „I'm opening one pilot slot…” — tag jak [For Hire].
    #   * clearmerit: „I fix the operational problems around Make + Airtable” +
    #     „I run CLEAR MERIT's own Internal / Working Demo”.
    #   * hugo_acevedo: „Got a broken n8n workflow? Give me one real problem and I'll
    #     try to diagnose it” — darmowa diagnoza jako lead magnet, nie zlecenie.
    # Klient piszący „need someone to fix it / we're looking for a freelancer” ma
    # język kupującego i nigdy nie trafia w te wzorce.
    r"\[available\]|"
    r"\[offer\s+help\]|"
    r"\bmy\s+(?:one|only)\s+listing\b|"
    r"\bfree\s+\d+-point\b|"
    r"\bi\s+fix\s+(?:the|your|broken|bad)\b|"
    r"\bi\s+open(?:ing)?\s+(?:one|a)\s+(?:pilot|slot)\b|"
    r"\bi(?:['\u2019]ll| will)\s+(?:try\s+to\s+)?diagnose\b|"
    r"\bi(?:['\u2019]ll| will)\s+take\s+a\s+look\b|"
    r"\bgive\s+me\s+one\s+real\b|"
    r"\bgot\s+a\s+broken\s+(?:n8n|make|zapier|automation|workflow)\b",
    re.IGNORECASE,
)

# Vendor marketing: a title that asks the reader a sales question („Do you want to
# automate processes?”, „Need help with Make?”) plus first-person plural in the
# body. A client commissioning work does not open with a pitch question.
_VENDOR_TITLE_RE = re.compile(
    r"^\s*(?:do\s+you\s+want|need\s+help|looking\s+to|ready\s+to|want\s+to|"
    r"chcesz|potrzebujesz|szukasz\s+autom)",
    re.IGNORECASE,
)
_FIRST_PERSON_PLURAL_RE = re.compile(
    r"\b(?:we|our|us|oferujemy|nasze\s+usługi|nasza\s+oferta|jesteśmy\s+agencj\w*|"
    r"nasza\s+firma\s+oferuje)\b",
    re.IGNORECASE,
)


def _is_vendor_marketing(title: str, body: str) -> bool:
    return bool(_VENDOR_TITLE_RE.search(title or "") and _FIRST_PERSON_PLURAL_RE.search(body or ""))


# The source itself classifies some n8ndevs listings as employment; that is
# metadata, not a guess, so an employment pill always loses.
_EMPLOYMENT_CONTRACT_RE = re.compile(
    r"\b(?:employment|full[- ]time|part[- ]time)\b", re.IGNORECASE
)

# Employment wording inside the ad itself (live 2026-09-19: the model wrote
# „to oferta pracy, nie zlecenie” in its own rationale and still bucketed TOP).
# Title markers are unambiguous; the body markers are salary vocabulary that a
# client commissioning a one-off project does not use.
_EMPLOYMENT_TITLE_RE = re.compile(r"\bfull[- ]?time\b", re.IGNORECASE)
_EMPLOYMENT_BODY_RE = re.compile(
    r"\b(?:monthly\s+salary|annual\s+salary|salary\s+range|per\s+annum|"
    r"permanent\s+(?:position|role|contract|employment)|"
    r"\d+\s*(?:lpa|lakhs?|k\s*(?:inr|per\s+annum))|"
    r"payroll|umow[ayę]\s+o\s+prac[ęe])\b",
    re.IGNORECASE,
)

# Jan's money floors (2026-09-19). Proposal built from market data: junior n8n
# work in Europe sits at €15–35/h, Upwork n8n freelancers average ~$29/h, and
# Polish junior IT freelancing runs 40–90 zł/h. Jan works with Hermes, which
# compresses delivery time but not client conversation, testing and revisions,
# so the hourly floor protects the worst-case engagements instead of pricing
# the good ones. Below the floor an offer is rejected outright.
PLN_PER_USD = 3.9
MIN_HOURLY_PLN = 90.0
COMFORT_HOURLY_PLN = 150.0
MIN_HOURLY_USD = round(MIN_HOURLY_PLN / PLN_PER_USD, 2)  # ≈ 23 USD/h
COMFORT_HOURLY_USD = round(COMFORT_HOURLY_PLN / PLN_PER_USD, 2)  # ≈ 38 USD/h
LOW_FIXED_USD = 100.0
_FX_TO_USD = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27}

# Jan's rule (2026-09-19): a commission that requires a language other than
# Polish or English is out of reach for now, regardless of how well it fits.
_FOREIGN_LANGUAGES = (
    "spanish", "español", "espanol", "german", "deutsch", "french", "français",
    "italian", "italiano", "portuguese", "português", "russian", "русский",
    "dutch", "nederlands", "czech", "hungarian", "romanian", "turkish",
    "arabic", "chinese", "mandarin", "japanese", "korean", "hindi", "ukrainian",
    "hiszpańsk", "niemieck", "francusk", "włoski", "rosyjsk", "ukraińsk",
    "czeski", "tureck", "arabsk", "chińsk", "japońsk",
)
_LANGUAGE_REQUIREMENT_RE = re.compile(
    r"\b(?:fluent|native|required|requirement|must\s+speak|speaking|"
    r"biegły|wymagany|wymagana|niezbędny|niezbędna|znajomość|konwersacje)\b",
    re.IGNORECASE,
)


def _requires_a_foreign_language(text: str) -> str | None:
    """Name of the required language other than PL/EN, or ``None``.

    The requirement word must sit within ~45 characters of the language name, so
    „our clients are Spanish-speaking, but the work is in English” is not a hit,
    while „fluent Spanish is required” is. Two explicit exceptions: a language
    that only describes other people („Spanish-speaking customers”) and a
    negated requirement („no Spanish needed”).
    """
    lowered = text.lower()
    for language in _FOREIGN_LANGUAGES:
        start = 0
        while (index := lowered.find(language, start)) != -1:
            start = index + len(language)
            after = lowered[start : start + 10]
            if after.startswith(("-speaking", " speaking")):
                continue  # describes somebody else, not a job requirement
            before = lowered[max(0, index - 25) : index]
            if re.search(r"\b(?:no|not|without|bez|nie)\b", before):
                continue  # „no Spanish needed on your side”
            window = lowered[max(0, index - 45) : index + len(language) + 45]
            if _LANGUAGE_REQUIREMENT_RE.search(window):
                return language
    return None

# Jan's rule (2026-09-21): a listing published ten or more days ago is dead
# ground — dozens of freelancers have already pitched, and the same window is
# why the model rejected „Firma Perca zleci wykonanie Landing page” (07.09, 91
# sent offers) while keeping the equally old Codex/Site GPT ad. The rule is
# deterministic and metadata-driven: it fires only when the source actually
# reports ``published_at`` (Useme prints the date on the offer page, not in the
# listing, so in listing mode the rule is silent there — that is honest, not
# hidden).
MAX_LISTING_AGE_DAYS = 10


def _listing_age_days(lead: JobLead, now: datetime | None = None) -> int | None:
    """Age of the listing in whole days, or ``None`` when the source has no date."""
    published = lead.published_at
    if published is None:
        return None
    reference = now if now is not None else datetime.now(UTC)
    return max(0, (reference - published).days)


# Jan's fourth axis (2026-09-21): „szukam człowieka do przepisywania faktur” is
# not a commission — it is an invitation to propose automation instead of a
# hire. The bucket is model-driven, but it only survives when the text really
# describes repetitive work, so a mislabelled real commission cannot silently
# leave the TOP pile and an employment ad cannot slip in as a false signal.
_REPETITIVE_WORK_RE = re.compile(
    r"\b(?:przepisywan\w*|wprowadzani\w+ danych|wpisywani\w+ danych|"
    r"przepisywani\w+ danych|dane z faktur|faktur do arkusza|"
    r"obsług\w+ (?:zgłoszeń|maili|skrzynki|korespondencji|klientów)|"
    r"odpowiadani\w+ na (?:maile|wiadomości)|odpisywani\w+ na maile|"
    r"kopiowani\w+ danych|przenoszeni\w+ danych|"
    r"aktualizacj\w+ (?:cennika|stanów|danych|oferty|asortymentu)|"
    r"wystawiani\w+ (?:faktur|dokumentów)|wypełnian\w+ dokumentów|"
    r"wypełnian\w+ arkuszy|raportowanie cykliczne|"
    r"ręczn\w+ (?:przepisyw\w+|wpis\w+|wprowadz\w+|kopiow\w+|uzupełni\w+)|"
    r"praca manualna|powtarzaln\w+|żmudn\w+ (?:prac\w+|wpisywanie)|"
    r"data entry|enter(?:ing)? data|manual (?:data|work|task)|repetitive (?:task|work|"
    r"process)|copy(?:ing)? data|transcrib\w+|invoice data|"
    r"updating (?:prices|inventory|stock)|answering (?:emails|messages)|"
    r"uporządkowani\w+ danych|porządkowani\w+ danych|deduplikacj\w+ danych|"
    r"scalani\w+ danych)\b",
    re.IGNORECASE,
)

# Jan's verdict 2026-09-21 („Aplikacja pos dla gastronomii” = reject): work on
# device-native layers and physical hardware is outside the scope, however
# tempting the web part of the system looks. The model hedged this exact ad to
# MOŻE while writing „wykracza poza zakres” in its own rationale, so the
# exclusion is deterministic.
_DEVICE_WORK_RE = re.compile(
    r"\b(?:drukark\w+ fiskaln\w+|fiskalizacj\w+|Posnet|Novitus|SUNMI|SoftPOS|"
    r"terminal\w* płatnicz\w+|natywn\w+ warstw\w+ (?:Android|iOS)|"
    r"SDK (?:producent\w+ )?urz\w+dz\w+|native (?:Android|iOS) layer|device SDK)\b",
    re.IGNORECASE,
)

# Oferteo's public list hides the description, so a lead is a bare title. Title
# keywords about hardware repair, computer service, home networks, data recovery
# and 3D printing therefore score 100/100 (the model has nothing else to read)
# and land in TOP — with a *paid* contact. Jan's Oferteo profile lists exactly
# these categories as the ones not to select ("żeby nie zbierać śmieciowych
# zapytań"). Jan's verdict 2026-09-25 (pierwsza wersja tylko degradowała do MOŻE):
# te zapytania nie są naszą robotą w żadnym kubełku, więc lecą do ODRZUĆ — nie
# płacimy punktów za podglądanie serwisu komputerowego.
_OFERTEO_SERVICE_TITLE_RE = re.compile(
    r"\b(?:"
    r"napraw\w*\s+(?:komputer\w*|laptop\w*|peceta|pc\b|drukark\w*|telefon\w*|sprz[ęe]tu)"
    r"|serwis\w*\s+(?:komputerow\w*|laptopow\w*|pc\b|sprz[ęe]tu)"
    r"|sie[cć]\w*\s+komputerow\w*"
    r"|specjalist\w*\s+od\s+(?:sieci|serwisu)"
    r"|(?:sie[cć]|internet\w*|po[łl][ąa]czeni\w+|problem\w*)\s+(?:z\s+)?"
    r"(?:wi-?fi|lan\b|bezprzewodow\w*)"
    r"|internet\w*\s+stacjonarn\w*|dostawc\w+\s+internetu"
    r"|konfiguracj\w+\s+(?:komputer\w*|sieci|oprogramowani\w+)"
    r"|instalacj\w+\s+oprogramowani\w+"
    r"|odzyskani\w+\s+danych|odzyska\w+\s+dane|recovery\s+danych"
    r"|druk\w*\s*3\s*d\b|drukark\w+\s+3d\b"
    r"|instalacj\w+\s+i\s+konfiguracj\w+\s+komputer\w*"
    r")\b",
    re.IGNORECASE,
)

# A weak agency mention only rejects when the author is *selling*; when the same
# post asks for someone to work with them, it is a commission (finding 2026-09-19).
_BUYER_SEARCH_RE = re.compile(
    r"\b(?:we(?:['\u2019]re| are)?\s+(?:looking|loking)\s+for|i(?:['\u2019]m| am)\s+looking\s+for|"
    r"looking\s+for\s+(?:an?|someone|a|several)|we\s+need\s+(?:an?|someone|help)|"
    r"seeking\s+(?:an?|a|someone)|we\s+want\s+to\s+(?:hire|work\s+with)|"
    r"szukamy|poszukujemy|poszukuj[eę]|szukam\s+(?:osoby|wykonawcy|specjalisty|programisty|"
    r"firmy|kogoś|integratora|developera))\b",
    re.IGNORECASE,
)

# Szum grupowy, który nie jest ogłoszeniem w żadnym kierunku: administracyjne
# powitania (w grupach FB zjadają 3 z 6 postów — pomiar 2026-09-21), zaproszenia
# na webinary i promocje szkoleń. Wchodzi do polityki bez bramki `_BUYER_SEARCH_RE`,
# bo klient nie pisze „powitajmy nowych członków”, a wzorce są celowo wąskie.
_GROUP_NOISE_RE = re.compile(
    r"\bpowitajmy\s+nowych\s+cz[łl]onk[óo]w\b|"
    r"\bwitamy\s+nowych\s+(?:uczestnik[óo]w|cz[łl]onk[óo]w)\b|"
    r"\bprzedstaw\s+si[ęe]\s+w\s+komentarzu\b|"
    r"\bnew\s+posts\b|"
    r"\b(?:bezp[łl]atn\w+|darmow\w+)\s+(?:webinar|szkoleni|kurs)\w*|"
    r"\bwidzimy\s+si[ęe]\s+(?:jutro\s+)?(?:o\s+\d|na\s|za\s+tydzie[ńn])|"
    r"\bdo\s+zobaczenia\b",
    re.IGNORECASE,
)


# Oferty handlowe: sprzedaż produktu, cennik hurtowy, wyprzedaż, CTA produktu SaaS.
# Zmierzone na żywo 2026-09-21 (pierwsze dostarczenie FB na Telegram): „Folia stretch
# … hurtowo prosto od importera. Cena od 22,59 zł netto" oraz „Twoja oferta … na Dashy
# … Klient kupuje usługę … zacznij za darmo" weszły do TOP jako zlecenia. Model nie jest
# tu deterministyczny: bliźniaczy post tego samego sprzedawcy (folia 23 mikrony) w tym
# samym przebiegu trafił do ODRZUĆ. Reklama produktu nie ma klienta, więc łapiemy ją
# deterministycznie; bramka `_BUYER_SEARCH_RE` chroni klienta, który np. „sprzedaje na
# Allegro i szuka osoby do poprowadzenia konta”.
_SUPPLIER_AD_RE = re.compile(
    r"\bhurtowo?\b|"
    r"\bcena\s+od\s+\d+[,.]?\d*\s*z[łl]\b|"
    r"\b\d+[,.]?\d*\s*z[łl]\s+netto\b|"
    r"\bwysy[łl]ka\s+gratis\b|"
    r"\bzam[óo]w\s+(?:teraz|ju[żz])\b|"
    r"\bklient\s+kupuje\s+usługę\b|"
    r"\bzacznij\s+za\s+darmo\b|"
    r"\bsprzedam\b",
    re.IGNORECASE,
)


# Bramka treści dla grup Facebooka (2026-09-25). Post z grupy to surowy tekst z social
# mediów: nie ma budżetu, widełek, typu umowy ani nawet własnego tytułu, więc model nie ma
# się czego zaczepić i daje 100/100 niemal wszystkiemu. Pomiar na produkcyjnym payloadzie
# 25.09 (61 postów z 7 grup, `scratch/fb-filter-probe.py`): po syntetycznym TOP/100 polityka
# przepuszczała **49 z 61** postów, w tym „Dziś pierwszy dzień jesieni!”, „Poznań.”,
# „Trwa promocja na kurs Accounting English”, „Choinki, lampki, bombki: jedna dostawa,
# jedna faktura” i „Cześć, mam na imię Magdalena.”. Dlatego w grupach FB obowiązuje jedna
# dodatkowa bramka: post musi mieć **ślad intencji klienta**, czyli słowo, którym ktoś
# prosi kogoś o pracę. Bramka czyta wyłącznie słowa autora — `_client_voice_text` zdejmuje
# nazwę grupy, bo „Szukam wykonawcy” w nazwie grupy fałszowało wynik (regresja 2026-09-21).
# Świadomie wąska: klient piszący „proszę o oferty z orientacyjnymi cenami” ma intencję,
# a „proszę o poradę” (pytanie do grupy) już nie.
_FB_CLIENT_INTENT_RE = re.compile(
    r"\b(?:szukam|szukamy|poszukuj[eę]|poszukujemy|poszukuje)\b|"
    r"\bpotrzebuj[eę]\b|\bpotrzebujemy\b|"
    r"\bpotrzebn[ay]\s+(?:osob|firm|ręk|wykonawc|specjalist|develop|programist|ekip|kogo)\w*|"
    r"\bzlec[ęe]\b|\bzlecam\b|\bchc[ęe]\s+zleci[ćc]\b|"
    r"\bmam\s+(?:zlecenie|projekt)\b|\bosob[ay]\s+do\s+wykonania\b|"
    r"\bpole[ćc]cie\b|\bkto[śs]\s+poleci\b|\bkto\s+poleci\b|\bkogo\s+polecacie\b|"
    r"\bmo[żz]e\s+poleci[ćc]\b|\bmo[żz]e\s+kto[śs]\s+poleci[ćc]\b|"
    r"\bkto\s+(?:zrobi|umie|pomo[żz]e|ogarnie|podejmie|si[ęe]\s+zajmie)\b|"
    r"\bprosz[ęe]\s+o\s+(?:wycen[ęe]|ofert[ęy]|oferty|kontakt\s+w\s+sprawie|piln[ąa]\s+pomoc)\b|"
    r"\bpotrzebuj[ęe]\s+pomocy\b|\bszukam\s+pomocy\b|"
    r"\bwycen[ay]\s+(?:zadania|projektu|wdro[żz]enia)\b|"
    r"\bbud[żz]et\s+(?:to|na|do|mam)\b|\bzap[łl]ac[ęe]\b|\bp[łl]ac[ęe]\s+za\b|"
    r"\bbrief\w*\b|"
    r"\blooking\s+for\b|\bneed\s+(?:a|an|someone|help|some)\b|\bhiring\b|"
    r"\banyone\s+(?:available|interested|recommend)\b|\bcan\s+anyone\s+recommend\b|"
    r"\bsend\s+(?:me\s+)?your\s+(?:quote|rate|offer)\b",
    re.IGNORECASE,
)


# Klient, który wprost odmawia automatyzacji/AI/skryptów, nie jest klientem (Jan,
# 2026-10-27, na przykładzie „Wprowadzanie 750 zderzaków do BaseLinker”: „Nie interesują
# mnie skrypty AI ani automaty – branża wymaga ludzkiego oka.”). Przekonywanie go to
# strata wiadomości i czasu, więc ogłoszenie odpada od razu, niezależnie od kubełka,
# a nie dopiero wtedy, gdy model uzna je za ZAMIEN. Wzorce są wąskie: refusal musi
# sąsiadować z nazwą automatyzacji i stać w jednym zdaniu, więc „chcę narzędzie bez AI”
# (wymaganie projektowe) i „szukam automatyzacji” (zlecenie) przechodzą nietknięte.
_NO_AUTOMATION_RE = re.compile(
    r"nie\s+interesuj\w*\s+(?:mnie|nas)[^.!?\n]{0,80}?"
    r"(?:automat\w*|skrypt\w*|bot\w*|\bai\b|sztuczn\w*\s+inteligencj\w*)"
    r"|(?:automat\w*|skrypt\w*|boty)\s+(?:mnie\s+|nas\s+)?nie\s+interesuj\w*"
    r"|nie\s+(?:chc[ęe]|chcemy|potrzebuj[ęe]|potrzebujemy|szukam|szukamy)\s+"
    r"(?:[a-ząćęłńóśźż]+\s+){0,3}?"
    r"(?:automat\w*|skrypt\w*|bot\w*|\bai\b|sztuczn\w*\s+inteligencj\w*)"
    r"|prosz[ęe]\s+nie\s+(?:proponow\w*|wysy[łl]a\w*|oferow\w*|pisa\w*)"
    r"[^.!?\n]{0,40}?(?:automat\w*|skrypt\w*|bot\w*|\bai\b)"
    r"|\bbez\s+(?:automat\w+|bot[óo]w|skrypt[óo]w)\b"
    r"|\bno\s+(?:ai|bots?|automation|scripts?)\b"
    r"|\b(?:not|never)\s+(?:looking\s+for|interested\s+in|want)\s+(?:any\s+)?"
    r"(?:ai|automation|bots?|scripts?)\b"
    r"|\bwithout\s+(?:any\s+)?(?:ai|automation|bots?|scripts?)\b",
    re.IGNORECASE,
)


def hourly_rate_range(lead: JobLead) -> tuple[float, float] | None:
    """USD hourly range from the source's budget pill, or ``None`` if unknown."""
    data = lead.raw_data or {}
    if data.get("rate_unit") != "hour":
        return None
    factor = _FX_TO_USD.get((lead.currency or "USD").upper())
    low = data.get("rate_min")
    high = data.get("rate_max")
    if factor is None or not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    return (float(low) * factor, float(high) * factor)


def fixed_project_usd(lead: JobLead) -> float | None:
    """USD value of a one-off project budget from the source's pill, if known."""
    data = lead.raw_data or {}
    if data.get("rate_unit") != "project":
        return None
    factor = _FX_TO_USD.get((lead.currency or "USD").upper())
    amount = data.get("rate_max")
    if factor is None or not isinstance(amount, (int, float)):
        return None
    return float(amount) * factor


def _downgrade(entry: ScoredLead, reason: str) -> ScoredLead:
    """Move one TOP entry down to MOŻE, keeping the model's score."""
    return ScoredLead(entry.lead, entry.score, ScoreBucket.MOZE, reason, entry.gap)


def _client_voice_text(lead: JobLead, body: str) -> str:
    """Text the buyer-language guard reads: the author's words, without source metadata.

    Facebook lead titles are built as ``"<group name>: <first sentence>"``, and group
    names themselves carry buyer words („Szukam wykonawcy", „Szukam programisty",
    „Zlecenia programistyczne | Szukam wykonawcy"). Feeding that title to
    :data:`_BUYER_SEARCH_RE` made **every** seller ad from those groups look like a
    client request, which silently disabled the anti-ad rules — live on 2026-09-21
    „TANIE STRONY INTERNETOWE – OD 300 ZŁ" landed in TOP for exactly this reason.
    A group name is metadata, not the client's voice, so it is stripped before the
    guard runs. Sources without ``raw_data['group_name']`` are unaffected.
    """
    group = str((lead.raw_data or {}).get("group_name") or "").strip()
    if group:
        body = body.replace(f"{group}: ", "", 1)
    return body


def _zamien_rejection(body: str, client_voice: str | None = None) -> str | None:
    """Why a ``ZAMIEN`` lead must not stay in the fourth bucket, or ``None``.

    The bucket exists to be *measured* over several runs, so it must not collect
    noise: a provider ad, an offer requiring a language Jan does not speak, or
    an ad with no trace of repetitive work all fall back to the normal rules
    (they are rejected here, exactly as they would have been before the bucket
    existed).
    """
    if _requires_a_foreign_language(body) is not None:
        return (
            "Odrzucone zgodnie z profilem: zlecenie wymaga języka innego niż polski "
            "lub angielski — nie da się zaproponować automatyzacji zamiast zatrudnienia."
        )
    guard = client_voice if client_voice is not None else body
    if (
        _SELLER_DELIVERS_RE.search(body)
        or _SELLER_THIRD_PERSON_RE.search(body)
        or (_PROVIDER_PITCH_RE.search(body) and not _BUYER_SEARCH_RE.search(guard))
        or _AUTOMATOR_POST_RE.search(body)
        or (_SELF_IDENTITY_RE.search(body) and _OFFER_RE.search(body))
    ):
        return (
            "Odrzucone zgodnie z profilem: to reklama wykonawcy, a nie ogłoszenie "
            "o zatrudnieniu kogoś do pracy — nie ma tu czego automatyzować."
        )
    if not _REPETITIVE_WORK_RE.search(body):
        return (
            "Odrzucone zgodnie z profilem: model wskazał „zaproponuj automatyzację zamiast "
            "zatrudnienia”, ale treść nie opisuje pracy powtarzalnej — nie ma podstawy do "
            "takiej propozycji."
        )
    return None


def apply_user_policy(scored: Sequence[ScoredLead]) -> list[ScoredLead]:
    """Enforce only unambiguous user exclusions after model scoring.

    Seniority is checked in the title, not the description, so a client can
    still say they want a senior-level outcome without losing a freelance
    automation project. Provider/agency language must be explicit in the
    title or body; an agency that is *buying* („we're looking for…") keeps its
    place, because a buyer mentioning an agency is a client. Money is checked
    deterministically: a joke hourly rate rejects, a marginal one demotes a TOP
    to MOŻE instead of hiding it. Two metadata/axis rules sit on top: a listing
    the source dates ten days back or more is dead ground (Jan 2026-09-21), and
    the ``ZAMIEN`` bucket survives only for real repetitive-work ads.
    """
    result: list[ScoredLead] = []
    for entry in scored:
        lead = entry.lead
        title = lead.title or ""
        body = f"{title}\n{lead.description or ''}"
        client_voice = _client_voice_text(lead, body)
        age_days = _listing_age_days(lead)
        if age_days is not None and age_days >= MAX_LISTING_AGE_DAYS:
            result.append(
                ScoredLead(
                    lead,
                    0,
                    ScoreBucket.ODRZUC,
                    f"Odrzucone zgodnie z profilem: ogłoszenie opublikowane {age_days} dni "
                    f"temu (próg {MAX_LISTING_AGE_DAYS} dni) — oferta jest przebrana, "
                    "a konkurs rozstrzygnięty bez nas.",
                    entry.gap,
                )
            )
            continue
        if _NO_AUTOMATION_RE.search(client_voice):
            result.append(
                ScoredLead(
                    lead,
                    0,
                    ScoreBucket.ODRZUC,
                    "Odrzucone zgodnie z profilem: klient wprost pisze, że nie chce "
                    "automatyzacji, AI ani skryptów — takiego zlecenia nie przekonujemy, "
                    "tylko od razu odkładamy.",
                    entry.gap,
                )
            )
            continue
        if entry.bucket is ScoreBucket.ZAMIEN:
            zamien_reason = _zamien_rejection(body, client_voice)
            if zamien_reason is None:
                result.append(entry)
            else:
                result.append(ScoredLead(lead, 0, ScoreBucket.ODRZUC, zamien_reason, entry.gap))
            continue
        reason: str | None = None
        if _SENIOR_TITLE_RE.search(title):
            reason = "Odrzucone zgodnie z profilem: stanowisko seniorskie/lead, a nie zlecenie automation dla freelancera."
        elif _EMPLOYMENT_CONTRACT_RE.search(lead.contract_type or ""):
            reason = (
                "Odrzucone zgodnie z profilem: źródło oznacza tę ofertę jako etat/"
                "pełny etat, a nie zlecenie dla freelancera."
            )
        elif _EMPLOYMENT_TITLE_RE.search(title) or _EMPLOYMENT_BODY_RE.search(
            lead.description or ""
        ):
            reason = (
                "Odrzucone zgodnie z profilem: ogłoszenie opisuje etat/widełki płacowe "
                "(„full-time”, „monthly salary”, „per annum”, „umowa o pracę”), a nie "
                "zlecenie dla freelancera."
            )
        elif lead.source == "oferteo" and _OFERTEO_SERVICE_TITLE_RE.search(title):
            reason = (
                "Odrzucone zgodnie z profilem: u Oferteo widać sam tytuł, a ten mówi "
                "o serwisie sprzętu, sieci komputerowej, odzyskiwaniu danych albo druku "
                "3D — to nie nasza robota, a kontakt jest płatny punktami."
            )
        elif _DEVICE_WORK_RE.search(body):
            reason = (
                "Odrzucone zgodnie z profilem: ogłoszenie wymaga pracy z urządzeniami i "
                "natywną warstwą platformy (drukarki fiskalne, terminale, SDK producentów), "
                "co jest poza zakresem Jana."
            )
        elif (language := _requires_a_foreign_language(body)) is not None:
            reason = (
                f"Odrzucone zgodnie z profilem: zlecenie wymaga języka innego niż polski "
                f"lub angielski ({language})."
            )
        elif _GROUP_NOISE_RE.search(body):
            reason = (
                "Odrzucone zgodnie z profilem: to nie ogłoszenie, a szum grupowy "
                "(powitanie nowych członków, zaproszenie na webinar albo promocja szkolenia)."
            )
        elif _SUPPLIER_AD_RE.search(body) and not _BUYER_SEARCH_RE.search(client_voice):
            reason = (
                "Odrzucone zgodnie z profilem: to oferta handlowa (produkt, cennik hurtowy, "
                "wyprzedaż albo promocja narzędzia), a nie zlecenie od klienta."
            )
        elif _is_vendor_marketing(title, body):
            reason = (
                "Odrzucone zgodnie z profilem: tytuł zadaje czytelnikowi pytanie sprzedażowe "
                "(„Do you want to automate…?”), a treść mówi o „naszej” ofercie — to reklama "
                "wykonawcy, nie zlecenie od klienta."
            )
        elif _SELLER_DELIVERS_RE.search(body) or _SELLER_THIRD_PERSON_RE.search(body):
            reason = (
                "Odrzucone zgodnie z profilem: autor sam wykonuje pracę dla czytelnika "
                "(„I do the integration myself”, „I map your actions”, „services offered”) "
                "— to pilotaż/partnerstwo sprzedawcy, nie zlecenie dla freelancera."
            )
        elif _PROVIDER_PITCH_RE.search(body) and not _BUYER_SEARCH_RE.search(client_voice):
            reason = (
                "Odrzucone zgodnie z profilem: autor opisuje własne usługi i zaprasza do "
                "swojego kalendarza/„darmowej pomocy” („I run … and help businesses”, „I automate "
                "your…”, „feel free to DM me”) — to reklama wykonawcy, nie zlecenie od klienta."
            )
        elif _AUTOMATOR_POST_RE.search(body) or (
            _AGENCY_NOUN_RE.search(body) and not _BUYER_SEARCH_RE.search(client_voice)
        ) or (_SELF_IDENTITY_RE.search(body) and _OFFER_RE.search(body)):
            reason = "Odrzucone zgodnie z profilem: ogłoszenie wygląda na ofertę usług automatyzacji, nie zlecenie od klienta."
        elif lead.source == "facebook_groups" and not _FB_CLIENT_INTENT_RE.search(client_voice):
            reason = (
                "Odrzucone zgodnie z profilem: post z grupy Facebooka bez śladu intencji "
                "klienta — nikt tu nikogo nie prosi o pracę, tylko pogadanka, pytanie do "
                "grupy, promocja, zaproszenie do interakcji albo sama nazwa miejscowości."
            )
        if reason is not None:
            result.append(ScoredLead(lead, 0, ScoreBucket.ODRZUC, reason, entry.gap))
            continue

        hours = hourly_rate_range(lead)
        if hours is not None and hours[1] < MIN_HOURLY_USD:
            result.append(
                ScoredLead(
                    lead,
                    0,
                    ScoreBucket.ODRZUC,
                    f"Odrzucone zgodnie z profilem: stawka {hours[0]:g}–{hours[1]:g} USD/h "
                    f"(≈ {hours[1] * PLN_PER_USD:.0f} zł/h) jest poniżej progu "
                    f"{MIN_HOURLY_PLN:.0f} zł/h.",
                    entry.gap,
                )
            )
            continue
        if entry.bucket is ScoreBucket.TOP and hours is not None and hours[1] < COMFORT_HOURLY_USD:
            result.append(
                _downgrade(
                    entry,
                    f"{entry.rationale} Stawka {hours[0]:g}–{hours[1]:g} USD/h poniżej "
                    f"komfortowej ({COMFORT_HOURLY_PLN:.0f} zł/h) — do rozważenia, nie priorytet.",
                )
            )
            continue
        fixed = fixed_project_usd(lead)
        if entry.bucket is ScoreBucket.TOP and fixed is not None and fixed < LOW_FIXED_USD:
            result.append(
                _downgrade(
                    entry,
                    f"{entry.rationale} Jednorazowe {fixed:g} USD za drobną pracę — "
                    "niski ticket, nie priorytet.",
                )
            )
            continue
        result.append(entry)
    return result


class DeterministicScorer:
    """Offline-only scorer for double-run E2E; never use in production.

    Buckets are derived from a stable hash of ``(source, source_id)`` only.
    Lead titles, descriptions, budgets and skills are never inspected, so this
    scorer cannot perform any keyword-based (blacklist) rejection. The outcome
    is fully reproducible across separate runs in separate processes, which is
    what the "run twice, second run zero duplicates" check needs.
    """

    def __init__(self, *, seed: bytes = b"snajper-v2-deterministic") -> None:
        self._seed = seed
        self._sequence = (ScoreBucket.TOP, ScoreBucket.MOZE, ScoreBucket.ODRZUC)

    def score(self, leads: Sequence[JobLead]) -> list[ScoredLead]:
        if not leads:
            return []
        scored: list[ScoredLead] = []
        modulus = len(self._sequence)
        for lead in leads:
            digest = hashlib.blake2s(
                f"{self._seed.decode()}|{lead.source}|{lead.source_id}".encode(),
                digest_size=8,
            ).hexdigest()
            index = int(digest, 16) % modulus
            bucket = self._sequence[index]
            score = {ScoreBucket.TOP: 95, ScoreBucket.MOZE: 55, ScoreBucket.ODRZUC: 20}[bucket]
            scored.append(
                ScoredLead(
                    lead,
                    score,
                    bucket,
                    f"Deterministyczna ocena offline ({index}); tekst leada nie brany pod uwagę.",
                )
            )
        return scored


def render_rejected_audit(scored: Sequence[ScoredLead]) -> str:
    """Build a simple plain-text audit of the ODRZUĆ bucket results.

    This is deliberately minimal: one block per rejected lead with its score,
    source, URL and the model's rationale, so rejected candidates stay
    inspectable without building any auditing subsystem.
    """
    lines: list[str] = []
    for entry in scored:
        if entry.bucket is not ScoreBucket.ODRZUC:
            continue
        lines.append(
            f"[ODRZUĆ] {entry.score}/100 — {entry.lead.title} ({entry.lead.source},"
            f" {entry.lead.source_id})"
        )
        lines.append(f"  url: {entry.lead.url}")
        lines.append(f"  rationale: {entry.rationale}")
    return "\n".join(lines)


__all__ = [
    "MAX_GAP_LEN",
    "MAX_LISTING_AGE_DAYS",
    "MAX_RATIONALE_LEN",
    "DeterministicScorer",
    "ScoreBucket",
    "ScoredLead",
    "Scorer",
    "apply_user_policy",
    "render_rejected_audit",
]