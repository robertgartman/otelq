# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.3"]
# ///
# duckdb is pinned exactly because otelq depends on the `otlp` *community*
# extension (smithclay/duckdb-otlp), which is built per DuckDB version and lags
# new releases. An open `>=` floats to the newest DuckDB (e.g. 1.5.4), for which
# the extension is not yet published — `INSTALL otlp FROM community` then 404s
# and every otelq command fails. 1.5.3 is the latest version with the extension
# built (community-extensions.duckdb.org/v1.5.3/<platform>/otlp...). Bump this
# only after confirming the extension exists for the target version.
"""otelq — query OTLP telemetry captured by the dev OTel Collector.

Reads .telemetry/*.jsonl (OTLP JSONL written by the Collector fileexporter)
via the smithclay/duckdb-otlp DuckDB extension.

Incremental parquet cache (SPEC-otelq-incremental-cache)
--------------------------------------------------------
Repeated queries no longer re-parse the whole .telemetry/ corpus. A per-signal
cursor reads only the new bytes of each raw file; complete minutes are sealed to
parquet under .telemetry/.otelq-cache/<signal>/<minute>.parquet once the signal's
max observed event-time has advanced MARGIN_MINUTES past the minute's end. A
rolling RETENTION_MINUTES window bounds the cache; queries reaching older data
fall back to a stateless cold scan of the raw files. Results are identical to a
full raw re-scan (the cache is a pure accelerator); pass --no-cache to force the
cold path. See context/spec/SPEC-otelq-incremental-cache.md.

2048-row workaround
-------------------
The duckdb-otlp extension's read_otlp_* table functions corrupt memory and
crash when a single call returns more than 2048 rows (DuckDB's
STANDARD_VECTOR_SIZE): the Rust backend writes the whole result into one
output vector instead of yielding 2048-row chunks. otelq works around it by
slicing each signal's JSONL into <=_SAFE_CHUNK-record pieces and giving every
piece its own read_otlp_* call, then materialising the unioned result into a
table. No single call ever crosses the boundary.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import weakref
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

# duckdb is imported lazily inside the three functions that actually open a
# connection (connect/build_connection) or catch its errors (cmd_sql), so commands
# that never touch it — doctor, collector-config, --help — don't pay its ~40 ms
# import. Module-wide, only the type annotations need the symbol, and those are
# strings under `from __future__ import annotations`, resolved by this
# type-checking-only import (no runtime cost).
if TYPE_CHECKING:
    import duckdb

# otelq's own version, reported by `otelq --version` (F-3). Kept in lockstep with
# the packaged version in pyproject.toml (a test asserts they match), so an
# agent driving otelq — and the DuckDB/extension pin governance of ADR-003 — can
# report exactly which build it is talking to.
__version__ = "0.3.0"

# Public surface of this single-file module. The CLI entry is `main`; the rest
# is the API the test suite pins. The trailing group is deliberately exported
# despite the leading underscore: it is internal to normal callers but part of
# the behaviour the tests verify directly (lock reaping, the no-telemetry text),
# so listing it here makes that test-visible contract explicit.
__all__ = [
    "main",
    "__version__",
    "build_parser",
    "build_connection",
    "connect",
    "run_command",
    "format_output",
    "create_unified_metrics_view",
    "parse_minute_key",
    "minute_key",
    "sealed_path",
    "pending_path",
    "stream_of",
    "Plan",
    "NoTelemetryError",
    "cmd_summary",
    "cmd_sql",
    "cmd_errors",
    "cmd_slow",
    "cmd_trace",
    "cmd_logs",
    "cmd_metric",
    "render_collector_config",
    "doctor_report",
    "CACHE_DIRNAME",
    "PENDING_DIRNAME",
    "CURSOR_FILENAME",
    "CURSOR_SCHEMA_VERSION",
    "LOCK_FILENAME",
    # tested internals
    "_reap_if_stale",
    "_LOCK_STALE_SECS",
    "_LOCK_HARD_STALE_SECS",
    "_NO_TELEMETRY_MSG",
    "_parse_since",
    "_seal_external_access",
    "_future_ceiling",
    "_fmt_ts",
]

# Default to ./.telemetry under the *current working directory* so the zero-config
# default works the same whether otelq is `uv run otelq.py` from a checkout (run
# from the repo root) or installed via uvx/pipx and run from a project dir. A
# script-relative default (Path(__file__).parent) would resolve into the install
# location — e.g. site-packages — for an installed copy. `--dir` overrides.
# The leading dot marks it as transient, not-git-tracked runtime capture.
DEFAULT_DIR = Path.cwd() / ".telemetry"

SIGNAL_GLOBS = {
    "traces": "traces*.jsonl",
    "logs": "logs*.jsonl",
    "metrics": "metrics*.jsonl",
}

# --- Reference producer (collector-config / doctor) --------------------------
# The Collector that writes the .telemetry/ contract is interchangeable (see
# CONTRACT-telemetry-directory: any conformant producer interoperates). otelq
# ships the reference producer settings here so `otelq collector-config` emits
# exactly the pinned values the consumer expects, generated — never hand-copied
# — so they cannot drift from this module. These MUST stay in lockstep with
# otel-collector-dev.yaml and the contract; a test asserts they match.
COLLECTOR_MOUNT_PATH = "/.telemetry"  # producer-side bind-mount target
ROTATION_MAX_MEGABYTES = 50  # rotation threshold per active file (< 100 MB reader cap)
ROTATION_MAX_BACKUPS = 5  # retained rotated backups per signal
# The OTLP/JSON top-level key each signal's lines must carry (contract framing).
OTLP_TOP_LEVEL_KEY = {
    "traces": "resourceSpans",
    "logs": "resourceLogs",
    "metrics": "resourceMetrics",
}

def _rows_upper_bound(line: str) -> int:
    """A cheap, sound UPPER bound on the read_otlp_* rows a JSONL line yields,
    computed WITHOUT parsing it (P-2).

    Every span, log record, and metric data point — i.e. every output row —
    carries at least one `*[Tt]imeUnixNano` key (spans and sum/histogram points
    carry two), and `"imeUnixNano"` is the shared substring of all of them
    (timeUnixNano, startTimeUnixNano, endTimeUnixNano, observedTimeUnixNano). So
    this count is never below the true row count; it can only overcount, which
    keeps chunk sizing conservative (smaller, never oversized) and is safe as the
    gate that decides whether a line even needs a real json.loads. A stray
    occurrence inside a string value only inflates the bound, at worst forcing an
    exact re-count via _decode_line — never a crash or a dropped row."""
    return line.count("imeUnixNano")


# read_otlp_* corrupts memory above 2048 output rows; _SAFE_CHUNK leaves a
# margin so a chunk can never reach the boundary. _CRASH_LIMIT is the boundary
# itself — a single export batch larger than this cannot be chunked (it is one
# indivisible JSON object) and is skipped with a warning rather than crashing.
_SAFE_CHUNK = 2000
_CRASH_LIMIT = 2048

# The duckdb-otlp extension stores timestamps as nanoseconds in a TIMESTAMP_MS
# column, so DuckDB reads the value 1000x too large (year 58358 instead of 2026).
# This SELECT-REPLACE corrects the timestamp column by dividing by 1000.
_TS_FIX = "make_timestamp(epoch_us(timestamp) // 1000) AS timestamp"

# --- Cache model (SPEC-otelq-incremental-cache) ------------------------------
# Three raw byte-streams feed six cache signals: the single metrics*.jsonl
# stream is read by four readers (gauge, sum, histogram, exp_histogram). The
# cursor tracks offsets and a watermark per *stream*; parquet partitions are
# kept per *signal*.
CURSOR_STREAMS = ("traces", "logs", "metrics")
CACHE_SIGNALS = (
    "traces",
    "logs",
    "metrics_gauge",
    "metrics_sum",
    "metrics_histogram",
    "metrics_exp_histogram",
)
STREAM_SIGNALS = {
    "traces": ("traces",),
    "logs": ("logs",),
    "metrics": (
        "metrics_gauge",
        "metrics_sum",
        "metrics_histogram",
        "metrics_exp_histogram",
    ),
}
SIGNAL_READERS = {
    "traces": "read_otlp_traces",
    "logs": "read_otlp_logs",
    "metrics_gauge": "read_otlp_metrics_gauge",
    "metrics_sum": "read_otlp_metrics_sum",
    "metrics_histogram": "read_otlp_metrics_histogram",
    "metrics_exp_histogram": "read_otlp_metrics_exp_histogram",
}

CACHE_DIRNAME = ".otelq-cache"
PENDING_DIRNAME = ".pending"
CURSOR_FILENAME = "cursor.json"
LOCK_FILENAME = ".lock"
CURSOR_SCHEMA_VERSION = 1
RETENTION_MINUTES = 30  # hot window
MARGIN_MINUTES = 2  # watermark lateness allowance before a minute may seal
# How far ahead of wall-clock an event-time may sit before otelq treats it as an
# implausible outlier (clock-skewed producer, ns/µs unit mistake). The event-time
# window anchor and the cache watermark are clamped to `wall_clock + this` so a
# single bogus far-future record can neither push the hot window past all real
# data (a silent "no telemetry") nor ratchet the retention floor forward and
# evict the whole sealed cache (B-1). Generous enough to tolerate ordinary
# divergent-clock skew (EC-12), which is seconds-to-minutes, not a day.
MAX_FUTURE_SKEW = timedelta(days=1)
FINGERPRINT_BYTES = 256
_LOCK_STALE_SECS = 120
# A lock "held" by a live pid for longer than this is almost certainly a reused
# pid (the original writer is long gone) or a wedged process — reap it so the
# cache can never deadlock permanently. Far longer than any real catch-up seal.
_LOCK_HARD_STALE_SECS = 3600
_TMP_SUFFIX = ".tmp"

_NO_TELEMETRY_MSG = (
    "no telemetry captured — is the collector that writes this directory running, "
    "and are your apps exporting OTLP to it? (run `otelq troubleshoot`)"
)


class NoTelemetryError(Exception):
    """Raised when a command's required signal has no captured files."""


def stream_of(signal: str) -> str:
    """The raw byte-stream backing a cache signal (gauge/sum share metrics)."""
    return "metrics" if signal.startswith("metrics") else signal


def _count_rows(signal: str, obj: dict[str, Any]) -> int:
    """Number of read_otlp_<signal> output rows one OTLP/JSON line yields.

    `signal` is a byte-stream name (traces/logs/metrics).
    """
    if signal == "logs":
        return sum(
            len(sl.get("logRecords", []))
            for rl in obj.get("resourceLogs", [])
            for sl in rl.get("scopeLogs", [])
        )
    if signal == "traces":
        return sum(
            len(ss.get("spans", []))
            for rs in obj.get("resourceSpans", [])
            for ss in rs.get("scopeSpans", [])
        )
    # metrics: count every data point across all metric types, so the chunk
    # budget bounds every read_otlp_metrics_<type> call (gauge, sum, histogram,
    # exp_histogram).
    total = 0
    for rm in obj.get("resourceMetrics", []):
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                for kind in (
                    "gauge",
                    "sum",
                    "histogram",
                    "exponentialHistogram",
                    "summary",
                ):
                    points = metric.get(kind)
                    if points:
                        total += len(points.get("dataPoints", []))
    return total


