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
created: 2026-07-07
last_updated: 2026-07-08
related_documents:
  - PRD-otelq
  - ADR-011-worktree-telemetry-identity
  - SPEC-otelq-cli
  - CONTRACT-telemetry-directory
ai_summary: "Opt-in worktree scoping for otelq: the otelq.worktree.* convention it consumes, a tags-present master switch, the set_resource_attributes command, worktree-grouped summary, default scoping of errors/slow/logs/metric with an --all-worktrees opt-out, the guarantee that sql and trace are never scope-rewritten, and the reserved $WORKTREE_ID sql parameter otelq binds from the env-file identity."
semantic_tags:
  - otelq
  - worktree
  - resource-attributes
  - scoping
  - telemetry
  - cli
  - opentelemetry
---

# SPEC — otelq Worktree Scoping

## Purpose

Define the exact, testable behavior by which `otelq` distinguishes and scopes
telemetry to the git **worktree** that produced it, in service of the product
intent in [PRD-otelq](../prd/PRD-otelq.md) (letting a developer or agent see the
telemetry *their* work emitted, even when several worktrees share one Collector).

Behavior here consumes the worktree-identity convention decided in
[ADR-011](../adr/ADR-011-worktree-telemetry-identity.md) and layers on top of the
base CLI behavior in [SPEC-otelq-cli](SPEC-otelq-cli.md).

The feature is **opt-in and driven by a single master switch**: unless the
telemetry under `--dir` actually carries worktree tags — or the user explicitly
passes a scope flag — otelq's output is **byte-identical** to its pre-feature
behavior.

## Scope

**In scope:**

- The `otelq.worktree.*` resource-attribute convention as **consumed** by otelq,
  and the "worktree telemetry present" master switch.
- Resolution of the current worktree identity.
- The `set_resource_attributes` command that populates the identity into the
  repo-root `.env.local`.
- Worktree grouping in the `summary` census.
- Default scoping of otelq's list-shaped aggregate commands, and the flag that
  opts out of it.
- The guarantees that `otelq sql` and `otelq trace` are never scope-rewritten.

**Out of scope:**

- How instrumented applications *emit* the attribute (the SDK/seam and the owner
  own that; otelq only writes the identity into `.env.local`).
- Physical isolation, Collector configuration, and per-worktree Collectors.
- The on-disk telemetry layout, which is unchanged
  ([CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md)).

## Definitions

- **Worktree tag** — the resource attribute `otelq.worktree.id` (the canonical
  discriminator) and its companion `otelq.worktree.branch` (descriptive only).
- **Tagged row** — a telemetry row whose `otelq.worktree.id` resource attribute
  is present and non-empty. An **untagged row** is any other row (attribute
  absent, null, or empty string).
- **Worktree telemetry present (master switch)** — at least one tagged row exists
  in the telemetry under `--dir` within the active query window.
- **Current worktree identity** — the single `otelq.worktree.id` value that
  identifies the invoking worktree, resolved per FR-2 (may be *undefined*).
- **`$WORKTREE_ID` (reserved `sql` parameter)** — a DuckDB named parameter that
  `otelq sql` reserves; when a submitted query references it, otelq binds it to the
  env-file worktree identity via DuckDB's native parameter binding (FR-13), never
  by rewriting the query text.

## Functional Requirements

**FR-1 — Master switch (opt-in).** Worktree-specific behavior — census
grouping (FR-4), default scoping (FR-5), and the scope banner (FR-6) — engages
**only** when worktree telemetry is present. When no worktree tag is present under
`--dir`, every command's output (rows, columns, headers, and the summary census
shape) is **byte-identical** to its behavior before this feature existed, and
`--all-worktrees` (FR-7) is a no-op.

**FR-2 — Identity resolution.** otelq resolves the current worktree identity by,
in order: (a) the `otelq.worktree.id` entry parsed from `OTEL_RESOURCE_ATTRIBUTES`
in the repo-root `.env.local` (then `.env`) of the current working directory, if
present and non-empty; else (b) the output of `git rev-parse --show-toplevel` run
from the current working directory; else (c) **undefined**. Resolution is
independent of the `--dir` value.

