---
doc_type: spec
authoritative: true
stability: evolving
status: active
decision_scope: feature
audience:
  - ai
  - engineering
must_not_contain:
  - product_vision
  - architectural_rationale
  - external_data_schemas
created: 2026-08-13
last_updated: 2026-08-13
related_documents:
  - PRD-otelq
  - ADR-013-bounded-blocking-single-invocation
  - ADR-012-exit-codes-as-public-contract
  - SPEC-otelq-cli
  - CONTRACT-telemetry-directory
retrieval_priority: normal
ai_summary: "The await command: block one invocation until a wrapped query yields a satisfying result or a caller-supplied deadline expires, reporting the elapsed wait measured inside otelq."
semantic_tags:
  - otelq
  - await
  - polling
  - liveness
  - deadline
  - automation
---

# SPEC — `await` (Bounded Waiting)

## Purpose

Answer *"has this happened yet?"* within a caller-supplied budget, so an
automated gate can assert that something reported itself in time without
implementing a polling loop of its own.

A caller-side loop cannot honour its own budget: it can count its `sleep`s but
not the per-iteration process spawn, so a nominal 60s budget elapses in roughly
twice that, varying with machine load. It also cannot poll faster than it can
spawn a process, and it cannot report how long the wait truly took. The
architectural permission for otelq to block instead is
[ADR-013](../adr/ADR-013-bounded-blocking-single-invocation.md); the outcome
taxonomy it reports through is
[ADR-012](../adr/ADR-012-exit-codes-as-public-contract.md). Derived from
[PRD-otelq](../prd/PRD-otelq.md).

## Scope

**In scope.** One new command, `await`, which re-runs an existing query command
until it is satisfied or a deadline expires; the satisfaction rule; the timing
guarantee and its disclosed overshoot; and the outcome reporting.

**Out of scope.** Any waiting that persists between invocations (forbidden by
ADR-013); watching the filesystem for change notifications; emitting telemetry;
and the correlation filters themselves, which are
[SPEC-otelq-cli](SPEC-otelq-cli.md) FR-40/FR-42 and merely compose with `await`.

### Definitions

- **Inner command** — the query command `await` wraps and re-runs, written after
  `await`'s own flags exactly as it would be written on its own.
- **Poll** — one complete evaluation of the inner command against the store.
- **Deadline** — the instant `--timeout` elapses, measured from otelq's own
  module load (the earliest point otelq can observe).
- **Satisfied** — the inner command produced a result meeting FR-2.

## Functional Requirements

- **FR-1 — The `await` command.** otelq **must** accept:

  ```
  otelq [global flags] await --timeout <duration> [--poll <duration>] <inner-command> [inner flags/args]
  ```

  `--timeout` **must** be **required**: a call that may block has to state its
  budget, and there is no defensible default for how long a caller is willing to
  wait. `--poll` **must** default to `1s`. Both **must** accept the same
  duration grammar as `--since` (`Ns`/`Nm`/`Nh`/`Nd`); a malformed or
  non-positive value **must** be a `usage_error` (exit `2`).

  A `--poll` greater than `--timeout` **must** be a `usage_error` — it can only
  ever produce a single poll, which is a mistake rather than an intent.

- **FR-2 — Satisfaction.** A poll is **satisfied** when the inner command
  produced **at least one row**, with one addition: when the result is exactly
  **one row and one column**, that single cell **must** additionally be *truthy*
  — `NULL`, `0`, `false`, and the empty string are **not** satisfied.

  The scalar clause exists because the aggregate is the dominant predicate shape
  in automated gates, and `SELECT count(*) … WHERE …` returns exactly one row
  whether the count is `0` or `500`. Under a bare row-count rule such a query
  would be satisfied instantly and always, making `await` silently useless at
  precisely the call sites it exists to serve. A multi-column or multi-row
  result is judged by row presence alone, so the clause only ever engages for
  `sql`.

- **FR-3 — Deadline and overshoot.** `await` **must not** begin a poll at or
  after the deadline, and **must not** sleep past it. When the deadline passes
  unsatisfied it **must** stop and report a timeout (FR-6).

  otelq **must not** interrupt a poll already in flight, so total wall-clock
  **may** exceed `--timeout` by at most the duration of one poll. This overshoot
  **must** be documented in `--help` rather than implied. Any wait whose final
  poll would start after the deadline **must** skip it rather than run it.

