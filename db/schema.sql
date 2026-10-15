-- Prospect — SQLite schema. Applied by `prospect db init`. Idempotent.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  name_norm     TEXT NOT NULL UNIQUE,          -- lowercased, punctuation stripped
  ats           TEXT NOT NULL,                 -- greenhouse | lever | ashby | github | other
  board_token   TEXT,
  careers_url   TEXT,
  notes         TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS postings (
  id            INTEGER PRIMARY KEY,
  company_id    INTEGER NOT NULL REFERENCES companies(id),
  source        TEXT NOT NULL,
  ext_id        TEXT,                          -- id at the source
  hash          TEXT NOT NULL,                 -- sha256(company_norm|title_norm|desc_norm)
  title         TEXT NOT NULL,
  title_norm    TEXT NOT NULL,                 -- lowercased, seniority/location tokens stripped
  url           TEXT NOT NULL,
  location_raw  TEXT,
  posted_at     TEXT,
  first_seen    TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen     TEXT NOT NULL DEFAULT (datetime('now')),
  repost_count  INTEGER NOT NULL DEFAULT 0,    -- times (company, title_norm) reappeared after disappearing
  status        TEXT NOT NULL DEFAULT 'new',   -- new | rejected | candidate | shortlisted | applied | ignored
  reject_reason TEXT,
  flags         TEXT NOT NULL DEFAULT '[]',    -- JSON list: french_regex | ghost | low_confidence | ...
  raw_json      TEXT NOT NULL,
  UNIQUE(source, ext_id),
  UNIQUE(hash)
);
CREATE INDEX IF NOT EXISTS ix_postings_company_title ON postings(company_id, title_norm);
CREATE INDEX IF NOT EXISTS ix_postings_status ON postings(status);

