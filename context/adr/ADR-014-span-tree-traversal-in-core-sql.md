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
  - ADR-003-duckdb-otlp-extension-pin-governance
  - ADR-010-adopt-duckdb-1.5.4-otlp-0.6.0
  - SPEC-otelq-cli
  - PRD-otelq
supersedes: null
superseded_by: null
retrieval_priority: normal
ai_summary: "Span-tree traversal is expressed in core recursive SQL; a graph extension is not required for a parent-child forest and is therefore not introduced, but remains open for genuinely graph-shaped needs."
semantic_tags:
  - otelq
  - duckdb
  - recursive-sql
  - span-tree
  - graph-queries
  - dependencies
---

# ADR-014 — Span-Tree Traversal in Core Recursive SQL; a Graph Extension Is Not Required

## Context

Answering questions about *structure* rather than *content* — did these spans
belong to one connected trace, what led to this span, did a hop lose trace
context — requires traversing the parent/child relationships between spans.
otelq exposes `traces` with `span_id` and `parent_span_id`, but nothing that
turns those two columns into a traversable relation. A caller must know that the
edge runs `parent_span_id → span_id`, that a root is signalled by an **empty
string** rather than `NULL`, that a `parent_span_id` may reference a span that
was never exported, and that span identifiers are only meaningful within their
own trace. That is otelq-internal knowledge of the reader schema, and leaving it
to callers reproduces the coupling the `resource_attr()` macro was introduced to
remove.

Two mechanisms could supply the traversal. DuckDB's core SQL provides
`WITH RECURSIVE`, available in every build with no additional dependency. The
**DuckPGQ** community extension provides SQL/PGQ (SQL:2023) property graphs,
pattern matching, path finding, and graph algorithms such as
`weakly_connected_component`.

The shape of the data constrains the choice. An OpenTelemetry span tree is a
**forest**: every span has at most one parent. It is not a general graph.

Dependency cost is also a live constraint here.
[ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md) pins DuckDB to an
exact version precisely because the `otlp` community extension is built per
DuckDB version and lags releases; that ADR already records a bump validated and
rejected for want of a published build. A second community extension makes the
supported set an **intersection** rather than a single dependency.

## Decision

otelq expresses span-tree traversal in **core DuckDB recursive SQL**, surfaced
as derived relations over `traces`.

**A graph extension is not introduced — because the traversal otelq needs does
not require one, not because graph querying was found wanting.**

Reachability, connected-component membership, and root/orphan identification
over a forest are fully expressible with `WITH RECURSIVE`. It is core, adds no
dependency, needs no additional network fetch on first run, introduces no
stateful graph definition to manage per connection, and is available on every
platform otelq already supports. Taking on a second community extension to
obtain capability that core SQL already provides would add version-coupling cost
under ADR-003 without answering a question core SQL leaves unanswered.

DuckPGQ itself was evaluated and **found to work** on the pinned DuckDB (see
Consequences); it is set aside as unnecessary here, not as unsuitable.

**This decision is scoped to the present need and is explicitly open to
revision.** It records a judgement about *this problem shape*, not a standing
position on graph querying.

## Alternatives Considered

**Adopt DuckPGQ and expose SQL/PGQ.** Not taken *at this time*. It would answer
the same questions the recursive formulation answers, at the cost of a second
community extension governed by ADR-003. The capability it adds beyond core SQL
— arbitrary many-to-many pattern matching, path ranking, centrality — is not
exercised by traversing a forest. See Consequences for what a future adopter
should know; the evaluation was favourable and is recorded rather than discarded.

**Leave traversal entirely to callers.** Rejected. `WITH RECURSIVE` already
works in `otelq sql`, so this is the status quo — and it is precisely the
problem. What callers lack is not SQL expressive power but *OTel-shaped
primitives*: the same reasoning that produced `resource_attr()` rather than
documenting a `json_extract_string` incantation.

**Materialise traversal results into the cache.** Rejected as premature. The
derived relations are views, evaluated only when referenced, and the cost of
recursion over a dev-scale store has not been shown to warrant materialisation.
Revisit if measurement, not intuition, says otherwise.

## Consequences

Callers gain structural queries — connectivity, ancestry, orphan detection —
without knowing the reader schema's conventions, and otelq gains no new
dependency, no new failure mode on first run, and no new version coupling.

Because the traversal is exposed as **relations rather than a verdict**, otelq
answers *"how is this trace shaped?"* and leaves *"is that acceptable?"* to the
caller. Predicates that classify a shape as pass or fail are composed by the
caller in `sql`, and gated through the existing exit-code contract
([ADR-012](ADR-012-exit-codes-as-public-contract.md)). Any future desire to have
otelq itself pronounce a verdict is a separate decision, not an extension of
this one.

### For whoever revisits the graph question

DuckPGQ was evaluated against the pinned DuckDB (1.5.4) and **works**: it
installs and loads, and `CREATE PROPERTY GRAPH`, pattern matching,
variable-length path finding, and `weakly_connected_component` all functioned.
Note that DuckDB's own documentation stated at the time of evaluation that
DuckPGQ was unavailable for 1.5.x and directed users to 1.4.4; that guidance was
stale — builds were published for every platform otelq supports. A future
adopter therefore starts from a **working baseline**, not an unknown.

Two practical notes worth carrying forward:

- **SQL/PGQ requires referential integrity between edge and vertex tables.**
  Real telemetry contains `parent_span_id` values referencing spans that were
  never exported — sampled away, still in flight, or owned by a service that
  does not export. An edge relation must therefore be filtered to resolvable
  parents before a property graph is built. This is a preparation step, not an
  obstacle; the recursive formulation absorbs the same condition in its anchor
  clause, which is one reason it was the lighter fit here.
- **Community-extension availability is an intersection.** At evaluation time
  `otlp` published builds for DuckDB 1.5.2 and 1.5.3 while `duckpgq` did not.
  Any future bump must confirm *both*, per platform, before the pin moves.

Adopting a graph extension would be justified by a need core SQL genuinely
cannot serve — for example a **service dependency graph aggregated across
traces**, which is a true many-to-many graph rather than a forest, or path
ranking and centrality measures over it. Should such a need arise, this ADR
should be **superseded** rather than worked around.
