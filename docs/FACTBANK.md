# Fact bank

The fact bank is a structured record of what the candidate actually knows and
did. It is the only source of claims the pipeline may make about the candidate:
`rank` embeds a profile built from it, and `score` sends it to Sonnet as the
cached prefix for tier classification. The default config points at the
fictional demo profile, `demo/factbank_demo.yaml`; to use your own, write the
same shape and set `[paths] factbank` in `prospect.toml`.

Editing rules: edited by hand, or by Claude Code with the user's approval of
each edit (CLAUDE.md Hard rule 1); truth-only; and a skill goes in *after* it
is acquired, never before. `prospect factbank lint` must pass before any
pipeline stage runs.

## Top-level shape

```yaml
version: 1            # bump on breaking schema changes
identity: {...}
targets: {...}
education: [...]
skills: [...]
experiences: [...]
projects: [...]
```

## `identity`

```yaml
identity:
  name: Demo Candidate
  email: demo.candidate@example.com
  phone: "(514) 555-0142"
  location: {city: Montreal, province: QC, country: CA}
  links: {portfolio: https://example.com/demo-candidate}
  work_authorization: citizen     # citizen | pr | pgwp | needs_sponsorship
  languages:
    - {lang: English, cefr: C2, render: English}
    - {lang: French, cefr: A2, render: "French (basic)"}
```

`location.city` is the home city for `fit(location)`. Email, phone and links
are never sent to the API (`loader.prompt_text` drops them).
`cefr ∈ {A1, A2, B1, B2, C1, C2, native}`; `render` is how the language is
written out. Human languages are never Tier 2; a language absent from this
list is a Tier 3 gap when a JD requires it. `prospect filter` refuses to run
until `work_authorization` is set.

## `targets` (what the matcher optimizes for)

```yaml
targets:
  roles: [software engineer, backend engineer, full-stack engineer, ML engineer,
          new grad developer]
  seniority: [intern, newgrad, junior]
  cities: [Montreal, Toronto]
  provinces: [QC, "ON"]
  remote_scope_ok: [canada, global]
  max_years: 4
```

Keys set in `prospect.toml [targets]` override these.

## `skills[]`

```yaml
- id: java                       # slug, unique, referenced everywhere
  label: Java                    # the canonical name
  category: language             # language | framework | ml | cloud | devops | db | testing | methodology | modeling | tool
  domains: [backend]             # frontend | backend | data | ml | llm | cloud | devops | testing | systems | algorithms | design | process | tooling
  level: working                 # familiar | working | proficient | expert
  evidence: [scheduler-solver]   # bullet ids or project ids
  basis: null                    # free text if no bullet evidence, e.g. "two systems courses"
  aliases: [OOP, object-oriented programming, object-oriented design]
  adjacent: [kotlin, csharp]
```

Field semantics — these matter, read them:

- **`level`** is the honest proficiency tier. `familiar` means "have used
  it (a course, a small personal project), could not be interviewed on it
  in depth". A requirement naming a `familiar` skill matches at Tier 0
  *weak*, which costs a little on a must-have; `working` and above match at
  Tier 0 *strong*. `rank`'s profile leaves `familiar` skills out.
- **`evidence`** is a list of bullet ids / project ids that demonstrate the
  skill. Lint **warns** when both `evidence` and `basis` are empty.
- **`aliases`** are terms that are *true to say* if you can say the label.
  "Java" → "OOP" is true. This list drives Tier 1 (RENAME). Be strict:
  an alias is something an interviewer would accept as the same claim.
  `PyTorch` → "deep learning frameworks" is an alias; `PyTorch` →
  "TensorFlow" is **not** an alias, it is `adjacent`.
- **`domains`** is where the skill lives (`category` is the coarse type;
  `domains` is the audience). TypeScript is `[frontend, backend]`; Python is
  `[backend, data, ml, tooling]`.
- **`adjacent`** are skills you *do not have* but could learn quickly
  *because* you have this one. A requirement found here is never worse than
  Tier 2: the model still labels it, and code turns any Tier 3 label into
  Tier 2 with `nearest_node` defaulting to the skill that lists it. Putting a
  skill in `adjacent` is a statement that you don't have it.

Lint rules on skills:
- ids unique, slug-cased
- every `evidence` entry resolves to an existing bullet or project id
- no alias appears as another skill's `id` or `label` (that would be a
  Tier 0 masquerading as Tier 1)
- no `adjacent` entry appears as a skill `id` (contradiction)
- `level` present and valid

## `experiences[]`

```yaml
- id: initech
  org: Initech (fictional)
  title: Backend Developer Intern
  start: 2024-05
  end: 2024-08
  location: Toronto, Canada
  kind: work                      # work | research | volunteer
  bullets:
    - id: initech-api
      text: >-
        Designed REST endpoints in FastAPI for an internal billing tool, serving 1.2K
        requests per minute at p95 latency under 120 ms
      skills: [fastapi, rest-apis, python]
      metrics: ["1.2K", "120"]      # every number in text
      tags: [backend, api, performance]
      strength: 5                   # 1–5, how strong this bullet is standalone
```

- **`metrics`** must list every numeric token in `text`; lint fails
  otherwise. Numeric token regex (`factbank/metrics.py`):
  `~?\d+(?:[.,]\d+)?(?:%|[KMB]|x)?\+?` — so `105+`, `~50%`, `3B`, `0.899`
  are each one token. Digits glued to a preceding letter (`p95`, `GPT-4`,
  `S3`) are part of a name, not a metric.
- **`strength`** is a human prior: bullets at 4 or 5 go into `rank`'s
  profile text.
- **`tags`** are free-form retrieval hints for Tier 1(b) matching.

## `projects[]`

Same shape as an experience bullet group plus:

```yaml
- id: support-bot
  name: "Helpdesk Copilot: LLM Ticket Triage"
  start: 2026-01
  end: 2026-04
  kind: team                        # team | solo | course
  course: Capstone                  # optional
  url: null
  stack: [python, fastapi, llm-apis, postgresql, docker]   # skill ids
  bullets: [...]
```

`stack` entries must be skill ids; lint fails otherwise. This is how a
project becomes evidence for a skill without repeating it in `evidence`.

## Versioning

Every `applications` row stores `factbank_version` = git blob hash of the
fact bank when the application was logged.