**FR-3 — `set_resource_attributes` command.** otelq exposes a
`set_resource_attributes` command that derives `otelq.worktree.id`
(`git rev-parse --show-toplevel`) and `otelq.worktree.branch`
(`git symbolic-ref --short HEAD`, falling back to `git rev-parse --abbrev-ref HEAD`
for a detached HEAD) from the current checkout and writes them into
`OTEL_RESOURCE_ATTRIBUTES` in the current working directory's `.env.local`,
creating the file if absent. The write is an **idempotent merge**: any existing
`otelq.worktree.id` / `otelq.worktree.branch` entries are updated in place, every
other attribute already in `OTEL_RESOURCE_ATTRIBUTES` (bespoke owner keys) is
preserved, and every other variable/line in the file is left untouched. After
writing, the command **prints** the resolved `otelq.worktree.id` and
`otelq.worktree.branch` values to stdout, together with a ready-to-paste `sql`
scope predicate (the mine-or-untagged clause of FR-9) that references the reserved
`$WORKTREE_ID` parameter (FR-13) rather than embedding the literal id, so the value
the identity is based on is visible and the self-scoping snippet never has to be
hand-built and stays valid across worktrees. The command needs no `--dir` and no telemetry; run outside a
git checkout it writes nothing and reports a friendly message, exiting 0.

**FR-4 — Worktree-grouped census in `summary`.** When worktree telemetry is
present, the `summary` services census groups its rows by
`(service_name, otelq.worktree.id, otelq.worktree.branch)` — keeping `service` as
the leading column and inserting an `otelq.worktree.id` and `otelq.worktree.branch`
column between it and the trailing count — ordered by descending count with a
deterministic tiebreak, and rows whose worktree tag is absent render under a
visible `(untagged)` marker. When no worktree telemetry is present, the census
keeps its pre-feature `(service, count)` shape exactly. The census is **always
global** — it is never reduced by scoping, regardless of the FR-7 flags.

**FR-5 — Default scoping of list-shaped aggregates.** The commands `errors`,
`slow`, `logs`, and `metric` scope their rows to the current worktree identity
**by default when worktree telemetry is present and the identity is defined**: a
row is included when its `otelq.worktree.id` equals the resolved identity **or**
its worktree tag is absent (mine-or-untagged). Scoping is not applied when the
master switch is off (FR-1) or the identity is undefined (FR-8).

**FR-6 — Scope banner.** Whenever scoping is active for a command (FR-5), that
command's response header carries a banner line naming the active worktree
identity, stating that untagged rows are included, and
reporting how many rows from other worktrees are hidden in the active window and
across how many distinct other worktrees. When the user passes `--all-worktrees`
on a command that would otherwise scope, the header instead states that all
worktrees are shown.

**FR-7 — Scope opt-out flag.** A single **global** flag (placed before the
subcommand, per the existing global-flag ordering) controls scoping:
`--all-worktrees` disables scoping entirely, so every worktree's rows are shown.
It affects output only when the master switch is on (FR-1); on untagged telemetry
it is a no-op. otelq deliberately provides **no** flag that scopes to an
arbitrary identity — the model is exactly two states, "mine" (the default) and
"all" (`--all-worktrees`). Inspecting one *specific* other worktree is done
through `sql` with an explicit `otelq.worktree.id` predicate (FR-9).

**FR-8 — Behavior when identity is undefined.** When worktree telemetry is
present but the identity is undefined (FR-2 case c) and no scope flag is given,
the list-shaped aggregate commands do **not** scope — they behave as though
`--all-worktrees` were given — and state that no worktree identity was resolved,
rather than erroring or returning nothing.

