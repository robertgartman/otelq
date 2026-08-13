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
  - ADR-012-exit-codes-as-public-contract
  - PRD-otelq
  - SPEC-otelq-await
supersedes: null
superseded_by: null
retrieval_priority: high
ai_summary: "A single otelq invocation may block on a caller-supplied deadline while polling the store; this is bounded waiting inside one process, not the daemon or warm process the CLI-only rule forbids."
semantic_tags:
  - otelq
  - cli-only
  - blocking
  - polling
  - await
  - liveness
---

# ADR-013 — A Single Invocation May Block on a Caller-Supplied Deadline

## Context

otelq is **CLI-only**: no server, no daemon, no long-running process, no warm
state between calls ([ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md),
[PRD-otelq](../prd/PRD-otelq.md)). The stated rationale is that low latency on a
single invocation is the whole point — anything that adds a hop or assumes a
warm process is out.

Every otelq command to date has been single-shot: read, answer, exit. A new
class of question does not fit that shape — *"has this happened yet?"* — where
the honest answer at time *t* may be "not yet" and at *t+6s* "yes".

The concrete driver is a **liveness gate**: an automated test asserting that
each step of a scenario reports its start within a fixed budget, measured at
otelq. Today a caller implements that by looping otelq from bash. That loop is
structurally unable to honour its own budget, for reasons that are properties of
the *outside-the-process* design rather than of any particular script:

1. **The budget is unmeasurable from outside.** A caller can count its own
   `sleep`s but not the per-iteration process spawn — measured at 2–5s under
   `uvx`. A nominal 60s budget elapses in ~100–120s, and the overshoot varies
   with machine load, so the number reported on failure is not the number that
   passed.
2. **The first poll is a guaranteed miss.** A Collector batching at 5s and
   flushing at 1s makes nothing readable for several seconds after emission.
   Every wait burns at least one full interval on a poll that cannot succeed.
3. **Poll granularity is floored by spawn cost.** Sub-second polling is
   impossible from outside without making the overshoot worse. Inside one
   process it is nearly free.

There is also a diagnostic asymmetry: only otelq can report *how long the wait
actually took*, because only otelq can observe both ends of it. That number is
what makes a budget auditable rather than asserted.

## Decision

**A single otelq invocation may block, bounded by a deadline the caller
supplies, while it polls the telemetry store.**

This is explicitly **not** a relaxation of the CLI-only rule. The rule forbids
three things, and blocking is none of them:

| Forbidden | Why blocking is not that |
|---|---|
| A server or daemon | Nothing listens; nothing is reachable; no port is bound |
| A long-running background process | The process is in the foreground, owned by the caller, and dies when the invocation ends |
| Warm state between invocations | Nothing survives the process. The next invocation starts as cold as the first |

The property the CLI-only rule actually protects is that **otelq holds no state
and offers no surface between invocations**. A bounded wait preserves that
completely. What it changes is only the *duration* of one foreground call, and
only when the caller explicitly asks for it and explicitly caps it.

Three constraints make the permission narrow rather than open-ended:

1. **Blocking is opt-in and capped.** It occurs only under a command that exists
   to wait, and only with a caller-supplied deadline. No existing command
   acquires waiting behavior, and there is no unbounded form.

2. **A wait is a query loop, not a subscription.** Each poll is an independent
   read of the store — the same read a fresh invocation would perform, against
   the same file seam of ADR-001. otelq acquires no watch, no inotify
   registration, no open handle held across polls, and no privileged
   relationship with the Collector.

3. **The elapsed time is reported.** A blocking call must disclose how long it
   actually blocked, measured inside the process. A budget nobody can audit is
   the defect this decision exists to remove, so re-introducing it would defeat
   the purpose.

The outcome of a wait uses the taxonomy already established by
[ADR-012](ADR-012-exit-codes-as-public-contract.md), and needs no new one: a
deadline that expires without the predicate holding is exit `1` — otelq was
asked a yes/no question and answered **no**. It is a verdict, not a failure to
answer. A store or query error remains exit `2`, and must surface immediately
rather than consuming the remaining budget, because "your store is broken" and
"it hasn't happened yet" are different facts a gate must not conflate.

## Alternatives Considered

**Leave polling to the caller.** Rejected — this is the status quo whose defects
are enumerated above. They are not caller mistakes to be fixed by writing a
better loop; they follow from paying process-spawn cost per poll, and no script
can avoid that from outside.

**A watch/daemon mode that streams as telemetry arrives.** Rejected outright.
This is precisely what CLI-only forbids: a long-running process with a surface
and state that outlives an invocation. It would also require otelq to hold
handles on the Collector's files, coupling it to producer behavior that
[ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md) deliberately keeps at
arm's length. The gain over bounded polling — latency measured in milliseconds
rather than a poll interval — does not come close to justifying it, since the
Collector's own batch/flush interval already dominates.

**Filesystem watch (inotify/FSEvents) within one bounded invocation.** Rejected
as unjustified complexity. It is platform-specific, degrades over the bind
mounts and network filesystems otelq must tolerate, and buys at most one poll
interval against a Collector that batches on the order of seconds anyway.
Re-reading is simpler and its failure modes are already understood.

**Return immediately with a "not yet" answer and let the caller retry.**
Rejected: this *is* the caller-side loop, merely with a friendlier exit code,
and it inherits every defect above.

## Consequences

otelq gains its first command whose latency is intentionally unbounded-by-design
(bounded only by the caller's deadline). The performance intuition that "an
otelq call is fast" no longer holds universally, so a waiting command must be
obviously named and obviously opt-in; a caller must never wait by accident.

Each poll re-reads the store, so a wait's cost scales with poll frequency and
store size. Implementations must not cache a snapshot across polls: a wait that
re-queried the same materialised state would never observe the event it was
asked to wait for, and would report a timeout that is indistinguishable from a
real one — a false verdict of exactly the kind ADR-012 exists to prevent.

Any timing guarantee is bounded by the duration of the final in-flight query:
otelq can promise not to *begin* a poll past the deadline, not to be interrupted
mid-query. That overshoot must be documented rather than implied, and stated in
terms a caller can reason about.

Because the elapsed measurement starts when otelq's own module is loaded,
interpreter and dependency startup preceding that point are outside what otelq
can observe. The reported figure is therefore a lower bound on the true
wall-clock cost, and must be described as such rather than presented as exact.

Future waiting behavior — for other predicates or other signals — inherits this
decision rather than re-litigating it. Anything that would hold state *between*
invocations does not, and needs its own ADR superseding this one.
