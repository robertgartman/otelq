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
last_updated: 2026-06-23
related_documents:
  - ADR-002-pep723-uv-single-file-distribution
  - ADR-006-read-otlp-extension-quirks
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
   community build at the time of writing. This pin appears in **both** the PEP
   723 inline block and the package `pyproject`, kept in sync per
   [ADR-002](ADR-002-pep723-uv-single-file-distribution.md).
2. **Govern the pin with CI.** A scheduled **extension-probe workflow** verifies
   that the pinned DuckDB version still has a loadable `otlp` community build, so
   that drift between DuckDB releases and extension availability is caught by
   automation rather than by a developer hitting a 404.
3. **Support an offline / vendored fallback.** For air-gapped or
   determinism-sensitive environments, the extension may be loaded from a mirror
   or a local directory instead of the community network repository.

### Live drift to record explicitly

There is a **known, tracked version drift** at the time of writing, and it is
recorded here deliberately rather than silently resolved:

- The code pins **`duckdb==1.5.3`**.
- The current community build, **`otlp` v0.5.0**, now **declares DuckDB
  `>=1.5.4`**.

A bump to **`duckdb==1.5.4`** is therefore a **tracked follow-up** that must be
**validated end-to-end before it is taken** — it must **not** be bumped blindly.
The scheduled extension-probe workflow exists precisely to surface this kind of
mismatch; the bump is gated on the validation checklist below, not on the
existence of a newer number.

## Alternatives Considered

- **Hand-write an OTLP-JSON parser.** Rejected. OTLP's JSON encoding (nested
  resource/scope structure, the metric type variants, attribute typing) is
  non-trivial and a moving target; owning a parser means owning that surface
  forever and re-deriving the SQL-queryable shape the extension already provides.
  The extension's own parsing quirks are instead documented and worked around in
  [ADR-006](ADR-006-read-otlp-extension-quirks.md), which is far less costly than
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
  1. Confirm a **published `otlp` build exists for the target DuckDB version
     across the platforms otelq supports** (per the platform support in
     [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md)), not merely that the extension
     *declares* compatibility.
  2. Bump the pin in **both** the PEP 723 inline block **and** `pyproject`
     together — never one without the other (ADR-002).
  3. **Re-validate the 2048-row workaround** against the new DuckDB version, since
     that workaround depends on `read_otlp_*` behavior the new version could alter
     (see [ADR-006](ADR-006-read-otlp-extension-quirks.md)).
- **A scheduled extension-probe workflow governs the pin continuously.** It is the
  early-warning system for both the current `1.5.3 → 1.5.4` drift and any future
  divergence; the pin is only ever moved through the checklist above, never
  reactively.
- **An offline / air-gapped path is available and deterministic.** The extension
  can be loaded without the community network repository by installing from a
  mirror or local directory — `INSTALL otlp FROM '<mirror-or-local-dir>'` — or by
  pointing DuckDB at a vendored extension directory (`SET extension_directory=...`)
  with `allow_unsigned_extensions` enabled. The `otlp` project additionally
  publishes an **unsigned GitHub-Pages repository**, which serves this offline /
  CI-determinism case directly. This keeps CI and air-gapped runs from depending
  on live community-repository availability.
- **The tool is agnostic to extension *acquisition*, not to the *pin*.** How the
  extension is loaded (community vs mirror vs vendored) can vary per environment,
  but the DuckDB version is fixed by the pin; the behavioral surface the loaded
  extension exposes is specified in
  [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md), and its parsing peculiarities are
  captured in [ADR-006](ADR-006-read-otlp-extension-quirks.md).