- **FR-4 — No mandatory initial delay.** The **first** poll **must** run
  immediately, before any sleep. A predicate already true at invocation
  **must** therefore return in approximately the cost of one query, never after
  a poll interval. `--poll` is the delay *between* polls, not before the first.

- **FR-5 — Every poll re-reads the store.** Each poll **must** observe the store
  as a fresh invocation would, including telemetry written **after** `await`
  started. otelq **must not** reuse a materialised snapshot, connection, or
  cached relation set across polls.

  This is the requirement most easily broken invisibly: a wait that re-queried
  one snapshot would never observe the event it was asked to wait for and would
  report a timeout indistinguishable from a real one — a false verdict about the
  system under observation.

- **FR-6 — Outcome.** `await` **must** report through the ADR-012 taxonomy:

  | Outcome | Exit | Where |
  |---|---|---|
  | Satisfied before the deadline | `0` | Inner command's result on **stdout** |
  | Deadline expired, never satisfied | `1` | stdout **empty**; `otelq: timeout: …` on stderr |
  | Store or query failure | `2` | stdout **empty**; the FR-37 reason on stderr |

  A timeout is a **verdict** — otelq was asked whether something would happen in
  time and answered no — not a failure to answer, which is why it is `1` and not
  `2`.

  A store or query error **must** abort the wait **immediately** and exit `2`.
  It **must not** be retried, **must not** consume the remaining budget, and
  **must not** be reported as a timeout: "your store is broken" and "it has not
  happened yet" are different facts, and a gate that conflates them blames the
  system under test for its own instrumentation failing.

  On timeout, stderr **must** carry `otelq: timeout: <message>`, matching the
  FR-37 reason-line form so one consumer rule reads every non-zero outcome. The
  `timeout` token is reserved for exactly this.

- **FR-7 — The wait is disclosed.** otelq **must** report how long it actually
  waited, measured **inside** the process:

  - On success (`0`), the response header **must** carry a line
    `Waited: <n>ms (<p> polls)` before the inner command's payload.
  - On timeout (`1`), the stderr message **must** state the elapsed wait and the
    poll count.
  - Under a machine `--format`, a single-line JSON object **must** be printed to
    **stderr** in both cases, carrying at least `otelq_version`, `ok`,
    `satisfied_after_ms` (null on timeout), `waited_ms`, `polls`, and
    `timeout_ms`.

  Elapsed time **must** be measured from otelq's module load, and its meaning
  **must** be documented as a **lower bound**: interpreter and dependency
  startup preceding that point cannot be observed from inside the process.

- **FR-8 — Permitted inner commands.** `await` **must** accept exactly the query
  commands that read the store and return rows: `summary`, `errors`, `slow`,
  `trace`, `logs`, `metric`, and `sql`. Any other (`doctor`, `history`,
  `triage`, `collector-config`, `troubleshoot`, `set_resource_attributes`,
  `help`, a nested `await`) **must** be a `usage_error` (exit `2`) naming the
  permitted set.

  `sql` is included deliberately: it is the only way to express a predicate the
  built-in verbs cannot, and FR-2's scalar clause exists to make its dominant
  shape behave correctly.

- **FR-9 — Awaiting log records.** `await` **must** support waiting on **log
  records**, filtered by resource attribute and by log attribute
  ([SPEC-otelq-cli](SPEC-otelq-cli.md) FR-40, FR-42).

  This is not a convenience. An OpenTelemetry SDK exports a span on span *end*,
  so a span cannot signal that work **started**, and a step that hangs forever
  emits no span at all. A start marker is therefore a log record, and an `await`
  restricted to spans could not observe the very condition — a step that never
  finishes — that bounded waiting exists to detect.

- **FR-10 — Composition.** Global flags **must** apply to every poll exactly as
  they would to a single invocation: `--dir`, `--since`/`--all`,
  `--resource-attr`, `--attr`, `--regex`, `--format`, `--session-id`. Inner
  command flags (`--service`, `--level`, `--grep`, `--top`, …) **must** be
  accepted after the inner command name, per the FR-11 ordering rule.

  `--since` **must** be re-evaluated per poll when it is a trailing window, so a
  long wait does not drift into querying an ever-staler window.

- **FR-11 — Interruption.** `SIGINT`/`SIGTERM` during a wait **must** exit `2`
  with reason `interrupted` and a short stderr message — never a Python
  traceback, and never a silent `0` that a caller would read as satisfied.

