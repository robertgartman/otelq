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
created: 2026-08-12
last_updated: 2026-08-12
related_documents:
  - ADR-001-host-cli-reads-bind-mounted-files
  - CONTRACT-telemetry-directory
  - SPEC-otelq-cli
  - PRD-otelq
supersedes: null
superseded_by: null
retrieval_priority: high
ai_summary: "otelq's exit code becomes a public compatibility surface: 0 answered, 1 negative verdict, 2 no answer — so an automated consumer can never mistake a failed query for an empty result."
semantic_tags:
  - otelq
  - exit-codes
  - cli-contract
  - compatibility
  - automation
  - fail-friendly
---

# ADR-012 — otelq's Exit Code Is a Public Compatibility Surface

## Context

otelq was designed for a human or an agent reading its output. Its failure
policy is **fail friendly** — absent telemetry produces an explanatory sentence
and exit `0`, never a stack trace
([PRD-otelq](../prd/PRD-otelq.md), `SPEC-otelq-cli` FR-17/FR-18/FR-19). That
policy is correct for a reader: an empty store is a normal state of the world,
not an error.

A second class of consumer has now appeared: an **automated gate** that runs
otelq unattended and branches on the result. For that consumer the same policy
is actively dangerous, because otelq's exit code does not distinguish *"I
queried the store and the answer is nothing"* from *"I never queried the
store."* Both are `0`.

The consequence is not theoretical. A gate written against today's surface must
capture stdout, suppress stderr, and infer the outcome from the shape of the
text — and when otelq crashes, is pointed at a wrong path, or hits a malformed
store, the parse silently degrades to the same value a genuine zero produces.
The gate then reports the *subject under test* as failing, when in fact the
*measuring instrument* failed. A monitoring tool whose breakage is
indistinguishable from the condition it monitors is worse than no tool, because
it converts an infrastructure fault into a false verdict about the system being
observed.

Three further facts constrain the fix.

**The store path is self-concealing.** A query against a `--dir` that does not
exist creates that directory (as a side effect of provisioning the
consumer-owned `.otelq-*` subtrees) and exits `0`. A typo is therefore
indistinguishable from an empty store on the first run, and on the second run
the typo has *materialized* into a real, empty, otelq-owned directory. There is
no invocation at which the mistake is visible.

**Zero rows is not the load-bearing signal.** The obvious remedy — map an empty
result set to a non-zero exit — does not survive contact with how predicates are
actually written. An aggregate such as `SELECT count(*) … WHERE …` returns
exactly one row whether the count is `0` or `500`; the answer is the row's
*value*, not the row's *presence*. A consumer of an aggregate already holds the
value it needs. What it cannot obtain at any price is confidence that the value
was produced by a successful query.

**Exit `0` is only meaningful if stdout is trustworthy.** A stale caller whose
arguments no longer parse must not receive a success code alongside output that
looks like data. Usage diagnostics already behave correctly, but a *bare*
invocation — the shape produced when a caller's argument variable fails to
expand — prints full help to stdout and exits `0`.

## Decision

**otelq's process exit code is a public compatibility surface, versioned at
least as strictly as its flags.** It carries a three-tier taxonomy governed by a
single invariant:

> **Exit `0` promises that stdout is the answer to the question that was
> asked.**

| Exit | Meaning |
|------|---------|
| `0` | otelq produced an answer, and any verdict it carries is affirmative. Includes zero rows, an empty store, and explicitly requested help. |
| `1` | otelq produced an answer, and the verdict is **negative** — the query ran and the assertion is false. |
| `2` | otelq produced **no answer**: usage error, store not found, store unreadable, query error, internal error. |

The `1` tier is not new. `doctor` already exits `1` when a store fails
contract validation, and that is exactly the right signal: `doctor` answers "is
this store healthy?", and "no" is an answer, not a failure to answer. This ADR
names the tier that command was already using, so that future assertion-style
commands inherit an established meaning instead of inventing one. What changes is
that codes previously *sharing* `1` for unrelated reasons — notably a malformed
query, which is a failure to answer — move to `2`.

Three consequences of the invariant are themselves decisions:

1. **Fail-friendly is preserved verbatim for absent data.** An empty or
   partially empty store remains exit `0` with an explanatory message. This ADR
   changes nothing about the case FR-17/FR-18/FR-19 exist to serve. What was
   previously conflated with it — an *unavailable* store — is separated out.

