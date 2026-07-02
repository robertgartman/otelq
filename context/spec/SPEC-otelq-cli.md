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
created: 2026-06-23
last_updated: 2026-07-02
related_documents:
  - PRD-otelq
  - SPEC-otelq-incremental-cache
  - CONTRACT-telemetry-directory
  - ADR-006-read-otlp-extension-quirks
ai_summary: "otelq CLI base behavior: the query relations/columns it exposes, its seven subcommands, global flags and argument order, and its friendly read-only failure handling."
semantic_tags:
  - otelq
  - cli
  - telemetry
  - duckdb
  - traces
  - logs
  - metrics
  - observability
---

# SPEC — otelq CLI (Base Behavior)

## Purpose

Define the exact, testable behavior of the `otelq` command-line tool
(`otelq.py`): the query relations and columns it exposes, the
seven subcommands and their output, the global flags and their argument-order
rule, and its robust, read-only handling of absent or malformed telemetry. This
is the base CLI contract on which the incremental cache builds.

This document specifies the CLI's externally observable behavior. The on-disk
telemetry directory and OTLP JSONL layout that otelq reads are defined in
[CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md) and
**must not** be redefined here. The cache that accelerates repeated and recent
queries is specified in
[SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md); the quirks of
the `duckdb-otlp` reader extension that otelq compensates for are recorded in
[ADR-006-read-otlp-extension-quirks](../adr/ADR-006-read-otlp-extension-quirks.md).
Product intent lives in [PRD-otelq](../prd/PRD-otelq.md).

## Scope

**Covered:** the named query relations/views and their column sets as exposed to
`sql` and the built-in commands; the behavior, inputs, and output columns of the
seven subcommands (`summary`, `errors`, `slow`, `trace`, `logs`, `metric`,
`sql`); the global flags (`--format`, `--dir`, `--all`, `--no-cache`, `--since`,
`--verbose`, `--version`) and the rule that global flags precede the subcommand;
the per-command output row bound (`--top`); the five output formats and their
format-independence; the `sql` filesystem-access boundary and the external-access
lockdown for built-in commands; timestamp correction (and far-future clamping) in
the presented output; and otelq's exit-code and stderr behavior when telemetry is
absent, partial, or malformed.

**Not covered:** the raw telemetry directory and OTLP JSONL schema (an external
input — see [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md));
the parquet cache mechanics, sealing, eviction, and cache-first read routing (see
[SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md)); the
`duckdb-otlp` extension itself and the rationale for working around it (see
[ADR-006](../adr/ADR-006-read-otlp-extension-quirks.md)); and the OTel Collector
configuration that produces the raw files.

### Definitions

- **Relation / view** — a queryable table or view name exposed to SQL:
  `traces`, `logs`, `metrics`, `metrics_gauge`, `metrics_sum`,
  `metrics_histogram`, `metrics_exp_histogram`.
- **Signal** — a user-facing telemetry kind: `traces`, `logs`, `metrics`.
- **Subcommand / command** — one of the seven verbs otelq accepts
  (`summary`, `errors`, `slow`, `trace`, `logs`, `metric`, `sql`).
- **Global flag** — a flag accepted before the subcommand (`--format`, `--dir`,
  `--all`, `--no-cache`, `--since`, `--verbose`, `--version`); contrast
  subcommand-specific flags (`--top`, `--service`, `--level`, `--grep`, the
  `trace_id`/`name`/`query` positionals), which follow the subcommand.
- **Default telemetry dir** — the `.telemetry/` directory under the current
  working directory (`<cwd>/.telemetry`, per
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md)),
  used when `--dir` is not given. A cwd-relative default works both for
  `uv run otelq.py` from a checkout (run from the repo root) and for an installed
  copy (`uvx`/`pipx`) run from a project directory; a script-relative default
  would resolve into the install location (e.g. site-packages).
- **Event-time** — a record's own timestamp, as corrected and presented in the
  `timestamp` column (see FR-16).
- **Result** — the `(columns, rows)` pair a command produces, rendered by the
  selected output format.

## Functional Requirements

### Query relations and columns

- **FR-1 — Exposed relations.** otelq **must** expose exactly these query
  relations over the captured telemetry: `traces`, `logs`, `metrics`,
  `metrics_gauge`, `metrics_sum`, `metrics_histogram`, and
  `metrics_exp_histogram`. `metrics` **must** be the union of whichever of the
  four per-type metric relations are present. All seven **must** be queryable by
  the `sql` command; the built-in commands query the subset they need.
  **Expose-empty:** **all seven** relations **must** always resolve — a signal or
  metric type with no captured rows resolves to an empty (0-row) result, **not** a
  "table does not exist" catalog error. This holds for a metrics-only corpus
  (`sql "SELECT * FROM traces"` → 0 rows) **and** for a valid-but-empty or absent
  `--dir` (where the schema is probed from an embedded sample), so a "table does
  not exist" error is never surfaced. The set of relations and the empty-vs-error
  outcome **must** be identical with the cache and with `--no-cache`. Presence for
  the built-in commands is judged by **row count**, not by relation existence, so
  an all-empty corpus still takes the friendly empty-telemetry path (FR-18) and
  emits no zero-count skeleton (FR-3). The OTel **Summary** metric type is **not**
  supported (the `duckdb-otlp` reader for it is an unimplemented stub) and is
  never exposed.