- **FR-12 — History.** An `await` **must** record **one** query-history entry
  for the whole invocation, not one per poll: a wait is a single analytical act,
  and per-poll records would swamp the ranking evidence
  ([ADR-009](../adr/ADR-009-query-history-triage-store.md)).

- **FR-13 — A timeout must say whether anything is arriving.** When a wait exits
  `1` (FR-3), otelq **must** additionally report, per signal with captured data,
  the newest record's age at the moment the budget expired — the same freshness
  values `summary` reports
  ([SPEC-otelq-cli](SPEC-otelq-cli.md) FR-47).
  - Without it a timeout is ambiguous between the two failures a caller must
    tell apart: **the step under test never ran**, and **the producer ran but
    the collector has not flushed**. The first is a defect in the system under
    test; the second is a defect in the measurement. Acting on the wrong one
    wastes the entire debugging cycle.
  - The `Query window:` line ([SPEC-otelq-cli](SPEC-otelq-cli.md) FR-46)
    **must not** be relied on to carry this: a window derived from wall-clock is
    identical whether or not anything is arriving, so it cannot discriminate.
  - The report **must** be a number, never a verdict (FR-47): otelq states the
    lag and the caller decides what it means.
  - A store with **no captured records at all** **must** be reported as such
    rather than the line being omitted, so "no telemetry reaching the collector"
    is distinguishable from "telemetry, but stale". Within a non-empty store,
    individual signals with no data are omitted exactly as
    [SPEC-otelq-cli](SPEC-otelq-cli.md) FR-47 requires.
  - This **must not** alter the exit code, which stays `1` (INV-3), nor delay
    the exit beyond the budget (FR-3).

## Edge Cases & Failure Modes

- **EC-1 — Already satisfied.** The predicate holds at invocation: returns after
  one poll, well inside a poll interval (FR-4).
- **EC-2 — Satisfied on the final permitted poll.** The last poll starting
  before the deadline succeeds: exit `0`, not a timeout.
- **EC-3 — Data arrives after the deadline.** Exit `1`. Later arrival does not
  retroactively satisfy.
- **EC-4 — Store disappears mid-wait.** The telemetry root is removed while
  waiting: exit `2` (`store_not_found`) immediately, not at the deadline.
- **EC-5 — Malformed inner query.** Fails on the **first** poll with exit `2`
  (`query_error`); it is not retried, since re-running invalid SQL cannot
  become valid.
- **EC-6 — Empty store that stays empty.** The friendly no-telemetry condition
  is *not* satisfaction: the wait continues and ends in a timeout (exit `1`),
  because "nothing captured" is precisely what the caller is waiting to stop
  being true.
- **EC-7 — `--timeout 0`.** Rejected as `usage_error`; a zero budget is a
  single-shot query, which the caller should express directly.
- **EC-8 — Aggregate predicate returning zero.** `sql "SELECT count(*) …"`
  yielding `0` is **not** satisfied (FR-2), and the wait continues.
- **EC-9 — Very long wait with a trailing `--since`.** The window is recomputed
  per poll (FR-10), so the wait cannot silently query a stale range.

## Acceptance Criteria

- **AC-1** (Verifies FR-1, FR-8, EC-7, INV-5): Given `await` without `--timeout`, with a
  malformed or zero duration, with `--poll` exceeding `--timeout`, or wrapping a
  disallowed inner command, when each runs, then each exits `2` with
  `otelq: usage_error:` and the disallowed-command message names the permitted
  set.
  *Verification hint: `test_ac1_await_argument_validation`, parametrised.*
- **AC-2** (Verifies FR-4, FR-7, EC-1): Given a store already containing a
  matching log record, when `await --timeout 30s --poll 5s logs --grep …` runs,
  then it exits `0` in **well under** one poll interval, and the header carries
  a `Waited:` line reporting one poll.
  *Verification hint: `test_ac2_already_satisfied_returns_immediately`; assert
  measured wall-clock is far below `--poll`.*
- **AC-3** (Verifies FR-5, FR-2, EC-2, INV-2, INV-4): Given an empty store and a writer that
  appends a matching record **after** the wait has begun, when `await` runs with
  a short poll, then it exits `0` and returns the new record — proving each poll
  re-read the store rather than re-querying a snapshot.
  *Verification hint: `test_ac3_await_sees_telemetry_written_mid_wait`; write
  from a background thread after the first poll.*
