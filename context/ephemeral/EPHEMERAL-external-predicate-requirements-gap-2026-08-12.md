---
doc_type: ephemeral
authoritative: false
stability: evolving
status: draft
decision_scope: feature
audience:
  - ai
  - engineering
must_not_contain:
  - product_requirements
created: 2026-08-12
last_updated: 2026-08-12
related_documents:
  - PRD-otelq
  - SPEC-otelq-cli
  - SPEC-otelq-worktree-scoping
  - CONTRACT-telemetry-directory
  - ADR-001-host-cli-reads-bind-mounted-files
  - ADR-012-exit-codes-as-public-contract
retrieval_priority: low
ai_summary: Gap analysis of an external consumer's predicate/verdict requirements (R1-R7) against otelq's context, code, and tests. R1 resolved via ADR-012. Not authoritative.
---

# Gap Analysis — External Predicate/Verdict Requirements (2026-08-12)

An external consumer project asked otelq for seven generic capabilities (R1–R7)
so its test harness can stop hand-rolling telemetry assertions in bash. The
request document lives outside this repository and is **not** authoritative
here. This document records what was verified against otelq's own canonical
context, `otelq.py`, and `tests/test_otelq.py`.

Nothing consumer-specific may enter otelq. Every item below is stated as a
generic capability.

**Baseline at time of review:** 169 tests pass; Ruff and Pyright strict clean.

---

## Summary

| Req | Capability | Impl | Tests |
|-----|-----------|------|-------|
| R1 | Three-way outcome exit codes + machine reason on failure | **resolved** (ADR-012) | **resolved** |
| R2 | Generic exact resource-attribute filter | 4 partial, 1 missing | 4 partial, 1 missing |
| R3 | Chain-connectivity predicate | 5 missing | 5 missing |
| R4 | `await` — block until predicate holds or deadline | 2 partial, 4 missing | 2 partial, 4 missing |
| R5 | Store discovery owned by otelq | 2 partial, 2 missing | 1 partial, 3 missing |
| R6 | Effective query-window disclosure | 2 missing | 2 missing |
| R7 | Store ingestion-lag disclosure | 2 missing | 2 missing |

**Totals:** 29 acceptance criteria — 0 complete, 11 partial, 18 missing
*(at time of review; R1 has since been resolved — see below)*.

---

## Runtime verification (2026-08-12)

Executed through the justfile gateway against a directory that did not exist:

```
just otelq --dir <nonexistent> --format json logs
```

Observed **before** ADR-012:

- **exit code `0`**
- stdout empty
- stderr: the friendly "no telemetry captured" message plus the session footer
- **side effect:** the missing directory was created, containing `.otelq-cache/`
  and `.otelq-history/`

This confirmed R1's central complaint empirically: a bad store path was
indistinguishable from an empty-but-healthy store. It also surfaced a separate
question not raised by the external document — whether a query should
*materialize* a telemetry root that did not exist.

Observed **after** ADR-012 (FR-38):

- **exit code `2`**
- stdout empty
- stderr: `otelq: store_not_found: telemetry directory '…' does not exist …`,
  plus the machine-readable failure object under a machine `--format`
- **no side effect:** the directory is not created.

---

## R1 — Predicate exit codes and verdict object — **RESOLVED**