- **FR-2 — Relation columns.** Each relation **must** present at least the
  following columns (the `sql` cheat-sheet), with `timestamp` carrying the
  corrected wall-clock event-time (FR-16):
  - **`traces`**: `timestamp`, `duration` (integer **milliseconds** — the
    duckdb-otlp extension reports span duration in ms, so sub-millisecond spans
    truncate to `0`), `trace_id`, `span_id`,
    `parent_span_id`, `service_name`, `span_name`, `span_kind`, `status_code`
    (`0`=unset, `1`=ok, `2`=error), `status_message`.
  - **`logs`**: `timestamp`, `trace_id`, `service_name`, `severity_text`,
    `severity_number`, `body`. `severity_number` is the OTel numeric severity;
    otelq maps it to a canonical level for `summary` (FR-3) using the standard
    ranges **TRACE** `1–4`, **DEBUG** `5–8`, **INFO** `9–12`, **WARN** `13–16`,
    **ERROR** `17–20`, **FATAL** `21–24` (values outside `1–24`, including `0`
    and null, are **UNSET**). The level is derived from `severity_number`, not
    the free-form `severity_text`, which carries inconsistent casing in practice
    (e.g. `Info`).
  - **`metrics`** (the union view over the per-type relations):
    `timestamp`, `service_name`, `metric_name`, `metric_type`, `value`,
    `metric_unit`. `metric_type` **must** be one of `gauge`, `sum`, `histogram`,
    or `exp_histogram`, naming the per-type relation each row originates from
    (`metrics_gauge`, `metrics_sum`, `metrics_histogram`,
    `metrics_exp_histogram`). The unified `value` **must** be the row's own
    `value` for `gauge`/`sum` rows and its `sum` for `histogram`/`exp_histogram`
    rows (the histogram types have no scalar value, so their distribution `sum`
    is surfaced as `value`).
  - **`metrics_gauge`** / **`metrics_sum`**: at least `timestamp`,
    `service_name`, `metric_name`, `metric_unit`, and a scalar `value`
    (`metrics_sum` additionally carries `aggregation_temporality`,
    `is_monotonic`).
  - **`metrics_histogram`**: at least `timestamp`, `service_name`,
    `metric_name`, `metric_unit`, `count`, `sum`, `min`, `max`, `bucket_counts`,
    `explicit_bounds`, and `aggregation_temporality`.
  - **`metrics_exp_histogram`**: at least `timestamp`, `service_name`,
    `metric_name`, `metric_unit`, `count`, `sum`, `min`, `max`, `scale`,
    `zero_count`, `zero_threshold`, the positive/negative bucket offsets and
    counts, and `aggregation_temporality`.

  The precise field semantics of the underlying raw records are owned by
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md);
  this requirement fixes only the column names otelq surfaces and the
  enumerations it relies on.

### The seven commands

- **FR-3 — `summary`.** `summary` **must** report a per-signal breakdown whose
  columns are, in order, `signal`, `details`, `count`, earliest `timestamp`,
  latest `timestamp`, and the distinct `service_name` count. Every numeric/time
  column **must** be scoped to its own row's subset (the count, span, and
  service count of just those records). Rows are produced **only for present
  signals**, as follows:
  - **`traces`** (when present): exactly two rows that partition spans by
    `duration` — `details = ">1s"` for `duration > 1s` (`> 1000` ms) and
    `details = "=<1s"` for the remainder. **Both** rows **must** appear even when
    a bucket's count is zero.
  - **`logs`** (when present): one row per canonical severity level
    (`TRACE`, `DEBUG`, `INFO`, `WARN`, `ERROR`, `FATAL`), the level derived from
    `severity_number` per the ranges in FR-2. **All six** rows **must** appear
    even at zero count. Log records whose `severity_number` is outside the
    canonical ranges **must** contribute an additional `details = "UNSET"` row,
    shown **only** when its count is non-zero.
  - **`metrics`** (when present): one row per metric type, with `details` set to
    the type — `gauge`, `sum`, `histogram`, `exp_histogram`. **All four** rows
    **must** appear even when a type's count is zero (a fixed skeleton like the
    log levels), and each row's count/earliest/latest/services **must** be scoped
    to that type.

  A signal with **no captured data** contributes **no rows** (its zero-count
  skeleton is not emitted) — "present" here means the signal **has captured rows**,
  not merely that its relation resolves: under expose-empty (FR-1) an absent
  signal's relation still resolves empty, yet **must not** emit a skeleton (e.g.
  metrics-only telemetry yields the four metric rows and **no** trace/log
  buckets). When **no** signal has any data at all, the friendly empty-telemetry
  behavior applies (FR-18). The zero-count rule thus governs *sub-rows within a
  signal that has data* (e.g. an `ERROR` level with no records), not signals
  without data.
- **FR-4 — `errors`.** `errors` **must** return error-status spans
  (`traces` rows with `status_code == 2`) and error/fatal logs (`logs` rows with
  `severity_text` in `{ERROR, FATAL}`, matched **case-insensitively** since
  `severity_text` carries inconsistent casing in practice — see FR-2), combined
  into one result and ordered newest-first by `timestamp`. Each row **must**
  identify whether it is a span or a log.
- **FR-5 — `slow`.** `slow` **must** return spans ordered by `duration`
  descending, limited to the top `N` where `N` is the value of `--top`
  (default **20**). The presented duration **must** be expressed in milliseconds.
- **FR-6 — `trace <trace_id>`.** `trace` **must** take a `trace_id` positional
  argument and return every span of that trace arranged as a parent/child tree
  (each span ordered under its parent by `timestamp`, with a depth indicator). A
  span whose `parent_span_id` is absent or not present among the trace's own
  spans **must** be treated as a root. The `trace_id` argument **must** accept a
  unique **prefix** of a trace id in addition to a full id: an exact match wins;
  otherwise a prefix that matches exactly one trace id resolves to it, and a
  prefix matching two or more **must** be rejected as a real error (FR-17) naming
  the ambiguity. A prefix matching none takes the normal empty-result path (EC-3).
