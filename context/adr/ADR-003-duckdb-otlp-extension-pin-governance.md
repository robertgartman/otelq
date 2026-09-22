---
doc_type: adr
authoritative: true
stability: stable
status: active
decision_scope: architecture
audience:
  - ai
  - engineering
must_not_contain:
  - feature_requirements
  - implementation_walkthroughs
  - reversible_decisions
created: 2026-06-23
last_updated: 2026-09-22
related_documents:
  - ADR-002-pep723-uv-single-file-distribution
  - ADR-006-read-otlp-extension-quirks
  - ADR-010-native-otlp-reader-schema
  - ADR-001-host-cli-reads-bind-mounted-files
  - SPEC-otelq-cli
supersedes: null
superseded_by: null
ai_summary: "Query OTLP via in-process DuckDB + the smithclay/duckdb-otlp community extension, pinned to the exact DuckDB version the extension is built for; govern the pin with CI; support an offline fallback."
semantic_tags:
  - otelq
  - duckdb
  - duckdb-otlp
  - community-extension
  - version-pin
  - ci-governance
  - offline-fallback
  - platform-coverage
  - observability
---

# ADR-003 — DuckDB / OTLP Extension Pin Governance

## Context

`otelq` reads OTLP-JSONL files (the durable half of the seam in
[ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md)) and answers SQL queries
over them. It needs an engine that can parse OTLP JSON and run SQL **in-process**,
with no server — consistent with the no-daemon stance of ADR-001 and the
single-file distribution of
[ADR-002](ADR-002-pep723-uv-single-file-distribution.md).

The chosen engine is **in-process DuckDB** plus the **`smithclay/duckdb-otlp`
community extension**, loaded with `INSTALL otlp FROM community; LOAD otlp`. This
introduces a sharp version constraint:

> The community extension is **built per DuckDB version** and **lags new DuckDB
> releases.** It is published to DuckDB's community-extensions repository for
> specific versions only. A floating `duckdb>=` dependency will resolve to the
> newest DuckDB, for which the matching `otlp` build may not yet exist — and then
> `INSTALL otlp FROM community` **404s and every otelq command fails.**

The pin is therefore not a hygiene preference; it is the load-bearing condition
for the tool functioning at all. It must be chosen to match an *actually
published* extension build, governed so it cannot silently drift, and have a
fallback for environments without network access to the community repository
(air-gapped machines, deterministic CI).

## Decision

Query OTLP via **in-process DuckDB + the `smithclay/duckdb-otlp` community
extension**, and govern its version as follows:

1. **Pin DuckDB to the exact version for which the extension is published.** The
   pin is `duckdb==1.5.3` — the latest DuckDB version with a published `otlp`
   community build at the time of writing. (Amended 2026-07-07: the pin is now
   `duckdb==1.5.4`; amended 2026-09-21: the pin is now `duckdb==1.5.5`; see the
   amendments below.) This pin appears in **both** the PEP
   723 inline block and the package `pyproject`, kept in sync per
   [ADR-002](ADR-002-pep723-uv-single-file-distribution.md).
2. **Govern the pin with CI.** A scheduled **extension-probe workflow** verifies
   that the pinned DuckDB version still has a loadable `otlp` community build, so
   that drift between DuckDB releases and extension availability is caught by
   automation rather than by a developer hitting a 404.
3. **Support an offline / vendored fallback.** For air-gapped or
   determinism-sensitive environments, the extension may be loaded from a mirror
   or a local directory instead of the community network repository.

### Version drift: validated 2026-06-23 — stay on 1.5.3

The `otlp` *source* manifest (v0.5.0) declares support for DuckDB `>=1.5.4`, which
suggested a bump. That bump was **validated end-to-end on 2026-06-23 and
rejected** — there is **no published `otlp` build for DuckDB 1.5.4**. Evidence:

- `community-extensions.duckdb.org/v1.5.4/<platform>/otlp.duckdb_extension.gz`
  returns **HTTP 404** on both `osx_arm64` and `linux_amd64`; the `v1.5.3` URL
  returns **200** on both.
- `INSTALL otlp FROM community; LOAD otlp` under `duckdb==1.5.4` fails with
  `HTTPException (HTTP 404)`; the full suite drops from **44 passed** to
  **20 passed / 24 failed** (only the extension-free cache-logic tests survive).
- `duckdb==1.5.3` loads `otlp` cleanly and keeps all **44 tests green**.

