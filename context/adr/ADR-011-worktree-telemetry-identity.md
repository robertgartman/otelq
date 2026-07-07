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
created: 2026-07-07
last_updated: 2026-07-07
related_documents:
  - ADR-001-host-cli-reads-bind-mounted-files
  - ADR-004-collector-in-docker-bind-mount
  - CONTRACT-telemetry-directory
  - SPEC-otelq-worktree-scoping
  - SPEC-otelq-cli
supersedes: null
superseded_by: null
ai_summary: "Worktree telemetry is distinguished by a git-derived OTel resource attribute injected through the existing repo-root env-file seam — opt-in, owner-controlled, and leaving the telemetry-directory CONTRACT untouched."
semantic_tags:
  - otelq
  - worktree
  - resource-attributes
  - opentelemetry
  - telemetry-identity
  - concurrency
  - observability
---

# ADR-011 — Worktree Telemetry Identity via Git-Derived Resource Attributes

## Context

Agentic development is converging on **git worktrees**: several checkouts of the
same repository live side by side on one host, each exercised concurrently by a
human or an agent.

otelq's capture model does not distinguish them. A single dev Collector binds
fixed OTLP ports (`:4317`/`:4318`) and appends every signal to one
host-bind-mounted `.telemetry/` directory
([ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md),
[ADR-004](ADR-004-collector-in-docker-bind-mount.md),
[CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md)).
Only one Collector can run per host, so every worktree's instrumented app targets
the same endpoint and all of their telemetry aggregates into the **same shared
store**. Signals emitted from concurrent worktrees become indistinguishable —
today the otelq skill can only warn operators to point `--dir` at the canonical
checkout and accept the mixing.

The project's non-negotiables constrain any fix: CLI-only (no server, port, or
daemon), the Collector file seam is the sole integration contract, failures must
be friendly, and no external solution name may leak into otelq. A fix must live
inside those bounds.

OpenTelemetry already offers a spec-blessed mechanism for producer-side
identity: every span, log, and metric carries a **Resource**, and every official
SDK merges the `OTEL_RESOURCE_ATTRIBUTES` / `OTEL_SERVICE_NAME` environment
variables onto that Resource at startup with **zero application code change**.
The reader extension already surfaces these on a `resource_attributes` column.
What OTel does **not** provide is any detector that knows about a VCS checkout or
a git worktree — that identity has to be supplied.

## Decision

Adopt a namespaced **OTel resource attribute** as the worktree discriminator,
supplied through the **env-file seam that already exists at the repository root**,
strictly **opt-in**.

1. **Identity keys.** A custom namespace avoids collision with semantic
   conventions: `otelq.worktree.id` (canonical discriminator, derived from
   `git rev-parse --show-toplevel` — the checkout path is guaranteed unique per
   worktree) and `otelq.worktree.branch` (a human-friendly secondary label,
   derived from `git rev-parse --abbrev-ref HEAD`).

2. **Injection through the existing seam.** The repository root already ships a
   committed `.env.example` whose sole purpose is producer-side OTel
   configuration (the Collector endpoint). Worktree identity rides the same
   convention: a gitignored, per-checkout `.env.local` carrying
   `OTEL_RESOURCE_ATTRIBUTES`. Whatever launches the instrumented app sources
   that file, and the SDK merges the values onto the Resource. otelq **never**
   sets this automatically; a dedicated command populates or updates it on
   request, and the repository owner may place **bespoke** attributes alongside
   the worktree keys.

3. **Identity is decoupled from `--dir`.** Because identity is derived from the
   current git checkout, "where otelq reads data" (the shared store, via `--dir`)
   and "which worktree otelq is" (resolved from cwd/git) are separate
   resolutions. otelq-in-worktree-B reading worktree-A's shared store still knows
   it is B.