- **FR-7 — `logs`.** `logs` **must** return log records ordered newest-first by
  `timestamp`, filtered by the optional subcommand flags `--service`
  (exact `service_name`), `--level` (exact `severity_text`, case-insensitive
  input), and `--grep` (case-insensitive substring of `body`). With no filter
  flags it **must** return all in-window log records.
- **FR-8 — `metric <name>`.** `metric` **must** take a `name` positional
  argument and return the time series for that metric (`metrics` rows whose
  `metric_name` equals `name`) ordered ascending by `timestamp`.
- **FR-9 — `sql "<query>"`.** `sql` **must** take a SQL string positional
  argument, execute it against the exposed relations (FR-1), and return its
  columns and rows. A SQL execution error **must** be reported as a real error
  (FR-17), not swallowed.

### Global flags and argument order

- **FR-10 — `--format`.** A `--format` global flag **must** accept exactly
  `table`, `json`, `jsonl`, `csv`, or `compact`, defaulting to `table`, and
  **must** select the rendering of the result. `table` is for human reading;
  `json` is a single compact JSON array for programmatic consumption (compact
  separators, no insignificant whitespace, to minimize tokens for the AI-agent
  consumer per [PRD-otelq](../prd/PRD-otelq.md)); `jsonl` emits one compact JSON
  object per line for streaming/line-oriented consumers; `csv` is the
  spreadsheet/interchange format; `compact` is a single compact JSON object of
  the form `{"columns":[...],"rows":[[...]]}` that declares the column names once
  and carries each row as a positional array — losslessly the same data as `json`
  but without repeating the column keys on every row, further reducing tokens for
  the AI-agent consumer. A `compact` result **must** be reconstructible to the
  exact records `json` would emit by zipping each `rows` entry with `columns`.
- **FR-11 — Global flags precede the subcommand.** `--format`, `--dir`,
  `--all`, `--no-cache`, and `--since` are global flags and **must** be accepted
  *before* the subcommand. Supplying a global flag *after* the subcommand
  **must** be rejected as an unrecognized argument (a hard parse error), not
  silently accepted. Subcommand-specific flags and positionals continue to follow
  the subcommand.
- **FR-12 — `--dir`.** A `--dir <path>` global flag **must** select the
  telemetry directory to read; when omitted, otelq **must** read the default
  telemetry dir (see Definitions).
- **FR-13 — `--all`.** An `--all` global flag **must** widen the query to the
  full raw history. (The routing this triggers is specified in
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md) FR-9.)
- **FR-14 — `--no-cache`.** A `--no-cache` global flag **must** force a pure
  raw-only scan of the raw files that neither reads nor writes any cache. (Cache
  interaction is specified in
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md) FR-17.)
- **FR-15 — `--since`.** A `--since <Ns|Nm|Nh|Nd>` global flag **must** restrict
  the query to a trailing window of `N` seconds (`s`), minutes (`m`), hours (`h`),
  or days (`d`). A malformed `--since` value **must** be rejected as a real error
  (FR-17) with a message naming the accepted forms.

### Presentation and robustness

- **FR-16 — Corrected timestamps.** The `timestamp` column in every relation and
  every command's output **must** render as the real wall-clock date/time of the
  event. otelq **must** correct the nanosecond-in-millisecond-column value
  surfaced by the reader extension (see
  [ADR-006](../adr/ADR-006-read-otlp-extension-quirks.md)); a raw 2026 event
  **must not** render as a far-future year. A single implausible far-future
  event-time (a clock-skewed producer or a unit mistake) **must not** blank out
  otherwise-valid queries: the trailing-window anchor is clamped to a plausible
  ceiling (`wall-clock + tolerance`) identically on the cache and `--no-cache`
  paths, so a bogus record beyond the ceiling is excluded from a windowed result
  rather than pushing the window past all real data. The clamp is defined once, as
  the window/watermark anchor, in
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md) (INV-7, EC-12);
  `doctor` surfaces the condition as a non-fatal warning (FR-26).
- **FR-17 — Exit codes.** otelq **must** exit `0` on success, including when a
  command produces zero result rows, prints a friendly "no telemetry" message
  (FR-18, FR-19), or prints help (a bare `otelq` or `otelq help`, FR-22). A
  non-zero exit **must** occur only on a real error — e.g. malformed SQL (FR-9),
  a malformed `--since`/argument-order parse failure (FR-11, FR-15), or an unknown
  `help` topic (FR-22).
- **FR-18 — Friendly empty-telemetry message.** When a command's required
  signal(s) carry **no captured data** — and **no** other signal does either
  (nothing captured at all) — otelq **must** print a short, friendly message to
  **stderr** (pointing at the Collector / export toggle) and exit `0`. It **must
  not** surface a reader/DuckDB stack trace. "Has data" (row count), not mere
  relation existence, governs this: under expose-empty (FR-1) a required signal's
  relation may resolve empty, which **must** still trigger the friendly path (or
  the gap message of FR-19 when another signal does have data).
- **FR-19 — Name the gap, don't blame the Collector.** When a command's required
  signal has **no captured rows** **but another signal does have data**, otelq
  **must** print a message that names the missing signal (and its likely cause:
  the apps aren't emitting it, or its file was deleted under the running
  Collector) rather than the generic "is the Collector running?" text. `errors`
  (which needs `traces` or `logs`) **must** name "traces or logs" when only
  `metrics` has data. A required signal whose relation resolves empty (FR-1) is
  treated as absent here — presence is by row count, not table existence.
