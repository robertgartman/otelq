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
created: 2026-07-04
last_updated: 2026-07-04
related_documents:
  - ADR-001-host-cli-reads-bind-mounted-files
  - ADR-008-unified-cache-first-read-and-retention
  - CONTRACT-telemetry-directory
  - SPEC-otelq-cli
supersedes: null
superseded_by: null
ai_summary: "otelq records every telemetry query it runs in a consumer-owned .otelq-history/ store (journal + Parquet, janitor-compacted per invocation) so an LLM agent can mine past investigations for the most likely next query."
semantic_tags:
  - otelq
  - query-history
  - triage
  - self-improving
  - parquet
  - janitor
  - observability
---

# ADR-009 — Query-History Triage Store

## Context

Every development environment is bespoke, and troubleshooting focus shifts over
time: one week the churn is a Docker deployment being stabilised, the next it is
one misbehaving code path. Each episode is a *burst* of otelq invocations, and
across episodes certain queries keep turning out to be the ones that crack the
problem. Today that experience evaporates the moment an invocation exits —
otelq retains no record of which queries were run, in what order, or which one
ended an investigation.

The consumer of that record is primarily an **LLM agent** driving otelq (see the
otelq skill): given a fresh symptom, an agent that can see "in this repo, these
are the queries that historically terminated investigations" starts several
steps ahead of one that must rediscover the corpus from scratch. The record is
also inherently sequential — within a burst, *query A was followed by query B*,
and the last query of a burst (followed by a long gap) is a reasonable proxy for
"the one that answered." First-order transition statistics over that sequence
(a Markov chain over query templates, computable with SQL window functions at
read time) can turn the log into next-query suggestions with zero new
dependencies.

An earlier design explored this session — otelq emitting OTLP/JSON *about
itself* into a `.otelq-self/` subtree — solved a narrower problem (performance
telemetry) and dragged in an unwanted tension: hand-rolled OTLP generation
versus the heavyweight official SDK (~120 ms import per invocation, five new
dependencies, against the CLI-only latency rule of
[ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md) and the single-file
minimal-dependency distribution of ADR-002). The question is what architecture
serves the actual end goal — faster, history-informed triage — without OTLP
generation, without new dependencies, and without violating the telemetry-
directory contract.

## Decision

otelq maintains a **query-history store**: a consumer-owned record of every
telemetry-interrogating invocation, kept under the telemetry root and mined at
read time as a triage assistant.

1. **A dedicated consumer-owned subtree.** The store lives in `.otelq-history/`
   under the telemetry root, a sibling of `.otelq-cache/` with the same
   ownership rule: otelq writes it, the Collector must never touch it
   ([CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md)
   v1.1). The consumer read-only guarantee over the Collector's `*.jsonl` files
   is untouched.

2. **Not OTLP — otelq-native operational data.** The history is a record of
   *otelq's usage*, not telemetry about an instrumented system. It is stored in
   the forms otelq already speaks natively (JSONL journal + Parquet tables via
   the pinned DuckDB), which dissolves the OTLP-generation question entirely: no
   OTLP encoder — bespoke or official — is needed, no new dependency is added,
   and no import latency lands on the hot path.

3. **Append-journal + janitor-compaction, mirroring the cache.** Parquet files
   are immutable, so the write path is a single atomic `O_APPEND` line into a
   journal; a **janitor** at the end of each recorded invocation — only if it
   wins an `O_EXCL` single-writer lock, the same pattern the cache uses —
   compacts the journal into two normalised Parquet tables (distinct query
   templates; individual invocations) and applies retention. Merging is
   idempotent (invocation identity dedupes re-consumed journal lines), so a
   lost lock or a crash mid-compaction never corrupts or double-counts; the
   next invocation simply finishes the work. No daemon, no warm process —
   ADR-001 is preserved by ordering, exactly as in
   [ADR-008](ADR-008-unified-cache-first-read-and-retention.md).

4. **Queries are stored as normalised templates plus a raw example.** Raw
   invocations rarely repeat verbatim (trace ids, timestamps); templates do.
   Template identity is what makes frequency, recency, and transition statistics
   meaningful; the raw example keeps suggestions concrete.

5. **Two-horizon retention under configurable business rules.** Recent history
   (a ~24 h eligibility floor) is kept *complete*, preserving the intra-burst
   sequences that transition mining needs — including failed queries, which are
   the transitions. Beyond that floor the janitor curates: zero-row queries,
   context-flooding queries, and long-unused queries are removed (in that
   badness order, oldest first), but never below a minimum library size. Every
   threshold is configurable (environment variables) so tests can trigger the
   janitor without waiting out wall-clock rules. The janitor logs its actions
   to an audit file in the subtree.

6. **Recording is best-effort and scoped to real queries.** Only
   telemetry-interrogating commands are recorded — meta commands (doctor,
   troubleshoot, collector-config, help, and the history read surface itself)
   are not, which also bounds the feedback loop. A failure to record or compact
   must never change a command's result, exit code, or noticeably its latency;
   an environment opt-out disables recording entirely.

7. **The read surface is otelq itself.** The store is exposed as SQL views in
   the existing `sql` escape hatch plus one convenience command returning the
   ranked triage list (frequency × recency × terminal-success). All sequence
   analytics (sessionisation, terminal-query detection, first-order
   transitions) are computed at read time in SQL — nothing is precomputed or
   learned online, so the "assistant" logic can evolve freely without touching
   the stored data.

## Alternatives Considered

