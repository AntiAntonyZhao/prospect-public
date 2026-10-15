# Prospect

Prospect is a job-posting pipeline for a Canadian new-grad software job search. It pulls
postings from public ATS APIs, rejects what rules can settle cheaply, ranks the rest
against a structured profile with a local embedding model, extracts structured facts
with Claude, and scores each posting with a deterministic formula over model-assigned
skill-gap tiers. Every model output that drives a decision is measured against
hand-labeled postings.

This repository is the public copy of a private project. It runs end to end on fictional
data with no API key (`uv run prospect demo`).

## Pipeline

Cheap and deterministic first, expensive and probabilistic last. Numbers are the private
copy's totals as of its last full run (postings ingested 2026-10-05 and 10-06; the
extract and score batches of 2026-10-06).

| stage | what it does | private run |
|---|---|---|
| ingest | Greenhouse, Lever and Ashby board APIs plus a community new-grad list → `postings`, with content-hash dedup and repost counting | 10,535 postings from 159 employers |
| filter (pre-pass) | rules on ATS metadata only: senior titles, locations unambiguously outside Canada, duplicate applications | 9,374 rejected before any model call |
| rank | local `BAAI/bge-small-en-v1.5` embeddings, cosine vs a profile built from the fact bank | top 150 shortlisted |
| extract | Claude Haiku 4.5 over the Batch API, strict tool calling into pydantic schemas, every fact backed by a verified verbatim quote | 141 postings for $0.71 |
| filter (full pass) | the same rules on the extracted facts: years, location, clearance, P.Eng, work authorization, French-language postings | 27 more rejected |
| score | rules tier each requirement first; Claude Sonnet 5 labels the rest (one cached label per skill); code computes the score | 30 postings for $0.39 |
| digest | ranked shortlist with each posting's gap report and employment-term costs | |

Every model call is logged with its tokens and cost (`prospect cost`), and a daily budget
cap stops a run before it is exceeded.

## Design decisions

- **Rules before models.** A posting the metadata already rules out never reaches a model;
  on the run above that was 89% of postings.
- **Models label, code scores.** The match score is a pure function of the requirement
  tiers and the extracted facts, with weights in `prospect.toml`. Changing the score means
  changing a formula, not a prompt, and the stored breakdown makes it regressable.
- **Evidence or it didn't happen.** Each extracted fact must come with a verbatim quote
  that visibly states the value; code checks the quote against the posting. A reject rule
  whose deciding fact kept no verified quote, or came from a low-confidence extraction,
  flags the posting instead of rejecting it (a years minimum quoted verbatim still
  rejects).
- **Structured output only.** Tool schemas are derived from pydantic models and called in
  strict mode; nothing parses a model's prose.
- **One tier per skill.** A skill's tier label is cached under a key that includes the
  model, the prompt and the fact bank, so a posting's score does not move between runs
  because the model labeled the same skill differently.
- **Absent is not impossible.** A tool the candidate has not used but could learn in a few
  hours is a small gap (Tier 2), not a hard one; code downgrades a model's depth-based
  "hard gap" whose own estimate is under the threshold.
- **A truth-only fact bank.** The profile the pipeline matches against is hand-edited YAML
  with honest levels, aliases (true to say) and adjacent skills (not held). No code writes
  it: a test scans every write site in the package, another spies on `open()` while every
  stage runs, and a Claude Code `PreToolUse` hook turns an agent's edit into an approval
  prompt.
- **Measure what is used.** Extraction is scored per field the way the pipeline consumes
  it, and on the decisions themselves.

## Evaluation

Four extracted fields drive a decision: city (does the job need a move?), posting
language (a posting written in French is rejected), minimum years (above the cap is
rejected) and seniority (an internship costs its term). Earlier prompt versions
(`extract_v2`, `extract_v3`) were evaluated on a 21-posting tuning set. A held-out set
of 30 postings was then labeled blind in an editor (`prospect eval export` /
`eval import`, no model output shown) and never used for tuning. Claude Haiku 4.5 with
`extract_v4` on the held-out set:

| field (as the pipeline uses it) | P | R | F1 |
|---|---|---|---|
| location (home / remote / elsewhere) | 89.7% | 86.7% | 88.1% |
| posting language | 100% | 100% | 100% |
| minimum years | 100% | 94.7% | 97.3% |
| internship | 100% | 100% | 100% |
| **macro** | 96.6% | 93.8% | **95.1%** |

