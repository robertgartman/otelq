---
name: otelq
description: "This is the **default** tool for accessing logs, metrics, and traces generated from project codebase and infrastructure. Use otelq CLI to query telemetry captured by the OpenTelemetry Collector."
---

# Query Telemetry

Inspect real OpenTelemetry signals to close the loop after a change or for troubleshooting: run the
code, then confirm from telemetry that it behaved as intended. If infrastructure is feeding into OpenTelemetry, you can also query that data to understand its behavior.

## Run

```
uvx otelq --dir .telemetry --format compact summary
```

`--dir .telemetry` is **required** — `uvx` runs otelq from an isolated build, so
the default path won't resolve to your project. Point it at the Collector's
output folder (the bind-mounted `.telemetry/` at the project root). Pin a
version with `uvx otelq@<version> …`.

## Start every investigation with `triage`

**The first instruction of an investigation is `triage`, not `summary`.**
otelq records every telemetry query it runs (locally, in `.otelq-history/`
under the telemetry dir), and `triage` acts on that history:

```
uvx otelq --dir .telemetry --format compact triage
```

What it does, in order:

1. **Detects where you are.** Pass `--session-id <id>` to continue a chain —
   triage anchors on that session's last query. Without the flag you are
   **always** starting fresh: the session id is the only chain signal
   (timestamps are never trusted — other agents may be querying the same
   store concurrently), which is why carrying the id matters.
2. **Auto-runs the best next query** when history is convincing (a Markov step
   over past investigation sessions: enough recent evidence, majority
   agreement, and a concrete template). You get the query's normal output —
   banner first, so you know triage chose it.
3. **Suggests the follow-up as its last output line** — a full
   `otelq --dir … --session-id … <query>` invocation to run next. Run it (or
   adapt any `?` placeholders) to keep the chain going.
4. **Grounds or admits ignorance** when history has no strong candidate: if
   this session hasn't run `summary` yet, triage runs it for you (RCA step 1 —
   read its output and proceed with step 2). If the session is already
   grounded, triage says it doesn't know and prints the ranked template list
   (`score` = recent-frequency × success; `terminal_pct` = how often the
   template *ended* a burst with usable rows). Pick one that matches your
   symptom, or fall through to the RCA guide below.

`history --top 20` prints the ranked list directly; the raw tables are `sql`
views (`history_queries`, `history_invocations` — sessions are `session_id`
groups ONLY; id-less rows belong to no session). History never records
`triage`/`history`/`doctor`/meta commands, and `OTELQ_HISTORY=0` disables it.

## How to investigate (RCA guide)

