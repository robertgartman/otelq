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

Reads telemetry/*.jsonl (OTLP JSONL written by the Collector fileexporter)
via the smithclay/duckdb-otlp DuckDB extension.

Incremental parquet cache (SPEC-otelq-incremental-cache)
--------------------------------------------------------
Repeated queries no longer re-parse the whole telemetry/ corpus. A per-signal
cursor reads only the new bytes of each raw file; complete minutes are sealed to
parquet under telemetry/.otelq-cache/<signal>/<minute>.parquet once the signal's
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
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict, cast

import duckdb

# Public surface of this single-file module. The CLI entry is `main`; the rest
# is the API the test suite pins. The trailing group is deliberately exported
# despite the leading underscore: it is internal to normal callers but part of
# the behaviour the tests verify directly (lock reaping, the no-telemetry text),
# so listing it here makes that test-visible contract explicit.
__all__ = [
    "main",
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
]

# otelq.py lives at the repo root; telemetry/ is its sibling.
DEFAULT_DIR = Path(__file__).resolve().parent / "telemetry"

SIGNAL_GLOBS = {
    "traces": "traces*.jsonl",
    "logs": "logs*.jsonl",
    "metrics": "metrics*.jsonl",
}

# --- Reference producer (collector-config / doctor) --------------------------
# The Collector that writes the telemetry/ contract is interchangeable (see
# CONTRACT-telemetry-directory: any conformant producer interoperates). otelq
# ships the reference producer settings here so `otelq collector-config` emits
# exactly the pinned values the consumer expects, generated — never hand-copied
# — so they cannot drift from this module. These MUST stay in lockstep with
# otel-collector-dev.yaml and the contract; a test asserts they match.
COLLECTOR_MOUNT_PATH = "/telemetry"  # producer-side bind-mount target
ROTATION_MAX_MEGABYTES = 50  # rotation threshold per active file (< 100 MB reader cap)
ROTATION_MAX_BACKUPS = 5  # retained rotated backups per signal
# The OTLP/JSON top-level key each signal's lines must carry (contract framing).
OTLP_TOP_LEVEL_KEY = {
    "traces": "resourceSpans",
    "logs": "resourceLogs",
    "metrics": "resourceMetrics",
}

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
# Three raw byte-streams feed four cache signals: the single metrics*.jsonl
# stream is read by two readers (gauge + sum). The cursor tracks offsets and a
# watermark per *stream*; parquet partitions are kept per *signal*.
CURSOR_STREAMS = ("traces", "logs", "metrics")
CACHE_SIGNALS = ("traces", "logs", "metrics_gauge", "metrics_sum")
STREAM_SIGNALS = {
    "traces": ("traces",),
    "logs": ("logs",),
    "metrics": ("metrics_gauge", "metrics_sum"),
}
SIGNAL_READERS = {
    "traces": "read_otlp_traces",
    "logs": "read_otlp_logs",
    "metrics_gauge": "read_otlp_metrics_gauge",
    "metrics_sum": "read_otlp_metrics_sum",
}

CACHE_DIRNAME = ".otelq-cache"
PENDING_DIRNAME = ".pending"
CURSOR_FILENAME = "cursor.json"
LOCK_FILENAME = ".lock"
CURSOR_SCHEMA_VERSION = 1
RETENTION_MINUTES = 30  # hot window
MARGIN_MINUTES = 2  # watermark lateness allowance before a minute may seal
FINGERPRINT_BYTES = 256
_LOCK_STALE_SECS = 120
# A lock "held" by a live pid for longer than this is almost certainly a reused
# pid (the original writer is long gone) or a wedged process — reap it so the
# cache can never deadlock permanently. Far longer than any real catch-up seal.
_LOCK_HARD_STALE_SECS = 3600
_TMP_SUFFIX = ".tmp"

_NO_TELEMETRY_MSG = (
    "no telemetry captured — is the collector running (just otel-up) "
    "and OTEL_ENABLED=true?"
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
    # budget bounds both read_otlp_metrics_gauge and read_otlp_metrics_sum.
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


def create_unified_metrics_view(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the `metrics` view as the union of whichever metric tables exist.

    Self-guarding: builds from metrics_gauge and/or metrics_sum, whichever are
    present, and no-ops when neither is. This keeps the hot path (which omits a
    signal that sealed no rows) and the cold path (which materialises an empty
    table) producing the identical `metrics` view — the FR-11 equivalence lever.
    Defined in exactly one place; reused by every relation builder and the test
    fixture.
    """
    have = _existing_relations(conn)
    parts = [
        f"SELECT timestamp, service_name, metric_name, metric_unit, value, "
        f"'{mtype}' AS metric_type FROM {tbl}"
        for tbl, mtype in (("metrics_gauge", "gauge"), ("metrics_sum", "sum"))
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
        select = f"SELECT * REPLACE ({ts_fix}) FROM {reader}('{chunk}')"
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


def _present_signals(have: set[str]) -> list[str]:
    """User-facing signal names (traces/logs/metrics) that have a relation."""
    return [s for s in ("traces", "logs", "metrics") if s in have]


def _no_signal_msg(have: set[str], missing: str, files: tuple[str, ...]) -> str:
    """Error text for a required signal that has no captured data.

    With NOTHING captured the collector is probably down, so the generic hint
    fits. But when other signals ARE present the collector is plainly up, so
    that hint actively misleads (it once cost a whole debugging session chasing
    a healthy collector). In that case name the gap and its usual cause: the
    apps aren't emitting it, or its file was deleted under the running collector
    — high-volume traces reappear on the next rotation, but low-volume
    logs/metrics don't until the collector restarts. `missing` is the human
    label (e.g. "logs", "traces or logs"); `files` are the jsonl base names to
    cite in the remediation hint.
    """
    present = _present_signals(have)
    if not present:
        return _NO_TELEMETRY_MSG
    names = " / ".join(f"{f}.jsonl" for f in files)
    return (
        f"no {missing} telemetry captured (present: {', '.join(present)}). "
        f"The collector is up, so either the apps aren't emitting {missing}, or "
        f"{names} was deleted while the collector was running — high-volume "
        f"traces reappear on rotation but low-volume logs/metrics don't until "
        f"the collector is restarted (see `just otel-clean`)."
    )


def _require(conn: duckdb.DuckDBPyConnection, *relations: str) -> None:
    have = _existing_relations(conn)
    missing = [r for r in relations if r not in have]
    if missing:
        raise NoTelemetryError(_no_signal_msg(have, missing[0], (missing[0],)))


def _fmt_ts(dt: datetime) -> str:
    """Format a datetime as a DuckDB TIMESTAMP literal / cursor string."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


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


def _sql_file_list(paths: list[str]) -> str:
    return "[" + ", ".join("'" + p + "'" for p in paths) + "]"


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
    """Remove stale *.tmp files left by a crashed write (older than the lock TTL,
    so a concurrent writer's in-flight temp file is never reaped)."""
    cdir = cache_dir(telemetry_dir)
    if not cdir.exists():
        return
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
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        (cdir / LOCK_FILENAME).unlink()
    except OSError:
        pass


# --- incremental raw reading -------------------------------------------------


def _file_identity(path: Path) -> str:
    """Identity key for a raw file: inode + hash of its first FINGERPRINT_BYTES.

    Rotation renames the active file to a backup but preserves the inode and the
    immutable prefix bytes, so the key carries over and the stored offset
    resumes (SPEC FR-3). The fingerprint guards inode reuse and filesystems
    where st_ino is 0/non-unique (EC-6); size is deliberately not part of the
    key (it grows, which would churn the key every run)."""
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
    re-read once complete."""
    for src in sorted(glob.glob(str(telemetry_dir / SIGNAL_GLOBS[stream]))):
        path = Path(src)
        try:
            key = _file_identity(path)
            fh = open(path, "rb")
        except OSError:
            continue
        prior = files_state.get(key)
        offset = prior["bytes_consumed"] if prior is not None else 0
        with fh:
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
                arms.append(f"SELECT * FROM read_parquet('{pending.as_posix()}')")
            for chunk in chunks:
                arms.append(
                    f"SELECT * REPLACE ({_TS_FIX}) FROM "
                    f"{SIGNAL_READERS[signal]}('{chunk}')"
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
        if signal not in staged:
            continue
        wm = _parse_ts(cursor["streams"][stream_of(signal)].get("max_event_ts_seen"))
        if wm is None:
            continue
        hot_floor = wm - timedelta(minutes=RETENTION_MINUTES)
        # Retention is per-minute but the query window is sub-minute: the minute
        # straddling hot_floor still holds records >= hot_floor that an in-window
        # query needs. Retain one extra minute below the window so the exact
        # sub-minute window filter in _finalize_relations trims it correctly,
        # keeping the hot read equal to a full raw re-scan (FR-11).
        retain_floor = hot_floor - timedelta(minutes=1)
        # A minute m=[m, m+60s) is sealable once wm has passed its end by MARGIN.
        seal_high = wm - timedelta(seconds=60 + MARGIN_MINUTES * 60)
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
            f"TO '{tmp.as_posix()}' (FORMAT PARQUET)"
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
        conn.execute(f"COPY ({final}) TO '{tmp.as_posix()}' (FORMAT PARQUET)")
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


def _assemble_hot(conn: duckdb.DuckDBPyConnection, telemetry_dir: Path) -> set[str]:
    """Build _all_<signal> = sealed parquet ∪ pending parquet.

    Sealed (complete past minutes) and pending (recent unsealed) are disjoint by
    construction, so a plain UNION ALL never double-counts. Returns the signals
    for which any cached rows exist."""
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
            arms.append(f"SELECT * FROM read_parquet('{pending.as_posix()}')")
        if arms:
            conn.execute(
                f"CREATE TABLE _all_{signal} AS " + " UNION ALL BY NAME ".join(arms)
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
        lo, hi = now_evt - window, now_evt
    for s in present:
        if lo is None or hi is None:
            conn.execute(f"CREATE TABLE {s} AS SELECT * FROM _all_{s}")
        else:
            conn.execute(
                f"CREATE TABLE {s} AS SELECT * FROM _all_{s} "
                f"WHERE timestamp >= TIMESTAMP '{_fmt_ts(lo)}' "
                f"AND timestamp <= TIMESTAMP '{_fmt_ts(hi)}'"
            )
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
    present = _assemble_hot(conn, telemetry_dir)
    if not present:
        build_cold(conn, telemetry_dir, window, tmp_dir)
        return
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
    _finalize_relations(conn, present, window)


def connect(telemetry_dir: Path) -> duckdb.DuckDBPyConnection:
    """Full unfiltered cold connection over a telemetry dir (no cache).

    Retained for the test fixtures and as the simplest read path. Builds the
    query relations directly from every raw file."""
    conn = duckdb.connect(database=":memory:")
    conn.execute("INSTALL otlp FROM community")
    conn.execute("LOAD otlp")
    with tempfile.TemporaryDirectory(prefix="otelq-") as tmp_dir:
        for stream in CURSOR_STREAMS:
            chunks = _chunk_signal(stream, telemetry_dir, tmp_dir)
            if not chunks:
                continue
            for signal in STREAM_SIGNALS[stream]:
                _materialize(conn, signal, SIGNAL_READERS[signal], chunks, _TS_FIX)
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
    """Translate a window like '10m' / '2h' / '1d' into a timedelta."""
    if not since:
        return None
    units = {"m": "minutes", "h": "hours", "d": "days"}
    unit = units.get(since[-1].lower())
    if unit is None or not since[:-1].isdigit():
        raise SystemExit(f"otelq: invalid --since '{since}' (use e.g. 10m, 2h, 1d)")
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
    elif cmd in ("trace", "metric"):
        route = "HOT_THEN_COLD"
    else:
        route = "HOT"
    return Plan(route, window, use_cache)


def build_connection(telemetry_dir: Path, plan: Plan) -> duckdb.DuckDBPyConnection:
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


def run_command(args: argparse.Namespace) -> CommandResult:
    """Plan, build the connection, dispatch, and apply trace/metric cold-fallback."""
    plan = plan_range(args)
    command = COMMANDS[args.command]
    conn = build_connection(args.dir, plan)
    fallback = plan.use_cache and plan.route == "HOT_THEN_COLD"
    try:
        columns, rows = command(conn, args)
    except NoTelemetryError:
        if not fallback:
            raise
        conn = build_connection(args.dir, Plan("COLD", None, True))
        return command(conn, args)
    if fallback and not rows:
        conn = build_connection(args.dir, Plan("COLD", None, True))
        return command(conn, args)
    return columns, rows


def format_output(columns: list[str], rows: list[tuple[Any, ...]], fmt: str) -> str:
    if fmt == "json":
        return json.dumps([dict(zip(columns, r)) for r in rows], default=str, indent=2)
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


def cmd_summary(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    have = _existing_relations(conn)
    columns = ["signal", "count", "earliest", "latest", "services"]
    rows: list[Row] = []
    for signal in ("traces", "logs", "metrics"):
        if signal not in have:
            continue
        count, lo, hi, services = _one_row(
            conn.execute(
                f"SELECT count(*), min(timestamp), max(timestamp), "
                f"count(DISTINCT service_name) FROM {signal}"
            )
        )
        rows.append((signal, count, lo, hi, services))
    if not rows:
        raise NoTelemetryError(_NO_TELEMETRY_MSG)
    return columns, rows


def cmd_sql(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    try:
        result = conn.execute(args.query)
        columns = [d[0] for d in result.description] if result.description else []
        rows = result.fetchall()
    except duckdb.Error as exc:
        raise SystemExit(f"otelq: SQL error: {exc}")
    return columns, rows


def cmd_errors(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    have = _existing_relations(conn)
    if "traces" not in have and "logs" not in have:
        # errors are derived from error spans and ERROR/FATAL logs; metrics
        # alone can't answer it, so name the gap rather than blaming the collector.
        raise NoTelemetryError(
            _no_signal_msg(have, "traces or logs", ("traces", "logs"))
        )
    # The time window is applied by the relation builder (SPEC INV-7), so the
    # command queries the already-scoped relations.
    columns = ["kind", "timestamp", "service_name", "label", "detail"]
    rows: list[Row] = []
    if "traces" in have:
        rows += conn.execute(
            "SELECT 'span', timestamp, service_name, span_name, status_message "
            "FROM traces WHERE status_code = 2"
        ).fetchall()
    if "logs" in have:
        rows += conn.execute(
            "SELECT 'log', timestamp, service_name, severity_text, body "
            "FROM logs WHERE severity_text IN ('ERROR', 'FATAL')"
        ).fetchall()
    rows.sort(key=lambda r: r[1], reverse=True)
    return columns, rows


def cmd_slow(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    _require(conn, "traces")
    columns = ["timestamp", "service_name", "span_name", "duration_ms", "trace_id"]
    rows = conn.execute(
        "SELECT timestamp, service_name, span_name, "
        "round(duration / 1e6, 2) AS duration_ms, trace_id "
        "FROM traces ORDER BY duration DESC LIMIT ?",
        [args.top],
    ).fetchall()
    return columns, rows


def cmd_trace(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    _require(conn, "traces")
    spans = conn.execute(
        "SELECT span_id, parent_span_id, span_name, service_name, "
        "round(duration / 1e6, 2), status_code, timestamp "
        "FROM traces WHERE trace_id = ? ORDER BY timestamp",
        [args.trace_id],
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
        for span in sorted(children.get(parent_key, []), key=lambda s: s[6]):
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
        where.append("severity_text = ?")
        params.append(args.level.upper())
    if args.grep:
        where.append("body ILIKE ?")
        params.append(f"%{args.grep}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    columns = ["timestamp", "service_name", "severity_text", "body", "trace_id"]
    rows = conn.execute(
        f"SELECT timestamp, service_name, severity_text, body, trace_id "
        f"FROM logs{clause} ORDER BY timestamp DESC",
        params,
    ).fetchall()
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
    rows = conn.execute(
        "SELECT timestamp, service_name, metric_name, metric_type, value, "
        "metric_unit FROM metrics WHERE metric_name = ? ORDER BY timestamp",
        [args.name],
    ).fetchall()
    return columns, rows


def render_collector_config() -> str:
    """Emit the reference file-export fragment to merge into a Collector.

    Generated from this module's pinned constants so the rotation thresholds and
    paths can never drift from the telemetry/ contract. Serves both humans and
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
            "# Make an existing OpenTelemetry Collector write the telemetry/ contract that",
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
            f"#        - ./telemetry:{COLLECTOR_MOUNT_PATH}",
            "#",
            "# 4) Query it, pointing otelq at that dir:",
            "#",
            "#      otelq --dir ./telemetry summary",
            "#      otelq --dir ./telemetry doctor      # verify the wiring",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="otelq",
        description="Query OTLP telemetry captured by the dev OTel Collector.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_DIR,
        help=f"telemetry folder (default: {DEFAULT_DIR})",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "csv"],
        default="table",
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

    def add_since(p: argparse.ArgumentParser) -> None:
        p.add_argument("--since", help="time window, e.g. 10m, 2h, 1d")

    sub = parser.add_subparsers(dest="command", required=True)
    add_since(sub.add_parser("summary", help="counts and time span per signal"))
    p_sql = sub.add_parser("sql", help="run an ad-hoc SQL query")
    p_sql.add_argument(
        "query",
        help="SQL over views: traces, logs, metrics, metrics_gauge, metrics_sum",
    )
    add_since(p_sql)
    add_since(sub.add_parser("errors", help="error spans and ERROR/FATAL logs"))
    p_slow = sub.add_parser("slow", help="slowest spans")
    p_slow.add_argument("--top", type=int, default=20, help="rows to show")
    add_since(p_slow)
    p_trace = sub.add_parser("trace", help="all spans of one trace as a tree")
    p_trace.add_argument("trace_id", help="trace id to expand")
    p_logs = sub.add_parser("logs", help="filtered log records")
    p_logs.add_argument("--service", help="filter by service name")
    p_logs.add_argument("--level", help="filter by severity, e.g. ERROR")
    p_logs.add_argument("--grep", help="case-insensitive substring of the body")
    add_since(p_logs)
    p_metric = sub.add_parser("metric", help="time series for one metric")
    p_metric.add_argument("name", help="metric name")
    add_since(p_metric)
    sub.add_parser(
        "collector-config",
        help="print the file-export fragment to add to an existing Collector",
    )
    sub.add_parser(
        "doctor",
        help="check that --dir satisfies the telemetry contract",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "collector-config":
        print(render_collector_config())
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


if __name__ == "__main__":
    sys.exit(main())