> **Outcome (2026-08-12).** Implemented under
> [ADR-012](../adr/ADR-012-exit-codes-as-public-contract.md) and
> [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md) FR-17 (amended), FR-22 (amended),
> FR-37, FR-38, FR-39, INV-5 (amended), INV-7 — **not** as specified. The
> requester's own evidence disproved the requested surface, so R1 was
> reframed from a *predicate* contract to an *outcome* contract:
>
> - **AC1.1 rejected by design.** `--exit-code-on-empty` maps *zero rows* to
>   exit `1`, but the call site R1 cites as its motivating evidence
>   (`run.sh`'s `SELECT count(*) FROM logs WHERE …`) always returns exactly one
>   row. The flag would exit `0` whether the count were `0` or `500` — inert at
>   the very place it was requested. The caller already holds the value; what it
>   lacked was confidence the query ran. Exit `1` was instead given its true
>   meaning — a *negative verdict* — which `doctor` was already emitting.
> - **AC1.2 met.** Missing `--dir` ⇒ exit `2`, `store_not_found`, and the
>   directory is no longer created (FR-38). This closed a defect the external
>   document did not know about: a typo'd path *materialised itself*, so it was
>   invisible from the second run onward.
> - **AC1.3 met**, and stronger than asked: malformed SQL moved `1` → `2`,
>   removing a live collision where `1` meant both "your store is unhealthy"
>   (doctor) and "your SQL is broken".
> - **AC1.4 met in adapted form.** The failure object goes to **stderr**, not
>   stdout. Putting it on stdout would hand a caller piping into a JSON parser
>   an error wearing the shape of data; stdout purity (INV-7) is the stronger
>   guarantee and required no change to the existing payload contract.
> - **AC1.5 met.** ADR-012 makes exit codes and the reason enum a versioned
>   compatibility surface.
>
> One hole the external document did not identify was also closed: a **bare**
> `otelq` printed 9.6 KB of help to stdout and exited `0` — the shape produced
> when a caller's argument variable fails to expand (FR-39).



Requested: `0` satisfied, `1` queried successfully but predicate unsatisfied,
`2` query did not complete; plus a machine-consumable verdict object on stdout
carrying `otelq_version`, `ok`, a closed `reason` enum, store, and window.

**Conflict with canonical context.** `FR-17` in
[SPEC-otelq-cli.md](../spec/SPEC-otelq-cli.md) deliberately specifies exit `0`
for zero-row results and for the friendly no-telemetry path, and `FR-18`/`FR-19`
make absent telemetry a friendly success. R1 is therefore a **compatibility
change to an existing, intentional contract**, not an additive feature. It
cannot be implemented as a silent behavior change to existing commands without
either an opt-in surface or a documented breaking change.

Current state:

- No predicate mode, no verdict object, no `reason` enum.
- Missing/absent store takes the friendly path and exits `0`.
- Malformed SQL does exit non-zero, but is not normalized to `2` and carries no
  structured reason.
- stdout/stderr hygiene is already good: payload on stdout, diagnostics and the
  session footer on stderr.
- Exit codes are documented but not versioned as a compatibility surface.

Open decisions: opt-in flag versus new subcommand; whether `sql` participates;
whether the verdict object replaces or wraps the existing payload; whether the
response header is suppressed in predicate mode.

---

## R2 — Generic exact resource-attribute filter

Requested: a repeatable global flag filtering any signal by exact equality on an
OTel **Resource** attribute, with conjunctive semantics and no substring
matching.

Current state: exact-equality extraction over the JSON `resource_attributes`
column exists, but only hard-wired to the worktree key
(`otelq.worktree.id`, per [SPEC-otelq-worktree-scoping.md](../spec/SPEC-otelq-worktree-scoping.md)).
There is no generic `key=value` flag, no arbitrary dotted-key support, and no
cross-signal generic test. The union helpers, parameter binding, and scoping
predicate builders are directly reusable.

Note: the global-flag list in `FR-11` is closed, so adding a global flag is a
SPEC change.

---

## R3 — Chain-connectivity predicate

Requested: given a set of `service:span` members, report per-member presence and
whether one trace contains all of them, distinguishing **missing** (never
observed) from **disconnected** (observed, but split across trace ids), and
treating an empty member set as an error rather than a vacuous pass.

Current state: entirely absent. The span tree primitives behind `trace` are
reusable, but there is no command, no member-set input, and no verdict shape.
`SPEC-otelq-cli.md` currently fixes the command surface at seven query verbs.

---

## R4 — `await`

Requested: poll internally until a predicate holds or a deadline expires,
reporting elapsed time measured inside otelq, and supporting **log records**
(not only spans, since a span is only exported on end and a hung step emits
nothing).

Current state: absent. Execution is single-shot dispatch. Timing exists only for
history bookkeeping.

Architecture note: bounded blocking inside one invocation does **not** violate
the CLI-only constraint in ADR-001 and the PRD, which prohibit a daemon, server,
or warm process. This should still be stated explicitly wherever `await` is
specified.

---

## R5 — Store discovery

Requested: resolution order of explicit flag, environment variable, then
walk-up from the working directory (resolving to the main checkout when run from
a linked worktree), with the resolved path and the mechanism reported in output.

Current state: `--dir` with a cwd-relative default only. No environment
variable, no walk-up, no worktree-aware resolution, and no provenance in output.

This is the deepest conflict in the set: the cwd-relative default is fixed in
[ADR-001](../adr/ADR-001-host-cli-reads-bind-mounted-files.md), restated in
[CONTRACT-telemetry-directory.md](../contract/CONTRACT-telemetry-directory.md),
and specified in `FR-12`. Changing it requires an ADR first, then the contract,
then the SPEC.

---

## R6 — Effective window disclosure

Requested: every response states the window it actually queried, including when
the window came from a default rather than an explicit flag.

Current state: the window is computed internally and disclosed only as a human
`--verbose` line on stderr. The header's `Time range` describes the returned
rows, not the queried window. Machine formats carry rows only.

Partially addressed already: the response header now names the telemetry
directory it read, which closes the "which store answered this?" half of the
same class of silent misreading.

---

## R7 — Ingestion-lag disclosure

Requested: report the newest record timestamp per signal so a caller can
distinguish "nothing happened" from "the collector has not flushed yet."

Current state: not exposed as an output contract, though watermark and
newest-event-time computations already exist for cache planning and for
`doctor`'s clock-skew check, and are reusable.

---

## Recommended order

1. ~~**R1**~~ — **done** (ADR-012). The trustworthy outcome signal now exists.
2. **R2** — removes storage-layout coupling and unblocks R3.
3. **R4** — makes a wall-clock budget measurable.
4. **R3** — retires the largest amount of bespoke caller logic.
5. **R5** — requires an ADR; closes a real divergence where two callers can read
   different stores without noticing.
6. **R6 / R7** — cheap disclosure, each closing a class of silent misreading.

---

## Cross-cutting notes

- Every requirement above needs governing context updated **before**
  implementation. R1, R3, R4 need a new SPEC; R5 needs an ADR plus a CONTRACT
  revision; R2 and R6 extend `SPEC-otelq-cli.md`.
- Exit-code semantics and any `reason` enum become a public compatibility
  surface the moment a test gate depends on them.
- Tests are in-process (`main` is called directly) and pin returned values
  rather than real process status. Any exit-code contract needs at least one
  subprocess-level test to be meaningful.