def _decode_line(signal: str, line: str) -> tuple[dict[str, Any], int] | None:
    """Parse one OTLP/JSON line, applying the two robustness guards.

    Returns (parsed_obj, output_row_count), or None when the line must be
    skipped: a blank/partially-written trailing line (JSONDecodeError), or a
    single export batch whose row count exceeds the read_otlp 2048 crash limit.
    Shared by the cold whole-file reader and the incremental tail reader so the
    skip behaviour (SPEC FR-15) can never drift between the two paths.
    """
    if not line.strip():
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None  # skip a partially-written trailing line
    rows = _count_rows(signal, obj)
    if rows > _CRASH_LIMIT:
        # One export batch is a single indivisible JSON object; if it alone
        # exceeds the limit it cannot be chunked.
        print(
            f"otelq: skipping a {signal} batch of {rows} records "
            f"(exceeds the {_CRASH_LIMIT}-row read_otlp limit — "
            f"lower the Collector's send_batch_max_size)",
            file=sys.stderr,
        )
        return None
    return obj, rows


def _buffer_to_chunks(
    signal: str, lines: Iterable[str], tmp_dir: str, tag: str
) -> list[str]:
    """Slice an iterable of JSONL lines into <=_SAFE_CHUNK-row chunk files.

    Each chunk file stays under the duckdb-otlp 2048-row crash boundary, so a
    read_otlp_* call over one chunk is safe. Returns the chunk file paths.
    """
    chunks: list[str] = []
    buf: list[str] = []
    buf_rows = 0

    def flush() -> None:
        nonlocal buf, buf_rows
        if not buf:
            return
        path = Path(tmp_dir) / f"{tag}_{len(chunks)}.jsonl"
        path.write_text("".join(buf), encoding="utf-8")
        chunks.append(path.as_posix())
        buf, buf_rows = [], 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        ub = _rows_upper_bound(line)
        if stripped.endswith("}") and ub <= _CRASH_LIMIT:
            # Fast path (P-2): a complete object (ends with '}') whose upper bound
            # is under the crash limit needs no parse — the bound is a safe row
            # count for chunk sizing. This skips json.loads for the overwhelming
            # majority of lines on the cold path over a large corpus.
            rows = ub
        else:
            # A partially-written trailing line (won't end in '}') or a batch whose
            # bound reaches the crash limit: confirm with a real parse, which skips
            # blank/partial lines and oversize batches with a warning (FR-15).
            decoded = _decode_line(signal, line)
            if decoded is None:
                continue
            _, rows = decoded
        if buf and buf_rows + rows > _SAFE_CHUNK:
            flush()
        buf.append(line if line.endswith("\n") else line + "\n")
        buf_rows += rows
    flush()
    return chunks


def _chunk_signal(signal: str, telemetry_dir: Path, tmp_dir: str) -> list[str]:
    """Slice a stream's whole JSONL file(s) into <=_SAFE_CHUNK-row chunk files.

    Used by the cold path, which re-reads every matching file. Streams lines
    from disk (no full-file buffering) and shares the per-line guards via
    _buffer_to_chunks/_decode_line.
    """
    sources = sorted(glob.glob(str(telemetry_dir / SIGNAL_GLOBS[signal])))
    if not sources:
        return []

    def line_iter() -> Iterator[str]:
        for src in sources:
            with open(src, encoding="utf-8") as handle:
                yield from handle

    return _buffer_to_chunks(signal, line_iter(), tmp_dir, signal)


def _schema_probe_chunk(telemetry_dir: Path, tmp_dir: str) -> str | None:
    """Write one small (crash-safe) chunk from whichever stream has data, usable
    as a typed-schema probe for ANY read_otlp_* reader.

    The readers are cross-tolerant: each returns its own fixed typed schema — and
    zero rows — over a batch of any other signal (read_otlp_logs over a traces
    batch yields the logs columns, empty). So the first decodable batch of the
    first stream that has one is enough to seed an empty typed relation for ANY
    absent signal via `read_otlp_<reader>(probe) WHERE false` — drift-free, with
    no embedded schema. Reads at most that first batch — never the whole corpus —
    so it stays cheap on the hot path. Returns the chunk path, or None when no
    telemetry is present at all (a truly empty dir, or all raw files rotated
    away)."""
    for stream in CURSOR_STREAMS:
        for src in sorted(glob.glob(str(telemetry_dir / SIGNAL_GLOBS[stream]))):
            try:
                handle = open(src, encoding="utf-8")
            except OSError:
                continue
            with handle:
                for line in handle:
                    if _decode_line(stream, line) is None:
                        continue  # blank/partial/oversized — try the next batch
                    chunks = _buffer_to_chunks(stream, [line], tmp_dir, "schema_probe")
                    return chunks[0] if chunks else None
    return None


# A minimal, valid OTLP/JSON metrics line (one gauge data point) used ONLY as a
# last-resort schema probe when the telemetry dir has no readable raw line of its
# own (a truly empty or absent dir). Every read_otlp_* reader parses a
# resourceMetrics-only line and returns its OWN typed schema with zero rows (the
# readers are cross-tolerant), so reading this sample `WHERE false` probes the
# schema of all six readers. This stays drift-free: the column types still come
# from the live extension reading the sample — it is a schema *probe*, never a
# hard-coded column list. A gauge metric needs no trace/span ids, so there are no
# id-format pitfalls.
_EMBEDDED_PROBE_LINE = json.dumps(
    {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "otelq"}}
                    ]
                },
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "otelq.schema.probe",
                                "gauge": {
                                    "dataPoints": [
                                        {"timeUnixNano": "0", "asDouble": 0.0}
                                    ]
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }
)


def _embedded_probe_chunk(tmp_dir: str) -> str | None:
    """Write the embedded schema-probe sample to a chunk file and return its path.

    The schema-source fallback when the telemetry dir offers no readable line of
    its own, so absent relations can be seeded empty even over a truly empty or
    absent directory (FR-1: no "table does not exist" ever)."""
    chunks = _buffer_to_chunks("metrics", [_EMBEDDED_PROBE_LINE], tmp_dir, "schema_probe")
    return chunks[0] if chunks else None


# gauge/sum expose a scalar `value`; histogram/exp_histogram have no scalar
# value, so the unified `metrics.value` surfaces their `sum` (quoted — it is a
# column name, not the aggregate). metric_type tags each row by its origin
# sub-relation; the suffixes match the relation names (metrics_<type>).
_METRIC_VIEW_PARTS: tuple[tuple[str, str, str], ...] = (
    ("metrics_gauge", "gauge", "value"),
    ("metrics_sum", "sum", "value"),
    ("metrics_histogram", "histogram", '"sum"'),
    ("metrics_exp_histogram", "exp_histogram", '"sum"'),
)


