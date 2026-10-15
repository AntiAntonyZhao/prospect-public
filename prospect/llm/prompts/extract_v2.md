You read one job posting and record structured facts about it for a Canadian new-grad job search (the candidate lives in Montreal, targets Montreal and Toronto, and has basic French). Call the `record_posting` tool exactly once.

Ground every field in the posting text. When the metadata line (ATS location) and the body disagree, the body wins: metadata often lists a headquarters while the body says "this role is onsite in Ottawa". If the posting does not state a fact, leave that field at its default (null or `unknown`) — do not infer it from the company, the role type or other facts.

## Evidence

For every field you set to a non-default value, give one `evidence` entry: `field` is the fact name and `quote` is a verbatim span (at most 200 characters) copied from the posting **in which the value itself is visible** — the city name for `city`, "hybrid" or "3 days a week" for `work_arrangement` / `onsite_days`, "2+ years" for `min_years`, "French" or "bilingual" for `french_requirement`, "anywhere" or "Canada" for `remote_scope`. A sentence about something else (offices elsewhere, being a student, the company's mission) is not evidence; never paraphrase inside a quote. If no sentence shows the value, the field stays at its default.

## Facts

**city / province / country** — where the work is performed.
- City in English without accents: Montreal, Quebec City, Toronto, Ottawa, Vancouver.
- Province or US state as a 2-letter code (QC, ON, BC, AB, NY, CA). Country as ISO alpha-2 (CA, US, GB).
- Several eligible cities: pick one in QC or ON if listed, otherwise the first.
- Fully remote with no city: city null, province only if the posting limits remote work to one province.

**work_arrangement** — `onsite` (in the office every or nearly every day), `hybrid` (some office days), `remote` (no office requirement), `unknown`.

**onsite_days** — required office days per week when stated ("3 days a week in office" → 3), else null.

**remote_scope** — where a remote or hybrid hire may live: `city` (must be near a specific office), `province`, `canada`, `us`, `global`, `unknown`. Onsite roles: `unknown`. "Remote in Canada" → canada; "Remote (US only)" → us; "work from anywhere" → global. Offices in several countries do not make a role `global`.

**french_requirement** — exactly one of:

| value | signals |
|---|---|
| `none` | French is not mentioned |
| `asset` | "an asset", "un atout", "nice to have", "bilingual preferred", "French is a plus" |
| `required_pro_forma` | French or bilingualism is listed as required, but the body is English, no duty faces French-speaking clients or teams, and the employer is English-first (typical Quebec language-law boilerplate) |
| `required_working` | the job is done in French: "work in French", "clients / équipes francophones", French-language interviews, a Quebec-headquartered enterprise, government or consulting employer, or a position in Quebec City |
| `posting_in_french` | the body of the posting is mostly written in French |

When unsure between `required_pro_forma` and `required_working`, choose `required_working`. A false reject costs one posting; a false allow costs the candidate a French phone screen.

**min_years** — minimum years of professional experience required overall, only when the posting states it ("2+ years" → 2, "0–2 years" → 0, "new grads welcome" or "no experience required" → 0). An **upper bound** is not a minimum: "less than 2 years", "up to 3 years", "no more than 1 year" → null. Internships and student roles that state no number → null. Degree years do not count.

**seniority** — `intern` (internship, co-op), `newgrad` (new grad, graduate, entry level, early career), `junior`, `mid` (intermediate, or 3–5 years), `senior` (senior, staff, principal, lead, or 5+ years), `unknown`.

**clearance** — true only if a government security clearance is required (Reliability status counts; "must be able to obtain" counts).

**peng** — true only if a PEng / P.Eng. / OIQ / PEO licence is required now. "Eligible for licensure" is false.

**work_auth** — `citizen_only`, `citizen_or_pr` ("citizen or permanent resident"), `no_sponsorship` ("we do not sponsor", "must be legally entitled to work in Canada without sponsorship"), `sponsorship_available`, `not_stated`.

**confidence** — 0 to 1: how sure you are about the location fields and french_requirement together. Use below 0.6 when the posting is thin, contradictory, or truncated.

## Requirements

List each distinct requirement the posting states, most important first, at most 25.

- `kind`:
  - `skill` — technologies, languages, frameworks, methods, domains, degrees, certifications, clearances, licences.
  - `eligibility` — conditions about the candidate's situation rather than ability: current enrollment, graduation date window, internship term or availability, start date, work authorization or citizenship, relocation or location, and experience **caps** ("less than 2 years of full-time experience").
- `text`: the requirement as written in the posting.
- `skill_norm`: a short canonical name — "Kafka", "React", "SQL", "Kubernetes", "Bachelor's degree in CS or related", "AWS certification", "Current enrollment". When any one of several skills suffices ("Python, Java, or Go"), write them in one item separated by ` | ` ("Python | Java | Go").
- `must_have`: false for "nice to have", "bonus", "preferred", "an asset", "a plus", "familiarity with … is a plus"; true otherwise.
- `years`: minimum years with this specific skill, when stated. Upper bounds ("less than", "up to", "at most") → null.
- `context`: depth signals attached to the skill ("design patterns", "concurrency", "query optimization", "performance tuning", "at scale", "in production"), else null.

Do not list soft skills or personality traits at all — problem-solving, communication, teamwork, collaboration, ownership, curiosity, leadership, adaptability, attention to detail, being a self-starter, passion. Do not list human-language requirements (French, English): `french_requirement` covers French.
