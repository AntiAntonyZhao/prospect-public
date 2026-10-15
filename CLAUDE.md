# Prospect — CLAUDE.md

Job-posting ingestion, filtering, extraction, scoring and evaluation pipeline for a
Canadian new-grad software job search. It ingests postings from public ATS APIs,
filters them with Canada-specific rules, extracts structured facts with LLMs, and
scores them against a hand-maintained fact bank. Every LLM output is evaluated
against hand-labeled data.

Out of the box the fact bank is a fictional profile, `demo/factbank_demo.yaml`.

@docs/FACTBANK.md

## Stack

- Python 3.12, `uv` for env + deps, `typer` CLI, `pydantic` v2 for every schema
- SQLite via stdlib `sqlite3`; DDL lives in `db/schema.sql`, applied by `prospect db init`
- `httpx` for fetching, `sentence-transformers` (local, `BAAI/bge-small-en-v1.5`) for embeddings — no embedding API key
- `anthropic` SDK. Model IDs: `claude-haiku-4-5-20251001`, `claude-sonnet-5`, `claude-opus-5`
- `pytest` for tests, `ruff` for lint/format

## Commands

```
uv sync                          # install
prospect demo [--record]         # offline end-to-end run on the fictional demo data (no key)
prospect db init                 # create/migrate SQLite from db/schema.sql
prospect factbank lint           # validate the fact bank (schema + evidence + alias rules)
prospect companies check         # open every companies.yaml board and count its jobs
prospect run                     # daily orchestrator: ingest → filter → rank → extract → filter → score → digest
prospect ingest                  # run all sources in sources/, upsert postings
prospect filter [--full]         # apply hard filters → postings.status (pre-pass; --full after extract)
prospect rank                    # embed + cosine → shortlist
prospect extract [--batch]       # Haiku → posting_facts + requirements
prospect score [--fresh-tiers]   # tier classification (Sonnet, cached per skill) + deterministic score
prospect digest                  # print the ranked shortlist with gap reports
prospect log <posting_id> [--referral]   # record a manual application (read by dedup + ghost rules)
prospect status <application_id> STATUS  # submitted | screen | interview | offer | rejected | ghosted
prospect eval extract --model {haiku,sonnet,opus} [--set FILE]   # P/R/F1 per field vs labels + $/posting
prospect eval export [--n 30] / eval import FILE         # label a held-out set in an editor
prospect eval report [--run ID]  # re-print an eval run's metrics
prospect cost [--since DATE]     # spend by stage and model from the runs table
uv run pytest && uv run ruff check .
```

## Repo layout

```
prospect/
  cli.py               # typer entrypoints only; no logic
  config.py            # settings from prospect.toml + .env (ANTHROPIC_API_KEY)
  demo.py              # `prospect demo`: real stages over local postings + replayed model responses
  models/              # pydantic: RawPosting, PostingFacts, Requirement, FactBank
  sources/             # one adapter per source; each implements Source.fetch() -> Iterable[RawPosting]
    greenhouse.py lever.py ashby.py github_newgrad.py
  pipeline/
    ingest.py filter.py rank.py extract.py score.py digest.py archive.py run.py
  llm/
    client.py          # single wrapper: model routing, prompt caching, batch, cost logging to runs
    prompts/           # one .md per prompt; versioned; never inline prompts in code
  factbank/            # loader, linter, alias index (read-only)
  eval/                # labeling CLI, label files, metrics, model comparison
  progress.py          # progress callbacks for long stages (no-op from the CLI)
db/schema.sql
docs/FACTBANK.md
demo/                  # fictional fact bank, companies, postings and recorded model responses
companies.yaml         # {name, ats, token, careers_url} — verify every token by opening the URL
prospect.toml          # targets, filters, caps, budgets (see prospect.toml.example); gitignored
tests/
```

## Pipeline

Cheap-and-deterministic first, expensive-and-probabilistic last. Each stage
reads/writes SQLite and is independently re-runnable.

| stage   | AI              | in → out                                  |
|---------|-----------------|-------------------------------------------|
| ingest  | none            | sources → `postings` (hash, repost_count) |
| filter  | none (rules)    | `postings.status` = rejected/candidate    |
| rank    | local embedding | candidates → top-N by cosine vs profile   |
| extract | Haiku (Batch)   | JD text → `posting_facts`, `requirements` |
| score   | Sonnet + code   | requirements → tiers → `scores`           |
| digest  | none            | ranked shortlist with gap reports         |

`applications` (written by `prospect log`) feeds two rules: the duplicate-application
reject and the ghost flag/penalty.

Daily budget cap in `prospect.toml` (`budget.daily_usd`, default 2.00);
`llm/client.py` aborts the run when exceeded.

## Hard rules