**FR-9 — `sql` is never rewritten.** `otelq sql "<query>"` executes the submitted
query verbatim — otelq injects no worktree predicate into it and never modifies its
text, regardless of the master switch or scope flags. (The one value otelq may
supply is the reserved `$WORKTREE_ID` named parameter, and only when the query
itself references it — native value binding, not text rewriting; see FR-13.) otelq
**must** instead make self-scoping discoverable — the resolved identity and a
ready-to-paste `resource_attributes`
predicate are surfaced through the `set_resource_attributes` command's output
(FR-3) and documented in `--help` (the `sql views` section) — and **must not**
apply scoping to `sql` implicitly. The offered predicate is the **mine-or-untagged**
clause (the row's `otelq.worktree.id` is absent **or** equals the identity),
identical in shape to the predicate the built-in commands apply (FR-5, INV-5), so
it is consistent wherever otelq emits it.

**FR-10 — `trace` is a targeted lookup, not scoped.** `otelq trace <id>` resolves
and renders the full span tree for the given trace id (or unique prefix)
regardless of worktree tags. A trace id is globally unique, so scoping it would
only break pivots from an `--all-worktrees` listing without adding isolation;
`trace` therefore ignores worktree scoping and shows no scope banner.

**FR-11 — Attribute namespace and scope key.** The discriminator keys are exactly
`otelq.worktree.id` and `otelq.worktree.branch`. Scoping, the master switch, and
the scope-key column of the census key on `otelq.worktree.id`;
`otelq.worktree.branch` is descriptive only and never used as a scope key. An
`otelq.worktree.id` whose value is the empty string is treated as untagged.

**FR-12 — Fail-friendly consumption.** Grouping, scoping, identity resolution, and
`set_resource_attributes` never raise on missing, empty, or malformed worktree
attributes or env files; an absent/empty tag is treated as untagged and included
per the mine-or-untagged rule of FR-5, consistent with the read-only,
friendly-failure behavior of [SPEC-otelq-cli](SPEC-otelq-cli.md).

**FR-13 — Reserved `$WORKTREE_ID` `sql` parameter.** `otelq sql` reserves the
DuckDB named parameter `$WORKTREE_ID`. When the submitted query references it — as
determined by DuckDB's own parser, so an occurrence inside a string literal or a
comment does **not** count — otelq resolves the current worktree identity from the
cwd env files **only** (the `otelq.worktree.id` parsed from
`OTEL_RESOURCE_ATTRIBUTES` in `.env.local`, then `.env`) and supplies it through
DuckDB's native parameter binding; the query **text is never modified** (value
binding, not the predicate injection FR-9 forbids). Resolution here deliberately
does **not** use FR-2's `git rev-parse` fallback: the bound value must equal what an
instrumented app in this checkout actually emits, which is exactly the env-file
value. When the query references `$WORKTREE_ID` but no non-empty `otelq.worktree.id`
can be read from the env files, otelq **fails with a friendly, non-zero error**
telling the user to set `OTEL_RESOURCE_ATTRIBUTES` (e.g. run
`set_resource_attributes`) before using `$WORKTREE_ID`, and runs nothing. When the
query does not reference `$WORKTREE_ID`, execution is unchanged and no identity
resolution is attempted, so a query unrelated to worktrees can never fail on
identity (preserving FR-1 / INV-2). Only `$WORKTREE_ID` is otelq-reserved; any other
named parameter in the query is the user's own and is left to DuckDB, which errors
if it is unbound.

## Edge Cases & Failure Modes

- **EC-1 — Detached HEAD.** In a detached-HEAD worktree, `otelq.worktree.branch`
  may be non-descriptive (e.g. `HEAD`); `otelq.worktree.id` (the checkout path)
  stays unique and remains the scope key. `set_resource_attributes` still records
  whatever branch value git reports.
- **EC-2 — `--dir` points at another worktree's shared store.** Identity is still
  resolved from cwd/git, so scoping reflects the invoking worktree, not the store
  from which data is read.
- **EC-3 — Bespoke attributes present.** `set_resource_attributes` preserves any
  non-`otelq.worktree.*` attributes; scoping ignores them.