- **AC-4** (Verifies FR-3, FR-6, FR-7, EC-3): Given a predicate that never
  becomes true, when `await --timeout <t>` runs, then it exits `1`, stdout is
  empty, stderr carries `otelq: timeout:` with the elapsed wait and poll count,
  and total wall-clock does not exceed `<t>` by more than one poll's duration.
  *Verification hint: `test_ac4_timeout_is_verdict_exit_1_within_budget`.*
- **AC-5** (Verifies FR-2, EC-8): Given `sql "SELECT count(*) FROM logs WHERE
  <false>"`, when awaited, then it is **not** satisfied and the wait times out
  (exit `1`); and given the same query once the count becomes non-zero, then it
  is satisfied (exit `0`). A single scalar cell is judged by truth, not by the
  existence of the row carrying it.
  *Verification hint: `test_ac5_scalar_aggregate_is_judged_by_value`.*
- **AC-6** (Verifies FR-6, EC-4, EC-5, INV-3): Given a nonexistent `--dir`, and
  separately a malformed inner `sql`, when awaited with a long `--timeout`, then
  each exits `2` with its FR-37 reason **immediately** — measured wall-clock far
  below the timeout — and neither is reported as a timeout.
  *Verification hint: `test_ac6_store_and_query_errors_abort_immediately`;
  assert elapsed « timeout.*
- **AC-7** (Verifies FR-9, FR-10): Given log records distinguished only by a log
  attribute and a resource attribute, when `await` wraps `logs` with `--attr`
  and `--resource-attr`, then it is satisfied only by the record matching both —
  a step marker is reachable without matching body text.
  *Verification hint: `test_ac7_await_logs_by_log_and_resource_attribute`.*
- **AC-8** (Verifies FR-7): Given a machine `--format`, when a wait succeeds and
  when one times out, then in both cases a single-line JSON object appears on
  **stderr** carrying `otelq_version`, `ok`, `satisfied_after_ms`, `waited_ms`,
  `polls`, and `timeout_ms`, with `ok` true/false respectively and
  `satisfied_after_ms` null on timeout; and stdout carries only the inner
  payload (success) or nothing (timeout).
  *Verification hint: `test_ac8_await_emits_machine_timing_object`.*
- **AC-9** (Verifies FR-12, INV-1): Given a wait spanning several polls, when it
  completes, then the query-history store gains exactly **one** record for the
  invocation.
  *Verification hint: `test_ac9_await_records_one_history_entry`.*
- **AC-10** (Verifies FR-6, EC-6): Given a completely empty store, when `await`
  wraps a command whose signal has no data, then the friendly no-telemetry
  condition does **not** satisfy the wait; it times out with exit `1`.
  *Verification hint: `test_ac10_empty_store_is_not_satisfaction`.*
- **AC-11** (Verifies FR-11): Given a wait interrupted by `SIGINT`, when it is
  signalled, then it exits `2` with reason `interrupted` and no traceback.
  *Verification hint: `test_ac11_interrupted_wait_is_exit_2`; send the signal to
  a subprocess.*
- **AC-12** (Verifies FR-3, EC-9): Given `--poll` larger than the
  remaining budget, when the wait proceeds, then otelq does **not** sleep past
  the deadline and does **not** start a poll after it; and given a trailing
  `--since`, the queried window advances across polls rather than being frozen
  at the first.
  *Verification hint: `test_ac12_no_poll_or_sleep_past_deadline`.*

- **AC-13** (Verifies FR-13, FR-3, INV-3): Given a store receiving records that
  never satisfy the predicate, when the wait times out, then it exits `1` and
  stderr reports the newest-record age per signal with captured data; and given
  a store that is empty throughout, the same timeout reports that no records
  were captured — so the two causes of an unsatisfied wait are distinguishable
  from the timeout output alone, without a second invocation.
  *Verification hint: `test_ac13_timeout_reports_per_signal_freshness`.*

## Invariants

- **INV-1** — Nothing survives the invocation: a wait holds no state, no
  registration, and no open handle beyond its own process (ADR-013).
- **INV-2** — A wait never satisfies on data it did not observe: satisfaction
  requires an actual poll returning a satisfying result, never an inference from
  elapsed time.
- **INV-3** — A timeout and an error are never conflated: exit `1` means
  *observed, not yet true*; exit `2` means *could not observe*.
- **INV-4** — `await` changes only *when* an inner command's result is produced,
  never *what* it is: the rows and their rendering are identical to running the
  inner command alone at that instant.
- **INV-5** — Waiting is always opt-in and always capped: no existing command
  acquires blocking behavior, and no invocation can wait without a caller
  supplied deadline.
