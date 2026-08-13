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
created: 2026-08-13
last_updated: 2026-08-13
related_documents:
  - ADR-001-host-cli-reads-bind-mounted-files
  - ADR-008-unified-cache-first-read-and-retention
  - ADR-012-exit-codes-as-public-contract
  - SPEC-otelq-cli
  - SPEC-otelq-incremental-cache
  - SPEC-otelq-await
supersedes: null
superseded_by: null
retrieval_priority: high
ai_summary: "The query window's lower bound is measured from host wall-clock, not from the newest observed event-time, so `--since 10m` means the last ten minutes; the upper bound stays a generous skew ceiling so clock-skewed producers are never silently dropped."
semantic_tags:
  - otelq
  - query-window
  - wall-clock
  - staleness
  - clock-skew
  - retention
---

# ADR-015 — The Query Window Is Anchored on Wall-Clock, Not on the Newest Record

## Context

otelq measured its trailing query window from the **maximum observed
event-time**: `hi = min(max_event_time, wall_clock + MAX_FUTURE_SKEW)` and
`lo = hi - window`. [SPEC-otelq-incremental-cache](../spec/SPEC-otelq-incremental-cache.md) INV-7
stated this explicitly —
windows were "measured relative to the maximum observed event-time, not the host
wall-clock".

Under that rule `--since 10m` does not mean "the last ten minutes". It means
**"the ten minutes preceding the newest record in the store"**. The two coincide
only while telemetry is actively arriving, and diverge without limit the moment
it stops.

The consequences are asymmetric, and the dangerous one is silent:

- A caller asking *"were there errors in the last 30 minutes?"* against a store
  whose producer died three hours ago receives errors from three hours ago — a
  **stale YES**.
- A caller asking *"is the last 30 minutes clean?"* receives **GREEN** for a
  window that closed three hours ago — a **false GREEN**. Nothing in the result
  distinguishes it from a genuinely clean recent window.

This is not hypothetical. Reading this repository's own telemetry store
reproduced it exactly:

```
Query window: 2026-07-02T16:02:38Z - 2026-07-02T16:32:38Z (30m, default)
```

A "last 30 minutes" that had ended six weeks earlier.

The disclosure added for FR-46/FR-47 makes this **visible**, but visibility is
not correctness: it moves the burden onto every caller to notice the window
drifted and to decide what to do about it. A predicate that must be re-derived
by each consumer is the same class of defect
[ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md)'s resolution chain and
[ADR-012](ADR-012-exit-codes-as-public-contract.md)'s exit codes were introduced
to remove.

## Decision

**The lower bound of a query window is measured from host wall-clock.** The
upper bound remains a generous plausibility ceiling:

```
lo = now - window
hi = now + MAX_FUTURE_SKEW
```

where `now` is captured **once per invocation** and shared by every build path.

Three properties of this are load-bearing:

**Only the floor carries the caller's meaning.** `--since 10m` is a statement
about how far *back* to look. The ceiling is not part of the request; it exists
solely to bound absurd timestamps.

**The ceiling must stay generous — `now` is the wrong upper bound.** A hard
`hi = now` would exclude every record from a producer whose clock runs even
slightly ahead. EC-12 explicitly contemplates such producers (seconds to
minutes of divergence). That would replace a stale-read defect with a
**disappearing-writer** defect, which is strictly worse: a stale read is at
least present in the output and can be questioned, whereas a silently dropped
producer leaves no trace at all.

**Retention must re-anchor with it.** Cache eviction derives its floor from the
event-time watermark. Once the query floor is wall-clock, a future-dated record
makes `retain_floor > lo`, so the hot path would evict rows still inside the
window and disagree with a `--no-cache` scan — breaking FR-11 and INV-4. The
retention anchor therefore becomes `min(watermark, now)`. Taking the **minimum**
is what makes this safe in both directions: a future-dated watermark cannot
ratchet the floor forward past the window, and a *stalled* producer (watermark
behind `now`) keeps its cache rather than having it evicted out from under a
query that can still legitimately ask for it.

## Consequences

**A stalled producer now yields an empty result rather than a stale one.** This
is the point, and it is only acceptable because it is explained: FR-46 discloses
the window that was searched and FR-47 the age of the newest record, so an empty
answer arrives with the evidence needed to interpret it. Shipping this change
*without* that disclosure would trade a wrong answer for an unexplained one.

**`summary` must not report the friendly empty-telemetry message when records
exist outside the window.** Otherwise the freshness block — the very thing that
explains the emptiness — would be suppressed in exactly the case it exists for.
This is recorded as a requirement in `SPEC-otelq-cli`, not left to
implementation.

**The far-future-record failure mode retires for the query window.** A single
implausibly-dated record can no longer push the window past all real data,
because the window no longer depends on record timestamps at all. (This was
catalogued as finding **B-1** in the 2026-07-02 architecture review; that review
is a non-authoritative point-in-time document, and this ADR is its resolution.) The clamp remains meaningful for the cache watermark and for
`doctor`'s skew check, and such records are still excluded from windowed results
by the ceiling.

**INV-7 is amended, not merely reinterpreted.** Its wall-clock statement is
reversed for the query window while its cache-path-equality guarantee is
retained. EC-12 of
[SPEC-otelq-incremental-cache](../spec/SPEC-otelq-incremental-cache.md) is
amended in the same way, as are FR-15, FR-16, FR-18, FR-46 and FR-47 of
[SPEC-otelq-cli](../spec/SPEC-otelq-cli.md).

**[ADR-008](ADR-008-unified-cache-first-read-and-retention.md)'s retention
anchor is amended.** ADR-008 evicts on an age basis measured against the observed
event-time watermark. That basis is retained — retention is still event-time
driven — but its anchor becomes `min(watermark, now)` for the reason given above.
ADR-008 is otherwise untouched and is **not** superseded: cache-first reading and
the retention model itself stand.

**Unbounded queries are unaffected.** `--all` and `trace` carry no window, so
they continue to read all available history regardless of the clock.

**Reproducing a historical window is not offered.** Answering "what would this
have returned at 14:00?" would require an as-of anchor, which is a distinct
capability with its own surface. It is deliberately out of scope here rather
than smuggled in as a side effect of the anchor becoming settable internally.

## Alternatives considered

**Keep event-time anchoring and rely on disclosure.** Rejected: it leaves every
consumer to implement the same staleness check, which is the divergence this
project keeps eliminating. Consumers that forget still get a false GREEN, and a
gate that silently passes is worse than one that reports nothing.

**Hard `hi = now`.** Rejected above — silently drops clock-skewed producers.

**A `--anchor wall|data` flag.** Rejected for now: it preserves the dangerous
default and adds a surface that interacts with cache retention. Wall-clock is
the semantics callers already assume `--since` has; making the safe reading the
default matters more than preserving the other one. Nothing here forecloses
adding it later.
