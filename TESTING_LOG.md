# otelq — black-box CLI test log

> **Temporary artifact** created by an autonomous testing pass (2026-06-25).
> Methodology: exercise the real `otelq` CLI (`uv run otelq.py …` / `just otelq …`)
> across every command, format, and edge case. **No new unit tests are authored**
> (per user steer 2026-06-25); bugs are fixed by sub-agents (code only) and
> re-verified by re-running `otelq`. Findings for the user are at the bottom.

## Environment
- uv 0.11.17, just 1.51.0, docker 29.5.3, macOS (darwin 25.5.0)
- Sandbox blocks docker socket + `~/.cache/uv`, so all runs use sandbox-disabled bash.
- Code under test: current working tree (uncommitted edits to otelq.py, tests, README, etc.).

## Data sources
- **D0** — existing `telemetry/` captured before this session (event-time 16:48–16:53).
- **D1** — fresh `just otel-demo` data (telemetrygen: fast+slow traces, metrics, all 6 log levels).
- **Cxx** — crafted corpora under the scratchpad, pointed at via `--dir`, for deterministic edge cases.

## Status legend
- ✅ PASS — behaves per SPEC/README/help
- ❌ FAIL — deviates from a stated promise (bug)
- ⚠️ NOTE — works but worth flagging / minor / doc-level
- 🔁 RETEST — re-verified after a fix

---

## Summary (read first)

Exercised **all 11 subcommands × 3 formats × ~60 cases** (happy paths + corner cases: mixed casing, odd
time windows, malformed/oversized/empty/wrong-key telemetry, argument order, bad `--dir`, broken pipe, etc.)
by running the real `otelq` CLI against fresh `just otel-demo` data and crafted corpora.

**Result: 6 bugs found, all fixed (code-only, uncommitted) and re-verified by re-running `otelq`. No
regressions** (ruff clean; existing 70 tests still pass).

| Bug | What | Severity | Status |
|-----|------|----------|--------|
| BUG-1 | `metrics_sum`/`metrics_gauge` queryable on `--no-cache` but errored on the default cache path (keystone violation) | medium | ✅ fixed (+ residual FR-1 decision N3) |
| BUG-2 | `slow --top <negative>` → raw DuckDB traceback | low–med | ✅ fixed |
| BUG-3 | `logs --grep` treated `_`/`%` as wildcards, not literal substring (FR-7) | low–med | ✅ fixed |
| BUG-4 | `--dir <file>` → raw `NotADirectoryError` traceback | low | ✅ fixed |
| BUG-5 | `sql ""` (empty query) → raw `AttributeError` traceback | low | ✅ fixed |
| BUG-6 | `logs` order differed cached vs `--no-cache` on tied timestamps (keystone) | low | ✅ fixed |

**Decisions — solving one by one:** ✅ **N1 RESOLVED** (`--since` promoted to a global flag — see N1 below).
✅ **N2 RESOLVED** (WONTFIX / out-of-scope — only reachable via non-conformant input; code left as-is).
✅ **N3 RESOLVED** (universal expose-empty — every relation resolves, empty when no data, never a catalog
error). ✅ **N4 RESOLVED** (6 regression tests added, one per original bug). **All decisions closed.**