2. **A missing telemetry root is a failure, not an empty store,** and otelq
   never creates it. The root is producer-owned
   ([CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md));
   otelq provisions only its own `.otelq-*` subtrees, and only inside a root
   that already exists. This preserves the cwd-relative default of
   [ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md) — the default path is
   unchanged, only the response to its absence is. `doctor` is exempt: diagnosing
   a store's absence is its purpose, so it reports the absence as a verdict
   (exit `1`) rather than a failure to answer.

3. **Help is an answer only when help was the question.** Explicitly requested
   help is stdout + `0`. Help emitted because the invocation was not
   understood — including a bare invocation — is a diagnostic: stderr + `2`.

Failures additionally carry a **closed, machine-readable reason token** on
stderr. Reasons are additive-only: a new token may be introduced under a minor
version, but an existing token's meaning and its associated exit code are
frozen. Diagnostics stay on stderr; stdout is never polluted with an error
object, because a caller piping stdout to a parser must receive rows or
nothing — never an error wearing the shape of data.

The behavioral detail — token spellings, message formats, which flag renders the
structured form — is specified in
[SPEC-otelq-cli](../spec/SPEC-otelq-cli.md), not here.

## Alternatives Considered

**Leave exit codes as they are; let consumers parse stdout.** Rejected. This is
the status quo that produces false verdicts. No amount of caller-side rigor
recovers information that was never emitted: a crash and a zero are byte-identical
after the caller's `2>/dev/null`.

**Map zero rows to a non-zero exit, behind an opt-in flag.** Rejected as the
*primary* mechanism. As established above, the aggregate predicate — the
dominant form in automated gates — always returns one row, so the flag would be
inert at exactly the call sites that motivated it. It also answers a question
the caller can already answer for itself. The `1` tier stays reserved for
*verdicts* — a command that was asked a yes/no question and answered no, as
`doctor` does — rather than being spent on a proxy for row count.

**Emit a verdict object on stdout in a dedicated mode.** Rejected for this
decision. It requires suppressing the response header on signal-bearing commands
and forks the output contract in a way that every downstream consumer must
learn. The exit code plus a stderr reason conveys the same outcome without
touching the stdout contract at all, which keeps existing consumers — who read
stdout and ignore stderr — working unchanged.

**Adopt BSD `sysexits.h` codes (`EX_USAGE=64`, `EX_NOINPUT=66`, …).** Rejected.
The finer taxonomy buys nothing here, and `2` for usage errors is both the
dominant convention and already argparse's behavior, so the parser and the
application agree without adaptation.

**Introduce a new binary or subcommand for automated consumers.** Rejected as
duplication. The gate needs the *existing* query surface — including arbitrary
`sql` — to be trustworthy; a parallel surface would have to re-expose all of it.

## Consequences

This is a **breaking change** to two behaviors, and it is deliberate:

- A malformed query previously exited `1`; it now exits `2`. Any caller
  treating `1` as "query error" must be updated. This also removes a live
  ambiguity: `doctor` was already using `1` as a verdict, so `1` meant two
  unrelated things at once — "your store is unhealthy" and "your SQL is
  broken". A caller could not tell them apart.
- A nonexistent `--dir` previously exited `0` (and created the directory); it
  now exits `2` and creates nothing. Scripts that relied on otelq to
  materialize a telemetry root must create it themselves, or start the
  Collector that owns it.
- A bare `otelq` previously printed help to stdout and exited `0`; it now
  prints to stderr and exits `2`. Interactive use is unaffected in practice —
  the help still renders — but any script capturing bare-invocation stdout will
  see it move.

The version in which these land must be treated as a compatibility break in the
changelog and in the release's version bump.

Going forward, otelq **acquires a consumer whose failure mode is a broken gate**.
Exit-code semantics and reason tokens may no longer be changed incidentally; they
change only with the same ceremony as a flag rename. In exchange, an automated
consumer can finally attribute a failure correctly — to the system under
observation, or to the observation itself.

Naming the `1` tier rather than inventing one leaves future assertion-style
commands cheap to add: they adopt an established meaning that `doctor` already
demonstrates, rather than forcing a second redesign of the same surface.

`SPEC-otelq-cli` FR-17 and FR-22 are amended by this decision;
`CONTRACT-telemetry-directory` gains an explicit statement of root ownership
that was previously implied by its ownership table.