- **FR-20 — Oversized export batch is skipped.** A single export batch whose
  record count exceeds the reader's 2048-row limit **must** be skipped, with a
  warning printed to stderr, rather than crashing the run. (The limit and its
  cause are described in
  [ADR-006](../adr/ADR-006-read-otlp-extension-quirks.md); the cache path shares
  this behavior per
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md) FR-15.)
- **FR-21 — Partial trailing line is skipped.** A partially-written trailing
  JSONL line (one that does not parse) **must** be skipped, and the run **must**
  still succeed; the line is re-read once complete on a later run.
- **FR-22 — Help affordances.** Beyond the seven query verbs, otelq **must** keep
  its help discoverable. A bare `otelq` (no command) **must** print the full
  top-level help and exit `0` — not the terse argparse "required: command" error.
  otelq **must** also accept a `help` meta-command: `otelq help` prints that same
  top-level help, and `otelq help <command>` prints the named command's own help
  (equivalent to `otelq <command> -h`). An unknown topic (`otelq help <unknown>`)
  **must** be rejected as a real error (FR-17) carrying argparse's invalid-choice
  message that names the valid commands. The `-h`/`--help` flags (top-level and
  per-subcommand) remain available and unchanged.

### Output bounds, metadata, and safety

- **FR-23 — Output row bound (`--top`).** `errors`, `logs`, and `metric` **must**
  each accept a `--top N` subcommand flag that caps the number of returned rows,
  defaulting to a sane bound (**50**) so a chatty window cannot flood an agent's
  context (`slow` keeps its own default of **20**, FR-5). `N` **must** be a
  non-negative integer; a negative value **must** be rejected as a real error
  (FR-17). `--top 0` returns zero rows. When — and **only** when — the bound
  actually truncates the result, otelq **must** print a one-line notice to
  **stderr** (never stdout, so `json`/`jsonl`/`csv` stay machine-parseable). The
  cap is applied after the command's own ordering, so the retained rows are the
  first `N` of the fully-ordered result.
- **FR-24 — `--version`.** A `--version` global flag **must** print otelq's own
  version and exit `0`. The reported version **must** match the packaged
  distribution version (so an agent can name the exact build it drives, relevant
  to the DuckDB/extension pin governance of
  [ADR-003](../adr/ADR-003-duckdb-otlp-extension-pin-governance.md)).
- **FR-25 — `--verbose`.** A `--verbose` global flag **must**, without changing
  the result rows or their rendering (INV-3), print a one-line description of the
  resolved query plan — the event-time window it covered, and how much of it was
  served from the cache versus gap-filled from raw — to **stderr**, so a result is
  self-describing and window/route surprises are diagnosable. When a `trace`
  lookup widens from an empty default window to the full history (FR-10 of
  [SPEC-otelq-incremental-cache](SPEC-otelq-incremental-cache.md)), `--verbose`
  **must** also note that widening.
- **FR-26 — `doctor` cache-health and clock-skew checks.** The `doctor` command
  **must**, in addition to validating the telemetry directory against
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md),
  report non-fatal diagnostics for the cache failure modes that silently degrade
  queries: cache-directory writability (a read-only dir disables the cache),
  a stale writer lock, an incompatible cursor schema version, and a newest cached
  event-time more than the clamp tolerance ahead of wall-clock (the FR-16 / cache
  INV-7 condition). These checks **must** be reported as `OK`/`INFO`/`WARN`
  only — never `FAIL` — because queries still answer from the raw files regardless,
  and **must not** change `doctor`'s exit code, which stays governed by the
  telemetry-contract validation.
- **FR-27 — `sql` filesystem boundary and built-in lockdown.** The `sql` command
  is an ad-hoc analysis escape hatch: it executes arbitrary SQL against the
  exposed relations with the invoking user's filesystem access (it can read and
  write local files via `read_csv`, `COPY`, etc.). otelq's help **must** document
  this so a caller treats an untrusted query with the same care as a shell
  command. Every **built-in** command (all commands other than `sql`) **must**
  run with DuckDB's external filesystem/network access revoked once its query
  relations are materialized, as defense-in-depth so a crafted relation cannot
  reach other files. Revoking access **must not** change any built-in command's
  result (its relations are already built by that point).
- **FR-28 — SQL-safe telemetry paths.** otelq **must** operate correctly when the
  telemetry directory (or any path it derives) contains characters that are
  significant in SQL string literals — notably a single quote (common on macOS,
  e.g. `Robert's Mac`). Such a path **must not** cause a SQL syntax error or
  permit injection; every filesystem path spliced into SQL **must** be escaped or
  bound. This holds identically on the cache and `--no-cache` paths.

## Edge Cases & Failure Modes

- **EC-1 — Nothing captured.** No matching `*.jsonl` files exist (Collector down
  or export disabled): every built-in command prints the friendly stderr message
  and exits `0`; no stack trace is shown. The relations still resolve empty
  (seeded from the embedded schema probe), so `sql "SELECT count(*) FROM traces"`
  returns `0` rather than a catalog error. (FR-1, FR-18)
- **EC-2 — One signal missing, others present.** `metrics` captured but no
  `traces`/`logs`: `errors`, `slow`, and `trace` name the missing signal instead
  of blaming the Collector; `metric`/`summary` still answer from `metrics`; and
  the `traces`/`logs` relations still **resolve empty** (FR-1), so
  `sql "SELECT * FROM traces"` returns 0 rows rather than a catalog error.
  (FR-1, FR-19)