- **EC-4 — Two untagged producers.** Both surface as `(untagged)` and, under the
  mine-or-untagged rule, are mutually visible from every worktree — a documented
  limit of opt-in tagging.
- **EC-5 — Malformed `.env.local`.** If `OTEL_RESOURCE_ATTRIBUTES` cannot be
  parsed for identity, otelq falls back to git resolution (FR-2b) and does not
  crash; `set_resource_attributes` reports a friendly message rather than
  corrupting the file.
- **EC-6 — Tags present but no identity.** Covered by FR-8: unscoped results plus
  a "no worktree identity resolved" note; the census still reveals every worktree.
- **EC-7 — Empty-string tag value.** A row whose `otelq.worktree.id` is `""` is
  treated identically to an absent tag (untagged) for the master switch, census,
  and scoping (FR-11).
- **EC-8 — `$WORKTREE_ID` only inside a string/comment.** A query whose sole
  occurrence of `$WORKTREE_ID` sits inside a string literal or SQL comment does not
  reference the parameter (DuckDB's parser reports no named parameter), so otelq
  attempts no resolution and runs the query verbatim (FR-13).
- **EC-9 — `$WORKTREE_ID` alongside another named parameter.** otelq binds only
  `$WORKTREE_ID`; any other `$name` the query references is left unbound and DuckDB
  raises its normal "values were not provided" error, surfaced as a friendly
  `otelq: SQL error` (FR-13).

## Acceptance Criteria

- **AC-1** (Verifies FR-1): Given telemetry with **no** worktree tags and no scope
  flag, when each of `summary`, `errors`, `slow`, `logs`, `metric`, `trace` runs,
  then output (rows, columns, header lines, and census shape) is byte-identical to
  a run of the same fixture with the feature disabled.
  *Verification hint: golden-compare against current outputs on an untagged
  fixture; assert the census block has exactly `service,count`.*
- **AC-2** (Verifies FR-2): Given a cwd `.env.local` whose
  `OTEL_RESOURCE_ATTRIBUTES` contains `otelq.worktree.id=/path/A`, when otelq
  resolves identity, then it is `/path/A`; and given no such entry but a git
  checkout at `/path/B`, the resolved identity is `/path/B`; and given neither,
  identity is undefined. Changing `--dir` changes none of these.
  *Verification hint: unit-test the resolver with a temp `.env.local` and a temp
  git repo; assert independence from `--dir`.*
- **AC-3** (Verifies FR-3): Given a checkout whose top-level is `/path/A` on
  branch `feat`, when `set_resource_attributes` runs, then `.env.local` contains
  `otelq.worktree.id=/path/A` and `otelq.worktree.branch=feat`; and when it runs a
  second time after a bespoke `team=blue` was added to
  `OTEL_RESOURCE_ATTRIBUTES`, then the worktree keys are updated in place and
  `team=blue` is still present. In both runs stdout echoes the resolved
  `otelq.worktree.id`/`otelq.worktree.branch` values and a mine-or-untagged `sql`
  predicate that references the `$WORKTREE_ID` parameter (FR-13) rather than
  embedding the literal `/path/A`.
  *Verification hint: run the command twice against a temp repo; diff `.env.local`
  and assert the printed id/branch and that the predicate contains `$WORKTREE_ID`.*
- **AC-4** (Verifies FR-3, EC-6): Given a directory that is not a git checkout,
  when `set_resource_attributes` runs, then no file is written, a friendly message
  is printed, and the exit code is 0.
  *Verification hint: invoke in a non-git temp dir; assert no `.env.local` and
  exit status 0.*
- **AC-5** (Verifies FR-4): Given captured telemetry from two worktree ids plus
  some untagged rows, when `summary` runs, then the census shows a row per
  `(worktree.id, worktree.branch, service_name)` combination, untagged rows appear
  under `(untagged)`, and the block is identical whether or not `--all-worktrees`
  is passed.
  *Verification hint: `just otelq summary` and
  `just otelq --all-worktrees summary` against a seeded store; compare census.*
- **AC-6** (Verifies FR-5): Given telemetry tagged for worktree `A`, worktree `B`,
  and untagged, when `errors`/`slow`/`logs`/`metric` run with resolved identity
  `A`, then only `A` and untagged rows are returned.
  *Verification hint: seed a store with all three cohorts; assert row membership
  per command.*
- **AC-7** (Verifies FR-6): Given the same store scoped to `A`, when a
  list-shaped aggregate runs, then its response header includes a banner naming
  `A`, stating untagged rows are included, and reporting the count of hidden
  other-worktree rows and the number of distinct other worktrees in the window.
  *Verification hint: assert the banner substring and the numeric counts against a
  known seeded distribution.*
- **AC-8** (Verifies FR-7): Given the same store, when a command runs with
  `--all-worktrees`, then rows from `A`, `B`, and untagged are all returned and the
  header states all worktrees are shown.
  *Verification hint: assert membership and the "all worktrees" banner under
  `--all-worktrees`.*
- **AC-9** (Verifies FR-8, EC-6): Given tagged telemetry but an undefined
  identity (non-git cwd, no `.env.local`), when a list-shaped aggregate runs, then
  results are unscoped and the output states that no worktree identity was
  resolved.
  *Verification hint: invoke from a non-git temp cwd against a tagged store.*
- **AC-10** (Verifies FR-9): Given any resolved identity and tagged telemetry,
  when `sql "SELECT count(*) FROM logs"` runs, then the returned count equals the
  unscoped count of all logs (no predicate injected), and the mine-or-untagged
  predicate is surfaced only through `set_resource_attributes` output and `--help`,
  never applied to `sql` implicitly.
  *Verification hint: compare the `sql` count to a direct DuckDB count over the
  same store; confirm no implicit filtering; assert the predicate appears in
  `set_resource_attributes` output.*
- **AC-11** (Verifies FR-10): Given a trace whose spans are tagged for worktree
  `B`, when `trace <id>` runs from worktree `A`, then the full span tree for `B`
  is returned and no scope banner is shown.
  *Verification hint: seed a B-tagged trace; run `trace` from identity A; assert
  the tree is complete and header has no banner.*
- **AC-12** (Verifies FR-11): Given a row carrying `otelq.worktree.branch` but no
  `otelq.worktree.id`, when scoping and the master switch are evaluated, then the
  row is treated as untagged — branch is never used to include, exclude, or
  trigger the switch.
  *Verification hint: seed a row with only the branch attribute; assert it is
  treated as untagged and does not by itself flip the census to worktree shape.*
- **AC-13** (Verifies FR-12, EC-5, EC-7): Given telemetry whose worktree tag value
  is empty/malformed and a `.env.local` that cannot be parsed for identity, when
  `summary` and a list-shaped aggregate run, then neither raises: the malformed
  rows render as `(untagged)` and identity falls back to git resolution.
  *Verification hint: seed empty/malformed tags; corrupt `.env.local`; assert no
  traceback and correct fallback.*
- **AC-14** (Verifies EC-2): Given otelq invoked from worktree `B` with `--dir`
  set to worktree `A`'s shared `.telemetry/`, when a list-shaped aggregate runs,
  then scoping uses identity `B`.
  *Verification hint: run from B's cwd pointing `--dir` at A's store; assert the
  banner names B.*
- **AC-15** (Verifies FR-13): Given a cwd `.env.local` whose
  `OTEL_RESOURCE_ATTRIBUTES` sets `otelq.worktree.id=/path/A` and a store holding
  `/path/A`, `/path/B`, and untagged rows, when `sql` runs a `SELECT count(*) FROM
  logs` whose `WHERE` is the mine-or-untagged clause referencing `$WORKTREE_ID`,
  then the count equals the number of `/path/A`-or-untagged log rows and the query
  text otelq hands DuckDB is byte-identical to the submitted query (the value is
  bound, not substituted into the text).
  *Verification hint: seed the three cohorts; compare against a hand-bound count;
  assert no text substitution occurred.*
- **AC-16** (Verifies FR-13): Given a query that references `$WORKTREE_ID` but a cwd
  with **no** `otelq.worktree.id` in any env file — even inside a git checkout whose
  top-level would resolve under FR-2 — when `sql` runs, then otelq exits non-zero
  with a message naming `OTEL_RESOURCE_ATTRIBUTES` / `set_resource_attributes`,
  executes nothing, and never falls back to the git path.
  *Verification hint: run in a git temp repo with no `.env.local`; assert non-zero
  exit, the guidance string, and that no rows were returned.*
- **AC-17** (Verifies FR-13, FR-1, INV-2): Given any cwd (including one with no
  `.env.local`), when a `sql` query that does **not** reference `$WORKTREE_ID` runs
  — including one where `$WORKTREE_ID` appears only inside a string literal — then
  execution and output are byte-identical to pre-feature behavior and no identity
  resolution is attempted.
  *Verification hint: run `sql "SELECT count(*) FROM logs"` and
  `sql "SELECT '$WORKTREE_ID'"` from a non-configured cwd; assert success and that
  neither reads env files.*

### Examples

- **Seed a tagged app run:** `just otelq set_resource_attributes` writes
  `OTEL_RESOURCE_ATTRIBUTES="otelq.worktree.id=/repo/wtA,otelq.worktree.branch=feat-x"`
  into `.env.local` and prints the resolved id/branch plus a ready-to-paste
  mine-or-untagged `sql` predicate that references `$WORKTREE_ID`; the launcher
  sources the file,
  the app is exercised, then `just otelq summary` shows a census row grouped under
  `/repo/wtA` / `feat-x`.
- **Default vs. global view:** `just otelq errors` from `/repo/wtA` returns only
  `wtA` + untagged errors with a scope banner naming `/repo/wtA`;
  `just otelq --all-worktrees errors` returns every worktree's errors.
- **Scoped `sql` stays opt-in:** `just otelq sql "SELECT count(*) FROM logs"`
  counts all logs; the worktree predicate is offered for the user to add, not
  injected. To scope to the **current** worktree without hand-copying its id, add
  the mine-or-untagged clause and reference the reserved parameter —
  `... WHERE <id-expr> IS NULL OR <id-expr> = $WORKTREE_ID` — and otelq binds
  `$WORKTREE_ID` from `.env.local` (FR-13). To inspect one *specific* other worktree
  (the job the removed `--worktree` flag used to do), use an explicit literal
  instead, e.g. `... WHERE json_extract_string(resource_attributes, '$."otelq.worktree.id"') = '/repo/wtB'`.

## Invariants

- **INV-1** — `--dir` and resolved identity are independent; changing `--dir`
  never changes the resolved worktree identity.
- **INV-2** — When no worktree tag is present and no scope flag is given, every
  command's output is byte-identical to its pre-feature behavior (master switch).
- **INV-3** — The `summary` census is always global; scope flags never reduce it.
- **INV-4** — otelq never injects a worktree predicate into `otelq sql` or
  `otelq trace`, and never rewrites the submitted query text; the only value it
  supplies to `sql` is the reserved `$WORKTREE_ID` parameter, and only when the
  submitted query itself references it (FR-13). `otelq trace` returns the complete
  tree.
- **INV-5** — Every scoping predicate includes the "or tag absent" branch, so
  untagged rows are never hidden by scoping.
- **INV-6** — `set_resource_attributes` only ever adds or updates
  `otelq.worktree.*` entries; it never removes or discards bespoke attributes or
  other variables, and never writes outside the cwd `.env.local`.
- **INV-7** — otelq introduces no writes under `--dir` beyond the consumer-owned
  subtrees; `.env.local` lives at the repository root, outside the telemetry root,
  so [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md)
  is unaffected.