**Conclusion:** `duckdb==1.5.3` remains the newest DuckDB with a loadable `otlp`
build and is the correct pin. A move to 1.5.4 is **deferred, not pending** — it is
the per-version build lag described in the Context, observed in the wild. Revisit
only when the extension-probe (which loads `otlp` against the pinned version)
shows a published 1.5.4 build; then run the checklist below.

### Amendment 2026-07-07 — pin bumped to 1.5.4 (checklist executed)

The condition above was met: upstream published **duckdb-otlp v0.6.0 built for
DuckDB 1.5.4** (extension version `5f32698`) to the community-extensions
repository. The pin-bump checklist was executed and the pin is now
**`duckdb==1.5.4`**:

1. **Published build confirmed** — `INSTALL otlp FROM community; LOAD otlp`
   under `duckdb==1.5.4` succeeds on `osx_arm64`, and
   `community-extensions.duckdb.org/v1.5.4/linux_amd64/otlp.duckdb_extension.gz`
   returns HTTP 200 — an actual published build on the supported platforms, not
   a manifest claim.
2. **Pin bumped in both places together** — the PEP 723 inline block and
   `pyproject` (plus the `justfile` test recipe that repeats the pin).
3. **2048-row workaround re-validated** — and found **obsolete**: v0.6.0 fixes
   the row-count crash (a 300 000-row single-call read is crash-free) and the
   `TIMESTAMP_MS` bug. The workaround is retired rather than re-applied.

v0.6.0 is a **breaking schema change** with new failure modes; how otelq
absorbs it is decided in
[ADR-010](ADR-010-native-otlp-reader-schema.md), which supersedes
[ADR-006](../archive/ADR-006-read-otlp-extension-quirks.md). The governance
model of this ADR — exact pin, CI probe, offline fallback, checklist-gated
bumps — is unchanged and applies to all future bumps.

### Amendment 2026-09-21 — the published build is per platform, and the offline path is now built

The 2026-07-07 bump confirmed a published build on `osx_arm64` and `linux_amd64`
only. That was not the whole condition. The community repository builds **per
platform as well as per version**, and each extension entry declares an
`excluded_platforms` set. otlp 0.6.x excludes `windows_amd64`,
`windows_amd64_mingw` and `linux_amd64_musl` — all of which had builds under
1.5.3. The bump to `duckdb==1.5.4` therefore **silently dropped Windows and musl
support**, and nothing could see it: CI runs `ubuntu-latest` and `macos-latest`,
and the extension probe ran the same two. It surfaced as a user report.

The failure is quiet in an unhelpful way: `uvx otelq` **installs cleanly** on
Windows — otelq's own wheel is `py3-none-any` and DuckDB publishes Windows wheels
— and then fails on the first query, when `INSTALL otlp FROM community` 404s. The
missing artefact is the extension, never the package, so "no Windows build of
otelq" is the wrong place to look.

Evidence, 2026-09-21:
`community-extensions.duckdb.org/v1.5.4/windows_amd64/otlp.duckdb_extension.gz`
returns **HTTP 404** while the `v1.5.3` URL returns **200**; `v1.5.5` (otlp 0.6.1)
is 404 on Windows as well. Upstream restored MSVC builds in
`smithclay/duckdb-otlp#67` (otlp 0.7.x, built against DuckDB v1.5.5), but nothing
is published until the entry in `duckdb/community-extensions` is updated — it
still carries otlp 0.6.1 with `windows_amd64` excluded.

**The supported-platform set is named here**, because the checklist below has to
check against something concrete and previously did not. As of `duckdb==1.5.4`
with otlp 0.6.x, otelq's query path works on `linux_amd64`, `linux_arm64`,
`osx_amd64` and `osx_arm64`. `windows_amd64`, `windows_amd64_mingw` and
`linux_amd64_musl` have **no published build**; on those, otelq installs and every
query fails unless the extension is supplied by the fallback below. WSL2 is the
Windows answer in the meantime, because it resolves as `linux_amd64`.

**Decision point 3 is now implemented rather than merely available.** The offline
/ vendored path had been decided in 2026-06-23 and never built, so an unsupported
platform had no way out at all. It is now reachable from the CLI through two
environment variables — one naming an extension file to load directly, one naming
a repository to install from, with the file winning over the repository and the
repository over the community default. Two DuckDB behaviours shaped that and are
recorded because neither is obvious:

