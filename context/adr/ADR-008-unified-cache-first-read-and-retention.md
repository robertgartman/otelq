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
created: 2026-07-02
last_updated: 2026-07-02
related_documents:
  - ADR-005-incremental-parquet-cache
  - ADR-001-host-cli-reads-bind-mounted-files
  - ADR-006-read-otlp-extension-quirks
  - SPEC-otelq-incremental-cache
  - SPEC-otelq-cli
  - CONTRACT-telemetry-directory
supersedes: ADR-005-incremental-parquet-cache
superseded_by: null
ai_summary: "Replace the bounded rolling accelerator with a single cache-first read that seals every parsed minute and retains a longer event-time window, so an investigation's repeated wide queries stay warm."
semantic_tags:
  - otelq
  - parquet
  - cache
  - retention
  - unified-read
  - investigation
  - observability
---

# ADR-008 — Unified Cache-First Read and Extended Retention

> **Status: accepted.** This ADR revises the cache architecture established in
> [ADR-005](../archive/ADR-005-incremental-parquet-cache.md) and **supersedes
> it** — specifically its *bounded-rolling-window* and *stateless-cold-path*
> decisions (and the corresponding "footprint is bounded to ~RETENTION minutes"
> consequence). The remainder of ADR-005 — the cursor, margin-based sealing, the
> event-time watermark, the `O_EXCL` single-writer lock, and `--no-cache` as the
> reference oracle — is carried forward unchanged and remains binding as restated
> here and in [SPEC-otelq-incremental-cache](../spec/SPEC-otelq-incremental-cache.md).

## Context

[ADR-005](../archive/ADR-005-incremental-parquet-cache.md) accelerates queries with a
per-minute Parquet cache, but deliberately as a **narrow rolling accelerator**:
it seals only minutes inside a short hot window (~RETENTION minutes), evicts
everything older, and answers any wider query on a **stateless cold path** that
neither reads nor writes the cache. That design optimises for a single recent
query.

Real usage is different. A debugging **investigation is a burst of queries over
the same, often wide, time window** — "show me the last hour," then a dozen
follow-ups narrowing, pivoting, and re-widening across that same hour. Under
ADR-005 every one of those wide queries is a full raw re-scan
([ADR-006](ADR-006-read-otlp-extension-quirks.md): the reader cannot tail or
seek, so it re-parses whole files from byte zero). The cache — the one mechanism
that could amortise the burst — is bypassed precisely when it would help most,
because the window reaches past the hot window. The first wide query and the
tenth identical wide query cost exactly the same.

The question is whether to change the cache from a *narrow recent-window
accelerator* into a *general accelerator for any repeated query over any window
still backed by raw data* — and what that does to the correctness invariant and
to the CLI-only constraint that
[ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md) makes foundational.

This ADR records the decision and its rationale only. The precise, testable
behaviour — split boundaries, retention constant, eviction trigger, signal
scoping, and the redefined equivalence rule — graduates into
[SPEC-otelq-incremental-cache](../spec/SPEC-otelq-incremental-cache.md).

## Decision

Replace the two-rail hot/cold model with a **single cache-first read path**, and
extend retention so a burst of wide queries stays warm.

1. **Cache-first, gap-filled reads.** For any command and window, otelq resolves
   the answer from `sealed parquet ∪ raw-for-the-uncovered-minutes`: minutes that
   have a sealed partition are read from Parquet, and only the minutes not yet
   sealed are read from raw. There is no longer a window threshold that switches
   off the cache. (Generalises ADR-005's existing hot-path `sealed ∪ pending-tail`
   union to arbitrary windows.) The union **must** partition minutes cleanly so a
   minute served from Parquet is never also counted from raw.

2. **Seal what you parse.** Any complete minute otelq parses to answer a query is
   sealed into the cache, not just minutes inside a short hot window. A wide query
   therefore *warms* the cache for the whole range it touched, so the next query
   over that range is served from Parquet. This retires ADR-005's "seal only
   within the hot window" stance and makes the former cold path a cache **writer**.

