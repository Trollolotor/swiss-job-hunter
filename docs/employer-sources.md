# Employer discovery and direct sources

This optional mode adds an evidence-backed employer registry, direct catalogue collection,
source provenance, and bounded connector generation/repair. It is disabled by default.
Existing board searches keep their existing schedules. Enabling discovery captures employers
before age/relevance filters, including vacancies rejected by those filters.

## Settings and normal operation

Settings → Employer Sources contains the feature switch, preview mode, all three geographical
coverage modes, collection interval, onboarding limits, and separate discovery/repair budget.
Preview mode records diagnostics but does not ingest vacancies or apply closure decisions.
The daily USD cap is unset initially. A model also needs conservative USD input/output prices
per million tokens and a search-request price before it can incur new discovery charges.
Budgets reset at midnight UTC. The repair fraction is reserved separately from onboarding.
Unknown request costs retain their reservation; failed requests do not silently refund it.

Add an employer manually or import stored employers, then resolve its portal. A manually supplied
official page must name the employer and visibly link to its portal. Ambiguous identities and
agencies remain unresolved/reviewable. Similar names and stripped legal suffixes do not merge
employers. One employer may own multiple sources.

Connected sources remain monitored independently of future board results. Pause cancels queued
work and fences running work. Resume restores collection. Explicit Retry revalidates the current connector (including newly pinned runtime images) and starts a fresh incident
audit; automatic scheduling cannot reset the two-candidate repair limit.

Vacancy source records preserve per-source identities and content revisions. Existing canonical
job IDs, primary board URLs, profile links, events, and applications remain intact. Direct links
appear as preferred application links. No application is submitted by this subsystem.

The initial maintained adapter is SmartRecruiters. Other platforms use the controlled SDK and
require a validated generated connector. The Workday fixture in `tests/fixtures/` is a test input,
not a pre-approved or silently installed production adapter.

## Schema upgrade and backup

Keep the feature disabled while deploying. Stop the backend scheduler before upgrading its
database. With the existing backend stopped, make a SQLite backup using the Python sqlite3
backup API; restore it into a second file and compare `integrity_check` and table counts.
Copy the stopped data directory as well if preserving existing diagnostic files is necessary.
Do not publish or log the backup: search profiles contain CV data.

Build the new backend, then run the migration once:

```bash
docker compose stop backend
# Back up data/jobs.db and verify the backup before continuing.
docker compose build backend frontend
docker compose run --rm --no-deps backend alembic upgrade head
docker compose up -d backend frontend
```

The revision `20261020_employer_sources` supports an empty database, the original unversioned
startup schema, and the preceding Alembic head. It creates absent tables with `checkfirst`;
it does not stamp an unverified existing schema. Startup retains legacy bootstrap compatibility.
The feature switch cannot be enabled through the API until this revision is present.

Import stored employers through Settings. The import checkpoints jobs and raw records in bounded
transactions, preserves canonical IDs, labels historical observations, and can resume. It cannot
recover source history that was never stored or undo uncertain historical title-based merges.

## Isolated runtime installation on Ubuntu ARM64

The trusted backend and SQLite stay in the existing Compose stack. Generated Python never runs
there. A narrow systemd launcher starts ephemeral network-disabled containers using approved image
IDs; the backend receives only a restricted Unix socket, never the Docker socket.

Build both pinned-dependency runtime targets on the deployment architecture:

```bash
docker build --target python -t sjh-connector-python -f connector_runtime/Dockerfile .
docker build --target browser -t sjh-connector-browser -f connector_runtime/Dockerfile .
docker image inspect sjh-connector-python --format '{{.Id}}'
docker image inspect sjh-connector-browser --format '{{.Id}}'
```

Install the reviewed host artifacts as root (these commands are intentionally separate from the
application deployment):

```bash
groupadd --system sjh-connector
install -d -m 0755 /etc/sjh-connector /usr/local/lib/sjh-connector
install -m 0644 deploy/connector-launcher.py /usr/local/lib/sjh-connector/
install -m 0644 deploy/browser-seccomp.json /etc/sjh-connector/
install -m 0644 deploy/sjh-connector.apparmor /etc/apparmor.d/sjh-connector
apparmor_parser -r /etc/apparmor.d/sjh-connector
install -m 0644 deploy/sjh-connector.service /etc/systemd/system/
```

Create root-owned `/etc/sjh-connector/images.json` using the two actual image IDs:

```json
{"python":"sha256:<64 hex characters>","browser":"sha256:<64 hex characters>"}
```

The launcher rejects tags, arbitrary images/options, bind mounts, and unknown run kinds.
Its image file and installation directory must not be writable by application users.
The vendored browser seccomp profile is the Moby default profile from
`https://github.com/moby/profiles/blob/main/seccomp/default.json` with the documented Chromium
user-namespace allowances for `clone`, `setns`, and `unshare`, plus `chroot` for Chromium's inner
sandbox. It retains default-deny behavior; the outer container still drops all capabilities.
Both AppArmor profiles deny IP networking; the browser profile allows its private Unix IPC and
user namespace. The browser must start with `chromium_sandbox=True`; there is no no-sandbox retry.

