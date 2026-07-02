---
doc_type: ephemeral
authoritative: false
stability: evolving
status: draft
decision_scope: architecture
audience:
  - ai
  - engineering
must_not_contain:
  - product_requirements
created: 2026-07-02
last_updated: 2026-07-02
related_documents:
  - PRD-otelq
  - SPEC-otelq-cli
  - SPEC-otelq-incremental-cache
  - CONTRACT-telemetry-directory
retrieval_priority: low
ai_summary: Point-in-time architecture review of otelq — bugs, spec drift, performance and functional improvement candidates. Not authoritative; findings graduate into SPECs/ADRs or issues.
---

# Architecture Review — otelq (2026-07-02)

Scope reviewed: `otelq.py` (CLI + incremental cache), `justfile`, `compose.yaml`,
`compose.demo.yaml`, `otel-collector-dev.yaml`, `README.md`, and the governing
context docs (PRD, both SPECs, CONTRACT, ADR index). Findings are ordered by
severity within each section. This is an ephemeral working document — each
accepted finding should graduate into a SPEC update, ADR, or fix; rejected ones
should be marked as such.

---

## Resolution status (2026-07-02)

All findings below have been **resolved** in `otelq.py` and its tests, with the
behavioral changes graduated into the SPECs (append-only):

- **B-1** — event-time anchor clamped to `wall_clock + MAX_FUTURE_SKEW` on both
  paths (SPEC-cache INV-7, EC-12/EC-13, AC-21; doctor clock-skew check
  SPEC-cli FR-26/EC-19/AC-35).
- **B-2** — `metric` no longer widens on an empty hot result (SPEC-cache FR-10,
  EC-14, AC-22).
- **B-3, B-7, B-8, B-9, B-11** — fixed in code/help; lock-reaper safety graduated
  (SPEC-cache FR-12, EC-17, AC-26).
- **B-4** — cursor entry carried forward on transient open failure (SPEC-cache
  FR-3, EC-16, AC-24).
- **B-5** — SQL literal escaping for all spliced paths (SPEC-cli FR-28, EC-21,
  AC-36).
- **B-6** — `size` dropped from the identity key + shrink-below-offset re-read
  (SPEC-cache FR-3, EC-15, AC-23).
- **B-10** — eviction runs for every signal with sealed partitions (SPEC-cache
  FR-6, AC-25).
- **D-1** — neutral no-telemetry hints. **D-2** — `sql` file-access boundary
  documented + built-ins locked down (SPEC-cli FR-27, EC-20, AC-34). **D-3** —
  connections closed via `contextlib.closing`. **D-4** — `doctor` extended
  (SPEC-cli FR-26, AC-35).
- **P-1** — hot relations as parquet-backed views. **P-2** — cheaper cold row
  bound (P-3 subsumed by P-2: a bounded newest-first trace fallback would break
  FR-6 full-tree and FR-11 equivalence, so it is intentionally not implemented).
  **P-4** — presence computed once per connection. **P-5** — compact `json` +
  `jsonl` (SPEC-cli FR-10, INV-2, AC-31).
- **F-1** — `--top` on errors/logs/metric + truncation notice (SPEC-cli FR-23).
  **F-2** — `--since` seconds unit (SPEC-cli FR-15). **F-3** — `--version`
  (SPEC-cli FR-24). **F-4** — trace-id prefix lookup (SPEC-cli FR-6). **F-5** —
  `--verbose` route metadata (SPEC-cli FR-25).

---

## 1. Bugs & errors

### B-1 (HIGH) — A single far-future timestamp poisons the watermark and every default query
`max_event_ts_seen` is the max event-time **ever** observed and only ratchets
forward (`_ingest_and_seal`), and *all* time windows — hot and cold — are anchored
to max observed event-time (`_finalize_relations`, SPEC-cache INV-7). One record
with a bogus future `timeUnixNano` (clock-skewed producer, bad instrumentation,
unit mistake — exactly the errors otelq exists to catch):

- jumps `hot_floor` into the future → `_evict_signal` deletes the entire sealed cache;
- makes every real record fall outside the default window → `summary`, `errors`,
  `logs`, … return the friendly "no telemetry" message while telemetry plainly exists;
- is unrecoverable by `otel-clean` alone, because the cold path anchors to the same
  bad max as long as the record remains in the raw files.