- **OTLP self-telemetry into `.otelq-self/` (the scrapped predecessor).**
  Implemented and reverted this session. It answered "how is otelq performing"
  but not "what should I query next", which is the actual goal; it forced the
  bespoke-OTLP-encoder vs. official-SDK dilemma (the SDK costs ~120 ms import
  per invocation plus five dependencies — unacceptable under ADR-001/ADR-002,
  while a hand-rolled encoder is bespoke surface the user explicitly wanted to
  avoid); and reading it required a second telemetry corpus with its own cache.
  The history store records the same duration/row-count facts as queryable
  columns, so the performance question stays answerable — without any OTLP.

- **Emit to the Collector like a normal app (fully official pipeline).**
  Rejected: adds a network hop and a hard dependency on a *running* Collector
  for otelq to observe itself — unavailable exactly when troubleshooting the
  Collector, and against the single-shot latency rule.

- **Inject history rows into `.otelq-cache/`.** Rejected: the cache is a
  derived, *evictable* accelerator (ADR-008 retention horizon; `otel-clean`
  wipes it) with hard invariants (immutable minute partitions, sealed-from-raw
  only, cursor semantics). History must be durable and curated — the opposite
  lifecycle. Overloading one subtree with both would couple unrelated concerns.

- **A persistent DuckDB database file as the store.** Rejected: a `.duckdb`
  file is single-writer with hard file locking (concurrent invocations would
  contend or corrupt), opaque to inspection, and version-coupled to the exact
  DuckDB pin. JSONL + Parquet are the formats the project already trusts,
  greppable and lock-free on the append path.

- **Record nothing (status quo).** Rejected by the premise: repeated
  investigations in a bespoke environment keep re-paying discovery cost that a
  small, curated, locally-owned record eliminates.

## Consequences

- **CONTRACT-telemetry-directory bumps to v1.1** (additive): `.otelq-history/`
  joins `.otelq-cache/` as a consumer-owned subtree the producer must not
  touch. No existing filename, framing, or mapping changes.

- **A new command and new SQL views** enter the CLI surface, and the otelq
  skill gains a "consult history first" step. The store only becomes valuable
  through that read surface; the skill text is part of the deliverable.

- **Bounded, configurable growth.** The store's size is governed by the
  retention rules, not by traffic; the janitor runs opportunistically per
  invocation and its cost is proportional to the (small, capped) table size.
  Rule thresholds and their precedence are behaviour, pinned by tests and the
  implementation's documented constants rather than by this ADR.

- **Local-only capture of query text.** Full `sql` texts and filter patterns
  are stored (that is the product); the store lives beside the telemetry it
  queried, inherits the same transient/not-version-controlled status as the
  rest of `.telemetry/`, and is reset by the same lifecycle (`otel-clean`).

- **The sequence-mining hypothesis stays cheap to test.** Because analytics are
  read-time SQL over complete recent history, the terminal-query heuristic and
  Markov-style suggestions can be tuned or discarded without migration; only
  the retention rules would need revisiting if longer full-fidelity sequences
  prove necessary.

- **Reversibility.** Dropping the feature is deleting the recording hook, the
  janitor, the read surface, and the subtree; nothing else depends on it, and
  the contract change was additive.

## Amendment (2026-07-04) — Sessions as Ground Truth, Scoring, and Triage

Accepted the same day, extending — not revising — the original decision. Three
additions:

1. **Explicit session ids are the ONLY sessionisation mechanism — time-gap
   inference is rejected outright.** The CLI's `--session-id` (SPEC-otelq-cli
   FR-33) is stored on history rows, but **only when explicitly supplied** (a
   generated default id is an offer in the session footer, not an asserted
   correlation). A session *is* the rows sharing one id: it bridges any pause
   and never merges with a neighbour. Timestamps must not infer membership,
   because the store is shared — **concurrent agent sessions interleave in
   wall-clock time**, so any gap heuristic chains unrelated investigations
   together and corrupts both the terminal-success proxy and the transition
   statistics. The accepted cost: rows recorded without an id carry
   frequency/recency evidence only, never sequence evidence — correlation is
   opt-in, and the skill instructs agents to always opt in.

2. **A defined ranking model.** "Top" now means *recent frequency × smoothed
   success*: each invocation carries an exponential half-life recency weight,
   and a template's success rate is Laplace-smoothed so a one-off fluke cannot
   outrank an established resolver, then the two are multiplied. The precise
   formula, defaults, and configurability are behaviour and live in
   [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md) (FR-34); the *decision* recorded
   here is that ranking must decay with time and must smooth small samples —
   raw lifetime counts and raw win rates are both rejected as ranking keys.

3. **The read surface graduates from reporting to acting: `triage`.** The
   recommended first instruction of an investigation becomes `otelq triage`
   (the skill is updated accordingly). Triage decides from the store whether
   the caller is mid-chain (anchored **only** by a supplied session id — see
   point 1) or starting fresh, takes a first-order Markov step over past session
   transitions, and **auto-runs** the winning candidate only under three
   simultaneous conditions — sufficient decayed evidence, majority share, and
   a *concrete* template (no normalisation placeholders; re-running someone
   else's stale trace id would query noise). It then prints the suggested
   follow-up invocation (session id included) as its last output line. When
   nothing clears the bar, triage falls back in order: if the session has not
   yet been grounded it runs `summary` — the RCA guide's step 1 — itself
   rather than telling the caller to; only a session that already grounded
   gets the honest refusal plus the ranked list, never a guess. Auto-run
   (grounding included) executes under the anchor's session id
   and is recorded so the chain advances; the triage wrapper itself is never
   recorded, keeping the feedback loop bounded exactly as the original
   decision required. Thresholds are configurable; behaviour is pinned in
   SPEC-otelq-cli FR-35/FR-36 and AC-62..AC-68.

The store's location, ownership (CONTRACT v1.1), journal-plus-janitor
architecture, retention rules, and best-effort guarantees are unchanged by
this amendment.