**🚧 FEATURE (user-requested, 2026-06-25): full metric-type support.** Beyond the original test scope, the
user asked otelq to support all metric types and split `summary` by type. Findings & decisions:
- Extension (`duckdb-otlp 1.5.3`) readers: gauge/sum/histogram/exp_histogram **work**; `read_otlp_metrics_summary`
  and the generic `read_otlp_metrics` are **stubs** ("not yet supported"). So **4 types supported, Summary cannot be** (also telemetrygen can't emit Summary).
- Demo (`compose.yaml`): added Histogram + ExponentialHistogram generators (now Gauge+Sum+Histogram+Exp). ✅
- Decisions: unified `metrics` view `value` = datapoint value (gauge/sum) or `sum` (histogram/exp); `summary`
  splits per type; N3 resolved as **expose-empty** (all 4 sub-relations resolve when metrics present, empty if
  the type is absent — schema seeded from the live extension, no drift); tests updated/added as needed.
- Implementation dispatched to an agent.

**Feature verification (metric types) — ✅ done, verified by running otelq:**
- demo `summary` splits metrics into 4 rows: gauge 153, sum 34, histogram 16, exp_histogram 16.
- `metrics` view spans all 4 types; **value=sum** confirmed for histograms (view value range == metrics_histogram.sum range).
- N3 (metrics scope): on gauge+sum-only corpus, `metrics_histogram`/`metrics_exp_histogram` → 0 rows (no error), cached==--no-cache.
- Friendly empty path intact; keystone holds; ruff clean; **74 tests pass**.
- Schema-seeding sourced from the **live extension** (read a sample chunk `WHERE false`), NOT a hard-coded/fetched
  schema — deliberately, because `duckdb-otlp` has breaking schema changes across versions (v0.4→v0.5, no
  stability guarantee), so any embedded schema would drift. Confirmed via the project's own docs.

**🚧 Universal expose-empty (user-requested follow-up): extend to traces + logs too.** All 7 relations must
resolve-empty whenever ANY telemetry is present (e.g. `sql "FROM traces"` on a metrics-only dir → 0 rows, not
a catalog error). Verified enabler: **cross-reader tolerance** — any reader on any present file yields its
typed-empty schema with no error, so any present chunk is a universal schema probe (still drift-free). Requires
decoupling "relation exists" (always, for sql) from "signal has data" (row-count, for the FR-3/FR-18/FR-19
friendly/gap-naming logic).

**✅ DONE & verified by running otelq (2026-06-26):** all 7 relations resolve-empty when any telemetry is
present. Implementation: `_schema_probe_chunk` (one crash-safe chunk from any present stream as a universal,
drift-free schema probe), `_seed_absent_relations` (seeds all absent base signals empty on hot/cold/connect),
and `_has_rows()` row-count presence used by `_present_signals`/`_no_signal_msg`/`_require`/`cmd_summary`/`cmd_errors`.
Verified symmetric: metrics-only / traces-only / logs-only → absent relations 0 rows (no error), cached==--no-cache;
`summary` shows only present signals (FR-3 ✓); slow/trace/metric/logs name the gap with the present signal (FR-19 ✓);
truly-empty dir → friendly _NO_TELEMETRY_MSG, exit 0 (FR-18 ✓); demo full skeleton intact (traces 2 buckets, 6 log
levels, 4 metric types); keystone cached==--no-cache across all commands; ruff clean; **76 tests pass**.
This fully **resolves N3** (and extends it well beyond the original metrics scope).

**✅ Truly-empty residual CLOSED (2026-06-26).** Added an embedded minimal gauge-only OTLP line
(`_EMBEDDED_PROBE_LINE` / `_embedded_probe_chunk`) used as a last-resort schema probe when no telemetry file
exists, so even a brand-new empty dir resolves ALL seven relations to 0 rows (verified, cached==--no-cache),
never a catalog error. Still drift-free: the column types come from the live extension reading the sample —
it is a schema *probe*, not a hard-coded schema. The friendly empty-telemetry message is preserved purely by
the row-count `_has_rows` presence checks (summary/slow/errors on an empty dir → `_NO_TELEMETRY_MSG`, exit 0).
Verified by running otelq; ruff clean; **82 tests pass** (incl. `test_empty_dir_resolves_empty_and_stays_friendly`).

### N4 — regression tests for the original 6 bugs ✅ DONE
Added one focused, behavior-pinning test per bug (each would fail if its fix were reverted; I read them to confirm):
`test_bug1_metrics_gauge_and_sum_queryable_cache_equals_nocache`, `test_bug2_slow_top_negative_rejected_at_parse`,
`test_bug3_logs_grep_is_literal_substring`, `test_bug4_dir_pointing_at_file_is_friendly`,
`test_bug5_empty_sql_query_is_friendly`, `test_bug6_logs_order_deterministic_across_cache_paths`.

The everything-else verdict: the tool is **solid** — friendly empty/missing-signal messages, gap-naming,
timestamp correction, oversized/truncated-line skipping, exit-code discipline, format-independence, help
affordances, and the cache keystone all behave per spec.

---

## Test results

| # | Command (as run) | Category | Expected | Observed | Status |
|---|------------------|----------|----------|----------|--------|
| 1 | `otelq` (bare) | help FR-22 | full help, exit 0 | full help + "GLOBAL flags", exit 0 | ✅ |
| 2 | `otelq help` | help FR-22 | full help, exit 0 | same, exit 0 | ✅ |
| 3 | `otelq help slow` | help FR-22 | slow help (`--top`), exit 0 | shows `--top`, exit 0 | ✅ |
| 4 | `otelq help not-a-command` | help/EC-13 | invalid-choice, exit 2 | "invalid choice", exit 2 | ✅ |
| 5 | `otelq -h` | help | help, exit 0 | exit 0 | ✅ |
| 6 | `otelq help help` | help FR-22 | help cmd help, exit 0 | exit 0 | ✅ |
| 7 | `otelq slow -h` | help | exit 0, `--top` | exit 0 | ✅ |
| 8 | `otelq help sql/trace/metric` | help FR-22 | positional help, exit 0 | exit 0, shows args | ✅ |
| 9 | `otelq help doctor/troubleshoot/collector-config` | help | exit 0 | exit 0 (usage only) | ✅ |
| 10 | `otelq errors --format json` | arg-order EC-5 | reject, exit 2 | "unrecognized", exit 2 | ✅ |
| 11 | `otelq --format json errors` | arg-order FR-11 | ok, exit 0 | json, exit 0 | ✅ |
| 12 | `otelq --format xml summary` | FR-10 | invalid choice, exit 2 | exit 2 | ✅ |
| 13 | `otelq --all summary` | FR-13 | ok, exit 0 | exit 0 | ✅ |
| 14 | `otelq summary --all` | FR-11 | reject, exit 2 | "unrecognized", exit 2 | ✅ |
| 15 | `otelq summary --no-cache` | FR-11 | reject, exit 2 | exit 2 | ✅ |
| 16 | `otelq summary --dir x` | FR-11 | reject, exit 2 | exit 2 | ✅ |
| 17 | `otelq summary --since 10m` | FR-15 | (impl) ok | exit 0 | ✅ (impl) |
| 18 | `otelq --since 10m summary` | FR-11/FR-15 | SPEC: accept before subcmd | **rejected, exit 2** | ⚠️ see N1 |
| 19 | `otelq summary --since 10x/abc/10` | EC-6 | non-zero, names forms | exit 1, "use e.g. 10m" | ✅ |
| 20 | `otelq summary --since 2H` | FR-15 | accept (case-insens unit) | exit 0 | ✅ |
| 21 | `otelq trace ID --since 10m` | FR-11 | trace has no --since → reject | exit 2 "unrecognized" | ✅ (impl) |
| 22 | `otelq trace <id>` (real) | FR-6 | parent/child tree, depth indicator | depth 0→1 indented | ✅ |
| 23 | `otelq --dir C_tree trace <id>` | FR-6 | root→child→grandchild, sibling, orphan→root | exact tree, orphan promoted | ✅ |
| 24 | `otelq trace <unknown>` | EC-3 | friendly stderr, exit 0 | "no spans found…", exit 0 | ✅ |
| 25 | `otelq errors` (real json) | FR-4 | newest-first | 83 rows, all `log`, desc | ✅ |
| 26 | `otelq --dir C_errors errors` | FR-4 | span+log tagged, newest-first | span+2 logs tagged, desc | ✅ |
| 27 | `otelq slow` / `--top 3/0/100000` | FR-5 | ms unit, desc, N rows | 2000ms desc; 3/0/282 rows | ✅ |
| 28 | `otelq slow --top abc` | FR-5 | argparse int error, exit 2 | exit 2 | ✅ |
| 29 | `otelq slow --top -1` / `-5` | FR-5/robust | graceful | **raw traceback, exit 1** | ❌ BUG-2 |
| 30 | `otelq logs --level error/ERROR/Error` | FR-7 | case-insensitive | all → 49 | ✅ |
| 31 | `otelq logs --level fatal/info/warn/WARNING` | FR-7 | exact text, case-insens | 34/228/50/0 | ✅ |
| 32 | `otelq logs --service X` | FR-7 | exact match | demo→426, bogus→0 | ✅ |
| 33 | `otelq logs` (no filter) | FR-7 | all, newest-first | 426, desc | ✅ |
| 34 | `otelq logs --service+--level+--grep` | FR-7 | AND filters | 49 | ✅ |
| 35 | `otelq --dir C_grep logs --grep user_id` | FR-7 | substring → 1 | **2 (matched userXid too)** | ❌ BUG-3 |
| 36 | `otelq --dir C_grep logs --grep 100%` | FR-7 | substring → 1 | **2 (matched 100zz too)** | ❌ BUG-3 |
| 37 | `otelq metric gen` | FR-8 | time series asc | 229 pts asc, gauge | ✅ |
| 38 | `otelq metric <unknown>` | EC-3 | 0 rows, exit 0 | 0 rows, exit 0 | ✅ |
| 39 | `otelq sql … FROM metrics_sum` (HOT vs COLD) | FR-1/cache | identical | **err vs empty** | ❌ BUG-1 |
| 40 | `otelq sql "SELEKT 1"` / unknown table | EC-4 | `otelq: SQL error:`, exit 1 | clean error, exit 1 | ✅ |
| 41 | `otelq sql ""` (empty) | FR-9/EC-4 | friendly SQL error | **AttributeError traceback** | ❌ BUG-5 |
| 42 | `otelq sql "SELECT 1; SELECT 2"` | FR-9 | runs, last result | returns 2, exit 0 | ✅ |
| 43 | `metrics` view DISTINCT metric_type (both) | FR-1/AC-1 | {gauge, sum} | {gauge, sum} | ✅ |
| 44 | DROP/CREATE/INSERT via `sql` | INV-1 | raw files unmodified | byte-identical ✓ | ✅ |
| 45 | `otelq --dir EMPTY {summary,errors,slow,logs,metric,trace}` | EC-1/FR-18 | friendly stderr, exit 0, no TB | all friendly, exit 0 | ✅ |
| 46 | `otelq --dir ONLY-METRICS errors/slow/logs/trace` | EC-2/FR-19 | names the gap, exit 0 | "no … (present: metrics)" | ✅ |
| 47 | `otelq --dir MALFORMED summary` | EC-9/FR-21 | truncated line skipped | 3 traces, exit 0 | ✅ |
| 48 | `otelq --dir OVERSIZED summary` | EC-8/FR-20 | warn+skip, exit 0 | warning + 5 traces | ✅ |
| 49 | FR-16 far-future timestamp | EC-7 | year 2026 | `2026-06-25` | ✅ |
| 50 | `otelq --dir NONEXISTENT summary` | FR-18 | friendly, exit 0 | friendly, exit 0 | ✅ |
| 51 | `otelq --dir <FILE> summary` | robust | friendly | **NotADirectoryError TB** | ❌ BUG-4 |
| 52 | `otelq doctor` (ok/missing/empty/partial/badjson/wrongkey) | doctor | correct status+exit | all correct | ✅ |
| 53 | `otelq --format json doctor` | doctor | json + cache row | correct | ✅ |
| 54 | `otelq collector-config` | — | exporters/file/rotation/contrib | all present, exit 0 | ✅ |
| 55 | `otelq troubleshoot` | — | loop + fixes + doctor | all present, exit 0 | ✅ |
| 56 | table==json==csv (summary/errors/slow/metric/logs) | INV-3/EC-10 | identical rows+order | identical | ✅ |
| 57 | `--since 1m` < default(30m) < `--all` | FR-13/15 | narrows/widens | 96 < 282 < 774 | ✅ |
| 58 | `otelq … | head` (broken pipe) | robust | clean, no TB | no traceback | ✅ |
| 59 | summary UNSET row (C_unset) | EC-12 | INFO=1, UNSET=2, 6-level skel | exact | ✅ |
| 60 | keystone cached==--no-cache (summary/errors/slow/metric/trace) | cache | identical | identical | ✅ |
| 61 | keystone cached==--no-cache (`logs`) | cache | identical | **tie-order differs** | ❌ BUG-6 |

---

## Bugs found

### BUG-1 — `metrics_sum`/`metrics_gauge` queryable on `--no-cache` but NOT on the default cache path 🐞
**Severity:** medium (cache/no-cache divergence — violates the project's keystone invariant)
**Repro (with gauge-only metrics, as the bundled demo produces):**
```
otelq sql "SELECT count(*) FROM metrics_sum"             # exit 1: Catalog Error: Table ... does not exist
otelq --no-cache sql "SELECT count(*) FROM metrics_sum"  # exit 0: returns 0 (empty table)
otelq --all sql "SELECT count(*) FROM metrics_sum"       # exit 0: returns 0
```
**Why it's a bug:** otelq.py docstring promises *"Results are identical to a full raw re-scan (the cache is
a pure accelerator)."* Here a query **errors on cache, succeeds on cold** → not identical. Also violates
SPEC FR-1 ("must expose … metrics_gauge, metrics_sum") and the `--help` epilog that lists `metrics_sum`.
**Root cause:** `build_cold` materializes both `_all_metrics_gauge` and `_all_metrics_sum` (one possibly
empty) for the metrics stream; `_assemble_hot` only creates `_all_<signal>` when sealed/pending parquet
exists, so the absent sub-type's relation is never created on the hot path.
**Missed by suite because:** `test_ac11_cached_equals_no_cache` compares only built-in commands (which use
the `metrics` view), never a direct `sql … FROM metrics_sum`.
**Status:** ✅ FIXED (code-only, in `otelq.py`) & re-verified by running `otelq` — see "Fix phase" below.

### BUG-2 — `slow --top <negative>` dumps a raw DuckDB traceback 🐞
**Severity:** low–medium (user-reachable input → ugly traceback, against the tool's "never show a DuckDB
stack trace" ethos: FR-9/FR-18/INV-4)
**Repro:**
```
otelq slow --top -1      # exit 1: _duckdb.BinderException: LIMIT/OFFSET cannot be negative + full traceback
otelq slow --top -5      # same
```
**Root cause:** `cmd_slow` passes `args.top` straight into `... LIMIT ?`; DuckDB rejects negative LIMIT and
the exception is uncaught (unlike `cmd_sql`, which wraps `duckdb.Error`).
**Suggested fix:** validate `--top` (reject `< 0` via argparse `type=`/a check, or clamp to 0) with a
friendly message; or treat negative as "no limit". Keep `--top 0` → 0 rows (already fine).
**Status:** ✅ FIXED (code-only, in `otelq.py`) & re-verified by running `otelq` — see "Fix phase" below.

### BUG-3 — `logs --grep` treats `_` and `%` as wildcards, not literal (FR-7 says "substring") 🐞
**Severity:** low–medium (silently wrong results for grep terms containing `_`/`%` — common in log bodies:
paths, percentages, identifiers)
**Repro (corpus C_grep):**
```
otelq --dir C_grep logs --grep user_id   # 2 rows: matches "user_id …" AND "userXid …"  (should be 1)
otelq --dir C_grep logs --grep 100%      # 2 rows: matches "100% …" AND "100zzcomplete" (should be 1)
```
**Root cause:** `cmd_logs` builds `body ILIKE '%' || grep || '%'` without escaping ILIKE metacharacters,
so `_` (any char) and `%` (any run) act as wildcards.
**Suggested fix:** use a literal substring test — e.g. `contains(lower(body), lower(?))` or
`position(lower(?) IN lower(body)) > 0`, or `ILIKE ? ESCAPE '\'` with `_`/`%`/`\` escaped in the term.
**Status:** ✅ FIXED (code-only, in `otelq.py`) & re-verified by running `otelq` — see "Fix phase" below.

### BUG-4 — `--dir <regular-file>` crashes with a raw traceback (NotADirectoryError) 🐞
**Severity:** low (user misuse — pointing `--dir` at a file, e.g. `--dir telemetry/traces.jsonl`)
**Repro:**
```
otelq --dir telemetry/traces.jsonl summary
# NotADirectoryError: [Errno 20] ... '/…/traces.jsonl/.otelq-cache'  (full traceback, exit 1)
```
**Root cause:** `build_hot` → `_acquire_lock` does `cdir.mkdir(parents=True, exist_ok=True)` where
`cdir = <file>/.otelq-cache`; mkdir under a non-directory raises `NotADirectoryError` (uncaught).
`--no-cache` would avoid the cache mkdir but the default path crashes. `doctor` already guards with
`is_dir()` and fails gracefully.
**Suggested fix:** validate `--dir` is a directory (friendly error) up front, or wrap cache dir creation /
the build in a try that degrades to the friendly path. Note: `doctor`'s message says "does not exist"
for a path that exists-but-is-a-file — minor wording nit.
**Status:** ✅ FIXED (code-only, in `otelq.py`) & re-verified by running `otelq` — see "Fix phase" below.

### BUG-5 — `otelq sql ""` (empty query) dumps a raw traceback 🐞
**Severity:** low (trivial input → traceback; defeats the purpose of `cmd_sql`'s error handler)
**Repro:** `otelq sql ""` → `AttributeError: 'NoneType' object has no attribute 'description'` (exit 1).
**Root cause:** `conn.execute("")` returns `None`; `result.description` then raises `AttributeError`, which
is not a `duckdb.Error`, so `cmd_sql`'s `except duckdb.Error` misses it.
**Suggested fix:** guard `if result is None:` (treat as empty result / friendly "empty query" error), and/or
broaden the except. Should land on the same `otelq: SQL error:` path as other bad SQL (EC-4).
**Status:** ✅ FIXED (code-only, in `otelq.py`) & re-verified by running `otelq` — see "Fix phase" below.

### BUG-6 — `logs` (and latently `slow`/`metric`/`trace`) order differs cached vs `--no-cache` on tied keys 🐞
**Severity:** low (same rows, both newest-first; only the tie-break order differs — but it breaks the
project's `cached == --no-cache` keystone on realistic sub-millisecond data)
**Repro:** with the demo data, `otelq logs` vs `otelq --no-cache logs` → identical 426-row multiset, both
newest-first, but two rows sharing `…16:52:58.325000` appear in opposite order (hot=Info-first,
cold=Warn-first).
**Root cause:** `ORDER BY timestamp DESC` (logs/metric: `ASC`) with no deterministic tie-breaker; hot
(parquet) and cold (raw) yield different orders for equal-key rows. The suite's
`test_ac11_cached_equals_no_cache` only uses distinct timestamps, so it never sees a tie.
**Suggested fix:** append a stable secondary sort key to the relevant `ORDER BY` clauses (e.g.
`timestamp DESC, trace_id, span_id`/`body`, or all selected columns) so both paths emit identical order.
FR-7/FR-8 only require primary order, so this is safe; must not change which rows are returned.
**Status:** ✅ FIXED (code-only, in `otelq.py`) & re-verified by running `otelq` (logs/slow/metric/errors/summary
all byte-identical cached vs `--no-cache`) — see "Fix phase" below.

## Fix phase & verification

All six fixes were made **in `otelq.py` only** (code-only, no test files touched, left **uncommitted**) by a
sub-agent, then **independently re-verified by me running `otelq`**:

| Bug | Fix (function) | Post-fix behavior (verified by running otelq) |
|-----|----------------|-----------------------------------------------|
| BUG-1 | `_finalize_relations` — drop an in-window-empty metric sub-relation on **both** paths | `metrics_sum` query now identical cached vs `--no-cache` (keystone restored); `metrics`/`metrics_gauge` & `C_onlymetrics` (gauge+sum) unchanged |
| BUG-2 | new `_non_negative_int` argparse `type` on `--top` | `slow --top -1/-5` → `error: argument --top: must be >= 0`, exit 2, **no traceback**; `--top 0`→0 rows; `--top abc`→exit 2 |
| BUG-3 | `cmd_logs` → `contains(lower(body), lower(?))` | `--grep user_id`/`100%` → 1 row each; still case-insensitive (`USER_ID`→1) |
| BUG-4 | `run_command` early `--dir` is-a-directory guard | `--dir <file>` → `otelq: --dir '…' is not a directory`, exit 1, **no traceback** |
| BUG-5 | `cmd_sql` guards `result is None` | `sql ""`/`"   "` → `otelq: SQL error: empty query`, exit 1, **no traceback** |
| BUG-6 | tie-breakers in `cmd_logs`/`cmd_slow`/`cmd_metric` ORDER BY + `cmd_trace` child sort | `logs`/`slow`/`metric`/`errors`/`summary` byte-identical cached vs `--no-cache` |

**Regression gate (independently re-run):** `uvx ruff check .` → clean; full suite → **70 passed** (unchanged).
**Regression sweep (real-data CLI):** summary 9 rows (282/426/229), errors 83, slow 20@2000 desc, logs
49/34/228/50, metric 229 asc, trace tree [0,1], doctor/collector-config/troubleshoot/help/arg-order all
unchanged. No regressions.

### ⚠️ BUG-1 — residual decision for you (N3)
The agent took the **lower-risk** direction (make cold match hot: both paths *omit* a metric sub-relation that
has no rows). This **fully restores the cache keystone** (cached == `--no-cache`), which was the actual bug.
But it does **not** close the SPEC FR-1 / `--help` gap: with gauge-only data (as the bundled demo produces),
`otelq sql "SELECT * FROM metrics_sum"` now **errors consistently on both paths** instead of returning an
empty table. The FR-1-faithful alternative (always expose `metrics_gauge`+`metrics_sum`, empty if no rows) was
judged too invasive for the hot path's parquet-only assembly (no schema source for the absent sub-type
without re-reading raw). **Decision for you:** accept "consistent error" (current), or invest in exposing the
empty relations (and/or soften FR-1/help wording to "whichever metric types are present").

## Notes (non-bug)
- **Harness artifact (NOT a bug):** an earlier batch showed `metric`/`trace` with `--dir` returning exit 2;
  direct reproduction shows they work (exit 0, friendly). Was a shell word-split in my loop, not otelq.
- **Regression-test reconciliation (N4):** you first answered "add a regression test per bug," then clarified
  "test by running otelq, not writing new unittests." I followed the **later** instruction: fixes are
  code-only and verified via the CLI; **no new unit tests were written**. The existing 70 tests still pass and
  guard against breaking old behavior, but the 6 fixes themselves are **not** pinned by a regression test. If
  you'd like, each bug above is a ready-made candidate — say the word and I'll add focused tests.
- **`sql` runs DDL/DML on the in-memory connection** (e.g. `DROP TABLE traces` exits 0) — harmless: verified
  INV-1 holds (raw `*.jsonl` byte-identical before/after). Mentioning only in case you want `sql` read-only.

### N2 — A line that is valid JSON but invalid OTLP crashes the whole run with a raw traceback ⚠️
otelq's robustness guards skip **invalid-JSON** lines (FR-21) and **oversized** batches (FR-20), but a line
that is *valid JSON yet not decodable as OTLP* (e.g. a non-hex trace id, or a payload the pinned
`duckdb-otlp` extension rejects) makes `read_otlp_*` throw an uncaught `_duckdb.IOException` → full
traceback, exit 1 (and the cold-fallback then crashes too). A conformant Collector won't emit such a line,
so this needs non-conformant/corrupt input to trigger.

**How it was triggered (full disclosure):** this was *not* hit through the real pipeline — it surfaced from a
**mistake in my own test fixtures**: I hand-wrote JSONL (`scratchpad/gen_corpora.py`) with non-hex
`traceId`/`spanId` values and pointed `--dir` at them, bypassing the Collector. Hex-encoding the ids (what
telemetrygen/the Collector actually emit) fixed the fixtures. Unlike the 6 bugs + N1 — all reachable through
normal supported usage — N2 is only reachable by injecting non-conformant data.

**✅ RESOLVED (2026-06-25) — WONTFIX, out of scope (code left as-is).** Per
[CONTRACT-telemetry-directory], producing valid OTLP is the **Collector's** responsibility; otelq's
robustness guards (FR-20 oversized, FR-21 torn trailing line) are scoped to specific *real-world* cases, not
arbitrary corrupt content. Realistic paths don't reach it: a torn write yields *invalid JSON* (already
skipped by FR-21); version drift is prevented by the pinned `duckdb==1.5.3`/extension + CI probe + `doctor`;
on-disk byte corruption is the only semi-plausible trigger. Decision (user, 2026-06-25): leave as-is and
document as out-of-scope. (If ever desired, defense-in-depth would be a per-chunk `try/except duckdb.Error`
that skips+warns like the oversized-batch path — but it could mask genuine data problems.)

## Notes / open questions for the user

### N1 — `--since` is classified inconsistently across the docs (SPEC contradicts itself) ⚠️
The **SPEC's Definitions, FR-11, and FR-15 call `--since` a _global_ flag** that "must be accepted
*before* the subcommand," and FR-11 states a global flag placed *after* the subcommand "must be
rejected." But the **implementation, the `--help` epilog, the README, and the SPEC's own _Examples_
section** all treat `--since` as a _subcommand_ flag (placed *after*). Verified behavior:
- `otelq summary --since 10m` → works (exit 0)
- `otelq --since 10m summary` → **rejected (exit 2)** — i.e. exactly backwards from FR-11/FR-15 prose.

**✅ RESOLVED (2026-06-25) — promoted `--since` to a global flag** (user chose this over the docs-only
option, because the usability fix they wanted — `--since` in the top-level usage line — is only achievable
without hand-maintained help text by making it global; and `--since` is the semantic twin of `--all`, which
is already global). Now: `otelq --since 10m errors` works (before the subcommand); `otelq errors --since 10m`
is rejected (exit 2), consistent with `--dir/--format/--all/--no-cache`. The SPEC's Definitions/FR-11/FR-15
were already written this way, so they became correct as-is; only the SPEC **Examples** line, the `--help`
epilog (now with a dedicated "time window" section), the README help-dump, `AGENTS.md`, and
`SPEC-otelq-incremental-cache.md` examples needed flipping, plus **one existing test** edited
(`test_ac8_since_beyond_window_is_cold`: moved `--since` before the subcommand). Verified by running otelq
(usage line shows `[--since SINCE]`; before/after/malformed/windowing all correct); ruff clean; 70 tests pass.