These are the project's integrity constraints. They are not style preferences.
If a task would violate one, stop and say so instead of finding a workaround.

1. **The fact bank is truth-only and no code writes it.** It is edited by hand, or
   by Claude Code one edit at a time with the user's approval: the `PreToolUse`
   hook (`.claude/hooks/protect_factbank.py`) turns every Edit of `factbank.yaml`
   into an approval prompt, blocks rewriting the whole file, and blocks shell
   writes to it. A skill goes in only after it is acquired. `test_hard_rules.py`
   scans every write site in the package and spies on `open()` while every stage
   runs; `test_factbank_hook.py` pins the hook's ask/block behaviour.
2. **Sources are public JSON APIs or explicitly permitted feeds.** No scraping
   of LinkedIn, Indeed, or any site whose ToS forbids it. Adding a source means
   adding an adapter with a `terms_url` field and a one-line justification in
   its docstring.
3. **Every LLM call is logged** to `runs` with model, input/output/cached
   tokens, and computed USD. No exceptions, including eval runs.
4. **LLMs produce labels; code produces scores.** The match score is a pure
   function of `requirements` tiers and `posting_facts`. Changing the score
   means changing a formula, not a prompt.

## Gap classification (three tiers)

Input: one `Requirement` extracted from a JD
(`text`, `skill_norm`, `must_have: bool`, `years: int|None`, `context`).
Output: `tier ∈ {0,1,2,3}`, `matched_node: str|None`, `evidence: list[str]`,
`est_hours: float|None`.