A fixed procedure beats free-form exploration: SOP-guided agents roughly
double free-form ReAct's accuracy on RCA benchmarks (Flow-of-Action, WWW'25),
and the documented LLM failure modes are skipping grounding, anchoring on the
first plausible cause, and stopping at a propagated symptom instead of its
origin (FORGE'26). Follow the steps in order; each step's output is the next
step's input. The ordering is adapted to otelq's dev-environment corpus:
logs here are small, local, and rich — go to them earlier than production
RCA lore suggests.

**Standing rule — constrain every query.** Default to a tight `--since`
window (the repro run, not the whole session) and an aggressive `--regex` on
anything list-shaped; the header's `Rows removed by regex` tells you what the
filter cost, so over-filtering is visible and correctable. Widening a window
or dropping a filter is one retry; flooding your context is unrecoverable.

**Standing rule — carry one session id through the whole investigation.** The
first call of an RCA stamps a `Session:` line in its response header and prints
a footer naming a session id; pass that exact id as `--session-id <id>` on every
follow-up call of the same investigation, so the run is correlated end-to-end.
Start a fresh investigation → let otelq generate a new id (omit the flag on the
first call); continue one → reuse the id it already gave you.

**0 · FRAME.** Before any query, state the symptom and the time window.
Tighten `--since` to the anomaly (e.g. `30s`/`10m` right after a repro run) —
never "look at everything."

**1 · GROUND with `summary`** *(skip when `triage` already ran a grounding
query for you — build on its output instead of re-grounding)*. One cheap call
maps the terrain: which signals
have data at all, per-level log counts (any ERROR/FATAL?), the `>1s` span
bucket, metric types, and each subset's time span. Its second block —
`** List of services in telemetry data **`, a `service`/`count` table ordered
most-active-first — tells you which service dominates the window and is the
first candidate to zoom in on (scope later queries with its name via
`--service` / a `--regex` / a `sql WHERE service_name = '…'`). Skip `summary`
only when you already hold a trace_id or an exact target. Target signal at 0
rows? Fix capture first (`troubleshoot`) — don't query a void.

**2 · TRIAGE — commit to ONE signal per symptom.** Each command below is a
pre-built aggregate; don't hand-roll `sql` for these:

| Symptom | Command |
| --- | --- |
| exception / failure | `errors` — error spans + ERROR/FATAL logs, newest-first |
| slow / latency | `slow` — spans by duration desc, carries trace_id |
| wrong or missing behavior | `logs --grep <token>` (literal, case-insensitive body contains) or `--regex` (pattern) — dev logs are rich, use them early |
| resource / counter anomaly | `metric <name>` — names come from `summary` |

Nothing in the window? Widen once (`--all`); still nothing means the
telemetry never captured it — say so instead of exploring sideways.

**3 · LOCALIZE with `trace <trace_id>`** (a unique id prefix is enough;
`errors`, `slow`, and `logs` rows all carry trace_id — pivot directly).
One full tree beats ten partial fetches. Read it with two heuristics:

- **Latency origin** = the span with the largest **self** time (its
  `duration_ms` minus its children's) — *not* the longest span; the root is
  long because it contains everyone.
- **Error origin** = the **deepest** error span whose children are *not* all
  errors. A span whose children all errored is merely propagating — keep
  descending.

**4 · EXPLAIN with scoped logs** — never a free-text sweep of the window:
`sql "SELECT timestamp, severity_text, body FROM logs WHERE trace_id = '<id>'"`.
Custom app attributes live in the `*_attributes` JSON columns — run
`sql "DESCRIBE logs"` first, then filter on what actually exists.

**5 · STOP** when you hold all three: the origin span, the log/exception that
explains it, and the service/attribute saying where. State the hypothesis
with that evidence. Do not keep exploring past this point.

**Token discipline** (why the order above): aggregates before rows — `summary`
or `sql "SELECT count(*), min(timestamp), max(timestamp) FROM …"` costs a
fraction of the rows themselves; filter server-side (`--since` / `--regex` /
`--top` / `--service` / `--level` / `--grep`) rather than fetching broadly and
post-filtering; carry only {window, signal, trace_id, span_id} between steps.

## Timestamps are UTC

**All timestamps are UTC** — every `timestamp` otelq prints, and every one you
write into a `sql` filter. Each response also restates this up front. For
`sql` literals, write them bare (`'2026-07-01 10:00:00'`) or `Z`-suffixed
(`'2026-07-01T10:00:00Z'`); never a `+02:00`-style offset — DuckDB silently
drops it instead of converting, so the comparison would be silently wrong.

## Pick the fewest-token format

**Default to `--format compact` for anything you parse yourself.** It returns one
lossless object — column names once, then each row as a positional array:

```
{"columns":["timestamp","service_name","body"],"rows":[["2026-…","api","hi"]]}
```

Read each row by position, or rebuild objects with `zip(columns, row)`. Versus
`--format json` (an array of per-row objects) it drops the repeated keys —
typically ~40–60% fewer tokens on the same rows, identical data.

| Format | Use it for |
| --- | --- |
| `compact` | **your own analysis** — smallest, lossless (prefer this) |
| `json` / `jsonl` | only when a downstream consumer needs self-describing rows |
| `csv` | spreadsheet / interchange |
| `table` | only when showing output to a human |

`--format` is a **global** flag: it (and `--all`, `--no-cache`, `--since`,
`--regex`, `--session-id`) goes **before** the subcommand; per-command flags
(`--top`, `--service`, `--level`, `--grep`) go **after**:

```
uvx otelq --dir .telemetry --since 10m --format compact errors --top 20
```

## Filter inside otelq (`--regex` / `--grep`), not shell `| grep`

Piping otelq's output through shell `grep` is blind to what got filtered away.
Use otelq-native filtering instead:

- `--regex PATTERN` (summary/errors/slow/trace/logs/metric): regex over rendered
  cell values, reported in the header with rows removed.
- `logs --grep TOKEN` (logs only): literal, case-insensitive substring match on
  `body`.

Both avoid shell-postprocessing ambiguity. `--regex` is the general mechanism;
`--grep` is a logs-only convenience when you want literal substring matching
without regex escaping.

`--regex` is applied to the already `--top`-capped result, while `logs --grep`
filters in the SQL query before capping. So for logs, `--grep` can surface the
latest matching rows without first raising `--top`.

Example:

```
uvx otelq --dir .telemetry --regex "timeout|ECONNRESET" errors
```

Case-sensitive by default (use inline `(?i)` for case-insensitive). Only
`summary`/`errors`/`slow`/`trace`/`logs`/`metric` support it — not `sql`
(use `WHERE col ~ 'pattern'` instead) or `doctor`/`collector-config`/
`troubleshoot`. It filters the same already-`--top`-capped result; raise
`--top` if you need to search further back.

## Commands

`summary`, `errors`, `slow`, `trace <id>`, `logs`, `metric <name>`,
`sql "<query>"`. Narrow the window with `--since 30s|10m|2h|1d` (or `--all` for
full history); cap rows with `--top N`. Full reference (incl. the `sql` view
columns):

```
uvx otelq --dir .telemetry --help
```

`--help`'s `sql` column list is a curated subset. For the full live schema —
including `*_attributes` columns carrying whatever custom OTel tags an app
actually emits — use standard DuckDB introspection:

```
uvx otelq --dir .telemetry sql "DESCRIBE traces"
uvx otelq --dir .telemetry sql "PRAGMA table_info('logs')"
```

## Not seeing data?

```
uvx otelq --dir .telemetry troubleshoot
```