Set `EMPLOYER_RUNNER_IMAGE` and `EMPLOYER_BROWSER_IMAGE` in the backend environment to the same
approved image IDs. Environment values identify images; they contain no provider credentials.
Then:

```bash
systemctl daemon-reload
systemctl enable --now sjh-connector
docker compose -f docker-compose.yml -f docker-compose.employers.yml up -d backend
python3 tools/check_connector_runtime.py sha256:<actual-python-image-id>
python3 tools/check_browser_runtime.py sha256:<actual-browser-image-id>
```

The current backend runs as root and can access the group-restricted launcher socket. If changing
the backend to non-root, grant only the socket group, not Docker-group membership. No launcher
TCP port is published. The browser does not share host IPC or a persistent browser profile.

The optional `--development-profile` smoke-test flag uses `docker-default` before host profile
installation. It does **not** change the launcher policy or certify the dedicated production
AppArmor profiles. Verify those profiles, Chromium sandbox startup, and limits on the actual host
before enabling generated sources. Missing profiles or mismatched image IDs fail closed.

For a development host, `check_browser_runtime.py --temporary-profiles` loads uniquely named
copies of the reviewed profiles with noninteractive sudo and removes them after testing. It does
not install the service or alter existing profiles. The launcher labels only its own containers
and cleans them on service startup/stop so a crashed supervisor cannot leave unbounded stale work.

## Collection and validation

`EmployerCollector` is implemented by async functions accepting `(config, CollectionContext)` and
returning `CollectionResult`. Generated connectors define synchronous `collect(config, sdk)`.
The version-1 SDK exposes public fetch/JSON and bounded browser observations. It has no database,
credentials, unrestricted browser objects, shell, application, message, or account tools.

The network broker pins public DNS answers to TCP connections while retaining TLS hostname
checks. It checks redirects and POST endpoint/body-field permissions independently. Browser HTTP
requests are fulfilled through the same broker; service workers and WebSockets are blocked.
Browser interaction currently supports observation/waits, not clicks, form submission, or eval.
Sites requiring unsupported interactions remain inactive instead of receiving wider permissions.

Manifest catalogue fields describe JSON item/ID/total paths or HTML item/next selectors. Set
`rendered: true` for browser DOM catalogues. Trusted validation checks observed counts, source
fields, page offsets or HTML next-link traversal, duplicate IDs, real sampled vacancy links,
two live runs, and replay against saved observations. Connector-supplied tests cannot override it.
Only a matching immutable bundle can become active; concurrent changes invalidate promotion.

Run outcomes distinguish complete, confirmed empty, partial, and failed. Partial runs can
contribute individually evidenced records. They never advance missing counters. Closure requires
two qualifying complete observations of the same scope separated by at least two hours.
Source closure does not replace user workflow statuses. A linked open/unknown board source
prevents a confident canonical closure. A large catalogue collapse receives a health recheck
before repair; transport failures receive backoff without an immediate LLM call.

HTML/JSON diagnostics have seven-day success and fourteen-day failure retention, capped at 1 GiB.
Active/previous-version fixtures are pinned within the same cap. Exhausting storage fails
validation rather than deleting required evidence or claiming a successful empty catalogue.

## Pilot and tests

Run offline tests with a temporary database; `tests/conftest.py` enforces database isolation.
Use a separate environment with dummy provider credentials and a nonexistent CV path. The suite
does not install or require a live launcher, call an LLM, or submit applications.

For a live SmartRecruiters validation/collection pilot without LLM spending or production writes:

```bash
python3 tools/employer_pilot.py --output /tmp/sjh-swissquote-pilot
```

The output directory must be new. The command creates a temporary application database and runs
official-page verification, candidate validation, fixture replay, activation, and routine shadow
collection. It deliberately clears provider credentials. Allow several minutes for polite requests.
Access challenges are recorded as failures, not bypassed.

Swisscom's official page currently links to Workday. A paid generation pilot requires the new
budget/prices to be entered, both runtime images to be pinned and installed, and host isolation
checks to pass. Do not describe a mocked generation test as a live LLM-generated connector.

## Rollback and measurement

Disable the mode to stop dispatching new tasks, pause affected sources, then restore the previous
application image if needed. Preserve additive tables; the downgrade intentionally refuses to
erase provenance. A previous connector must pass current live/replay validation before rollback.
Failed repair candidates remain inactive and cannot erase vacancies or application history.

Run/source/version/incident IDs, source memberships, request counts/bytes, latency, fixture hashes,
and the cost ledger support reliability and cost measurement. Compare first observations after
onboarding baselines and account for polling/search coverage. Initial imports are not new
publications, and uncertain publication dates remain unknown.