- **EC-3 — Empty result, valid query.** A well-formed query that matches no rows
  (e.g. `trace <unknown-id>`, a `logs --grep` with no hits, a `metric <name>`
  with no series) exits `0`. `trace <unknown-id>` reports that no spans were found
  for the id. The `table` format renders an empty result as `(no rows)`. (FR-17)
- **EC-4 — Malformed SQL.** `sql "SELEKT 1"` exits non-zero with an `otelq: SQL
  error:` message; it does not print a Python traceback. (FR-9, FR-17)
- **EC-5 — Global flag after subcommand.** `otelq errors --format json` is
  rejected as an unrecognized argument; the user must write
  `otelq --format json errors`. (FR-11)
- **EC-6 — Malformed `--since`.** `--since 10x` (or `--since abc`) exits non-zero
  with a message naming the accepted forms (`10m`, `2h`, `1d`). (FR-15)
- **EC-7 — Far-future timestamps avoided.** With raw records carrying nanosecond
  timestamps in a millisecond column, presented `timestamp` values render in the
  correct year, not ~58000 CE. (FR-16)
- **EC-8 — Oversized batch present.** A corpus containing one export batch of
  more than 2048 records yields a stderr warning naming the batch size and the
  limit, the batch is omitted, and the rest of the corpus is queried normally.
  (FR-20)
- **EC-9 — Truncated trailing line present.** A corpus whose final line is a
  half-written JSON object is queried as if that line were absent, with no error.
  (FR-21)
- **EC-10 — Format does not change rows.** Running the same command with
  `--format table`, `--format json`, and `--format csv` yields the same logical
  rows in the same order; only the rendering differs. (INV-3)
- **EC-11 — Sparse `summary` sub-rows.** Logs present but only at one level (e.g.
  all `INFO`), and all spans `=<1s`: `summary` still emits the full skeleton for
  each present signal — the other five log levels and the `>1s` trace bucket
  appear with count `0` (and null earliest/latest, `0` services). (FR-3)
- **EC-12 — Level from `severity_number`, not text.** Logs whose `severity_text`
  is mixed-case (`Info`) but whose `severity_number` is `9` are counted under the
  `INFO` row; a log whose `severity_number` is outside `1–24` adds an `UNSET`
  row, which is absent when no such record exists. (FR-3, FR-2)
- **EC-13 — Unknown help topic.** `otelq help not-a-command` **must** exit
  non-zero with argparse's invalid-choice error naming the valid commands; it
  **must not** silently fall back to general help and exit `0`. (FR-22, FR-17)
- **EC-14 — `--top` truncation notice.** Given more matching rows than the bound,
  `otelq logs --top 2` returns exactly two rows and prints a one-line truncation
  notice to **stderr**; under the bound (e.g. `--top 50` over six rows) the full
  result is returned and **no** notice is printed. The notice never appears on
  stdout. (FR-23)
- **EC-15 — `--since` seconds unit.** `--since 30s` restricts the query to the
  trailing 30 seconds (anchored at the max in-window event-time), a tighter window
  than the `1m` floor previously allowed. (FR-15)
- **EC-16 — `--version`.** `otelq --version` prints `otelq <version>` and exits
  `0`, where `<version>` equals the packaged distribution version. (FR-24)
- **EC-17 — `--verbose` metadata.** `otelq --verbose summary` prints the same
  result rows as without `--verbose`, plus a one-line window/route/cache
  description to stderr; the stdout rows and their order are unchanged. (FR-25,
  INV-3)
- **EC-18 — `jsonl` format.** `--format jsonl` emits one compact JSON object per
  line (each line independently parseable), while `--format json` emits a single
  compact array; both carry the same logical rows in the same order as `table`.
  (FR-10, INV-3)
- **EC-19 — Trace-id prefix.** `trace <unique-prefix>` returns the matching
  trace's tree; an exact id still works; a prefix matching two or more trace ids
  exits non-zero with an ambiguity message; a prefix matching none takes the
  empty-result path (EC-3). (FR-6, FR-17)
- **EC-20 — Built-in lockdown vs `sql` escape hatch.** After a built-in command's
  relations are built, DuckDB external file access is revoked, so a built-in
  cannot be coerced into reading an unrelated file; `sql` retains file access so
  `read_csv`/`COPY` continue to work as documented. (FR-27)
- **EC-21 — Quote in `--dir`.** A telemetry directory whose path contains a single
  quote (e.g. `.../Robert's Mac/.telemetry`) is queried normally — no SQL syntax
  error, identical result cached vs `--no-cache`. (FR-28)
- **EC-22 — `compact` format.** `--format compact` emits a single JSON object
  `{"columns":[...],"rows":[[...]]}` — the column names once, then one positional
  array per row — carrying the same logical rows in the same order as `table` and
  `json`. Zipping each `rows` entry with `columns` reconstructs exactly the
  objects `--format json` would emit. (FR-10, INV-2, INV-3)

## Acceptance Criteria

> Given/When/Then, each independently testable. Command-level criteria exercise
> the in-memory `synth_conn` fixture and the direct `cmd_*` / `format_output` /
> `main` entry points; file-level robustness criteria use the file-based
> `temp_telemetry` fixture. Hints reference `just otelq`, `just otelq-test`, and
> `tests/test_otelq.py`.