4. **Consumption is logical scoping only, and engages only when telemetry is
   tagged.** otelq uses the attribute to group its census and to filter the
   queries **it itself constructs**. That behavior activates only when the
   telemetry actually carries `otelq.worktree.id` tags (or the user explicitly
   asks for it); on untagged telemetry otelq is byte-identical to its pre-feature
   behavior. It does **not** rewrite user-supplied `sql`. The discriminator
   travels inside the resource attributes already present in every OTLP-JSON line,
   so the Collector file seam and its CONTRACT are unchanged.

This yields **logical, not physical, isolation**: one shared Collector and one
shared store, with rows distinguished — and queries scoped — by attribute.

## Alternatives Considered

- **One Collector per worktree** (own ports and own `.telemetry/`). Delivers true
  physical isolation, but reintroduces a port-allocation problem, multiplies
  containers, and forces each app to target a per-worktree endpoint — friction
  against the single-Collector model and the zero-config default. Rejected as the
  default; it remains the escape hatch for the rare case that a hard
  resource boundary (a genuine noisy-neighbor problem) is required, since one
  shared Collector still shares `memory_limiter`/`batch`.
- **Collector routing/transform to per-worktree files.** The file exporter's path
  is static; an output path cannot be templated by attribute value, so this fans
  telemetry into a fixed set of pipelines and changes the on-disk file naming —
  a **breaking** change to
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md).
  Rejected: heavier and it touches the seam for no advantage over attribute
  tagging.
- **Identity file inside `.telemetry/.env.local`.** The bind-mounted `.telemetry/`
  is shared — only the canonical checkout's directory is mounted — so a file
  placed there is overwritten by every worktree, recreating the very collision
  being solved, and writing non-cache/-history files under the telemetry root
  violates the consumer-ownership rules of
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md).
  Rejected in favor of the per-checkout **repo-root** `.env.local`.
- **Auto-rewriting `otelq sql` to inject a worktree predicate.** Would require
  parsing arbitrary DuckDB (CTEs, joins, unions, subqueries) to decide where a
  predicate belongs, could silently change the rows a user asked for, and
  contradicts `sql` being the raw, full-access escape hatch. Rejected as
  astonishing, fragile, and unnecessary — otelq owns the SQL behind its built-in
  commands and can scope those cleanly instead.

## Consequences

- **CONTRACT-telemetry-directory is untouched.** The discriminator lives in
  resource attributes that already flow through the seam; no new file, name,
  extension, or framing is introduced, and otelq writes nothing new under the
  telemetry root. The identity file sits at the repository root, outside `--dir`.
- **Opt-in and owner-controlled.** Nothing changes unless the owner populates
  `.env.local` (via the dedicated command or by hand); bespoke attributes are
  preserved. Apps or infrastructure that do not honor `OTEL_RESOURCE_ATTRIBUTES`
  simply emit **untagged** telemetry.
- **Untagged rows remain visible to every worktree.** Because scoping must
  tolerate an absent attribute — otherwise untagged infrastructure would vanish —
  isolation is only ever as strong as producer tagging discipline: two untagged
  worktrees still bleed into each other. This is an accepted limit of an opt-in,
  logical scheme.
- **otelq gains a small scope surface** — a populate command, a census grouping,
  and a filter over its built-in queries that is **default-on only when the
  telemetry is tagged**, with a global opt-out — whose exact behavior is specified
  in [SPEC-otelq-worktree-scoping](../spec/SPEC-otelq-worktree-scoping.md). The
  `sql` escape hatch stays literal; scoping there is opt-in and visible, never
  implicit.
- **Resource-level, not datapoint-level.** Worktree attributes partition the
  Resource, so the `batch` processor will not merge batches from distinct
  resources and metric label cardinality is unaffected.
- **A new dependency at the identity boundary.** Resolving identity relies on
  git; outside a git checkout there is simply no worktree identity and therefore
  no scoping — consistent with fail-friendly behavior. This introduces a git
  touch-point on the consumer side that otelq did not previously have, bounded to
  identity resolution.
