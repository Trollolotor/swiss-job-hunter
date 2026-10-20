# Agent-first collection and review

The agent investigates and maintains a connector. Normal board and employer
collection do not call an LLM. Matching uses a separate policy and spending cap.

## Matching and review

Settings → Matching controls shortlist/archive thresholds, concurrency, model
prices, the daily USD cap and the maximum cost of one attempt. Both matching and
employer budgets default to unset. A configured matching cap is required for paid
screening, including manual/CLI calls, retries, fallbacks and JSON repair. Free
cache hits and local keyword matching remain available. Budgets reset at midnight
UTC. Unknown charges retain their reservation; unknown pricing blocks a call.

OpenRouter prices use the routed model slug. Legacy paid providers use
`provider:model`; input/output prices are USD per million tokens. Local Ollama
does not incur a paid reservation. The ledger retains usage, request ID, fallback
index and latency without CV text. Keyword extraction during collection is local.

Each profile has its own evaluation, keyed by CV/content hashes. Keyword coverage
is provisional and never archives a role. All Profiles shows the best current
screening result and its CV. Editing a CV or changing selected vacancy content
makes prior scores stale. Existing scores are labelled legacy rather than
silently reassigned to another profile. An old score is not proof of completed
screening for a newly connected source.

Descriptions have complete, preview, failed, insufficient or unknown extraction
quality. Completeness comes from source fields/selectors, not a 400-character
threshold. Failed/partial extraction leaves fit unknown and retries with bounded
backoff. A short genuine description can be complete.

Consider, skip, stars, notes and application history are shared across profiles.
Review Queue uses these same records; skip reasons are explicit history. Automatic
changes cannot overwrite user decisions. Historical archives with unknown origin
remain protected. Automatically archived roles can recover after new evidence.

The score filter affects display only. Company lookup and purge have independent
controls. Purge excludes user-owned, historical-unknown, stale and incomplete
records, as well as existing application and interaction history.

## Collection and observation evidence

jobs.ch/jobup use their current structured search pages with visible card fields;
JobScout24 uses `ft`, a verified postcode `psz`, and `p`; Michael Page uses `search`.
Unknown locations and changed markup are errors. Board runs record queries,
scope, counts, termination and errors at `/collection/board-runs`. Every retained
observation uses shared ingestion; employer discovery precedes vacancy filters.
Partial runs keep valid sightings without inferring absence.

SDK v1 `collect(config,sdk)` remains supported. New bundles use SDK v2:

- `enumerate(config,sdk)` returns entries, observed IDs, total and termination.
- Each entry has posting_id, optional variant/change_token, detail_url, metadata.
- `fetch_detail(config,entry,sdk)` retrieves a detail response and evidence keys.
- `normalize(config,entry,detail)` returns a CollectedPosting without persistence.

The trusted controller chooses details to refresh, validates staged sightings,
and applies matching. New/changed entries fetch details; unchanged entries reuse
version-scoped evidence for at most 24 hours. Validation bypasses caching. The
cache is disposable, limited to 128 MiB, and never extends TTL on cache hits.
Both request and cached evidence count against the run's byte limit.

Browser observations expose only approved network requests/responses through the
existing broker. The launcher enforces one browser globally. Generated stdout is
separated from SDK protocol output. SDK v2 adds no direct network or filesystem
permissions. Existing host policies and ARM64 checks remain mandatory.

Direct-only means **not yet observed on our monitored boards**. It does not claim
absence from other job boards. Employer metrics exclude historical imports and
initial onboarding from incremental yield. Relevant yield requires a current LLM
evaluation meeting the configured shortlist threshold. After seven days and ten
complete runs, Settings offers a reversible 2–4-hour polling suggestion. Applying
it is explicit. One daily onboarding slot explores older queued employers.

## Upgrade and rollback

The Alembic head is `20261021_agent_optimization`, following the employer-source
migration. It supports fresh, unversioned and already-upgraded databases through
schema inspection. Existing IDs, CVs, scores, events and applications are retained.
Startup compatibility adds missing columns, but feature activation still requires
the explicit Alembic head.

Prepare images and run checks before the outage. Preserve the running image IDs.
Stop the backend to suspend scheduling, then create and verify a private backup:

```bash
docker compose stop backend
python3 tools/backup_database.py data/jobs.db data/backups/pre-agent-upgrade.db
docker compose run --rm --no-deps backend alembic upgrade head
python3 tools/backup_database.py data/jobs.db /tmp/unused --verify-manifest data/backups/pre-agent-upgrade.manifest.json
```

Install the restricted launcher as described in `employer-sources.md`, pin the
new runtime images, and start the Compose overlay. Initially keep employer
collection disabled and matching screening paused. Backfill and run shadow
collections; configure approved budgets/prices before paid pilot work.

Rollback disables new work and restores the preserved application image. Leave
additive tables and history intact. Restore a verified database backup only for
a demonstrated data issue, not as a routine schema downgrade.

Verification commands (isolated test database and dummy provider credentials):

```bash
pytest -q
cd ui
npm ci
npm test
npm run build
```

Host checks: `tools/check_connector_runtime.py`, `tools/check_browser_runtime.py`
and `tools/check_sdk_protocol.py` exercise actual ARM64 containers. The SDK check
uses an offline broker through the installed launcher. `tools/employer_pilot.py`
performs explicit public live collections in a new temporary database. A paid
generated-connector/repair run must be reported separately from mocked tests.
