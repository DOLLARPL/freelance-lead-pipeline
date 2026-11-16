# freelance-lead-pipeline

A lead pipeline that runs the boring half of freelancing: it collects offers from nine sources, filters out duplicates and noise, scores what is left, drafts a reply in the owner's voice and keeps a ledger of what was sent. Around 31,000 lines of Python, 48 test modules, one operator.

I built it for my own work. It has been running since September 2026 and has processed several thousand listings.

## How it works

```
sources (9 adapters)
   │  collect: Useme, Oferteo, Rocketjobs, NoFluffJobs, JustJoinIT,
   │           Facebook groups, Discourse, Make community, n8n community
   ▼
normalise  → one Lead model, source-agnostic
   ▼
dedup      → lead identity: same offer from two sources is one lead
   ▼
score      → deterministic rules first, LLM second (buckets: TOP / MOŻE / ODRZUCONE)
   ▼
report     → text digest, plus JSON for the panel
   ▼
draft      → proposal in the operator's voice, with price and scope
   ▼
delivery   → email or portal message, confirmation recorded
   ▼
state      → atomic commit: a crash never leaves a half-written state file
```

Two design choices matter more than the rest:

**Deterministic scoring before the model.** Rules remove obvious mismatches (wrong budget, wrong technology, wrong country) for free. The LLM only sees what survives, which keeps the cost at a few cents per run and makes the score reproducible.

**Atomic state.** The pipeline commits state and report together. A failed run either completed or did not happen; there is no state where a lead is marked as sent but was never sent.

## Sources

| Adapter | Type | Notes |
|---|---|---|
| Useme | marketplace | offers with budget ranges |
| Oferteo | lead marketplace | email ingestion, revealed-contact parsing |
| Rocketjobs | job board | contract and B2B listings |
| NoFluffJobs | job board | salary-normalised listings |
| JustJoinIT | job board | API-shaped listings |
| Facebook groups | social | public group posts, manual assist |
| Discourse | forum | automation and no-code communities |
| Make community | forum | integration gigs |
| n8n community | forum | workflow gigs |

Each adapter is a separate module behind a common interface. Adding a source means writing one adapter and one fixture, not touching the pipeline.

## Panel

A FastAPI + Jinja panel on top of the same engine: lead list with filters, lead detail, status changes, draft generation and improvement, notes, thread preview, quote builder, mail send, Facebook outreach plan, push notifications (Web Push), CSV/JSON import and export. Routes are in `src/freelance_pipeline/web/routes.py`.

The panel runs privately behind authentication and is not part of this repository's demo. The engine, the panel code and the tests are here.

## Quality gates

- 48 test modules, offline fixtures, no network in CI.
- End-to-end offline run that can be executed twice against the same fixture set and must produce the same output.
- Prompt-injection and hallucination checks on generated proposals (`proposal_check.py`): the draft must not invent a price, a deadline or a portfolio item.
- Ruff for linting.

```bash
uv sync --group dev
uv run pytest -q
uv run python -m freelance_pipeline.cli run --state /tmp/s.json --report /tmp/r.txt --fixtures tests/fixtures
```

## Configuration

Everything sensitive comes from the environment: `OPENROUTER_API_KEY`, mail credentials, Telegram flags. No credentials, no client data and no scraped content are included in this repository. `data/`, `config/` and `research/` are git-ignored.

## Scope and limits

This is an operator's tool, not a SaaS: one user, one state file, one server. The scrapers target public listings; where a portal forbids automated access, the pipeline uses its email or manual path instead. Rate limits and robots rules are respected per source.

## Contact

Jan Noga, [jnxautomation.pl](https://jnxautomation.pl), jannoga@jnxautomation.pl

MIT licence, see [LICENSE](LICENSE).