3. **Signal-scoped maintenance.** Ingest and sealing run only for the signal
   stream(s) the current command actually reads; a `logs` query does not parse and
   seal traces or metrics. (Commands that span all signals — e.g. `summary`,
   `sql` — still scope to all.) Cursors are already per-stream, so this is a
   narrowing of work, not a new mechanism.

4. **Extended, event-time retention.** Retention grows from a short rolling window
   to a longer horizon (target: ~24h of event-time), evicted on an age basis
   measured against the observed event-time watermark and protected by the
   existing far-future clamp so a poison timestamp cannot evict the whole cache.
   Eviction is decoupled from sealing (it runs for every signal with sealed
   partitions) and is a cheap unlink of out-of-horizon partitions.

5. **Maintenance stays off the query's critical path, without a daemon.** The
   answer to the current query is produced from `cache ∪ raw` and returned
   **before** any sealing of newly-parsed minutes; sealing only ever benefits
   *future* queries. Cheap maintenance (evicting out-of-horizon partitions;
   sealing the handful of newly-complete recent minutes) runs in the same
   invocation. The potentially-large **historical backfill** — sealing many old
   minutes read by a first wide query — is paid **once**, by that first query of
   an investigation, so queries 2..N in the burst are fast. otelq **must not**
   introduce a background daemon, listener, or warm-process assumption to do this
   work (see Alternatives; this preserves
   [ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md)).

6. **Minute-granular sealing with a pending-tail; empty-delta short-circuit.**
   Sealed partitions stay **minute-granular**, and the freshest, not-yet-sealable
   data (the current partial minute plus the ~MARGIN of minutes still open for
   late arrival) is carried forward from ADR-005's **pending-tail** mechanism: it
   is held in a small `pending` Parquet, not re-read from raw at query time. A
   query touching "now" therefore parses only the **incremental raw delta since the
   previous invocation's cursor position** (proportional to inter-query arrival,
   not to corpus or tail size) and reads `sealed ∪ pending`. When that delta is
   **empty** — a burst of "now" queries with no new bytes between them — otelq
   **must** skip ingest, sealing, and the pending rewrite entirely and answer from
   the existing `sealed ∪ pending`, so a rapid burst does zero write work.

7. **`--no-cache` remains the pure-raw path** — the debugging escape hatch and the
   test oracle for correctness (see the redefined invariant below).

**Redefined correctness invariant.** ADR-005's invariant — *cached output is
byte-identical to a full raw re-scan* — no longer holds unconditionally once the
cache can retain data that the Collector's raw backups have already rotated away
(the cache may outlive raw). The binding invariant becomes: **over the range that
raw still covers, cached output is byte-identical to `--no-cache`; where raw
backups have been evicted, the cache may return additional records that raw no
longer has, never fewer and never different.** The cache is still an accelerator
that never *changes* an answer within raw's coverage; beyond that coverage it is
an additive, longer-lived view. This redefinition is the load-bearing consequence
of decisions 2 and 4 and is why this ADR supersedes ADR-005 rather than amending
the SPEC alone.

## Alternatives Considered

- **Keep ADR-005 as-is (narrow rolling accelerator).** Rejected for the
  investigation workload: it structurally cannot amortise a burst of wide queries,
  because the cache is bypassed for any window past the hot window. The repeated
  full re-scans are the exact cost this ADR exists to remove.

- **Cache older windows but keep the cold path stateless (read cache, never write
  it from wide queries).** Rejected as internally inconsistent: if wide queries
  never seal, the cache is never populated beyond the recent window, so
  cache-first reads (decision 1) would have nothing to hit for historical ranges.
  Decisions 1 and 2 only pay off together.