The four decisions agree with the labels on 117 of 120 checks (French reject 30/30,
years reject 30/30, internship 30/30, no move needed 27/30). Under the earlier scoring,
which compared the city name and the seniority label exactly, the same
predictions score 80.9% macro F1; scoring was switched to the pipeline's own reading of
each field after that first held-out run. The labeled sets are not in this repository:
they contain the full text of third-party job postings.

## What the public copy leaves out

The private project also has a tailoring stage, in which code selects fact-bank content by
relevance to a posting the user picks and a model rephrases it into a reviewable résumé
diff rendered to PDF; it is left out because it is built around one person's résumé.
Also removed: the local web UI, a tool that syncs a prose résumé into the fact bank, the
application-side features built on that stage, an aggregator source that was never run,
and all personal data (the real fact bank, the database, labeled postings and
application history). The ingestion, filtering, extraction, scoring and evaluation code
is copied from the private project, with only the edits needed to remove those parts.

## Quickstart

```
uv sync
uv run prospect demo      # offline end to end on fictional data, no API key
uv run pytest -q          # 306 tests, no API key, no network
```

`prospect demo` runs the real stages: 20 synthetic postings at fictional companies, in the
response shapes of the Greenhouse, Lever and Ashby APIs, go through the real adapters,
filters, ranking and scoring against a fictional profile (`demo/factbank_demo.yaml`). The
model stages use the real client with recorded API responses (`demo/responses/`, which
cost $0.13 to record) replayed in place of the network. The first run downloads the bge
model; if that fails, the demo ranks with TF-IDF and says so. Each posting's outcome:

```
 id  outcome                            posting
  1  scored 97                          Hooli — Software Engineer, New Grad
  2  rejected: senior_title             Hooli — Senior Backend Engineer
  3  rejected: location_metadata        Hooli — Software Engineer, University Graduate
  4  rejected: location:country         Vandelay Industries — Backend Developer
  5  rejected: clearance                Vandelay Industries — Software Developer, Defence Systems
  6  scored 67                          Vandelay Industries — Data Engineer
  7  scored 87                          Massive Dynamic — Machine Learning Engineer
  8  rejected: years                    Massive Dynamic — Software Engineer II
  9  scored 80                          Tyrell Corporation — Software Developer Intern - Winter 2027
 10  scored 70                          Tyrell Corporation — Full-Stack Developer
 11  rejected: french:posting_in_french Tyrell Corporation — Développeur logiciel junior
 12  scored 15                          Cyberdyne Systems — Low-Latency C++ Developer
 13  scored 61                          Cyberdyne Systems — Junior Software Developer (Part-Time)
 14  rejected: years:requirement        Cyberdyne Systems — Backend Developer (Python)
 15  scored 90                          Sterling Cooper Digital — Junior Software Developer
 16  scored 63                          Sterling Cooper Digital — AI Engineer, New Grad
 17  scored 78                          Dunder Mifflin Paper Company — Software Engineer, Platform
 18  rejected: senior_title             Dunder Mifflin Paper Company — Engineering Manager, Payments
 19  rejected: peng                     Dunder Mifflin Paper Company — Embedded Software Engineer
 20  scored 90                          Dunder Mifflin Paper Company — QA Automation Developer
```

### Live mode

```
cp .env.example .env                     # set ANTHROPIC_API_KEY
cp prospect.toml.example prospect.toml   # [paths] factbank: your own profile (docs/FACTBANK.md)
uv run prospect factbank lint
uv run prospect db init
uv run prospect run                      # ingest → filter → rank → extract → filter → score → digest
```

`companies.yaml` lists a few example boards; add your own and check them with
`prospect companies check`. `budget.daily_usd` (default $2.00) caps a day's spend.
`prospect eval export`, `eval import` and `eval extract --model {haiku,sonnet,opus}
--set FILE` label a held-out set and compare models on it. `prospect demo --record`
re-records the demo responses with your key.

## Stack

Python 3.12, uv, typer, pydantic v2, SQLite (stdlib), httpx, sentence-transformers
(`BAAI/bge-small-en-v1.5`, local), the Anthropic SDK (Claude Haiku 4.5 via the Batch API
for extraction, Claude Sonnet 5 for tier labels, prompt caching), structlog, pytest, ruff,
and a Claude Code `PreToolUse` hook. The `prospect/` package has 37 modules (45 Python
files with `__init__.py`) and 7 versioned prompts; `tests/` has 306 tests in 17 files.
`CLAUDE.md` is the spec the code was built against.