- **AC-1** (Verifies FR-1, FR-2): Given a synthetic connection with traces, logs,
  and gauge/sum/histogram/exp_histogram metrics, when each relation is queried via
  `sql`, then `traces`, `logs`, `metrics`, `metrics_gauge`, `metrics_sum`,
  `metrics_histogram`, and `metrics_exp_histogram` all resolve and expose the
  columns listed in FR-2; `metrics` returns all four `metric_type` values; and the
  unified `value` equals the `sum` for the `histogram`/`exp_histogram` rows.
  *Verification hint: `cmd_sql` with `SELECT * FROM <relation> LIMIT 1` per
  relation; assert column names, the `metric_type` set, and the value-or-sum rule.*
- **AC-2** (Verifies FR-3): Given a corpus with traces of differing duration,
  logs across several levels, and metrics, when `summary` runs, then the columns
  are `signal, details, count, earliest, latest, services` in that order; traces
  yield a `>1s` and a `=<1s` row; logs yield one row for each of
  `TRACE/DEBUG/INFO/WARN/ERROR/FATAL`; metrics yield one row for each of
  `gauge/sum/histogram/exp_histogram`; and each row's
  count/earliest/latest/services are scoped to that row's subset.
  *Verification hint: `test_summary_breakdown_rows`; assert the column order, the
  present (signal, details) pairs, and per-subset counts.*
- **AC-3** (Verifies FR-4): Given a span with `status_code == 2` and an `ERROR`
  log, when `errors` runs, then both appear in one result, each tagged as a span
  or a log, ordered newest-first by `timestamp`.
  *Verification hint: `test_errors_finds_error_span_and_log`; assert both kinds
  present and descending order.*
- **AC-4** (Verifies FR-5): Given several spans of differing durations, when
  `slow --top N` runs, then exactly the `N` longest are returned in descending
  duration order, with the duration shown in milliseconds.
  *Verification hint: `test_slow_orders_by_duration_desc` with `top=2`.*
- **AC-5** (Verifies FR-6): Given the spans of one trace with a parent/child
  relationship, when `trace <trace_id>` runs, then every span of that trace is
  returned arranged as a tree (root first, children nested by depth).
  *Verification hint: `test_trace_returns_tree_for_one_trace`.*
- **AC-6** (Verifies FR-7): Given logs across services, levels, and bodies, when
  `logs` runs with `--service`, `--level`, or `--grep`, then only matching rows
  are returned (level matching is case-insensitive), newest-first.
  *Verification hint: `test_logs_filter_by_service`, `..._by_level`,
  `..._by_grep`.*
- **AC-7** (Verifies FR-8): Given a metric with multiple data points, when
  `metric <name>` runs, then its time series is returned ordered ascending by
  `timestamp` with the FR-2 metric columns.
  *Verification hint: `test_metric_returns_time_series` for `db.pool.in_use`.*
- **AC-8** (Verifies FR-9): Given a synthetic connection, when `sql "SELECT
  count(*) AS n FROM traces"` runs, then the query's own columns and rows are
  returned verbatim.
  *Verification hint: `test_sql_passthrough`.*
- **AC-9** (Verifies FR-9, FR-17, INV-5, EC-4): Given a malformed SQL string, when
  `sql` runs, then otelq exits non-zero with an `otelq: SQL error:` message and no
  Python traceback.
  *Verification hint: invoke `cmd_sql`/`main` with `"SELEKT 1"`; assert
  `SystemExit` / non-zero and the message prefix.*
- **AC-10** (Verifies FR-10, INV-2): Given any result, when `--format json` is
  selected, then the output is a JSON array of objects keyed by the result
  columns; `--format csv` emits a header row plus CSV rows; `--format table` is
  the default human layout.
  *Verification hint: `test_format_output_json`, `test_format_output_csv`,
  `test_format_output_table_empty`.*
- **AC-11** (Verifies FR-11, EC-5): Given a subcommand, when a global flag such as
  `--format json` is placed *after* it, then argument parsing fails with an
  unrecognized-argument error; placing it *before* the subcommand succeeds.
  *Verification hint: call `build_parser().parse_args([...])` with the flag after
  the subcommand and assert `SystemExit`; assert success when before.*
- **AC-12** (Verifies FR-12): Given `--dir <path>`, when a command runs, then
  otelq reads that directory; with `--dir` omitted it reads the default telemetry
  dir.
  *Verification hint: `_run(dirpath, ...)` passes `--dir`; assert results come
  from the supplied temp corpus.*
- **AC-13** (Verifies FR-13, FR-14): Given a corpus, when `--all` and `--no-cache`
  are supplied as global flags before the subcommand, then they parse and select
  the widened / cache-bypassing query path.
  *Verification hint: routing assertions live in
  `SPEC-otelq-incremental-cache` (AC-9, AC-17); here assert the flags parse as
  globals.*
- **AC-14** (Verifies FR-15, EC-6): Given `--since 10m`, when a command runs, then
  the query is restricted to the trailing 10-minute window; given a malformed
  `--since` (e.g. `10x`), otelq exits non-zero with a message naming `10m/2h/1d`.
  *Verification hint: `_parse_since` accepts `10m/2h/1d` and raises `SystemExit`
  on `10x`; window behavior cross-checked via `test_ac9_recent_default_vs_all`.*
- **AC-15** (Verifies FR-16, EC-7): Given the real fixture whose raw timestamps
  are nanoseconds stored in a millisecond column, when any relation is queried,
  then the presented `timestamp` falls in the correct (near-present) year, not a
  far-future one.
  *Verification hint: `test_integration_timestamps_are_scaled`.*