- **A background maintenance process (detached child, or any resident helper) that
  seals/evicts asynchronously after the agent gets its answer.** This is the
  literal "async maintenance" idea and is attractive for shaving the first wide
  query. **Deferred, not adopted.** It sits against ADR-001's foundational rule of
  "no server, no long-running daemon… the CLI holds no state and serves no
  requests," and its benefit is only the *first* query of a burst (sealing helps
  future queries, and eviction is microseconds). Adopting it would overturn a
  foundational non-negotiable for a one-query win; it should be justified by its
  own evidence-backed ADR if the synchronous first-query cost is later measured to
  be unacceptable. Until then, decision 5 keeps maintenance off the critical path
  by *ordering* (answer first, then seal) rather than by *concurrency*.

- **A persistent materialised DuckDB store as system of record.** Rejected for the
  same reasons as in ADR-005: it makes otelq the telemetry system of record rather
  than a thin reader over the Collector's files, which the dev use case does not
  justify. Deferred, not foreclosed.

- **Sub-minute (e.g. per-second) sealed partitions to shrink the "unsealed now"
  gap.** Rejected. The freshest data is already served warm from the pending tail
  (decision 6), so finer sealed partitions would not make "now" queries faster;
  they would only fragment the *sealed* side. The genuine reason recent minutes are
  unsealed is the MARGIN late-arrival tolerance, which is independent of partition
  size — sub-minute sealing would still hold ~MARGIN open. Meanwhile it multiplies
  per-file Parquet overhead, `read_parquet` fan-out, and eviction churn by the
  granularity factor. Minute granularity, plus the pending tail and the empty-delta
  short-circuit, addresses the rapid-"now"-query case without this cost.

## Consequences

- **The hot/cold dichotomy collapses into one path.** SPEC-otelq-incremental-cache
  must be amended to describe a single cache-first read with gap-fill, the
  seal-what-you-parse rule, signal-scoped maintenance, the extended event-time
  retention and its eviction trigger, and — most importantly — the **redefined
  equivalence invariant**. Requirement IDs are append-only; the existing FR/EC/AC
  that assert "stateless cold path" and "footprint bounded to ~RETENTION minutes"
  become stale and must be reconciled.

- **Correctness testing changes shape.** The `cached == --no-cache` diff remains
  the oracle **only over raw's covered range**. A new test dimension is needed for
  the cache-outlives-raw case (cache ⊇ raw). The clean no-double-count partition
  of the `cache ∪ raw` union becomes a first-class correctness property with its
  own coverage.

- **Disk footprint grows substantially** — a ~24h horizon is roughly two orders of
  magnitude more partitions per signal than the former short window. This is
  acceptable for dev telemetry but must be a documented, ideally configurable,
  number, and it remains scoped inside `telemetry/.otelq-cache/`
  ([CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md)) so
  `otel-clean` still resets it.

- **ADR-001 is preserved by decision 5.** No daemon, no listener, no warm process;
  maintenance is reordered after the response, not moved to a concurrent service.
  The one query that pays the historical backfill is the investigation's first,
  which is an acceptable, bounded, one-time cost.

- **Recommended phased rollout** (each phase independently shippable and testable):
  (A) decisions 1 + 2 with synchronous sealing and a modest horizon — captures
  most of the value with the invariant still `cache ⊆ raw`; (B) decision 3
  signal-scoping; (C) decision 4 extended retention — this is the phase that
  triggers the invariant redefinition and its SPEC/tests; (D) revisit the deferred
  background-maintenance alternative only if Phase A's first-query cost proves too
  high, via a dedicated ADR.

- **Reversibility.** Phases A–C are contained within the existing cache layout and
  lock; reverting to ADR-005 behaviour is a matter of restoring the hot-window
  seal bound and the stateless cold path. The invariant redefinition (Phase C) is
  the only hard-to-reverse commitment, which is why it is isolated to its own
  phase and gated on acceptance of this ADR.