The event-time anchoring is a deliberate design (EC-12), but it has no defense
against implausible timestamps. Mitigations to consider: clamp the window/watermark
anchor to `wall_clock + tolerance`; or anchor to a high percentile rather than max;
at minimum have `doctor`/`summary` warn when max event-time is far ahead of wall
clock. Needs a SPEC amendment either way.

### B-2 (HIGH) — `metric` fallback silently ignores `--since` and the default window
`run_command` re-runs any `HOT_THEN_COLD` command with `Plan("COLD", None, True)`
whenever the windowed result is empty. For `trace` that is specified behavior. For
`metric` it means:

- `otelq --since 10m metric x` returns rows **outside** the 10-minute window when
  none exist inside it — violating SPEC-otelq-cli **FR-15** ("--since must restrict
  the query to a trailing window");
- a plain `otelq metric x` silently escapes the default hot window, violating
  SPEC-cache **FR-9**, and **FR-10** allows widening for `metric` only "when
  explicitly requested".

Fix: only fall back for `trace`; or fall back for `metric` solely when neither
`--since` nor the default window was user-relevant — and preserve the window in the
fallback plan (`Plan("COLD", plan.window, …)`).

### B-3 (HIGH) — `just otel-clean` does not stop the demo Collector
The recipe stops only a container matching `^otel-collector$`, but the demo project
(`compose.demo.yaml`) names its container `otelq-demo-collector`. After
`just otel-demo`, running `just otel-clean` truncates the active `*.jsonl` files
**while the demo Collector still holds them open** — exactly the NUL-hole /
orphaned-fd footgun the recipe's own comment warns about. Fix: stop both container
names (or `docker ps -q -f name=collector` across both projects).

### B-4 (MEDIUM) — Cursor-entry loss on a transient open failure duplicates rows
`_iter_stream_delta` skips a file on `OSError` and the caller then **replaces** the
stream's `files` state with `new_state_out`, which lacks the skipped file's key. The
next run re-reads that file from offset 0. Rows in already-sealed minutes are
removed by the `EXCEPT ALL` in `_rewrite_pending`, but rows in **unsealed** minutes
now exist twice in `_stage_<signal>` (once from the pending-parquet arm, once from
the re-read delta) and are written to pending twice — breaking FR-11/INV-4
(cache result ≠ raw re-scan). Fix: carry forward prior `FileState` entries whose
files could not be opened (or whose identity could not be computed) instead of
dropping them.

### B-5 (MEDIUM) — Quote-unsafe path interpolation into SQL
Every file path reaching DuckDB is spliced into SQL string literals:
`_materialize`, `_build_staging`, `_assemble_hot`, `_seal_signal` (`COPY … TO
'{tmp}'`), `_sql_file_list`, `_seed_absent_relations`. A telemetry dir containing a
single quote — common on macOS ("Robert's Mac…", `it's-a-demo/`) — breaks every
command with an opaque SQL syntax error, and technically permits SQL injection via
`--dir` (low impact locally, but it is an OWASP-class injection seam). Fix: use
parameter binding where DuckDB allows it (`read_parquet(?)`, `read_otlp_*(?)`) or a
single `escape_sql_literal` helper (`'` → `''`) applied at every splice point.

### B-6 (MEDIUM) — Spec/code drift on raw-file identity (`size` omitted)
SPEC-cache **FR-3** requires identifying a raw file by
`(inode, first-256-byte fingerprint, size)`. `_file_identity` deliberately drops
`size` (documented in-code: "it grows, which would churn the key"). The code
rationale is sound, but the authoritative SPEC now contradicts the implementation.
Also note the residual hole the SPEC's `size` was guarding: a file truncated and
rewritten whose first 256 bytes happen to be identical resumes at a stale offset →
silent data loss. Either amend FR-3 (and record the truncation caveat), or include
a "shrunk below stored offset ⇒ treat as new file" check, which is cheap
(`st_size < bytes_consumed` ⇒ reset offset) and closes the hole entirely.

### B-7 (LOW) — `errors` ordering is non-deterministic on timestamp ties
Every other command adds explicit deterministic tie-breakers so hot (parquet) and
cold (raw) results are byte-identical (FR-11). `cmd_errors` runs two `SELECT`s with
**no ORDER BY** and then a stable Python sort on `timestamp` alone — equal-timestamp
rows keep engine scan order, which may differ cached vs `--no-cache`. Fix: sort key
`(timestamp, kind, service_name, label, detail)` or ORDER BY in SQL.

### B-8 (LOW) — `_reap_tmp` can delete a live writer's temp file
`*.tmp` files older than `_LOCK_STALE_SECS` (120 s) are reaped by **any** run,
including lock losers, while a legitimate lock holder is allowed to run for up to
`_LOCK_HARD_STALE_SECS` (3600 s) — e.g. a catch-up seal over a large corpus. A
single `COPY` that takes >120 s between mtime updates can have its temp file
unlinked mid-write. Low probability, but the in-code comment ("a concurrent
writer's in-flight temp file is never reaped") overpromises. Fix: skip `_reap_tmp`
when the lock is currently held by a live pid, or reap only without an active lock.

### B-9 (LOW) — `_release_lock` unlinks by name unconditionally
If a holder wedges past the 3600 s hard ceiling, another process reaps and
re-acquires; when the original holder later resumes, its `finally` unlinks the
**new** holder's lock file → two concurrent writers become possible (INV-5
violation). Fix: before unlink, verify the lock file still contains our pid.

### B-10 (LOW) — Eviction and pending-rewrite skipped for idle signals
`_ingest_and_seal` seals/evicts only signals in `staged`. A signal that stops
receiving data retains its sealed partitions beyond RETENTION until it next
receives data (SPEC-cache FR-6 says partitions older than the hot window "must be
removed"). Bounded in size, unbounded in time; also means the `metrics` view can
carry stale sub-relations. Cheap fix: run `_evict_signal` for all signals whose
stream has a watermark, not only staged ones.

### B-11 (LOW) — README errors
- The plain-Docker-Compose demo block prints commands like
  `uv run otelq.py otelq summary` — doubled `otelq`; should be `uv run otelq.py summary`.
- "This is a dump from running `uvx otelq.py --help`" — wrong invocation
  (`uvx otelq` or `uv run otelq.py --help`), and the dump leaks a machine-specific
  default (`--dir DIR … default: /Users/robertgartman/dev/otelq/telemetry`).
- Dangling sentence: "Run  the full, authoritative command behavior." (missing the
  command name, double space).
- The help epilog (and its README copy) omits `severity_number` from the `logs`
  view cheat-sheet, though FR-2 exposes it and `summary` depends on it.

---

## 2. Design concerns

### D-1 — Repo-specific hints leak into the generic CLI
`_NO_TELEMETRY_MSG` says "is the collector running (**just otel-up**) and
**OTEL_ENABLED=true**?" and `_no_signal_msg` cites "**see `just otel-clean`**".
`render_troubleshooting` is deliberately project-agnostic, but the two most common
error paths are not — in an integrated target project (the primary deployment per
README/ADR-007) these hints reference recipes and an env var that don't exist
there, actively misleading agents. This also brushes against the AGENTS.md
"no solution-name leakage / keep code neutral" rule. Fix: make the hints neutral
("start the Collector that writes this directory; run `otelq troubleshoot`"), keep
repo-specific wording in the repo's own skill/docs.

### D-2 — Arbitrary SQL is an accepted-but-undocumented capability boundary
`otelq sql` hands raw SQL to an in-process DuckDB that can read **and write** the
local filesystem (`COPY TO`, `read_csv('~/…')`, `INSTALL`). For a local dev tool
running as the invoking user this is acceptable, but nothing documents it; an AI
agent driving otelq has effectively a file-write primitive. Worth one line in the
SPEC/help ("sql executes with your user's file access") and possibly
`SET enable_external_access=false` + `LOCK_CONFIGURATION` for non-`sql` commands.

### D-3 — Connections are never closed
`run_command` opens a second connection on the fallback path and abandons the
first; `_dispatch` never closes any. Harmless today (short-lived process,
in-memory DB) but sloppy for a library-shaped module the test suite imports.
`contextlib.closing` / try-finally would do.

### D-4 — `doctor` doesn't cover the failure modes that actually bite
It validates dir existence and first-line framing, but not: cache-dir writability,
a stale lock file, cursor version, or the B-1 condition (max event-time far beyond
wall clock — the one failure that makes every query return nothing with no
explanation). doctor is the natural home for all of these.

---

## 3. Performance improvements

### P-1 — Double materialization per query on the hot path
Every hot query copies the whole hot window twice: parquet → `_all_<signal>`
tables (`_assemble_hot`) → windowed final tables (`_finalize_relations`). Both
could be **views**; DuckDB then does one lazy scan of the parquet with predicate
pushdown of the window filter. For 30 minutes of chatty telemetry this is the
dominant per-invocation cost after extension load. (The cold path must
materialize — the temp chunk files vanish — but the hot path need not.)

### P-2 — Cold scan pays full Python JSON parse plus corpus rewrite
`_chunk_signal`/`_buffer_to_chunks` `json.loads` **every line** of the corpus in
Python (for `_count_rows`) and rewrite the entire corpus into temp chunk files —
2× I/O and Python-side JSON decode of potentially 250 MB+ (50 MB × 5 backups)
per signal, on every `--all`, `--since > 30m`, or `trace`/`metric` cold fallback.
Options: a cheaper row-count bound (e.g. count `"timeUnixNano"` / `"spanId"`
substring occurrences — conservative, no full decode); chunk by byte budget with a
records-per-byte estimate and only fully decode lines near the budget; or persist
a per-file line/row index next to the cache so repeated cold scans skip the count.

### P-3 — `trace` misses always trigger a full-history cold scan
`trace <id>` on the hot path that misses re-scans **all** raw history (P-2 cost).
Since trace ids queried in the inner loop are nearly always recent, a bounded
fallback first (e.g. newest rotated backup only, then full) would keep the common
miss cheap. Minor; acceptable if P-2 lands.

### P-4 — Repeated `count(*)` presence probes
`cmd_summary` + `_has_rows` + `_require` issue several `count(*)` round-trips per
command before the real query. Trivial per-call cost on materialized tables, but
if P-1 turns relations into parquet-backed views, each probe becomes a scan —
compute presence once per connection and cache it.

### P-5 — `--format json` uses `indent=2`
Pretty-printed JSON roughly doubles token count for the tool's primary consumer
(AI agents, per the PRD's token-efficiency goal). Default to compact separators
(or add `--format jsonl`), keep `table` for humans.

---

## 4. Functional improvements

### F-1 — No output bounds on `errors`, `logs`, `metric`
`slow` has `--top`, but `errors`/`logs`/`metric` return every matching row —
against a chatty 30-minute window this floods an agent's context (PRD:
"token-efficient"). Add `--top/--limit` with a sane default and a truncation
notice on stderr.

### F-2 — `--since` lacks a seconds unit
Tight agent loops ("what did the last run emit?") want `--since 30s`; today the
floor is `1m`. Trivial to add to `_parse_since` and the help text.

### F-3 — No `--version`
A pinned, PyPI-distributed tool that agents drive should report its own version
(`otelq --version`), especially given the DuckDB/extension pin governance
(ADR-003).

### F-4 — `trace` could accept a trace-id prefix
Agents copy trace ids from logs/summary output; a unique-prefix lookup (like git
short hashes) with an ambiguity error would remove a whole class of copy/paste
failures. Optional.

### F-5 — Surface window/route metadata on request
Agents cannot currently tell *which* window a result covered (hot default vs
`--since` vs the silent `metric` widening of B-2). A `--verbose`/stderr one-liner
("window: last 30m by event-time, route: hot") would make results
self-describing and make B-1/B-2-class surprises diagnosable.

---

## 5. Positive observations (no action)

- The 2048-row crash workaround, timestamp ×1000 fix, and expose-empty relation
  seeding are all carefully centralized with single-home helpers (`_decode_line`,
  `_TS_FIX`, `_seed_absent_relations`) — the drift-resistance discipline is real.
- Friendly-failure behavior (exit 0 + stderr guidance, gap-naming vs
  collector-blaming) matches the SPEC precisely and is unusually thoughtful.
- The cursor/seal/pending design (immutable partitions, `EXCEPT ALL` late-arrival
  reconciliation, event-time watermarks) is sound and well-commented; the SPECs'
  acceptance criteria genuinely cover it.
- Lock design (O_EXCL sentinel, never blocks, reader never waits) fits the
  CLI-only constraint well, modulo B-8/B-9 edge cases.
- `collector-config`/`doctor` being generated from the same pinned constants the
  reader uses is exactly the right anti-drift move.