- **AC-16** (Verifies FR-17, FR-18, INV-1, INV-4, EC-1): Given an empty telemetry
  directory, when any command runs through `main`, then a friendly message is
  printed to stderr, the process exits `0`, and no `*.jsonl` file is created or
  modified.
  *Verification hint: `cmd_summary` over an empty `connect` raises
  `NoTelemetryError`; `main` catches it, prints to stderr, returns `0`
  (`test_summary_raises_when_empty`).*
- **AC-17** (Verifies FR-19, EC-2): Given a corpus where only `metrics` is
  present, when `logs`/`errors` run, then the error names the missing signal and
  differs from the generic "no telemetry" text; when nothing is present, the
  generic text is used.
  *Verification hint: `test_require_names_missing_signal_when_others_present`,
  `test_require_keeps_generic_message_when_nothing_present`,
  `test_errors_names_gap_when_only_metrics_present`.*
- **AC-18** (Verifies FR-17, EC-3): Given a valid query with no matches (unknown
  `trace_id`, non-matching `--grep`, unknown metric name), when it runs, then the
  process exits `0`; `trace` reports no spans for the id, and `table` output for
  an empty result is `(no rows)`.
  *Verification hint: `test_trace_unknown_id_raises` (caught by `main` → exit 0);
  `test_format_output_table_empty`.*
- **AC-19** (Verifies FR-20, EC-8): Given a corpus containing one export batch of
  more than 2048 records, when otelq runs, then that batch is skipped with a
  stderr warning naming the size and the limit, and the run exits `0` returning
  the remaining rows.
  *Verification hint: `test_ac15_robust_tail_parsing` (oversized batch arm);
  assert warning on stderr and success.*
- **AC-20** (Verifies FR-21, EC-9): Given a corpus whose final JSONL line is
  truncated, when otelq runs, then the partial line is skipped and the run exits
  `0` with the complete records returned.
  *Verification hint: `test_ac15_robust_tail_parsing` (truncated-line arm).*
- **AC-21** (Verifies INV-3, EC-10): Given any command, when it is rendered as
  `table`, `json`, and `csv`, then the underlying rows (and their order) are
  identical across all three formats.
  *Verification hint: run a command, capture `(columns, rows)` once, and assert
  each `format_output` rendering reflects the same rows/order.*
- **AC-22** (Verifies INV-1): Given any otelq run over a fixture, when it
  completes (including the empty-telemetry and skip paths), then no raw `*.jsonl`
  file has been modified or deleted by otelq.
  *Verification hint: `test_ac18_raw_files_unmodified` (checksum before/after).*
- **AC-23** (Verifies FR-3, EC-11): Given logs all at `INFO` and spans all
  `=<1s`, when `summary` runs, then the `logs` rows still include
  `WARN`/`ERROR`/`FATAL`/`DEBUG`/`TRACE` at count `0`, and the `traces` `>1s` row
  appears at count `0`; the present buckets carry the real counts.
  *Verification hint: `test_summary_zero_count_skeleton`; assert all six log
  levels and both trace buckets present with the expected counts.*
- **AC-24** (Verifies FR-3, FR-2, EC-12): Given logs whose `severity_text` is
  `Info` but `severity_number` is `9`, plus one log with an out-of-range
  `severity_number`, when `summary` runs, then the mixed-case records are counted
  under `INFO` (level taken from `severity_number`) and an `UNSET` row appears
  with the out-of-range record's count; with no out-of-range record, no `UNSET`
  row appears.
  *Verification hint: `test_summary_level_from_severity_number`,
  `test_summary_unset_row_only_when_present`.*
- **AC-25** (Verifies FR-3): Given a corpus where only `metrics` is present, when
  `summary` runs, then it returns exactly the four metric-type rows
  (`gauge/sum/histogram/exp_histogram`, types with no captured rows at count `0`)
  and emits no zero-count log/trace skeleton; an absent signal contributes no
  rows.
  *Verification hint: `test_summary_absent_signal_has_no_rows`.*
- **AC-26** (Verifies FR-22, EC-13): Given the parser, when `main([])` or
  `main(["help"])` runs, then each prints the top-level help (its `usage:` line
  and the global-flags-first rule) and exits `0`; when `main(["help", "slow"])`
  runs, it prints `slow`'s own help (its `--top` flag) and exits `0`; and
  `main(["help", "not-a-command"])` exits `2` with an invalid-choice message.
  *Verification hint: `test_bare_otelq_prints_full_help`,
  `test_help_command_prints_general_help`,
  `test_help_command_topic_prints_subcommand_help`,
  `test_help_command_unknown_topic_errors`.*
- **AC-27** (Verifies FR-23, EC-14): Given more matching log rows than the bound,
  when `logs --top 2` runs, then exactly two rows are returned and a truncation
  notice is written to stderr; when the bound is not exceeded (`--top 50`), the
  full result is returned with no stderr notice.
  *Verification hint: `test_f1_top_caps_rows_and_warns_on_stderr`; also
  `test_bug2_slow_top_negative_rejected_at_parse` for the non-negative rule.*
- **AC-28** (Verifies FR-24, EC-16): Given the CLI, when `otelq --version` runs,
  then it prints `otelq <version>` and exits `0`, and `<version>` equals the
  `pyproject.toml` project version.
  *Verification hint: `test_f3_version_flag_prints_and_exits_zero`,
  `test_f3_version_matches_pyproject`.*
- **AC-29** (Verifies FR-25, EC-17, INV-3): Given any command, when `--verbose` is
  supplied, then the stdout rows and order match the non-verbose run and a
  window/route/cache line is written to stderr.
  *Verification hint: run a command with and without `--verbose`; assert stdout is
  identical and stderr carries the plan summary.*