Rules decide first (`score.deterministic_tier`): human languages, a years
requirement above both the fact bank's work years and the cap, credentials and
degrees, exact skill matches and alias matches. Sonnet labels only what the
rules leave open, and `enforce_tier_rules` re-checks its label in code (a
requirement in some skill's `adjacent` list is capped at Tier 2 there).

**Tier 0 — HAVE.** `skill_norm` matches a fact bank skill id (exact or
canonical synonym). Not a gap. `strong` when `level ≥ working`; `weak` when
`level = familiar` (true, but the candidate could not be interviewed on it in
depth), which costs a little on a must-have.

**Tier 1 — RENAME (truthful reframing).** Either (a) `skill_norm` matches an
entry in some skill's `aliases`, or (b) the model can point to specific bullet
ids whose content substantively demonstrates the requirement. **Tier 1
requires an evidence chain: the model must cite the node ids. No citation →
not Tier 1.** Not a gap.

**Tier 2 — ADJACENT (small gap).** Not possessed. Either the requirement
appears in some skill's `adjacent` list, or the model estimates
`est_hours ≤ tiers.t2_max_hours` (default 8) to reach "can discuss it and
show a small demo". Reported in the gap report with `est_hours` and
`nearest_node`.

**Tier 3 — HARD GAP.** Any of: a stated years minimum above `targets.max_years`;
certifications; professional designations (PEng); security clearance; human
languages the candidate lacks; degrees not held; deep specialties the model
estimates `> t2_max_hours` (compilers, kernels, low-latency C++, FPGA, etc.).
Heavily penalizes the score when `must_have`. The model names the reason
(`t3_reason` ∈ credential, degree, language, depth). **Absent from the fact
bank is not a hard gap**: a tool learnable in a few hours is Tier 2, and code
turns a `depth` (or unreasoned) Tier 3 whose own `est_hours ≤ t2_max_hours`
into Tier 2 (`enforce_tier_rules`, `score_v3`).

**Soft requirements are never gaps.** Traits, interests, values and work
styles ("passion for transportation", "thrive in a startup", "comfort with
ambiguity") are not a skill match. The score model labels each one `soft`,
the row is stored as `kind = soft`, and it is never tiered or scored.
Softness belongs to the sentence, not the skill ("genuine interest in
distributed systems" vs "experience building distributed systems"), so it is
kept out of the per-skill tier cache: wording that states only an interest is
soft by a code rule (`guards.expresses_interest`) before any model call, and a
model soft label on interest wording is not cached.

Tie-breaking: when unsure between 1 and 2, choose 2. When unsure between 2
and 3, choose 3. The system errs toward under-claiming.

**One tier per skill.** The model's label is cached in `tier_cache` under the
normalized `skill_norm` (`score.tier_key`: case, accents, spacing and the
order of `A | B` alternatives don't matter) and reused by every later posting
and run; a skill listed twice in one posting is asked once. The cache key
includes the model, the score prompt and the serialized fact bank
(`score.tier_context`), so editing the fact bank or bumping the prompt starts
fresh. The stored value is the model's label, so `enforce_tier_rules`
re-applies on every read. A skipped label (the cautious Tier 3 default) is
never cached. `prospect score --fresh-tiers` asks again and overwrites.

## Match score (deterministic, in `pipeline/score.py`)

```
score = 100
      - 25 * (# must_have Tier 3)
      - 10 * (# must_have Tier 2)
      -  3 * (# nice_to_have Tier 2/3)
      -  2 * (# Tier 0 weak on must_have)
      + fit(location)                                             # ∈ [-30, 0]
      - 30 * max(0, 1 - hours_per_week / 37.5)     # part-time        0..30
      - 30 * max(0, 1 - term_months / 12)          # contract/intern  0..30
      - ghost_penalty(repost_count, prior_no_response)            # 0..20
```
`fit(location)` prices a move, not a city preference: 0 in the home city
(`identity.location.city`) or for a remote role, −10 elsewhere in Canada, −5
when unknown. The posting's whole ATS location list counts: the home city
anywhere in it, or any remote option, is 0 — never lower for listing more
places and never higher for offering both (`score.location_key`; the
breakdown records which key applied). A role is remote when it offers **any**
remote option; `extract.to_facts` enforces this in code for a remote entry
open to Canada in the ATS location list (`filter.metadata_remote_entry`).
Remote days inside an office schedule ("hybrid, 2 days remote") stay
`hybrid`. A remote role carries no city.

Employment terms, both linear and 0 when "full": fewer weekly hours cost
more (37.5 h is full time); a shorter contract costs more (12 months is a
full term); an internship counts as a contract of its stated length.
Unstated values fall back to typical ones — part-time 20 h, a contract 6
months, an internship 4 months. Each counts only when a verified quote shows
it (they only ever cost points, so no evidence means no penalty). `digest`
prints the cost in front of the gaps: `[part-time 16h/wk -17.2, term 6mo -15]`.
Clamp to [0,100]. Weights live in `prospect.toml [score]`. The breakdown JSON
is stored so eval can regress on it.

## Hard filters (`pipeline/filter.py`, no AI)

Reject when any of: the posting is written in French (`posting_language =
fr`, or `french_requirement = posting_in_french`); the job is outside Canada
(country ≠ CA, or a non-Canadian province/state) and not `remote_scope ∈
{canada, global}`; `min_years > targets.max_years`, or a must-have skill
requirement whose own text states more years than that (`years:requirement`,
e.g. "5+ years of Python"); `clearance` or `peng` required; `work_auth`
incompatible with `identity.work_authorization`; duplicate of an application
within `dedup.window_days` (default 90). Flag (don't reject) when
`repost_count ≥ 3` and a prior application to the same `(company,
title_norm)` got no response.

`french_requirement` (none | asset | required_pro_forma | required_working |
posting_in_french) is extracted and shown in `digest`; only a posting written
in French is a filter input.

`posting_facts` come from `extract`, so `filter` runs twice:

1. **Pre-pass** (`prospect filter`, before rank/extract) on raw metadata.
   The French regex (`fran[cç]ais|bilingu|french`) only **flags**. The
   pre-pass rejects only what raw metadata settles unambiguously, so
   `extract` budget isn't spent on it:
   - `location_metadata`: every listed location is recognisably outside
     Canada and none is Canadian or open-scope (bare "Remote", "Worldwide",
     "North America", "Americas"). Anything ambiguous passes.
   - `senior_title`: Senior / Sr / Staff / Principal / Lead / Manager /
     Director / Head of / VP in the title.
   - `duplicate_application`: an application to the same
     `(company, title_norm)` within `dedup.window_days`.
   Location and title rejects can be turned off with
   `[filter] prepass_reject_location` / `prepass_reject_senior_titles`.
2. **Full pass** (`prospect filter --full`, after `extract`) applies every
   rule above to `posting_facts`. A rule backed by an extraction with
   `confidence < filter.min_confidence_to_reject` (default 0.6) flags
   (`low_confidence:<rule>`) instead of rejecting, and so does a rule whose
   deciding field kept no evidence quote (`weak_evidence:<rule>`) — a quote
   survives `extract` only if it is verbatim *and* visibly states the value
   ("Toronto" for city, "2+ years" for min_years). Exceptions:
   `posting_language = fr` is deterministic and always rejects; years
   reject on a verbatim quote whatever the confidence, and a requirement
   keeps its `years` only when its text states that number verbatim, so
   `years:requirement` always rejects. A years quote that says "preferred"
   flags `optional:years` instead. The full pass re-judges the postings it
   rejected earlier, so a rule change (a higher years cap) restores them;
   flagged rows are marked ⚠ in `digest`.

## Core data contracts (pydantic, `models/`)

- `RawPosting`: source, ext_id, company, title, url, location_raw,
  description_html, posted_at, raw
- `PostingFacts`: city, province, country, work_arrangement
  ∈ {onsite, hybrid, remote, unknown}, onsite_days, remote_scope
  ∈ {city, province, canada, us, global, unknown}, posting_language
  ∈ {en, fr, bilingual}, french_requirement ∈ {none, asset,
  required_pro_forma, required_working, posting_in_french}, min_years,
  seniority ∈ {intern, newgrad, junior, mid, senior, unknown}, clearance,
  peng, work_auth, part_time, hours_per_week, contract, term_months,
  confidence ∈ [0,1], per-field `evidence_span`
- `Requirement`: text, skill_norm, must_have, years, context,
  kind ∈ {skill, eligibility, soft}, tier, tier_substate ∈ {strong, weak, None},
  matched_node, evidence, est_hours. Only `skill` rows are tiered and scored;
  `eligibility` (enrollment, graduation window, term/availability, work
  authorization, relocation, experience caps) is shown in `digest`;
  `soft` is set by the score stage and never scored.
  Degrees, certifications, clearances and licences are always `skill`.
  `pipeline/guards.py` enforces this in code: upper-bound years ("less than
  2 years") are never stored as a minimum, an experience cap is
  `eligibility` even with a condition around it, and "preferred" / "a plus"
  / "nice to have" in a requirement's own text make it `must_have = false`.

## Model routing (`llm/client.py`)

| task                    | model  | mode                  | notes                                   |
|-------------------------|--------|-----------------------|-----------------------------------------|
| extract facts + reqs    | Haiku  | Batch API, structured | 200/day fine                            |
| tier classification     | Sonnet | sync, structured      | fact bank as cached prefix; one label per skill (`tier_cache`) |
| eval reruns             | any    | sync or Batch API     | compare all three on the same labels    |

Structured output: use tool_use with the pydantic-derived JSON schema; parse
with `Model.model_validate`. Never regex a model's prose.
Prompt caching: the serialized fact bank + system prompt is the cached
prefix for every Sonnet call. Check current caching/batch docs before
changing `client.py`: https://docs.claude.com/en/docs_site_map.md

## Eval (`eval/`)

`labels` table holds human gold values per `(posting_id, field)`. Labeled
fields are exactly the facts that drive a decision: `city` (home-city fit),
`posting_language` (French posting → reject), `min_years` (above
`targets.max_years` → reject), `seniority` (internship term penalty).
Label conventions: city `Remote` for a remote role; a years range labels
its lower bound; `min_years` 0 and "not stated" score as the same value.
Each field is **scored the way the pipeline uses it**: `city` → `location` =
`score.location_key` over the label or prediction plus the ATS location list
(home | remote | elsewhere | unknown), `seniority` → `internship` (intern or
not), `posting_language` and `min_years` as labeled. Exact label agreement
on `city` and `seniority` is printed as a reference line, not scored. The
report also scores the four decisions themselves and lists every
disagreement.
`prospect eval sample` picks diverse unlabeled postings; `prospect eval
extract` reports per-field precision/recall/F1, $/posting, p50 latency, for
each model. Keep the eval script stable once numbers are published.
Held-out sets are labeled in an editor: `prospect eval export` writes
`labels/heldout-<date>.md` (full description, one blank `label` block per
posting, no model output), `prospect eval import FILE` saves the filled
blocks (re-runnable; blank blocks skipped), and `prospect eval extract --set
FILE` scores only that set — the number to publish, since prompts are tuned
on the earlier labels. Label files hold third-party posting text and are
gitignored.

## Demo (`prospect/demo.py`, `demo/`)

`prospect demo` runs the real stage functions on fictional data with no API
key: postings come from `demo/postings/<ats>/<token>.json` through the real
adapters, and the real `LLMClient` talks to a replay of recorded API
responses (`demo/responses/`), keyed by a hash of the full request. A change
to a prompt, the demo fact bank, a posting or a tool schema is a replay miss
that fails loudly; `prospect demo --record` (needs a key, ~$0.13) records
again. Scoring runs one posting at a time in id order so the requests don't
depend on the embedder. If bge can't be downloaded, the demo alone falls back
to TF-IDF with a warning.

## Conventions

- Type hints everywhere; `ruff` clean; a test for every hard rule.
- Prompts are files in `llm/prompts/`, named `{stage}_v{N}.md`. Bumping a
  prompt = new file + `runs.prompt_version`.
- Idempotent stages: re-running `ingest` or `extract` must not duplicate rows.
- Log with `structlog`; one line per posting per stage.
- Commits: `stage: short imperative` (e.g. `extract: add remote_scope field`).
- English only in code, prompts and docs.

## Do not

- Do not write the fact bank from code (Hard rule 1).
- Do not hardcode board tokens; they live in `companies.yaml`.
- Do not call the API from tests. Mock `llm/client.py` or use the demo replay.
- Do not autofill or submit applications. That is out of scope by design.