def create_unified_metrics_view(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the `metrics` view as the union of whichever metric tables exist.

    Unions all four per-type sub-relations that are present — gauge, sum,
    histogram, exp_histogram — projecting the common columns plus a `metric_type`
    discriminator. The unified `value` is each gauge/sum row's `value` and each
    histogram/exp_histogram row's `sum`. Self-guarding: builds from whichever
    sub-relations are present and no-ops when none are. Both the hot and cold
    paths expose all four sub-relations whenever metrics are present (absent
    types seeded empty) and none when metrics are absent, so they produce the
    identical `metrics` view — the FR-11 equivalence lever. Defined in exactly
    one place; reused by every relation builder and the test fixture.
    """
    have = _existing_relations(conn)
    parts = [
        f"SELECT timestamp, service_name, metric_name, metric_unit, "
        f"{value} AS value, '{mtype}' AS metric_type FROM {tbl}"
        for tbl, mtype, value in _METRIC_VIEW_PARTS
        if tbl in have
    ]
    if parts:
        conn.execute("CREATE OR REPLACE VIEW metrics AS " + " UNION ALL ".join(parts))


def _materialize(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    reader: str,
    chunks: list[str],
    ts_fix: str,
) -> None:
    """Read each chunk with its own read_otlp_* call into one table.

    Every call sees at most _SAFE_CHUNK rows, so none can trip the extension's
    2048-row crash. The result is materialised, so the chunk files are no
    longer needed once this returns.
    """
    for index, chunk in enumerate(chunks):
        select = f"SELECT * REPLACE ({ts_fix}) FROM {reader}({_sql_str(chunk)})"
        if index == 0:
            conn.execute(f"CREATE TABLE {name} AS {select}")
        else:
            conn.execute(f"INSERT INTO {name} {select}")


# --- small helpers -----------------------------------------------------------

# DuckDB query results are dynamically typed at the SQL boundary: a row is a
# tuple of column values whose Python types depend on the query, so the element
# type is genuinely `Any` (mirrors the duckdb stub's own `tuple[Any, ...]`).
Row = tuple[Any, ...]
# Every command returns its column headers and the result rows.
CommandResult = tuple[list[str], list[Row]]
# A subcommand handler: a built connection + parsed args -> a CommandResult.
Command = Callable[["duckdb.DuckDBPyConnection", argparse.Namespace], CommandResult]


def _one_row(rel: duckdb.DuckDBPyConnection) -> Row:
    """The single row of a query guaranteed to return exactly one.

    A bare aggregate (no GROUP BY) always yields one row, so fetchone() is never
    None here; assert it so the `tuple | None` Optional is discharged at this one
    seam rather than leaking into callers that unpack the columns."""
    row = rel.fetchone()
    assert row is not None, "aggregate query returned no row"
    return row


def _scalar(rel: duckdb.DuckDBPyConnection) -> Any:
    """First column of a query that returns exactly one row.

    Returns `Any` because the value's type is the SQL expression's type (a count
    is int, max(timestamp) is datetime|None, ...) — the dynamic result boundary."""
    return _one_row(rel)[0]


def _existing_relations(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
    return {r[0] for r in rows}


# Per-connection memo of `relation -> has-rows`. otelq's query relations are built
# once and never mutated by a read command, so presence is stable for a
# connection's life; caching it collapses the several count(*) round-trips a
# single command makes (cmd_summary + _require + _has_rows) into one probe per
# relation (P-4). Keyed weakly so a closed connection's entry is collectable.
_presence_memo: "weakref.WeakKeyDictionary[duckdb.DuckDBPyConnection, dict[str, bool]]" = (
    weakref.WeakKeyDictionary()
)


def _has_rows(conn: duckdb.DuckDBPyConnection, relation: str) -> bool:
    """True iff `relation` exists AND holds at least one row.

    Existence is checked first, so this never raises on a missing relation — and,
    crucially, treats a seeded-empty relation (FR-1 expose-empty) exactly like an
    absent one. "Present" therefore means *has captured data*, not mere table
    existence: the presence checks below stay correct now that every documented
    relation resolves (empty) whenever any telemetry is present. The result is
    memoised per connection (P-4)."""
    memo = _presence_memo.setdefault(conn, {})
    cached = memo.get(relation)
    if cached is not None:
        return cached
    if relation not in _existing_relations(conn):
        memo[relation] = False
        return False
    result = bool(_scalar(conn.execute(f"SELECT count(*) FROM {relation}")))
    memo[relation] = result
    return result


def _present_signals(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """User-facing signals (traces/logs/metrics) that have captured DATA (rows),
    not merely a seeded-empty relation."""
    return [s for s in ("traces", "logs", "metrics") if _has_rows(conn, s)]


def _no_signal_msg(
    conn: duckdb.DuckDBPyConnection, missing: str, files: tuple[str, ...]
) -> str:
    """Error text for a required signal that has no captured data.

    With NOTHING captured the collector is probably down, so the generic hint
    fits. But when other signals ARE present the collector is plainly up, so
    that hint actively misleads (it once cost a whole debugging session chasing
    a healthy collector). In that case name the gap and its usual cause: the
    apps aren't emitting it, or its file was deleted under the running collector
    — high-volume traces reappear on the next rotation, but low-volume
    logs/metrics don't until the collector restarts. `missing` is the human
    label (e.g. "logs", "traces or logs"); `files` are the jsonl base names to
    cite in the remediation hint. "Present" means has-data (FR-19), so a
    seeded-empty relation never counts as present.
    """
    present = _present_signals(conn)
    if not present:
        return _NO_TELEMETRY_MSG
    names = " / ".join(f"{f}.jsonl" for f in files)
    return (
        f"no {missing} telemetry captured (present: {', '.join(present)}). "
        f"The collector is up, so either the apps aren't emitting {missing}, or "
        f"{names} was deleted while the collector was running — high-volume "
        f"traces reappear on rotation but low-volume logs/metrics don't until "
        f"the collector is restarted (run `otelq troubleshoot`)."
    )


def _require(conn: duckdb.DuckDBPyConnection, *relations: str) -> None:
    # "Required" means the relation must carry DATA, not merely exist: every
    # relation now resolves (empty) whenever any telemetry is present (FR-1), so a
    # row-count check is what keeps slow/trace/logs/metric naming the gap (FR-19).
    missing = [r for r in relations if not _has_rows(conn, r)]
    if missing:
        raise NoTelemetryError(_no_signal_msg(conn, missing[0], (missing[0],)))


def _fmt_ts(dt: datetime) -> str:
    """Format a datetime as a DuckDB TIMESTAMP literal / cursor string."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _utc_now() -> datetime:
    """Wall-clock UTC as a naive datetime, matching the naive-UTC event-times the
    OTLP reader produces (so the two are directly comparable)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _future_ceiling() -> datetime:
    """The largest event-time otelq will anchor to: wall-clock + MAX_FUTURE_SKEW.

    Event-times above this are implausible outliers; clamping the window anchor
    and the cache watermark to this ceiling keeps one bogus far-future record from
    blacking out every query or wiping the sealed cache (B-1)."""
    return _utc_now() + MAX_FUTURE_SKEW


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
    except (ValueError, TypeError):
        return None


def minute_key(dt: datetime) -> str:
    """UTC, colon-free, filename-safe minute key, e.g. 2026-06-22T13-05."""
    return dt.strftime("%Y-%m-%dT%H-%M")


def parse_minute_key(stem: str) -> datetime | None:
    try:
        return datetime.strptime(stem, "%Y-%m-%dT%H-%M")
    except (ValueError, TypeError):
        return None


def _sql_str(value: str) -> str:
    """Quote a string as a DuckDB single-quoted SQL literal, escaping embedded
    quotes (`'` -> `''`).

    Every filesystem path otelq splices into SQL passes through here so a
    telemetry dir containing a single quote — common on macOS ("Robert's Mac",
    `it's-a-demo/`) — neither breaks the query with an opaque syntax error nor
    permits SQL injection via `--dir` (B-5)."""
    return "'" + value.replace("'", "''") + "'"


def _sql_file_list(paths: list[str]) -> str:
    return "[" + ", ".join(_sql_str(p) for p in paths) + "]"


# --- cache paths -------------------------------------------------------------


def cache_dir(telemetry_dir: Path) -> Path:
    return telemetry_dir / CACHE_DIRNAME


def sealed_dir(telemetry_dir: Path, signal: str) -> Path:
    return cache_dir(telemetry_dir) / signal


def pending_path(telemetry_dir: Path, signal: str) -> Path:
    return cache_dir(telemetry_dir) / PENDING_DIRNAME / f"{signal}.parquet"


def sealed_path(telemetry_dir: Path, signal: str, minute: datetime) -> Path:
    return sealed_dir(telemetry_dir, signal) / f"{minute_key(minute)}.parquet"


# --- cursor + self-heal ------------------------------------------------------


class FileState(TypedDict):
    """Per raw-file progress: bytes consumed from its head."""

    bytes_consumed: int


class StreamState(TypedDict):
    """Per-stream cursor state: file offsets keyed by file identity, plus the
    max event-time ever observed in the stream (the seal/evict watermark)."""

    files: dict[str, FileState]
    max_event_ts_seen: str | None


class Cursor(TypedDict):
    """The persisted incremental-cache cursor (cache_dir/cursor.json)."""

    version: int
    streams: dict[str, StreamState]


def _fresh_cursor() -> Cursor:
    return {
        "version": CURSOR_SCHEMA_VERSION,
        "streams": {
            s: {"files": {}, "max_event_ts_seen": None} for s in CURSOR_STREAMS
        },
    }


def _load_cursor(telemetry_dir: Path) -> Cursor | None:
    """Return the persisted cursor, or None when missing/corrupt/wrong-version."""
    path = cache_dir(telemetry_dir) / CURSOR_FILENAME
    try:
        # json.loads is dynamically typed; bind as `object` and narrow by shape.
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    payload = cast(dict[str, Any], data)
    if payload.get("version") != CURSOR_SCHEMA_VERSION:
        return None
    streams = payload.get("streams")
    if not isinstance(streams, dict) or any(s not in streams for s in CURSOR_STREAMS):
        return None
    # Shape validated above (version + every stream present); the JSON payload is
    # dynamic, so narrow to the Cursor contract at this trust boundary.
    return cast(Cursor, payload)


def _write_cursor(telemetry_dir: Path, cursor: Cursor) -> None:
    cdir = cache_dir(telemetry_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / CURSOR_FILENAME
    tmp = path.with_suffix(path.suffix + _TMP_SUFFIX)
    tmp.write_text(json.dumps(cursor), encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX and Windows


def _wipe_cache(telemetry_dir: Path) -> None:
    shutil.rmtree(cache_dir(telemetry_dir), ignore_errors=True)


def _reap_tmp(telemetry_dir: Path) -> None:
    """Remove stale *.tmp files left by a crashed write.

    Skipped entirely while a live pid holds the writer lock: a legitimate holder
    may run a long catch-up seal whose single `COPY` exceeds _LOCK_STALE_SECS
    between mtime updates, and reaping its in-flight temp file mid-write would
    corrupt the seal (B-8). With no live holder, *.tmp older than the lock TTL is
    a crash leftover and is removed."""
    cdir = cache_dir(telemetry_dir)
    if not cdir.exists():
        return
    if _lock_held_by_live_pid(cdir):
        return  # a live writer owns the cache; never touch its in-flight temp files
    now = time.time()
    for p in cdir.rglob("*" + _TMP_SUFFIX):
        try:
            if now - p.stat().st_mtime > _LOCK_STALE_SECS:
                p.unlink()
        except OSError:
            pass


def _self_heal(telemetry_dir: Path) -> Cursor:
    """Load the cursor; wipe + rebuild on corruption/version mismatch; reap tmp."""
    cur = _load_cursor(telemetry_dir)
    if cur is None and (cache_dir(telemetry_dir) / CURSOR_FILENAME).exists():
        _wipe_cache(telemetry_dir)
    _reap_tmp(telemetry_dir)
    return cur if cur is not None else _fresh_cursor()


# --- writer lock (dependency-free O_EXCL sentinel, never blocks) --------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False  # Windows raises OSError for a dead pid
    return True


def _lock_held_by_live_pid(cdir: Path) -> bool:
    """True iff the writer lock exists and names a currently-live pid.

    Used to hold off destructive housekeeping (tmp reaping) while a legitimate
    writer is mid-run, and to refuse unlinking a lock that a reaper handed to
    another process."""
    lock = cdir / LOCK_FILENAME
    try:
        pid = int((lock.read_text(encoding="utf-8") or "0").strip() or "0")
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def _reap_if_stale(lock: Path) -> bool:
    try:
        pid = int((lock.read_text(encoding="utf-8") or "0").strip() or "0")
        age = time.time() - lock.stat().st_mtime
    except (OSError, ValueError):
        return False
    if _pid_alive(pid) and age < _LOCK_HARD_STALE_SECS:
        return False  # a live holder within the ceiling — never reap (catch-up seal)
    if age < _LOCK_STALE_SECS:
        return False  # dead/unknown pid but recent: could be a lock mid-creation
    try:
        lock.unlink()  # dead pid, or "live" past the hard ceiling (pid reuse/wedge)
    except OSError:
        return False
    return True


def _acquire_lock(cdir: Path) -> int | None:
    """Try once (with one stale-reap retry) to take the writer lock. Returns an
    open fd on success, or None on contention — the caller then queries without
    sealing (SPEC FR-12). Never blocks."""
    cdir.mkdir(parents=True, exist_ok=True)
    lock = cdir / LOCK_FILENAME
    for attempt in (1, 2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if attempt == 1 and _reap_if_stale(lock):
                continue
            return None
        os.write(fd, str(os.getpid()).encode())
        return fd
    return None


def _release_lock(cdir: Path, fd: int) -> None:
    """Release the writer lock, but unlink the sentinel only when it still names
    our pid. If a reaper decided we blew past the hard ceiling and re-acquired for
    another process, unlinking by name would delete THAT holder's lock and let two
    writers run at once (INV-5); verify ownership first (B-9)."""
    try:
        os.close(fd)
    except OSError:
        pass
    lock = cdir / LOCK_FILENAME
    try:
        holder = int((lock.read_text(encoding="utf-8") or "0").strip() or "0")
    except (OSError, ValueError):
        return  # gone or unreadable — nothing of ours to remove
    if holder != os.getpid():
        return  # reaped and handed to another process; leave their lock intact
    try:
        lock.unlink()
    except OSError:
        pass


# --- incremental raw reading -------------------------------------------------


def _file_identity(path: Path) -> str:
    """Identity key for a raw file: inode + hash of its first FINGERPRINT_BYTES.

    Rotation renames the active file to a backup but preserves the inode and the
    immutable prefix bytes, so the key carries over and the stored offset
    resumes (SPEC FR-3). The fingerprint guards inode reuse and filesystems
    where st_ino is 0/non-unique (EC-6). Size is deliberately not part of the
    key (it grows every append, which would churn the key every run); the
    residual truncate-and-rewrite hole the size once guarded is closed by the
    shrink check in _iter_stream_delta (st_size < stored offset ⇒ reset), so
    identity stays stable while a rewritten file is never resumed at a stale
    offset (B-6)."""
    st = os.stat(path)
    with open(path, "rb") as fh:
        head = fh.read(FINGERPRINT_BYTES)
    return f"{st.st_ino}:{hashlib.sha1(head).hexdigest()}"


_READ_BLOCK = (
    1 << 20
)  # 1 MiB — stream the delta so cold-start never buffers a whole file


def _iter_stream_delta(
    telemetry_dir: Path,
    stream: str,
    files_state: dict[str, FileState],
    new_state_out: dict[str, FileState],
) -> Iterator[str]:
    """Yield each not-yet-consumed complete line of a stream's files, streaming in
    bounded blocks so a cold-start over a multi-GB corpus never holds a whole file
    in memory (SPEC FR-2). Rotation-aware: files are keyed by identity, offsets
    carry over, and rotated-away files auto-prune. The advanced per-file offsets
    are written into `new_state_out` as each file is exhausted.

    Binary reads keep byte offsets exact on every platform (text-mode tell() is
    opaque under newline translation). A trailing line without a newline is a
    partial write: it is not yielded and its bytes are not consumed, so it is
    re-read once complete.

    A file that shrank below its stored offset was truncated and rewritten under
    a coincidentally-identical identity; it resumes from 0 (B-6). A file listed by
    the glob but unreadable this run has its prior offset carried forward rather
    than dropped, so it is not re-read from 0 (and its unsealed rows duplicated)
    next run (B-4)."""
    any_open_failure = False
    for src in sorted(glob.glob(str(telemetry_dir / SIGNAL_GLOBS[stream]))):
        path = Path(src)
        try:
            key = _file_identity(path)
            fh = open(path, "rb")
        except OSError:
            any_open_failure = True
            continue
        prior = files_state.get(key)
        offset = prior["bytes_consumed"] if prior is not None else 0
        with fh:
            try:
                size = os.fstat(fh.fileno()).st_size
            except OSError:
                size = offset
            if size < offset:
                offset = 0  # truncated + rewritten under a stale identity -> restart
            fh.seek(offset)
            buf = b""
            while True:
                block = fh.read(_READ_BLOCK)
                if not block:
                    break
                buf += block
                nl = buf.rfind(b"\n")
                if nl == -1:
                    continue  # no complete line yet; keep accumulating
                complete, buf = buf[: nl + 1], buf[nl + 1 :]
                offset += len(complete)
                for ln in complete.split(b"\n"):
                    if ln.strip():
                        yield ln.decode("utf-8", "replace")
        new_state_out[key] = {"bytes_consumed": offset}
    if any_open_failure:
        # We cannot map an unreadable glob path back to its identity key, so
        # conservatively carry forward every prior offset we did not just refresh.
        # The only cost is briefly retaining a key for a genuinely rotated-away
        # file when some *other* file also failed to open — bounded, and pruned on
        # the next clean run — whereas dropping a live file's offset would
        # re-parse it from 0 and break FR-11/INV-4 (B-4).
        for key, state in files_state.items():
            new_state_out.setdefault(key, state)


# --- ingest, seal, evict -----------------------------------------------------


def _build_staging(
    conn: duckdb.DuckDBPyConnection,
    telemetry_dir: Path,
    stream_chunks: dict[str, list[str]],
) -> set[str]:
    """Create _stage_<signal> = persisted pending parquet UNION this run's delta.

    The pending parquet already holds post-_TS_FIX rows, so it is read as-is;
    the delta chunks come straight from read_otlp_* and get the timestamp fix.
    Both arms share a column set, unioned by name. Returns the signals staged.
    """
    staged: set[str] = set()
    for stream in CURSOR_STREAMS:
        chunks = stream_chunks.get(stream, [])
        for signal in STREAM_SIGNALS[stream]:
            arms: list[str] = []
            pending = pending_path(telemetry_dir, signal)
            if pending.exists():
                arms.append(
                    f"SELECT * FROM read_parquet({_sql_str(pending.as_posix())})"
                )
            for chunk in chunks:
                arms.append(
                    f"SELECT * REPLACE ({_TS_FIX}) FROM "
                    f"{SIGNAL_READERS[signal]}({_sql_str(chunk)})"
                )
            if arms:
                conn.execute(
                    f"CREATE TABLE _stage_{signal} AS "
                    + " UNION ALL BY NAME ".join(arms)
                )
                staged.add(signal)
    return staged


def _ingest_and_seal(
    conn: duckdb.DuckDBPyConnection,
    telemetry_dir: Path,
    cursor: Cursor,
    tmp_dir: str,
) -> None:
    """Read each stream's delta, seal newly-complete minutes, rewrite pending,
    evict stale partitions, and persist the advanced cursor. Caller holds the
    writer lock."""
    stream_chunks: dict[str, list[str]] = {}
    for stream in CURSOR_STREAMS:
        new_state: dict[str, FileState] = {}
        stream_chunks[stream] = _buffer_to_chunks(
            stream,
            _iter_stream_delta(
                telemetry_dir,
                stream,
                cursor["streams"][stream].get("files", {}),
                new_state,
            ),
            tmp_dir,
            f"{stream}_delta",
        )
        cursor["streams"][stream]["files"] = new_state

    staged = _build_staging(conn, telemetry_dir, stream_chunks)

    # Advance each stream watermark to the max event-time ever seen.
    for stream in CURSOR_STREAMS:
        candidates: list[datetime] = []
        prev = _parse_ts(cursor["streams"][stream].get("max_event_ts_seen"))
        if prev:
            candidates.append(prev)
        for signal in STREAM_SIGNALS[stream]:
            if signal in staged:
                hi = _scalar(
                    conn.execute(f"SELECT max(timestamp) FROM _stage_{signal}")
                )
                if hi is not None:
                    candidates.append(hi)
        if candidates:
            cursor["streams"][stream]["max_event_ts_seen"] = _fmt_ts(max(candidates))

    for signal in CACHE_SIGNALS:
        wm = _parse_ts(cursor["streams"][stream_of(signal)].get("max_event_ts_seen"))
        if wm is None:
            continue
        # Clamp the retention anchor to a plausible ceiling: a single far-future
        # record must not ratchet hot_floor forward and evict the whole sealed
        # cache (B-1). The persisted watermark keeps the true max (INV-7); only
        # the floors derived here are clamped.
        anchor = min(wm, _future_ceiling())
        hot_floor = anchor - timedelta(minutes=RETENTION_MINUTES)
        # Retention is per-minute but the query window is sub-minute: the minute
        # straddling hot_floor still holds records >= hot_floor that an in-window
        # query needs. Retain one extra minute below the window so the exact
        # sub-minute window filter in _finalize_relations trims it correctly,
        # keeping the hot read equal to a full raw re-scan (FR-11).
        retain_floor = hot_floor - timedelta(minutes=1)
        # Sealing and pending-rewrite need this run's _stage_<signal> table, so
        # they only run for staged signals. Eviction is decoupled: it runs for
        # every signal whose stream has a watermark, so a signal that stops being
        # staged (e.g. a sibling metric type on a stream still advancing via
        # another type) still has its out-of-window partitions removed rather than
        # lingering in the `metrics` view (FR-6; B-10).
        if signal in staged:
            # A minute m=[m, m+60s) is sealable once wm has passed its end by MARGIN.
            seal_high = anchor - timedelta(seconds=60 + MARGIN_MINUTES * 60)
            newly_sealed = _seal_signal(
                conn, telemetry_dir, signal, retain_floor, seal_high
            )
            _rewrite_pending(conn, telemetry_dir, signal, retain_floor, newly_sealed)
        _evict_signal(telemetry_dir, signal, retain_floor)

    cursor["version"] = CURSOR_SCHEMA_VERSION
    _write_cursor(telemetry_dir, cursor)


def _seal_signal(
    conn: duckdb.DuckDBPyConnection,
    telemetry_dir: Path,
    signal: str,
    hot_floor: datetime,
    seal_high: datetime,
) -> set[datetime]:
    """Seal every complete, in-window minute that is not already on disk, and
    return the set of minutes freshly sealed this run."""
    sdir = sealed_dir(telemetry_dir, signal)
    sdir.mkdir(parents=True, exist_ok=True)
    minutes = conn.execute(
        f"SELECT DISTINCT date_trunc('minute', timestamp) AS m "
        f"FROM _stage_{signal} ORDER BY m"
    ).fetchall()
    sealed: set[datetime] = set()
    for (m,) in minutes:
        if m is None or m < hot_floor or m > seal_high:
            continue  # too old to retain, or too recent to be complete
        target = sealed_path(telemetry_dir, signal, m)
        if target.exists():
            continue  # immutable: never re-seal a minute (INV-2, crash-safe)
        tmp = target.with_suffix(target.suffix + _TMP_SUFFIX)
        conn.execute(
            f"COPY (SELECT * FROM _stage_{signal} "
            f"WHERE date_trunc('minute', timestamp) = TIMESTAMP '{_fmt_ts(m)}') "
            f"TO {_sql_str(tmp.as_posix())} (FORMAT PARQUET)"
        )
        os.replace(tmp, target)
        sealed.add(m)
    return sealed


def _rewrite_pending(
    conn: duckdb.DuckDBPyConnection,
    telemetry_dir: Path,
    signal: str,
    hot_floor: datetime,
    newly_sealed: set[datetime],
) -> None:
    """Persist the unsealed tail: every in-window staged row whose minute was NOT
    freshly sealed into a partition this run. That is the too-recent minutes AND
    any late/out-of-order arrival for a minute already sealed in a prior run —
    those rows are absent from the immutable partition, so they must live in
    pending to stay queryable and keep the hot read equal to a full raw re-scan
    (SPEC FR-5/FR-11/INV-4)."""
    pending = pending_path(telemetry_dir, signal)
    pending.parent.mkdir(parents=True, exist_ok=True)
    where = f"date_trunc('minute', timestamp) >= TIMESTAMP '{_fmt_ts(hot_floor)}'"
    if newly_sealed:
        excl = ", ".join(f"TIMESTAMP '{_fmt_ts(m)}'" for m in sorted(newly_sealed))
        where += f" AND date_trunc('minute', timestamp) NOT IN ({excl})"
    candidate = f"SELECT * FROM _stage_{signal} WHERE {where}"
    # When a candidate minute already has a sealed partition (a genuine late/
    # out-of-order arrival, or a crash that rolled the cursor back and re-read the
    # minute), EXCEPT the partition's rows: a re-read of identical rows is removed
    # (no duplication — INV-2/EC-11), while a genuinely new late row is kept (so
    # the hot read still equals a full raw re-scan — FR-11/INV-4).
    minutes = conn.execute(
        f"SELECT DISTINCT date_trunc('minute', timestamp) FROM ({candidate})"
    ).fetchall()
    overlap = [
        sealed_path(telemetry_dir, signal, m).as_posix()
        for (m,) in minutes
        if m is not None and sealed_path(telemetry_dir, signal, m).exists()
    ]
    final = candidate
    if overlap:
        # EXCEPT ALL (bag semantics), not EXCEPT (set): a crash-rolled-back re-read
        # of N identical rows cancels exactly N copies, while a genuinely-new late
        # record byte-identical to a sealed one survives — both correct at once.
        final = (
            f"({candidate}) EXCEPT ALL "
            f"(SELECT * FROM read_parquet({_sql_file_list(overlap)}))"
        )
    count = _scalar(conn.execute(f"SELECT count(*) FROM ({final})"))
    if count:
        tmp = pending.with_suffix(pending.suffix + _TMP_SUFFIX)
        conn.execute(f"COPY ({final}) TO {_sql_str(tmp.as_posix())} (FORMAT PARQUET)")
        os.replace(tmp, pending)
    else:
        try:
            pending.unlink()
        except OSError:
            pass


def _evict_signal(telemetry_dir: Path, signal: str, hot_floor: datetime) -> None:
    sdir = sealed_dir(telemetry_dir, signal)
    if not sdir.exists():
        return
    for p in sdir.glob("*.parquet"):
        mk = parse_minute_key(p.stem)
        if mk is not None and mk < hot_floor:
            try:
                p.unlink()
            except OSError:
                pass


# --- relation building (hot / cold) ------------------------------------------


def _seed_absent_relations(
    conn: duckdb.DuckDBPyConnection,
    telemetry_dir: Path,
    tmp_dir: str,
    present: set[str],
    prefix: str,
) -> None:
    """Seed every absent base relation empty so ALL documented relations resolve.

    Create each base signal not already in `present` (traces, logs, and the four
    metric types) as an empty typed table — seeded from a cross-tolerant schema
    probe — and add it to `present`. The probe is the dir's own first raw batch
    when it has one, else the embedded sample, so absent relations resolve empty
    in EVERY case, including a valid-but-empty or absent `--dir` (FR-1: never a
    "table does not exist" error). `prefix` is the relation-name prefix: "_all_"
    for the hot/cold builders that window via _finalize_relations, "" for connect
    (which materialises final relations directly). Seeding identically on the hot
    and cold paths keeps an absent relation's empty result byte-identical cached
    vs --no-cache (FR-11). The friendly empty-telemetry (FR-18) and gap-naming
    (FR-19) paths are preserved by the callers' row-count presence checks
    (_has_rows), not by withholding the relation: an all-empty corpus still raises
    the friendly message."""
    absent = [s for s in CACHE_SIGNALS if s not in present]
    if not absent:
        return
    probe = _schema_probe_chunk(telemetry_dir, tmp_dir)
    if probe is None:
        probe = _embedded_probe_chunk(tmp_dir)  # truly empty / absent dir
    if probe is None:
        return
    for signal in absent:
        conn.execute(
            f"CREATE TABLE {prefix}{signal} AS SELECT * REPLACE ({_TS_FIX}) "
            f"FROM {SIGNAL_READERS[signal]}({_sql_str(probe)}) WHERE false"
        )
        present.add(signal)


def _assemble_hot(
    conn: duckdb.DuckDBPyConnection, telemetry_dir: Path, tmp_dir: str
) -> set[str]:
    """Build _all_<signal> = sealed parquet ∪ pending parquet.

    Sealed (complete past minutes) and pending (recent unsealed) are disjoint by
    construction, so a plain UNION ALL never double-counts. Returns the signals
    that have CACHED rows (no seeding here): the caller uses an empty return as the
    signal to fall back to a stateless cold scan, so seeding the absent relations
    empty must happen only on the non-empty branch (build_hot), never before that
    fallback decision — otherwise a lock-loser with an empty cache but live raw
    files would expose empty relations instead of reading the raw delta."""
    present: set[str] = set()
    for signal in CACHE_SIGNALS:
        arms: list[str] = []
        sealed = sorted(glob.glob(str(sealed_dir(telemetry_dir, signal) / "*.parquet")))
        if sealed:
            arms.append(
                f"SELECT * FROM read_parquet({_sql_file_list(sealed)}, "
                f"union_by_name=true)"
            )
        pending = pending_path(telemetry_dir, signal)
        if pending.exists():
            arms.append(f"SELECT * FROM read_parquet({_sql_str(pending.as_posix())})")
        if arms:
            # A VIEW, not a table: the final windowed relations are materialised
            # once from this in _finalize_relations, so a table here would copy the
            # whole hot window twice (parquet -> _all_ -> final). As a view DuckDB
            # does a single lazy parquet scan with the window predicate pushed down
            # (P-1). max(timestamp) in _finalize uses parquet zone-map stats, not a
            # full scan. The final relations stay materialised tables, so the reads
            # (and any "a partition vanished mid-query" error) still happen inside
            # build_connection's guarded region, preserving the cold fallback
            # (FR-12); a fully-lazy final relation would move that error to query
            # time, past the guard.
            conn.execute(
                f"CREATE VIEW _all_{signal} AS " + " UNION ALL BY NAME ".join(arms)
            )
            present.add(signal)
    return present


def _finalize_relations(
    conn: duckdb.DuckDBPyConnection, present: set[str], window: timedelta | None
) -> None:
    """Create the final query relations (traces/logs/metrics_*) from _all_<signal>,
    restricted to the planned event-time window. `now` is the max observed
    event-time across the built relations, so the hot and cold paths apply the
    identical window basis (SPEC INV-7)."""
    if not present:
        return
    maxes: list[datetime] = []
    for s in present:
        v = _scalar(conn.execute(f"SELECT max(timestamp) FROM _all_{s}"))
        if v is not None:
            maxes.append(v)
    now_evt = max(maxes) if maxes else None
    if window is None or now_evt is None:
        lo = hi = None
    else:
        # Clamp the window's upper anchor to a plausible ceiling so one far-future
        # record can't push the whole window past every real record and return the
        # friendly "no telemetry" while telemetry plainly exists (B-1). Applied on
        # BOTH hot and cold paths so cached == --no-cache (FR-11); a no-op unless
        # an event-time sits more than MAX_FUTURE_SKEW ahead of wall-clock.
        hi = min(now_evt, _future_ceiling())
        lo = hi - window
    for s in present:
        if lo is None or hi is None:
            conn.execute(f"CREATE TABLE {s} AS SELECT * FROM _all_{s}")
        else:
            conn.execute(
                f"CREATE TABLE {s} AS SELECT * FROM _all_{s} "
                f"WHERE timestamp >= TIMESTAMP '{_fmt_ts(lo)}' "
                f"AND timestamp <= TIMESTAMP '{_fmt_ts(hi)}'"
            )
    # FR-1 expose-empty: whenever ANY telemetry is present, `present` carries all
    # six base signals — present streams are materialised (cold) or read from
    # parquet (hot), and every absent signal is seeded empty
    # (_seed_absent_relations) — so all seven documented relations resolve (a query
    # gets 0 rows in-window, never a catalog error) and the hot and cold paths
    # expose the identical set, keeping cached == --no-cache (FR-11). Empty
    # in-window relations are kept, not dropped.
    create_unified_metrics_view(conn)


def build_hot(
    conn: duckdb.DuckDBPyConnection,
    telemetry_dir: Path,
    window: timedelta | None,
    tmp_dir: str,
) -> None:
    """Ingest the delta if we win the writer lock, then build the query relations
    from the parquet cache (sealed ∪ pending). A run that loses the lock answers
    from the cache as last committed; if the cache is empty (a concurrent first
    run, or a stale foreign lock), it falls back to a stateless cold scan so the
    query still answers (SPEC FR-12)."""
    cursor = _self_heal(telemetry_dir)
    cdir = cache_dir(telemetry_dir)
    fd = _acquire_lock(cdir)
    if fd is not None:
        try:
            _ingest_and_seal(conn, telemetry_dir, cursor, tmp_dir)
        finally:
            _release_lock(cdir, fd)
    present = _assemble_hot(conn, telemetry_dir, tmp_dir)
    if not present:
        # Empty cache: a stateless cold scan reads the raw delta (and itself seeds
        # absent relations), so the query still answers (SPEC FR-12).
        build_cold(conn, telemetry_dir, window, tmp_dir)
        return
    # Cache has data: seed the signals that sealed none empty so all documented
    # relations resolve on the hot path exactly as on the cold path (FR-1/FR-11).
    _seed_absent_relations(conn, telemetry_dir, tmp_dir, present, "_all_")
    _finalize_relations(conn, present, window)


def build_cold(
    conn: duckdb.DuckDBPyConnection,
    telemetry_dir: Path,
    window: timedelta | None,
    tmp_dir: str,
) -> None:
    """Stateless full raw scan, restricted to the window. The --no-cache path and
    the fallback for queries reaching older than the hot window."""
    present: set[str] = set()
    for stream in CURSOR_STREAMS:
        chunks = _chunk_signal(stream, telemetry_dir, tmp_dir)
        if not chunks:
            continue
        for signal in STREAM_SIGNALS[stream]:
            _materialize(
                conn, f"_all_{signal}", SIGNAL_READERS[signal], chunks, _TS_FIX
            )
            present.add(signal)
    _seed_absent_relations(conn, telemetry_dir, tmp_dir, present, "_all_")
    _finalize_relations(conn, present, window)


def connect(telemetry_dir: Path) -> duckdb.DuckDBPyConnection:
    """Full unfiltered cold connection over a telemetry dir (no cache).

    Retained for the test fixtures and as the simplest read path. Builds the
    query relations directly from every raw file."""
    import duckdb  # lazy; see TYPE_CHECKING note above

    conn = duckdb.connect(database=":memory:")
    conn.execute("INSTALL otlp FROM community")
    conn.execute("LOAD otlp")
    with tempfile.TemporaryDirectory(prefix="otelq-") as tmp_dir:
        present: set[str] = set()
        for stream in CURSOR_STREAMS:
            chunks = _chunk_signal(stream, telemetry_dir, tmp_dir)
            if not chunks:
                continue
            for signal in STREAM_SIGNALS[stream]:
                _materialize(conn, signal, SIGNAL_READERS[signal], chunks, _TS_FIX)
                present.add(signal)
        # Expose-empty (FR-1): seed every absent signal empty so all documented
        # relations resolve, even over an empty/absent dir (embedded probe).
        _seed_absent_relations(conn, telemetry_dir, tmp_dir, present, "")
        create_unified_metrics_view(conn)
    return conn


# --- query planning ----------------------------------------------------------


class Plan:
    __slots__ = ("route", "window", "use_cache")

    def __init__(self, route: str, window: timedelta | None, use_cache: bool):
        self.route = route  # "HOT" | "COLD" | "HOT_THEN_COLD"
        self.window = window  # None = unbounded
        self.use_cache = use_cache


def _parse_since(since: str | None) -> timedelta | None:
    """Translate a window like '30s' / '10m' / '2h' / '1d' into a timedelta."""
    if not since:
        return None
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    unit = units.get(since[-1].lower())
    if unit is None or not since[:-1].isdigit():
        raise SystemExit(
            f"otelq: invalid --since '{since}' (use e.g. 30s, 10m, 2h, 1d)"
        )
    return timedelta(**{unit: int(since[:-1])})


def plan_range(args: argparse.Namespace) -> Plan:
    cmd = args.command
    no_cache = bool(getattr(args, "no_cache", False))
    all_flag = bool(getattr(args, "all", False))
    since = _parse_since(getattr(args, "since", None))
    hot = timedelta(minutes=RETENTION_MINUTES)
    use_cache = not no_cache

    if cmd == "trace" or all_flag:
        window: timedelta | None = None
    elif since is not None:
        window = since
    else:
        window = hot  # windowless commands default to the hot window (FR-9)

    if no_cache or all_flag:
        route = "COLD"
    elif since is not None and since > hot:
        route = "COLD"
    elif cmd == "trace":
        # Only `trace` widens on a hot miss: a trace id is looked up across all
        # history (FR-10), ignoring the window. `metric` deliberately does NOT —
        # widening on an empty hot result would return points OUTSIDE the --since
        # or default window (FR-15/FR-9); `metric` widens only when explicitly
        # requested via --all/--since, which already routes COLD above (B-2).
        route = "HOT_THEN_COLD"
    else:
        route = "HOT"
    return Plan(route, window, use_cache)


def _human_window(window: timedelta | None) -> str:
    """Render a plan window as the compact form the user typed (30s/10m/2h/1d)."""
    if window is None:
        return "all history"
    seconds = int(window.total_seconds())
    for size, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds % size == 0:
            return f"last {seconds // size}{suffix}"
    return f"last {seconds}s"


def _plan_summary(plan: Plan) -> str:
    """One-line window/route/cache description for --verbose (stderr, F-5)."""
    cache = "on" if plan.use_cache else "off"
    return (
        f"otelq: window={_human_window(plan.window)} (by event-time), "
        f"route={plan.route.lower()}, cache={cache}"
    )


def build_connection(telemetry_dir: Path, plan: Plan) -> duckdb.DuckDBPyConnection:
    import duckdb  # lazy; see TYPE_CHECKING note above

    conn = duckdb.connect(database=":memory:")
    conn.execute("INSTALL otlp FROM community")
    conn.execute("LOAD otlp")
    with tempfile.TemporaryDirectory(prefix="otelq-") as tmp_dir:
        if plan.route == "COLD" or not plan.use_cache:
            build_cold(conn, telemetry_dir, plan.window, tmp_dir)
        else:
            try:
                build_hot(conn, telemetry_dir, plan.window, tmp_dir)
            except duckdb.Error:
                # A cache parquet vanished mid-read (a concurrent writer's
                # eviction) or was torn — fall back to a stateless cold scan on a
                # fresh connection so the query still answers (SPEC FR-12).
                conn.close()
                conn = duckdb.connect(database=":memory:")
                conn.execute("INSTALL otlp FROM community")
                conn.execute("LOAD otlp")
                build_cold(conn, telemetry_dir, plan.window, tmp_dir)
    return conn


def _seal_external_access(conn: duckdb.DuckDBPyConnection, command: str) -> None:
    """Revoke DuckDB's filesystem/network access before a built-in query runs.

    By this point the query relations are fully materialised (or, on the hot
    path, are parquet-backed views the built-in commands never touch — they read
    the materialised final tables), so the built-ins need no further file access.
    Dropping it is defense-in-depth against a crafted view/relation reaching other
    files (D-2). `sql` is exempt on purpose: it is an ad-hoc analysis escape hatch
    that runs with your user's file access — the help documents this — so it must
    keep read/write access to `read_csv`, `COPY`, etc."""
    if command == "sql":
        return
    conn.execute("SET enable_external_access=false")


def run_command(args: argparse.Namespace) -> CommandResult:
    """Plan, build the connection, dispatch, and apply trace/metric cold-fallback."""
    # A --dir that exists but is not a directory (e.g. a regular file) would
    # crash deep in the cache layer: _acquire_lock does mkdir('<file>/.otelq-cache')
    # and raises NotADirectoryError. Reject it up front with a friendly message
    # (exit non-zero, no traceback), mirroring doctor's is_dir() guard. A missing
    # dir is left alone — it routes to the normal friendly no-telemetry path.
    if args.dir.exists() and not args.dir.is_dir():
        raise SystemExit(f"otelq: --dir '{args.dir}' is not a directory")
    plan = plan_range(args)
    verbose = bool(getattr(args, "verbose", False))
    if verbose:
        print(_plan_summary(plan), file=sys.stderr)
    command = COMMANDS[args.command]
    fallback = plan.use_cache and plan.route == "HOT_THEN_COLD"
    with closing(build_connection(args.dir, plan)) as conn:
        _seal_external_access(conn, args.command)
        try:
            columns, rows = command(conn, args)
        except NoTelemetryError:
            if not fallback:
                raise
        else:
            if not (fallback and not rows):
                return columns, rows
    # Fell through: the hot window held no answer and this command may widen to a
    # full-history cold scan (only `trace` today). closing() has already released
    # the hot connection before we open the cold one (D-3).
    if verbose:
        print(
            "otelq: hot window empty — widened to a full-history cold scan",
            file=sys.stderr,
        )
    with closing(build_connection(args.dir, Plan("COLD", None, True))) as conn:
        _seal_external_access(conn, args.command)
        return command(conn, args)


def format_output(columns: list[str], rows: list[tuple[Any, ...]], fmt: str) -> str:
    if fmt == "json":
        # Compact separators (no spaces): pretty-printing roughly doubles the
        # token count for the tool's primary consumer — AI agents — against the
        # PRD's token-efficiency goal (P-5). Use `table` for humans, `jsonl` for
        # streaming one object per line.
        return json.dumps(
            [dict(zip(columns, r)) for r in rows], default=str, separators=(",", ":")
        )
    if fmt == "jsonl":
        return "\n".join(
            json.dumps(dict(zip(columns, r)), default=str, separators=(",", ":"))
            for r in rows
        )
    if fmt == "compact":
        # Columns declared once, each row a positional array: losslessly the same
        # data as `json` without repeating the column keys per row, so fewer
        # tokens for the AI-agent consumer (PRD P-5). Reconstruct downstream with
        # zip(columns, row). Order matches `table`/`json` (INV-3).
        return json.dumps(
            {"columns": columns, "rows": rows}, default=str, separators=(",", ":")
        )
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        writer.writerows(rows)
        return buf.getvalue().rstrip("\r\n")
    # table
    if not rows:
        return "(no rows)"
    widths = [len(str(c)) for c in columns]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(str(value)))

    def render(vals: Iterable[Any]) -> str:
        return "  ".join(str(v).ljust(w) for v, w in zip(vals, widths))

    separator = "  ".join("-" * w for w in widths)
    return "\n".join([render(columns), separator] + [render(r) for r in rows])


# Canonical OTel severity levels by severity_number range. `summary` buckets
# logs by these rather than by the free-form severity_text, which carries
# inconsistent casing in practice (e.g. "Info"). See SPEC-otelq-cli FR-2/FR-3.
_LOG_LEVELS: tuple[tuple[str, int, int], ...] = (
    ("TRACE", 1, 4),
    ("DEBUG", 5, 8),
    ("INFO", 9, 12),
    ("WARN", 13, 16),
    ("ERROR", 17, 20),
    ("FATAL", 21, 24),
)
_TRACE_SLOW_MS = 1000  # 1s in milliseconds; the >1s / =<1s span split. The
# duckdb-otlp extension reports `duration` in integer milliseconds (sub-ms spans
# truncate to 0), NOT nanoseconds — so the threshold and duration_ms below are ms.
# Per-subset aggregates every summary row carries (count, span, distinct services).
_SUMMARY_AGG = "count(*), min(timestamp), max(timestamp), count(DISTINCT service_name)"
_SUMMARY_ZERO = (0, None, None, 0)  # a bucket/level present but with no records
# The four metric types `summary` breaks metrics into (the suffixes of the
# metrics_<type> relations). All four rows appear when metrics are present, zeros
# included — a fixed skeleton like the log levels (FR-3). The OTel Summary type
# is not among them: the duckdb-otlp reader for it is an unsupported stub.
_METRIC_TYPES: tuple[str, ...] = ("gauge", "sum", "histogram", "exp_histogram")

# Default row cap for the list-style commands (errors/logs/metric). `slow` keeps
# its own smaller default. A cap keeps an accidental `logs` over a large capture
# from flooding an agent's context; --top overrides it and a stderr notice fires
# when the cap actually truncated the result (F-1).
_DEFAULT_TOP = 50


def _limited(
    conn: duckdb.DuckDBPyConnection,
    query: str,
    params: Sequence[Any],
    top: int,
) -> list[Row]:
    """Run `query` (which must already carry its ORDER BY) capped at `top` rows.

    Fetches one extra row so truncation is detected without a second count query;
    when the cap bites, the extra row is dropped and a one-line notice is written
    to stderr (never stdout, so --format json/csv stays machine-parseable). `top`
    of 0 returns no rows, matching `slow --top 0`."""
    if top == 0:
        return []
    rows: list[Row] = conn.execute(
        f"{query} LIMIT ?", [*params, top + 1]
    ).fetchall()
    if len(rows) > top:
        print(
            f"otelq: output truncated to {top} rows; pass --top N for more",
            file=sys.stderr,
        )
        rows = rows[:top]
    return rows


def _summary_traces(conn: duckdb.DuckDBPyConnection) -> list[Row]:
    """Two duration buckets; both rows present even when one is empty (FR-3)."""
    bucket = f"CASE WHEN duration > {_TRACE_SLOW_MS} THEN '>1s' ELSE '=<1s' END"
    got = {
        r[0]: r[1:]
        for r in conn.execute(
            f"SELECT {bucket} AS b, {_SUMMARY_AGG} FROM traces GROUP BY b"
        ).fetchall()
    }
    return [("traces", b, *got.get(b, _SUMMARY_ZERO)) for b in (">1s", "=<1s")]


def _summary_logs(conn: duckdb.DuckDBPyConnection) -> list[Row]:
    """One row per canonical level (all six present, zeros included), plus an
    UNSET row only when out-of-range severities exist (FR-3). Level is derived
    from severity_number, not severity_text (FR-2)."""
    cases = " ".join(
        f"WHEN severity_number BETWEEN {lo} AND {hi} THEN '{name}'"
        for name, lo, hi in _LOG_LEVELS
    )
    level = f"CASE {cases} ELSE 'UNSET' END"
    got = {
        r[0]: r[1:]
        for r in conn.execute(
            f"SELECT {level} AS lvl, {_SUMMARY_AGG} FROM logs GROUP BY lvl"
        ).fetchall()
    }
    rows: list[Row] = [
        ("logs", name, *got.get(name, _SUMMARY_ZERO)) for name, _lo, _hi in _LOG_LEVELS
    ]
    if "UNSET" in got:
        rows.append(("logs", "UNSET", *got["UNSET"]))
    return rows


def _summary_metrics(conn: duckdb.DuckDBPyConnection) -> list[Row]:
    """One row per metric type (all four present, zeros included), each scoped to
    that type over the unified `metrics` view (FR-3). `details` is the type."""
    got = {
        r[0]: r[1:]
        for r in conn.execute(
            f"SELECT metric_type AS t, {_SUMMARY_AGG} FROM metrics GROUP BY t"
        ).fetchall()
    }
    return [
        ("metrics", mtype, *got.get(mtype, _SUMMARY_ZERO)) for mtype in _METRIC_TYPES
    ]


def cmd_summary(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    # Include a signal only when it has captured DATA (rows), not merely a
    # seeded-empty relation — so an empty traces relation emits no zero-count trace
    # buckets (FR-3: an absent signal contributes no rows; AC-25). The within-
    # signal skeletons (both trace buckets, all six log levels, all four metric
    # types) still appear for a present signal.
    columns = ["signal", "details", "count", "earliest", "latest", "services"]
    rows: list[Row] = []
    if _has_rows(conn, "traces"):
        rows += _summary_traces(conn)
    if _has_rows(conn, "logs"):
        rows += _summary_logs(conn)
    if _has_rows(conn, "metrics"):
        rows += _summary_metrics(conn)
    if not rows:  # no signal has data -> friendly empty-telemetry path (FR-18)
        raise NoTelemetryError(_NO_TELEMETRY_MSG)
    return columns, rows


def cmd_sql(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    import duckdb  # lazy; see TYPE_CHECKING note above

    try:
        result = conn.execute(args.query)
        # An empty / whitespace / comment-only query has no statement to run, so
        # DuckDB's execute() returns None and result.description below would raise
        # AttributeError (not a duckdb.Error). Route it to the same friendly
        # SQL-error path as any other bad SQL instead of a raw traceback (EC-4).
        if result is None:
            raise SystemExit("otelq: SQL error: empty query")
        columns = [d[0] for d in result.description] if result.description else []
        rows = result.fetchall()
    except duckdb.Error as exc:
        raise SystemExit(f"otelq: SQL error: {exc}")
    return columns, rows


def cmd_errors(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    # errors are derived from error spans and ERROR/FATAL logs; with neither
    # signal carrying data (metrics-only, or nothing), name the gap rather than
    # blaming the collector. "Has data", not mere existence: both relations now
    # resolve (empty) whenever any telemetry is present (FR-1).
    if not _has_rows(conn, "traces") and not _has_rows(conn, "logs"):
        raise NoTelemetryError(
            _no_signal_msg(conn, "traces or logs", ("traces", "logs"))
        )
    # The time window is applied by the relation builder (SPEC INV-7), so the
    # command queries the already-scoped relations.
    columns = ["kind", "timestamp", "service_name", "label", "detail"]
    arms: list[str] = []
    if _has_rows(conn, "traces"):
        arms.append(
            "SELECT 'span' AS kind, timestamp, service_name, "
            "span_name AS label, status_message AS detail "
            "FROM traces WHERE status_code = 2"
        )
    if _has_rows(conn, "logs"):
        # FR-4: match case-insensitively. severity_text carries inconsistent
        # casing in practice (e.g. "Error"), so fold before comparing (cf. FR-2).
        arms.append(
            "SELECT 'log' AS kind, timestamp, service_name, "
            "severity_text AS label, body AS detail "
            "FROM logs WHERE upper(severity_text) IN ('ERROR', 'FATAL')"
        )
    if not arms:
        return columns, []
    # FR-4 orders newest-first; the trailing keys are a deterministic tie-breaker
    # so equal-timestamp rows render in the same order on the hot (parquet) and
    # cold (raw) paths — byte-identical cached vs --no-cache (B-7). ORDER BY in
    # SQL (not a Python sort) also tolerates null label/detail without raising.
    query = (
        " UNION ALL ".join(arms)
        + " ORDER BY timestamp DESC, kind, service_name, label, detail"
    )
    rows = _limited(conn, query, [], getattr(args, "top", _DEFAULT_TOP))
    return columns, rows


def cmd_slow(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    _require(conn, "traces")
    columns = ["timestamp", "service_name", "span_name", "duration_ms", "trace_id"]
    rows = _limited(
        conn,
        # FR-5 orders by duration desc; the trailing keys (all output columns, so
        # they exist on every traces relation) are a deterministic tie-breaker so
        # equal-duration spans — and which ones the LIMIT keeps — match on the hot
        # (parquet) and cold (raw) paths — byte-identical cached vs --no-cache.
        "SELECT timestamp, service_name, span_name, "
        "duration AS duration_ms, trace_id "
        "FROM traces "
        "ORDER BY duration DESC, timestamp DESC, trace_id, service_name, span_name",
        [],
        args.top,
    )
    return columns, rows


def _resolve_trace_id(conn: duckdb.DuckDBPyConnection, wanted: str) -> str:
    """Resolve `wanted` to a full trace id, accepting a unique prefix (F-4).

    An exact match always wins (and is the common path, so it costs one indexed
    lookup). Otherwise a `git`-style unique-prefix match lets an agent paste a
    shortened id; two or more matches raise a friendly SystemExit naming the
    ambiguity rather than silently picking one. A prefix that matches nothing is
    returned unchanged so the caller emits the normal 'no spans found' message."""
    exact = conn.execute(
        "SELECT 1 FROM traces WHERE trace_id = ? LIMIT 1", [wanted]
    ).fetchall()
    if exact:
        return wanted
    # starts_with keeps the term a literal prefix (no LIKE wildcard handling);
    # LIMIT 2 is enough to tell unique from ambiguous without scanning the rest.
    matches = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT trace_id FROM traces WHERE starts_with(trace_id, ?) "
            "ORDER BY trace_id LIMIT 2",
            [wanted],
        ).fetchall()
    ]
    if len(matches) > 1:
        raise SystemExit(
            f"otelq: trace id prefix '{wanted}' is ambiguous "
            f"(matches {matches[0]}, {matches[1]}, …); use more characters"
        )
    if len(matches) == 1:
        return matches[0]
    return wanted


def cmd_trace(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    _require(conn, "traces")
    trace_id = _resolve_trace_id(conn, args.trace_id)
    spans = conn.execute(
        "SELECT span_id, parent_span_id, span_name, service_name, "
        "duration, status_code, timestamp "
        "FROM traces WHERE trace_id = ? ORDER BY timestamp",
        [trace_id],
    ).fetchall()
    if not spans:
        raise NoTelemetryError(f"no spans found for trace_id '{args.trace_id}'")
    span_ids = {s[0] for s in spans}
    children: dict[str | None, list[Row]] = {}
    for span in spans:
        parent = span[1] if (span[1] and span[1] in span_ids) else None
        children.setdefault(parent, []).append(span)
    columns = [
        "depth",
        "span_name",
        "service_name",
        "duration_ms",
        "status_code",
        "span_id",
    ]
    rows: list[Row] = []

    def walk(parent_key: str | None, depth: int) -> None:
        # Order siblings by timestamp (FR-6), then span_id (unique) as a
        # deterministic tie-breaker so the tree renders in the same order on the
        # hot (parquet) and cold (raw) paths — byte-identical cached vs --no-cache.
        for span in sorted(children.get(parent_key, []), key=lambda s: (s[6], s[0])):
            rows.append(
                (depth, "  " * depth + span[2], span[3], span[4], span[5], span[0])
            )
            walk(span[0], depth + 1)

    walk(None, 0)
    return columns, rows


def cmd_logs(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    _require(conn, "logs")
    where: list[str] = []
    params: list[str] = []
    if args.service:
        where.append("service_name = ?")
        params.append(args.service)
    if args.level:
        # FR-7: case-insensitive match. severity_text carries inconsistent
        # casing in practice (e.g. "Info"), so fold both sides, not just input.
        where.append("upper(severity_text) = ?")
        params.append(args.level.upper())
    if args.grep:
        # FR-7: --grep is a literal, case-insensitive SUBSTRING match. ILIKE
        # treated `_` and `%` in the term as wildcards (so "user_id" matched
        # "userXid"); contains() matches the raw text literally, with lower() on
        # both sides keeping it case-insensitive.
        where.append("contains(lower(body), lower(?))")
        params.append(args.grep)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    columns = ["timestamp", "service_name", "severity_text", "body", "trace_id"]
    rows = _limited(
        conn,
        # FR-7 orders newest-first; the trailing keys are a deterministic
        # tie-breaker so rows sharing a timestamp render in the same order on the
        # hot (parquet) and cold (raw) paths — byte-identical cached vs --no-cache.
        f"SELECT timestamp, service_name, severity_text, body, trace_id "
        f"FROM logs{clause} "
        f"ORDER BY timestamp DESC, service_name, severity_text, body, trace_id",
        params,
        getattr(args, "top", _DEFAULT_TOP),
    )
    return columns, rows


def cmd_metric(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    _require(conn, "metrics")
    columns = [
        "timestamp",
        "service_name",
        "metric_name",
        "metric_type",
        "value",
        "metric_unit",
    ]
    rows = _limited(
        conn,
        # FR-8 orders ascending by timestamp; the trailing keys are a
        # deterministic tie-breaker (metric_name is filtered to a constant) so
        # equal-timestamp points render in the same order on the hot (parquet)
        # and cold (raw) paths — byte-identical cached vs --no-cache.
        "SELECT timestamp, service_name, metric_name, metric_type, value, "
        "metric_unit FROM metrics WHERE metric_name = ? "
        "ORDER BY timestamp, service_name, value, metric_type, metric_unit",
        [args.name],
        getattr(args, "top", _DEFAULT_TOP),
    )
    return columns, rows


def render_collector_config() -> str:
    """Emit the reference file-export fragment to merge into a Collector.

    Generated from this module's pinned constants so the rotation thresholds and
    paths can never drift from the .telemetry/ contract. Serves both humans and
    AI agents wiring otelq into an existing (integrated) Collector.
    """
    signals = ("traces", "logs", "metrics")
    exporters: list[str] = ["exporters:"]
    for sig in signals:
        exporters += [
            f"  file/{sig}:",
            f"    path: {COLLECTOR_MOUNT_PATH}/{sig}.jsonl",
            "    flush_interval: 1s",
            "    rotation:",
            f"      max_megabytes: {ROTATION_MAX_MEGABYTES}",
            f"      max_backups: {ROTATION_MAX_BACKUPS}",
        ]
    pipelines: list[str] = ["service:", "  pipelines:"]
    for sig in signals:
        pipelines += [
            f"    {sig}:",
            f"      # add file/{sig} alongside your existing {sig} exporters",
            f"      exporters: [file/{sig}]",
        ]
    return "\n".join(
        [
            "# ── otelq collector integration ─────────────────────────────────────────────",
            "# Make an existing OpenTelemetry Collector write the .telemetry/ contract that",
            "# otelq reads. Requires the *-contrib collector image: the `file` exporter is",
            "# not in the core distribution.",
            "#",
            "# 1) Merge these exporters into your collector config:",
            "",
            *exporters,
            "",
            "# 2) Wire them into your existing pipelines (keep your current exporters too):",
            "",
            *pipelines,
            "",
            "# 3) Bind-mount a host telemetry dir into the collector service (compose):",
            "#",
            "#      volumes:",
            f"#        - ./.telemetry:{COLLECTOR_MOUNT_PATH}",
            "#",
            "# 4) Query it, pointing otelq at that dir:",
            "#",
            "#      otelq --dir ./.telemetry summary",
            "#      otelq --dir ./.telemetry doctor      # verify the wiring",
        ]
    )


def render_troubleshooting() -> str:
    """Emit the capture → query loop and the common fixes.

    Project-agnostic on purpose: this command ships in the CLI and is the
    single home for the loop/fixes guidance, so the query skill can stay a
    thin pointer (`otelq troubleshoot`) instead of restating it.
    """
    return "\n".join(
        [
            "The capture → query loop",
            "",
            "  1. Start the Collector that writes to your telemetry dir, however this",
            "     project starts it.",
            "  2. Make sure the apps you are exercising export OTLP to that Collector",
            "     (e.g. OTEL_EXPORTER_OTLP_ENDPOINT points at it), then (re)start them.",
            "  3. Reproduce the behaviour (hit an endpoint, run a flow, run a test).",
            "  4. Query:  otelq --dir <dir> --format json <command>",
            "  5. Inspect the JSON, then iterate.",
            "",
            "otelq only *reads* the telemetry dir; bringing the Collector up and",
            "resetting it is the project's own concern.",
            "",
            "Fixes",
            "",
            '  Empty output / "no telemetry captured"',
            "      The Collector is not running, or the apps are not exporting OTLP to",
            "      it. Start the Collector, confirm the app's OTLP export is enabled and",
            "      pointed at it, then reproduce again. Run `otelq doctor` to check the",
            "      dir against the contract.",
            "",
            "  Stale data",
            "      Clear the telemetry dir before a fresh run. Stop the Collector first —",
            "      truncating those files while it is writing corrupts them — then empty",
            "      the dir while it is down.",
        ]
    )


def _validate_active_file(path: Path, signal: str) -> tuple[str, str]:
    """Check one signal's active .jsonl against the contract framing."""
    if not path.exists():
        return "WARN", "active file absent (only rotated backups present)"
    if path.stat().st_size == 0:
        return "WARN", "empty — no batches written yet"
    key = OTLP_TOP_LEVEL_KEY[signal]
    try:
        with path.open(encoding="utf-8") as fh:
            first = fh.readline()
        obj = json.loads(first)
    except (OSError, json.JSONDecodeError) as exc:
        return "FAIL", f"first line is not valid OTLP/JSON: {exc}"
    if not isinstance(obj, dict) or key not in obj:
        return "FAIL", f"first line missing top-level '{key}' (wrong signal or format)"
    return "OK", f"valid OTLP/JSON ('{key}' present)"


def _doctor_cache_health(telemetry_dir: Path, cache: Path) -> list[Row]:
    """Non-fatal cache diagnostics for the failure modes that actually bite (D-4):
    a read-only dir (cache silently disabled), a stale writer lock, an
    incompatible cursor, and a far-future watermark (B-1) that hides real
    telemetry. All are OK/INFO/WARN — never FAIL — because queries still answer
    via the cold path regardless."""
    rows: list[Row] = []
    probe = cache if cache.is_dir() else telemetry_dir
    if os.access(probe, os.W_OK):
        rows.append(("cache writable", "OK", f"{probe} is writable"))
    else:
        rows.append(
            (
                "cache writable",
                "WARN",
                f"{probe} is not writable — the parquet cache is disabled and "
                f"every query runs a full cold scan",
            )
        )

    lock = cache / LOCK_FILENAME
    if lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0.0
        if age > _LOCK_STALE_SECS:
            rows.append(
                (
                    "cache lock",
                    "WARN",
                    f"a writer lock has been held ~{int(age)}s (stale; it is "
                    f"reaped automatically on the next query)",
                )
            )
        else:
            rows.append(("cache lock", "INFO", "held (a query is ingesting now)"))

    cursor_file = cache / CURSOR_FILENAME
    payload: dict[str, Any] | None = None
    if cursor_file.exists():
        try:
            loaded: object = json.loads(cursor_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rows.append(
                (
                    "cache cursor",
                    "WARN",
                    "cursor.json is unreadable — the cache rebuilds on the next query",
                )
            )
        else:
            if isinstance(loaded, dict):
                payload = cast(dict[str, Any], loaded)
            ver = payload.get("version") if payload is not None else None
            if ver == CURSOR_SCHEMA_VERSION:
                rows.append(("cache cursor", "OK", f"schema v{ver}"))
            else:
                rows.append(
                    (
                        "cache cursor",
                        "WARN",
                        f"schema v{ver} != current v{CURSOR_SCHEMA_VERSION} — the "
                        f"cache rebuilds on the next query",
                    )
                )

    watermarks: list[datetime] = []
    if payload is not None:
        streams = payload.get("streams")
        if isinstance(streams, dict):
            for state in cast(dict[str, Any], streams).values():
                if isinstance(state, dict):
                    ts = _parse_ts(cast(dict[str, Any], state).get("max_event_ts_seen"))
                    if ts is not None:
                        watermarks.append(ts)
    if watermarks:
        newest = max(watermarks)
        if newest > _future_ceiling():
            rows.append(
                (
                    "clock skew",
                    "WARN",
                    f"newest event-time {_fmt_ts(newest)} is more than "
                    f"{MAX_FUTURE_SKEW} ahead of wall-clock — a skewed producer or "
                    f"bad instrumentation can hide real telemetry; the query window "
                    f"is clamped to compensate (see `otelq troubleshoot`)",
                )
            )
        else:
            rows.append(
                ("clock skew", "OK", "newest event-time is within range of wall-clock")
            )
    return rows


def doctor_report(telemetry_dir: Path) -> tuple[list[Row], bool]:
    """Check a telemetry dir against the contract. Returns (rows, ok)."""
    rows: list[Row] = []
    if not telemetry_dir.is_dir():
        rows.append(("directory", "FAIL", f"{telemetry_dir} does not exist"))
        return rows, False
    rows.append(("directory", "OK", str(telemetry_dir)))

    any_files = False
    ok = True
    for signal in ("traces", "logs", "metrics"):
        files = sorted(glob.glob(str(telemetry_dir / SIGNAL_GLOBS[signal])))
        if not files:
            rows.append((signal, "WARN", "no files — this signal is not being captured"))
            continue
        any_files = True
        status, detail = _validate_active_file(telemetry_dir / f"{signal}.jsonl", signal)
        if status == "FAIL":
            ok = False
        rows.append((signal, status, detail))

    if not any_files:
        rows.append(
            ("telemetry", "FAIL", "no *.jsonl found — is the collector running and exporting?")
        )
        ok = False

    cache = telemetry_dir / CACHE_DIRNAME
    rows.append(
        (".otelq-cache", "INFO", "present" if cache.is_dir() else "absent (built on first query)")
    )
    rows.extend(_doctor_cache_health(telemetry_dir, cache))
    return rows, ok


COMMANDS: dict[str, Command] = {
    "summary": cmd_summary,
    "sql": cmd_sql,
    "errors": cmd_errors,
    "slow": cmd_slow,
    "trace": cmd_trace,
    "logs": cmd_logs,
    "metric": cmd_metric,
}


def _non_negative_int(value: str) -> int:
    """argparse `type` for `slow --top`: a non-negative int.

    A negative value reaches DuckDB as `LIMIT -1`, which raises an uncaught
    BinderException (a raw traceback); reject it at parse time (exit 2) instead.
    A non-int keeps argparse's standard "invalid int value" error, and `--top 0`
    stays valid (it returns zero rows)."""
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: '{value}'")
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {ivalue}")
    return ivalue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="otelq",
        description="Query OTLP telemetry captured by the dev OTel Collector.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            argument order:
              --dir / --format / --all / --no-cache / --since / --verbose are
              GLOBAL flags and must come BEFORE the subcommand:
                otelq --since 10m --format compact errors
              (not: otelq errors --since 10m). Per-command flags (--top, --service,
              --level, --grep) go AFTER the subcommand.

            output format (pick the fewest tokens the consumer can parse):
              --format compact  BEST for agents/LLMs: a single
                                {"columns":[...],"rows":[[...]]} object — column
                                names once, each row a positional array. Lossless
                                and the smallest machine format (no repeated keys).
                                Reconstruct rows with zip(columns, row).
              --format json     a JSON array of per-row objects; use only when a
              --format jsonl    consumer needs self-describing rows / streaming.
              --format csv      spreadsheet/interchange.
              --format table    default; for humans, not for parsing.

            time window (filters by each record's own event-time):
              (default)            a recent window (the cache's hot window)
              --since Ns|Nm|Nh|Nd  only the trailing window, e.g. 30s, 10m, 2h, 1d
              --all                the full captured history (no window)
              `trace` ignores the window — a trace id is looked up across all
              history, and a unique id prefix is accepted.

            row limits:
              errors / slow / logs / metric cap output with --top N and print a
              one-line notice to stderr when the result was truncated.

            sql views (for `otelq sql "<query>"`):
              traces   timestamp, duration (ms), trace_id, span_id, parent_span_id,
                       service_name, span_name, span_kind,
                       status_code (0=unset,1=ok,2=error), status_message
              logs     timestamp, trace_id, service_name, severity_text,
                       severity_number, body
              metrics  timestamp, service_name, metric_name, metric_type, value,
                       metric_unit  (metric_type: gauge|sum|histogram|exp_histogram;
                       value = the value of gauge/sum, the sum of histogram/exp)
              per-type metric relations (metrics unions whichever are present):
                metrics_gauge, metrics_sum               value
                metrics_histogram, metrics_exp_histogram  count, sum, min, max
                       (+ bucket_counts/explicit_bounds, or scale/zero_count/…)
              (the OTel Summary metric type is unsupported by the reader extension)
              the built-in commands read only the telemetry under --dir. `sql`
              is an escape hatch that runs with YOUR user's file access (it can
              read/write local files via read_csv/COPY), so treat untrusted
              queries with the same care as a shell command.

            Run `otelq troubleshoot` for the capture → query loop and common fixes.
            """
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"otelq {__version__}",
        help="print otelq's version and exit",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_DIR,
        help=f"telemetry folder (default: {DEFAULT_DIR})",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "jsonl", "csv", "compact"],
        default="table",
        help="output format (default: table; json/jsonl/compact are compact for agents)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="widen the query to the full raw history (cold scan)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass the parquet cache entirely (pure cold scan)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print the resolved time window and route to stderr",
    )
    # --since is a GLOBAL flag: it shapes the query's time window (the twin of
    # --all), so like the other query-shaping flags it precedes the subcommand
    # and appears in the top-level usage line. See SPEC-otelq-cli FR-11/FR-15.
    parser.add_argument(
        "--since",
        help="restrict to a trailing time window: Ns/Nm/Nh/Nd (e.g. 30s, 10m, 2h, 1d)",
    )

    # Not required: a bare `otelq` (command is None) prints full help in main().
    sub = parser.add_subparsers(dest="command", required=False)
    sub.add_parser("summary", help="counts and time span per signal")
    p_sql = sub.add_parser("sql", help="run an ad-hoc SQL query")
    p_sql.add_argument(
        "query",
        help=(
            "SQL over views: traces, logs, metrics, metrics_gauge, metrics_sum, "
            "metrics_histogram, metrics_exp_histogram"
        ),
    )
    p_errors = sub.add_parser("errors", help="error spans and ERROR/FATAL logs")
    p_errors.add_argument(
        "--top", type=_non_negative_int, default=_DEFAULT_TOP, help="rows to show"
    )
    p_slow = sub.add_parser("slow", help="slowest spans")
    p_slow.add_argument(
        "--top", type=_non_negative_int, default=20, help="rows to show"
    )
    p_trace = sub.add_parser("trace", help="all spans of one trace as a tree")
    p_trace.add_argument("trace_id", help="trace id (a unique prefix is accepted)")
    p_logs = sub.add_parser("logs", help="filtered log records")
    p_logs.add_argument("--service", help="filter by service name")
    p_logs.add_argument("--level", help="filter by severity, e.g. ERROR")
    p_logs.add_argument("--grep", help="case-insensitive substring of the body")
    p_logs.add_argument(
        "--top", type=_non_negative_int, default=_DEFAULT_TOP, help="rows to show"
    )
    p_metric = sub.add_parser("metric", help="time series for one metric")
    p_metric.add_argument("name", help="metric name")
    p_metric.add_argument(
        "--top", type=_non_negative_int, default=_DEFAULT_TOP, help="rows to show"
    )
    sub.add_parser(
        "collector-config",
        help="print the file-export fragment to add to an existing Collector",
    )
    sub.add_parser(
        "doctor",
        help="check that --dir satisfies the telemetry contract",
    )
    sub.add_parser(
        "troubleshoot",
        help="print the capture → query loop and common fixes",
    )
    p_help = sub.add_parser("help", help="show help for otelq or a command")
    p_help.add_argument(
        "topic", nargs="?", help="command to show help for (omit for general help)"
    )
    return parser


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "collector-config":
        print(render_collector_config())
        return 0
    if args.command == "troubleshoot":
        print(render_troubleshooting())
        return 0
    if args.command == "doctor":
        rows, ok = doctor_report(args.dir)
        print(format_output(["check", "status", "detail"], rows, args.format))
        return 0 if ok else 1
    try:
        columns, rows = run_command(args)
    except NoTelemetryError as exc:
        print(exc, file=sys.stderr)
        return 0
    print(format_output(columns, rows, args.format))
    return 0


def _help_for(parser: argparse.ArgumentParser, topic: str | None) -> int:
    """`help` command: print general help when no topic, else the named
    command's own help — delegated to `<topic> -h` so argparse both renders it
    and validates the name (unknown topic -> its usual invalid-choice error)."""
    if topic is None:
        parser.print_help()
        return 0
    try:
        parser.parse_args([topic, "-h"])
    except SystemExit as exc:  # argparse exits after printing help/usage
        return exc.code if isinstance(exc.code, int) else 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Bare `otelq` and `otelq help [topic]` are usability affordances handled
    # before dispatch (FR-22); every other command flows through _dispatch.
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "help":
        return _help_for(parser, args.topic)
    try:
        return _dispatch(args)
    except BrokenPipeError:
        # Downstream closed the pipe (e.g. `otelq ... | head`). Point stdout at
        # /dev/null so the interpreter's flush-on-exit doesn't re-raise, and exit
        # cleanly. (When stdout has no real fd — e.g. under test capture — the
        # dup2 is simply skipped.)
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