- **AC-30** (Verifies FR-26): Given a valid telemetry dir, when `doctor` runs,
  then it reports cache-writability (and, when a far-future cursor watermark is
  present, a clock-skew `WARN`) as non-fatal rows without changing the exit code.
  *Verification hint: `test_d4_doctor_reports_cache_health`,
  `test_d4_and_b1_doctor_flags_clock_skew`.*
- **AC-31** (Verifies FR-27, EC-20): Given a built-in command, when it runs, then
  DuckDB external file access is revoked after its relations are built (a
  `read_csv` is denied); given `sql`, file access is retained (`read_csv`
  succeeds).
  *Verification hint: `test_d2_sql_boundary_locks_builtins_not_sql`.*
- **AC-32** (Verifies FR-10, EC-18, INV-3): Given any result, when `--format json`
  is selected it is a single compact JSON array, and `--format jsonl` emits one
  compact JSON object per line; both carry the same rows in the same order as
  `table`.
  *Verification hint: `test_p5_format_json_compact_and_jsonl`,
  `test_p5_format_jsonl_via_cli`.*
- **AC-33** (Verifies FR-15, EC-15): Given `--since 30s`, when a command runs,
  then the window is the trailing 30 seconds; `_parse_since` accepts the `s` unit
  alongside `m/h/d` and rejects a malformed value.
  *Verification hint: `test_f2_since_accepts_seconds_unit`,
  `test_f2_since_seconds_windows_end_to_end`.*
- **AC-34** (Verifies FR-6, EC-19): Given several traces, when `trace` is given a
  unique id prefix it returns that trace; an exact id also works; an ambiguous
  prefix exits non-zero with an ambiguity message.
  *Verification hint: `test_f4_trace_prefix_resolves_and_flags_ambiguity`.*
- **AC-35** (Verifies FR-16, EC-7): Given a corpus with one far-future record plus
  in-window records, when a windowed command runs, then the far-future record is
  excluded (the window anchor is clamped) while the in-window records are
  returned, identically cached vs `--no-cache`; `--all` includes both.
  *Verification hint: `test_b1_window_anchor_clamped_to_ceiling`.*
- **AC-36** (Verifies FR-28, EC-21): Given a telemetry directory whose path
  contains a single quote, when a command runs, then it returns the correct result
  with no SQL error, identical cached vs `--no-cache`.
  *Verification hint: cross-checked by the incremental-cache SPEC's path-escaping
  criteria; assert a quoted `--dir` yields the same rows on both paths.*
- **AC-37** (Verifies FR-10, EC-22, INV-2, INV-3): Given any result, when
  `--format compact` is selected it is a single compact JSON object with a
  `columns` array and a `rows` array of positional arrays; zipping each row with
  `columns` yields exactly the objects `--format json` produces, in the same
  order.
  *Verification hint: `test_p5_format_compact_columns_rows`,
  `test_p5_format_compact_via_cli`.*

### Examples

- **Argument order (FR-11).** `just otelq --format json errors` succeeds;
  `just otelq errors --format json` fails with
  `otelq: error: unrecognized arguments: --format json`. Global flags — including
  the time-window flag `--since` — precede the subcommand:
  `just otelq --since 10m errors`; per-command flags still follow it:
  `just otelq slow --top 5`.
- **Relations for `sql` (FR-1/FR-2).**
  `just otelq-sql "SELECT service_name, count(*) FROM traces WHERE status_code = 2 GROUP BY 1"`
  groups error spans by service; `metrics` unifies the per-type relations, so with
  all four present `SELECT DISTINCT metric_type FROM metrics` returns
  `{gauge, sum, histogram, exp_histogram}`. Under expose-empty (FR-1), any
  documented relation resolves to `0` rows (not a catalog error) when its signal
  has no data but some other telemetry is present — `SELECT count(*) FROM
  metrics_histogram` and `SELECT count(*) FROM traces` both return `0` on a
  gauge/sum-only corpus.
- **Friendly emptiness (FR-18 vs FR-19).** With nothing captured, every command
  prints "no telemetry captured — is the collector running …?" to stderr and
  exits 0. With only `metrics` captured, `errors` instead prints "no traces or
  logs telemetry captured (present: metrics) …", naming the gap.
- **Help discoverability (FR-22).** `just otelq` (no command) and `just otelq help`
  both print the full top-level help; `just otelq help slow` prints `slow`'s own
  help (its `--top` flag). `just otelq help nope` exits non-zero with an
  invalid-choice message naming the valid commands.

## Invariants

- **INV-1** — Read-only over telemetry: otelq never modifies, creates, or deletes
  the raw telemetry files it reads. Its output is a pure function of (the
  telemetry it reads, the command, and the flags).
- **INV-2** — Output-format roles are fixed: `table` is the human-facing default;
  `json` (a single compact array), `jsonl` (one compact object per line), and
  `compact` (a single object with a `columns` header and positional `rows`
  arrays) are the machine/automation formats; `csv` is the spreadsheet/interchange
  format. Choosing a format never changes which command runs.
- **INV-3** — Format independence: the rows a command returns, and their order,
  do not depend on which `--format` is chosen; only the rendering differs.
- **INV-4** — Friendly failure: absent telemetry yields a human-readable stderr
  message and exit `0`; a reader/DuckDB stack trace is never the user-facing
  result of "nothing captured" or "this signal is missing".
- **INV-5** — Exit-code discipline: exit `0` covers every success including empty
  results and the friendly "no telemetry" path; a non-zero exit is reserved for
  real errors (malformed SQL, malformed `--since`, argument-order parse failure).
