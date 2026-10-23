# Internal ATS libraries

Employer onboarding first tries a reviewed public protocol for a recognized portal. It builds an
immutable SDK v2 connector candidate and uses the existing two-run validation, saved-fixture
replay and activation process. Unsupported portals and structural failures retain the existing
bounded LLM investigation/generation/repair flow. Ready onboarding requires the installed Python
runner, but does not call a model or reserve discovery budget.

## Package boundaries

- `ats_core` contains plain value objects, strict JSON/XML parsing, text conversion, locations,
  publication-date parsing and stable content hashes. It has no database, credentials or HTTP client.
- `ats_vendors/<vendor>` is a separate internal library for each vendor. Every module exposes
  `detect`, `catalogue_request`, `parse_catalogue`, `detail_request` and `parse_posting`.
- `ats_vendors.registry` binds verified portal identities to known endpoints and network rules.
  `ats_vendors.runtime.Connector` implements SDK v2 enumeration, details and normalization using
  only brokered `sdk.fetch` calls. Inline full ads avoid unnecessary detail requests.
- `employers.ready` creates candidates; `employers.ats_validation` independently reparses the
  broker's observations, binds each posting to its catalogue item and checks all extracted fields.
  It indexes each inline feed once per run, keeping the index out of saved evidence and detail
  caches. Ordinary generated JSON/HTML manifests keep their existing validator.

These are packages in the application's existing wheel and runner images, not external services
or eight separately deployed applications. There is no additional scraping framework dependency
or database migration.

## Supported public protocols

| Library | Catalogue and details | Behavior that matters |
| --- | --- | --- |
| Workable | `/api/v1/widget/accounts/{tenant}?details=true`; `/{tenant}/jobs/view/{shortcode}.md` fallback | Preserve description, requirements and benefits; retain the entire Markdown body; merge consistent duplicate-location records by shortcode. |
| SuccessFactors | RMK `/sitemal.xml` RSS; legacy `/career?company=…&career_ns=job_listing_summary&resultType=XML` | Separate protocols; custom RMK domains need observed script/config evidence; legacy locale is explicit; unknown description template fields fail visibly. |
| Workday | CXS `/wday/cxs/{tenant}/{board}/jobs`, then item detail | Offset traversal in pages of 20, first-total reconciliation, later zero sentinels, repeated-page detection; a total of 2,000 remains partial. |
| Greenhouse | Global/EU board API with `content=true` | Full inline content, offices, departments and requisitions; first publication is separate from update time. |
| Lever | Global/EU public postings API | Combine introduction, every list section and additional text; keep hybrid uncertainty; creation time is metadata, not publication. |
| Ashby | Public posting API with compensation | Rich description, secondary locations, explicit workplace type and structured compensation. |
| Recruitee | Tenant `/api/offers/` | Combine description and requirements; retain separate application links and update times. |
| Teamtailor | Tenant `/jobs.rss` | Parse namespaced locations and explicit remote status; full inline description. |

SmartRecruiters continues to use its existing maintained adapter. URL matching is exact and keeps
regional vendor hosts. Custom domains for the other vendors, interactive/authenticated portals,
unrecognized XML layouts, and unresolved legacy template variants need the custom flow or a
reviewed library extension. The generic generated validator supports JSON/HTML; it does not accept
arbitrary XML manifests. A repair can inspect a public HTML/JSON detail or require review if no
supported evidence path exists. No parser turns an unexpected page into a successful empty feed.

## Preserved product rules

The libraries emit the existing `CollectedPosting` contract. Existing provenance, source scopes,
exact deduplication, profile matching, LLM enhancement, CV tailoring, cost reservations, user
statuses, events, applications and letters continue through the same code paths. Routine jobs do
not silently replace active custom connectors with a ready library.

Descriptions are complete source fields, without a fixed-length cut. Missing descriptions stay
partial and cannot activate a candidate. Remote work alone does not establish Swiss eligibility.
Unknown locations and publication dates stay unknown. An update, expiration, start or creation
timestamp is not substituted for publication.

JSON feed fields and XML RSS/legacy records are checked per posting. Text from a different vacancy
cannot validate an extracted title or description. XML rejects DTDs, entities and malformed input;
it never loads external resources. All network requests still use the existing public-DNS-pinned
broker, limits, redirect policy and read-only allowlist. The isolated runner has no network,
credentials or application database.

Detail caches remain version-scoped with the original TTL. A hit rebinds cached detail evidence
to the fresh matching catalogue item; it neither reuses an old catalogue as coverage proof nor
extends the detail TTL. Inline feeds already contain the detail and need no additional request.

Partial, malformed, repeated, oversized and capped collections never advance missing counters.
The existing two complete observations separated by at least two hours are still necessary for
source closure. HTTP 429/5xx and transport failures back off without immediate model repair.
Collection byte/page result caps require review; regenerating code cannot raise trusted limits.

## Building and updating

Build both runtime targets from the same checkout as the backend and pin their immutable image
IDs using [the existing runtime installation procedure](employer-sources.md#isolated-runtime-installation-on-ubuntu-arm64).
The library source fingerprint is part of the candidate manifest and checked by both host and
container. A changed library or stale image fails closed. Explicit Retry for a ready source builds
a fresh candidate and repeats validation before promotion. An active custom source retains its
saved code during revalidation. Existing versions and vacancy history remain recorded.

For HTTPS-intercepting development build environments, Docker accepts an optional build-only CA
secret: `--secret id=proxy_ca,src=/path/to/trusted-ca-bundle.pem`. TLS verification remains enabled;
the secret is not copied into the image. Standard deployments need no secret.

## Verification

```bash
python -m pytest tests/test_ats_libraries.py tests/test_ats_product.py -q
docker build --target python -t sjh-connector-python -f connector_runtime/Dockerfile .
docker image inspect sjh-connector-python --format '{{.Id}}'
python tools/check_ats_libraries.py sha256:<actual-image-id>
```

The tests use synthetic Acme fixtures and temporary databases, with no model calls. They cover
all eight vendors plus legacy SuccessFactors, full bodies, item-specific evidence, malformed and
empty feeds, dates, locations, cache rebinding, ingestion history, queue fallback, limits and
official-page verification. The container tool exercises actual SDK IPC in a read-only,
network-disabled container. It does not certify production AppArmor profiles, ARM64, live tenant
access or vendor completeness. Run a shadow pilot on each actual source before enabling ingestion.

Research sources and design choices are recorded in [ATS research](ats-research.md).

On 2026-10-23, read-only public protocol checks parsed nonempty catalogues and a full posting sample
from Hugging Face (Workable), Swiss Re (SuccessFactors RMK), NVIDIA (Workday, first page only),
Figma (Greenhouse), Spotify (Lever), Linear (Ashby), bunq (Recruitee), and Teamtailor's own careers
site. Tellent's Recruitee feed returned an explicit empty offers array. Retired Proton/komoot
Recruitee URLs and the guessed Teamtailor tenant URL returned 404; those failures were not counted
as empty jobs. These development checks used the environment's approved HTTPS proxy, made no
application-database writes or LLM calls, and do not certify the production DNS-pinned broker,
official employer ownership, full Workday coverage, legacy SuccessFactors live access or ARM64.

The implementation passed 226 Python regression tests, the existing UI tests and production UI
build, strict lint on the new packages/tests/tool, wheel package inclusion checks, and all nine
fixture variants in the Python 3.11 container on x86_64. The manual CI workflow now repeats the
offline library/product tests, strict library lint and network-disabled container check.