- A custom repository must be installed with **`FORCE`**. A machine that has
  already installed `otlp` from the community repository refuses a plain install
  of the same extension from a different origin, so without `FORCE` the mirror
  path would fail for every existing user and work only on a clean machine.
- Loading an extension file directly requires **`allow_unsigned_extensions`**, a
  connection-time setting. A self-built extension — the whole reason to use this
  path on a platform the community repository does not cover — carries no
  signature. Naming a file in that variable is therefore the trust decision, and
  it is scoped to that case alone: the mirror and community paths keep signature
  verification. The normative CLI contract for the two variables belongs in
  [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md); this ADR records only that the
  fallback exists and why it has this shape.

**The probe now asserts a state per platform.** Each platform leg declares whether
a build is expected to exist, and a leg goes red when its platform's state
*changes*. The Windows leg is green while the gap persists and red on the day the
build appears — that red is the signal to run the checklist, not a regression.
Without the per-platform expectation, adding Windows to the probe would only mean
a permanently red job that nobody reads.

### Amendment 2026-09-21 (second) — pin bumped to 1.5.5; Windows is reachable through a supplied binary

The amendment above left Windows with one way forward: build the extension
yourself. That was incomplete. A `windows_amd64` build of `otlp` **already
exists** — published by the extension's own project rather than by the community
repository — and it is built for DuckDB **v1.5.5 only**. Under `duckdb==1.5.4`
DuckDB refuses it outright, because an extension binary is locked to the exact
DuckDB version it was built against; and the community repository builds against
the current DuckDB release, so no Windows build will ever appear under `v1.5.4`
there either. **The pin was the blocker on every route to Windows**, which is why
it moves now instead of waiting for the community entry to catch up.

The pin-bump checklist was executed and the pin is now **`duckdb==1.5.5`**:

1. **Published build confirmed on every supported platform** —
   `community-extensions.duckdb.org/v1.5.5/<platform>/otlp.duckdb_extension.gz`
   was fetched, not sampled, for `linux_amd64`, `linux_arm64`, `osx_amd64` and
   `osx_arm64`. Each is a signed build whose metadata footer names DuckDB
   `v1.5.5`, its own platform, and extension version `8e627d8` (otlp 0.6.1). No
   supported platform is dropped. `windows_amd64`, `windows_amd64_mingw` and
   `linux_amd64_musl` are 404 under `v1.5.5`, exactly as under `v1.5.4`.
2. **Pin bumped everywhere together** — the PEP 723 inline block, `pyproject`
   and its lockfile, and every place that repeats the pin (the `justfile` test
   recipe, the pre-commit hook, and the CI, release and probe workflows).
3. **Large single-call read re-validated** — a 300 000-row single-call
   `read_otlp_traces` is complete and crash-free under 1.5.5, so the retired
   2048-row workaround stays retired. The full suite passes under 1.5.5.

otlp 0.6.0 → 0.6.1 is not a schema change; the data-model decisions of
[ADR-010](ADR-010-native-otlp-reader-schema.md) carry over unchanged.

**The supported-platform set, as of `duckdb==1.5.5`.** The community path is
unchanged: `linux_amd64`, `linux_arm64`, `osx_amd64`, `osx_arm64`.
`windows_amd64` becomes **supported through a supplied binary**: upstream
`smithclay/duckdb-otlp#67` restored MSVC builds, and from otlp 0.7.1 the project
publishes a `windows_amd64` binary for DuckDB `v1.5.5` in its own extension
repository (served from GitHub Pages, and attached to each release as an
archive). Loaded through the explicit-file variable, it runs otelq on native
Windows without WSL2. `windows_amd64_mingw` and `linux_amd64_musl` still have no
build anywhere.

Two properties of that binary decide how it may be used:

- **It is unsigned, so only the explicit-file path can load it.** The repository
  path keeps signature verification (the decision above), and DuckDB refuses an
  unsigned extension at install time. Pointing the repository variable at
  upstream's own repository therefore does **not** work, on any platform.
  Whether to add an explicit opt-in for unsigned repositories is a separate trust
  decision and is **not** taken here.
- **Version skew between platforms is accepted.** Windows loads otlp 0.7.x while
  the community platforms load 0.6.1, until the community entry moves. The reader
  surface otelq depends on is the same in both: the full suite and the
  300 000-row read were run under 1.5.5 against 0.6.1 *and* 0.7.x and pass
  identically.