CREATE TABLE IF NOT EXISTS posting_facts (
  posting_id        INTEGER PRIMARY KEY REFERENCES postings(id),
  city              TEXT,
  province          TEXT,
  country           TEXT,
  work_arrangement  TEXT,     -- onsite | hybrid | remote | unknown
  onsite_days       INTEGER,
  remote_scope      TEXT,     -- city | province | canada | us | global | unknown
  posting_language  TEXT,     -- en | fr | bilingual
  french_requirement TEXT,    -- none | asset | required_pro_forma | required_working | posting_in_french
  min_years         INTEGER,
  seniority         TEXT,     -- intern | newgrad | junior | mid | senior | unknown
  clearance         INTEGER,
  peng              INTEGER,
  work_auth         TEXT,
  part_time         INTEGER NOT NULL DEFAULT 0,
  hours_per_week    REAL,     -- stated weekly hours (part-time penalty)
  contract          INTEGER NOT NULL DEFAULT 0,
  term_months       REAL,     -- stated contract / internship length (short-term penalty)
  confidence        REAL,
  evidence_json     TEXT,     -- {field: quoted span}
  model             TEXT NOT NULL,
  prompt_version    TEXT NOT NULL,
  extracted_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS requirements (
  id            INTEGER PRIMARY KEY,
  posting_id    INTEGER NOT NULL REFERENCES postings(id),
  text          TEXT NOT NULL,                 -- quoted from JD
  skill_norm    TEXT NOT NULL,
  must_have     INTEGER NOT NULL,
  years         INTEGER,
  context       TEXT,
  kind          TEXT NOT NULL DEFAULT 'skill', -- skill | eligibility (eligibility is never tiered)
  tier          INTEGER,                       -- 0..3, NULL until scored
  tier_substate TEXT,                          -- strong | weak (tier 0 only)
  matched_node  TEXT,                          -- factbank id
  evidence_json TEXT,                          -- [node ids] for tier 1
  est_hours     REAL,
  nearest_node  TEXT,                          -- factbank skill id closest to a tier 2/3 gap
  rationale     TEXT,
  model         TEXT,
  prompt_version TEXT
);
CREATE INDEX IF NOT EXISTS ix_requirements_posting ON requirements(posting_id);

-- One model tier label per skill, so the same skill gets the same tier in every posting and
-- every run. Keyed by what the label depends on (score.tier_context): the model, the score
-- prompt and the fact bank as sent. Change any of them and the cache starts fresh.
CREATE TABLE IF NOT EXISTS tier_cache (
  skill_key      TEXT NOT NULL,                -- score.tier_key(skill_norm)
  context_hash   TEXT NOT NULL,
  label_json     TEXT NOT NULL,                -- the model's TierLabel; code rules re-apply on read
  model          TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  requirement_id INTEGER,                      -- the requirement it was first asked for
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (skill_key, context_hash)
);

CREATE TABLE IF NOT EXISTS embeddings (
  posting_id    INTEGER PRIMARY KEY REFERENCES postings(id),
  model         TEXT NOT NULL,
  vector        BLOB NOT NULL,                 -- float32 array
  cosine        REAL,                          -- vs the factbank profile vector
  profile_hash  TEXT,                          -- which profile `cosine` was computed against
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scores (
  posting_id    INTEGER PRIMARY KEY REFERENCES postings(id),
  score         REAL NOT NULL,
  breakdown_json TEXT NOT NULL,
  cosine        REAL,
  scored_at     TEXT NOT NULL DEFAULT (datetime('now')),
  weights_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
  id                INTEGER PRIMARY KEY,
  posting_id        INTEGER NOT NULL REFERENCES postings(id),
  resume_path       TEXT,
  factbank_version  TEXT NOT NULL,             -- git blob hash of the fact bank when logged
  submitted_at      TEXT,
  channel           TEXT,                      -- direct | referral | email
  referral          INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL DEFAULT 'submitted',  -- submitted | screen | interview | offer | rejected | ghosted
  status_at         TEXT,
  notes             TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_applications_posting ON applications(posting_id);

CREATE TABLE IF NOT EXISTS runs (
  id              INTEGER PRIMARY KEY,
  stage           TEXT NOT NULL,
  model           TEXT,
  prompt_version  TEXT,
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  n_in            INTEGER,
  n_out           INTEGER,
  input_tokens    INTEGER,
  output_tokens   INTEGER,
  cached_tokens   INTEGER,                     -- cache reads
  cache_write_tokens INTEGER,                  -- cache writes (billed at 1.25x input)
  cost_usd        REAL,
  batch_id        TEXT,
  posting_id      INTEGER,                     -- the posting an LLM call was about, if any
  error           TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_started ON runs(started_at);

-- One row per source feed fetch (a company board, a list file, a search).
-- Repost detection needs "was this feed fetched successfully since the posting
-- was last seen?" — a failed fetch must not make every posting look closed.
CREATE TABLE IF NOT EXISTS fetch_log (
  id              INTEGER PRIMARY KEY,
  source          TEXT NOT NULL,
  feed_key        TEXT NOT NULL,
  fetched_at      TEXT NOT NULL,
  ok              INTEGER NOT NULL,
  n_postings      INTEGER,
  error           TEXT
);
CREATE INDEX IF NOT EXISTS ix_fetch_log_feed ON fetch_log(feed_key, fetched_at);

-- Message Batches in flight (extract --submit / --collect).
CREATE TABLE IF NOT EXISTS batches (
  batch_id        TEXT PRIMARY KEY,
  stage           TEXT NOT NULL,
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  submitted_at    TEXT NOT NULL DEFAULT (datetime('now')),
  status          TEXT NOT NULL DEFAULT 'submitted',   -- submitted | collected | failed
  collected_at    TEXT,
  items_json      TEXT NOT NULL                        -- {custom_id: posting_id}
);

CREATE TABLE IF NOT EXISTS labels (
  posting_id    INTEGER NOT NULL REFERENCES postings(id),
  field         TEXT NOT NULL,                 -- posting_facts column name
  gold_value    TEXT,
  labeled_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (posting_id, field)
);

-- `prospect eval extract` reruns: predictions never touch posting_facts.
CREATE TABLE IF NOT EXISTS eval_runs (
  id              INTEGER PRIMARY KEY,
  stage           TEXT NOT NULL,
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  mode            TEXT NOT NULL,               -- sync | batch
  started_at      TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS eval_items (
  eval_run_id     INTEGER NOT NULL REFERENCES eval_runs(id),
  posting_id      INTEGER NOT NULL REFERENCES postings(id),
  latency_ms      REAL,
  cost_usd        REAL,
  error           TEXT,
  PRIMARY KEY (eval_run_id, posting_id)
);

CREATE TABLE IF NOT EXISTS eval_predictions (
  eval_run_id     INTEGER NOT NULL REFERENCES eval_runs(id),
  posting_id      INTEGER NOT NULL REFERENCES postings(id),
  field           TEXT NOT NULL,
  value           TEXT,
  PRIMARY KEY (eval_run_id, posting_id, field)
);

-- Views used by `prospect digest` and analytics.

CREATE VIEW IF NOT EXISTS v_shortlist AS
SELECT p.id, c.name AS company, p.title, p.url, s.score, p.repost_count,
       f.city, f.province, f.work_arrangement, f.remote_scope, f.french_requirement,
       f.min_years, p.first_seen
FROM postings p
JOIN companies c ON c.id = p.company_id
JOIN scores s ON s.posting_id = p.id
LEFT JOIN posting_facts f ON f.posting_id = p.id
WHERE p.status = 'candidate'
ORDER BY s.score DESC;

-- Conversion by factor (referral, posting age at submission, match score).
CREATE VIEW IF NOT EXISTS v_conversion AS
SELECT
  a.referral,
  CAST(julianday(a.submitted_at) - julianday(p.first_seen) AS INTEGER) AS days_after_post,
  s.score >= 70 AS high_score,
  a.status
FROM applications a
JOIN postings p ON p.id = a.posting_id
LEFT JOIN scores s ON s.posting_id = p.id
WHERE a.submitted_at IS NOT NULL;