**Windows support is verified, not inferred.** CI gains a Windows job that loads
a pinned upstream release binary — pinned by release tag *and* checksum, so the
run is deterministic and moving it is a deliberate act — and runs the full suite
against it. The statement "otelq works on native Windows" is exactly as true as
that job is green. The suite otherwise forces the community path for hermeticity,
which is precisely what does not exist on Windows, so it gains one explicit,
test-only override that names a supplied binary.

**The probe's Windows leg keeps `expect: unavailable`**, now against `v1.5.5`.
When the community entry moves to otlp 0.7.x a signed `windows_amd64` build
appears there and the leg goes red — and the response no longer involves the pin
at all: flip the leg, move the Windows CI job onto the community path, and drop
the supplied-binary caveats.

## Alternatives Considered

- **Hand-write an OTLP-JSON parser.** Rejected. OTLP's JSON encoding (nested
  resource/scope structure, the metric type variants, attribute typing) is
  non-trivial and a moving target; owning a parser means owning that surface
  forever and re-deriving the SQL-queryable shape the extension already provides.
  The extension's own parsing quirks are instead documented and worked around in
  [ADR-006](../archive/ADR-006-read-otlp-extension-quirks.md), which is far less costly than
  reimplementing the parser.
- **A different embedded query engine.** Rejected. No other embeddable, in-process
  SQL engine pairs with a ready-made OTLP reader; switching engines would forfeit
  the extension and reopen the hand-written-parser problem.
- **Float the DuckDB version (`duckdb>=`).** Rejected — this is the failure mode
  the whole ADR exists to prevent: an open range resolves ahead of the
  per-version extension build and makes `INSTALL otlp FROM community` 404, failing
  every command.

## Consequences

- **A pin-bump checklist is mandatory.** Before changing the DuckDB pin, all of
  the following must hold:
  1. Confirm a **published `otlp` build exists for the target DuckDB version on
     every supported platform** — each one fetched, not sampled, and never a
     compatibility claim in the extension's manifest. The supported set is named
     in the 2026-09-21 amendment above. A bump that drops a platform from that
     set is a breaking change and must be recorded as one; absorbing it silently
     is exactly what the 2026-07-07 bump did to `windows_amd64` and
     `linux_amd64_musl`.
  2. Bump the pin in **both** the PEP 723 inline block **and** `pyproject`
     together — never one without the other (ADR-002).
  3. **Re-validate the 2048-row workaround** against the new DuckDB version, since
     that workaround depends on `read_otlp_*` behavior the new version could alter
     (see [ADR-006](../archive/ADR-006-read-otlp-extension-quirks.md)).
  4. **Move the supplied-binary references with the pin** (added 2026-09-21). The
     Windows route is locked to the pinned DuckDB version exactly as a community
     build is: the Windows CI job's pinned upstream release and checksum, and the
     download location the README gives Windows users, both name the DuckDB
     version and must move in the same change. A bump to a DuckDB version for
     which upstream publishes no `windows_amd64` binary drops Windows, and falls
     under item 1 — a breaking change, recorded as one.
- **A scheduled extension-probe workflow governs the pin continuously**, one leg
  per platform, each asserting whether a build is expected to exist. It is the
  early-warning system in both directions: a supported platform losing its build,
  and an unsupported one gaining one. The pin is only ever moved through the
  checklist above, never reactively.
- **An offline / air-gapped path is available and deterministic**, and as of the
  2026-09-21 amendment it is implemented rather than notional: the extension can
  be loaded from a supplied file or from a mirror instead of the community network
  repository. The `otlp` project additionally publishes its own **unsigned**
  extension repository (GitHub Pages, plus a per-release archive). Because it is
  unsigned it is usable **only through the supplied-file path** — fetch the
  binary, then name the file; the repository path keeps signature verification
  and DuckDB refuses an unsigned extension at install. (Corrected 2026-09-21:
  this bullet previously said that repository "serves this case directly", which
  the implementation never allowed.) This keeps CI and air-gapped runs from
  depending on live community-repository availability — and it is the only way to
  run otelq at all on a platform with no published community build.
- **The tool is agnostic to extension *acquisition*, not to the *pin*.** How the
  extension is loaded (community vs mirror vs vendored) can vary per environment,
  but the DuckDB version is fixed by the pin; the behavioral surface the loaded
  extension exposes is specified in
  [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md), and its parsing peculiarities are
  captured in [ADR-006](../archive/ADR-006-read-otlp-extension-quirks.md).
