# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.4"]
# ///
# duckdb is pinned exactly because otelq depends on the `otlp` *community*
# extension (smithclay/duckdb-otlp), which is built per DuckDB version and lags
# new releases. An open `>=` floats to the newest DuckDB, for which the
# extension may not yet be published — `INSTALL otlp FROM community` then 404s
# and every otelq command fails. 1.5.4 carries otlp v0.6.0
# (community-extensions.duckdb.org/v1.5.4/<platform>/otlp...). Bump this only
# through the ADR-003 checklist, after confirming the extension exists for the
# target version.
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

Reader schema adoption (ADR-010)
--------------------------------
otelq targets duckdb-otlp v0.6.0 (DuckDB 1.5.4) and adopts the extension's
reader schema natively: the six relations carry the read_otlp_* columns
verbatim (SELECT * at the read seam) — the upstream duckdb-otlp project docs
are the reference for otelq's data model. Each relation's event-time is its
TIMESTAMP_NS column (start_time_unix_nano for traces, time_unix_nano
otherwise, see EVENT_TIME_COLUMNS); commands present a friendly `timestamp`
alias in their output. The reader hard-errors on unparsable input (a
half-written Collector line would fail the whole query), so raw JSONL is
sanitized line-by-line in Python first — undecodable lines are skipped (SPEC
FR-15/FR-21) — and the sanitized bytes are staged to temp files split under
the reader's per-file size cap.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import textwrap
import time
import weakref
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict, cast

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
# report exactly which build it is talking to. The trailing marker lets
# release-please bump this line alongside pyproject.toml (see release-please.yml).
__version__ = "0.5.0"  # x-release-please-version

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
    "HISTORY_DIRNAME",
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
    "_SUMMARY_SERVICE_LABEL",
    "_history_normalize",
    "_history_raw",
    "_history_config",
    "_history_janitor",
    "_HISTORY_NO_STORE_MSG",
    "_triage_config",
    "_TRIAGE_NO_HISTORY_MSG",
    "_TRIAGE_NO_CANDIDATE_MSG",
    # worktree scoping (SPEC-otelq-worktree-scoping)
    "resolve_worktree_identity",
    "_parse_env_assignments",
    "_parse_resource_attributes",
    "_worktree_scope_clause",
    "_run_set_resource_attributes",
    "_execute_sql",
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

# The duckdb-otlp reader (v0.6.0) refuses any file larger than this; sanitized
# staging files are split below it, with margin, so one read_otlp_* call per
# staged file always succeeds.
_READER_FILE_CAP = 100 * 1024 * 1024
_STAGE_FILE_BUDGET = 90 * 1024 * 1024

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

# --- Native reader schema (ADR-010) -------------------------------------------
# The duckdb-otlp v0.6.0 reader columns ARE otelq's data model: relations carry
# them verbatim (SELECT * below), so the upstream project's documentation
# describes otelq's tables and no otelq-side column dictionary can drift.
# Each relation's event-time is its TIMESTAMP_NS column; generic cache logic
# (sealing, hot-window filtering, watermarks) keys on this per-signal mapping.
EVENT_TIME_COLUMNS = {
    "traces": "start_time_unix_nano",
    "logs": "time_unix_nano",
    "metrics_gauge": "time_unix_nano",
    "metrics_sum": "time_unix_nano",
    "metrics_histogram": "time_unix_nano",
    "metrics_exp_histogram": "time_unix_nano",
}


# --- Worktree identity (ADR-011 / SPEC-otelq-worktree-scoping) ----------------
# A git-derived OTel resource attribute distinguishes telemetry from concurrent
# worktrees that share one dev Collector. otelq only ever CONSUMES the tag (and
# writes it into .env.local on request); the instrumented app emits it via
# OTEL_RESOURCE_ATTRIBUTES. The whole feature is opt-in: nothing engages unless
# the telemetry actually carries `otelq.worktree.id` (the master switch, FR-1).
WORKTREE_ID_KEY = "otelq.worktree.id"
WORKTREE_BRANCH_KEY = "otelq.worktree.branch"
_WORKTREE_ENV_VAR = "OTEL_RESOURCE_ATTRIBUTES"
# Precedence for the current worktree identity (FR-2): the per-checkout .env.local
# the launcher sources, then the committed-convention .env, then git.
_WORKTREE_ENV_FILES = (".env.local", ".env")
_WORKTREE_UNTAGGED_LABEL = "(untagged)"
# The one DuckDB named parameter `otelq sql` reserves (FR-13): when a submitted
# query references `$WORKTREE_ID`, otelq binds it to the cwd's env-file worktree
# identity via DuckDB's native parameter binding, never by rewriting the text.
_RESERVED_SQL_PARAM = "WORKTREE_ID"
# The list-shaped aggregates that scope to the current worktree by default
# (FR-5). `summary` (a global discovery map), `trace` (a globally-unique id
# lookup, FR-10), and `sql` (never rewritten, FR-9) are deliberately excluded.
_WORKTREE_SCOPED_COMMANDS = frozenset({"errors", "slow", "logs", "metric"})


def _git_output(git_args: list[str], cwd: Path) -> str | None:
    """Run `git <git_args>` in `cwd`; return trimmed stdout, or None on any
    failure (git absent, not a checkout, non-zero exit). otelq's only git
    touch-point — bounded to worktree-identity resolution, and fail-friendly:
    outside a checkout there is simply no identity (FR-12)."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", *git_args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _git_toplevel(cwd: Path) -> str | None:
    """The worktree's root path — guaranteed unique per worktree (the canonical
    `otelq.worktree.id` value)."""
    return _git_output(["rev-parse", "--show-toplevel"], cwd)


def _git_branch(cwd: Path) -> str | None:
    """The current branch name (a human-friendly `otelq.worktree.branch`).

    `symbolic-ref --short HEAD` resolves the branch on both a normal and an
    unborn branch (a fresh checkout with no commit yet); a detached-HEAD worktree
    has no symbolic HEAD, so fall back to `rev-parse --abbrev-ref HEAD`, which
    reports the non-descriptive `HEAD` there (EC-1). None only if neither yields a
    value (branch is descriptive-only, so its absence never blocks scoping)."""
    return _git_output(["symbolic-ref", "--short", "HEAD"], cwd) or _git_output(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd
    )


def _parse_env_assignments(text: str) -> dict[str, str]:
    """Parse `KEY=VALUE` lines from a .env-style file into a dict, honoring an
    optional `export ` prefix and stripping one layer of matching quotes. Last
    assignment wins. Never raises on odd lines — they are skipped (FR-12)."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def _parse_resource_attributes(value: str) -> dict[str, str]:
    """Parse an `OTEL_RESOURCE_ATTRIBUTES` value (`k1=v1,k2=v2`) into a dict,
    per the OpenTelemetry env-var convention. Malformed pairs are skipped."""
    attrs: dict[str, str] = {}
    for pair in value.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, val = pair.partition("=")
        key = key.strip()
        if key:
            attrs[key] = val.strip()
    return attrs


def _render_resource_attributes(attrs: dict[str, str]) -> str:
    return ",".join(f"{key}={val}" for key, val in attrs.items())


def _worktree_id_from_env_files(cwd: Path) -> str | None:
    """The `otelq.worktree.id` declared in the cwd's .env.local (then .env)
    `OTEL_RESOURCE_ATTRIBUTES`, or None. Empty is treated as absent (FR-11)."""
    for name in _WORKTREE_ENV_FILES:
        try:
            text = (cwd / name).read_text(encoding="utf-8")
        except OSError:
            continue
        raw = _parse_env_assignments(text).get(_WORKTREE_ENV_VAR)
        if not raw:
            continue
        wid = _parse_resource_attributes(raw).get(WORKTREE_ID_KEY)
        if wid:
            return wid
    return None


def resolve_worktree_identity(cwd: Path | None = None) -> str | None:
    """The current worktree identity (FR-2): the env-file `otelq.worktree.id`
    if declared, else `git rev-parse --show-toplevel`, else None. Resolved from
    the CURRENT WORKING DIRECTORY — never from `--dir` (INV-1), so otelq reading
    another worktree's shared store still knows which worktree it itself is."""
    base = Path.cwd() if cwd is None else cwd
    from_env = _worktree_id_from_env_files(base)
    if from_env:
        return from_env
    return _git_toplevel(base)


def _upsert_env_line(lines: list[str], key: str, value: str) -> list[str]:
    """Return `lines` with the first `KEY=`/`export KEY=` assignment replaced by
    `KEY="value"` (preserving an `export ` prefix), or the assignment appended
    when none exists. Every other line is left byte-for-byte intact (INV-6)."""
    rendered = f'{key}="{value}"'
    out: list[str] = []
    replaced = False
    for raw in lines:
        stripped = raw.strip()
        bare = (
            stripped[len("export ") :].lstrip()
            if stripped.startswith("export ")
            else stripped
        )
        if not replaced and bare.startswith(key + "="):
            prefix = "export " if stripped.startswith("export ") else ""
            out.append(prefix + rendered)
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        out.append(rendered)
    return out


def _run_set_resource_attributes(cwd: Path) -> int:
    """`set_resource_attributes` (FR-3): merge the git-derived worktree keys into
    the cwd's .env.local `OTEL_RESOURCE_ATTRIBUTES`, preserving bespoke attributes
    and every other line. A friendly no-op (exit 0) outside a git checkout."""
    top = _git_toplevel(cwd)
    if top is None:
        print(
            "otelq: not a git checkout — nothing written. Run "
            "set_resource_attributes from within a worktree.",
            file=sys.stderr,
        )
        return 0
    branch = _git_branch(cwd) or ""
    env_path = cwd / ".env.local"
    try:
        existing_text = env_path.read_text(encoding="utf-8")
    except OSError:
        existing_text = ""
    attrs = _parse_resource_attributes(
        _parse_env_assignments(existing_text).get(_WORKTREE_ENV_VAR, "")
    )
    attrs[WORKTREE_ID_KEY] = top
    attrs[WORKTREE_BRANCH_KEY] = branch
    lines = existing_text.splitlines() if existing_text else []
    lines = _upsert_env_line(lines, _WORKTREE_ENV_VAR, _render_resource_attributes(attrs))
    try:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"otelq: could not write {env_path}: {exc}", file=sys.stderr)
        return 1
    print(
        f"otelq: wrote worktree identity into {env_path} "
        "(source it before launching the instrumented app):"
    )
    print(f"  {WORKTREE_ID_KEY}     = {top}")
    print(f"  {WORKTREE_BRANCH_KEY} = {branch}")
    print(
        "to scope an `otelq sql` query to this worktree (its rows plus untagged "
        "infra), add to your WHERE (otelq binds $WORKTREE_ID from .env.local):"
    )
    print(f"  {_worktree_scope_clause()}")
    return 0


def _read_select(signal: str, staged_path: str) -> str:
    """The SELECT over one staged file for one signal.

    The single place a read_otlp_* call is written. SELECT * — the reader's
    schema is adopted verbatim (ADR-010); nothing is renamed or converted.
    """
    return f"SELECT * FROM {SIGNAL_READERS[signal]}({_sql_str(staged_path)})"


CACHE_DIRNAME = ".otelq-cache"
PENDING_DIRNAME = ".pending"
CURSOR_FILENAME = "cursor.json"
LOCK_FILENAME = ".lock"
# v3: ADR-010 — the duckdb-otlp v0.6.0 reader schema is adopted natively;
# older caches hold rows in the pre-v0.6.0 canonical shape and must self-wipe
# (FR-14).
CURSOR_SCHEMA_VERSION = 3
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


def _line_is_readable(line: str) -> bool:
    """True when a JSONL line is safe to hand to a read_otlp_* call.

    The v0.6.0 reader hard-errors on any line that is not a JSON object —
    a half-written trailing line would fail the whole query — so this guard
    is what keeps FR-15/FR-21's skip-and-continue behaviour: blank lines,
    partially-written lines (JSONDecodeError), and non-object JSON are
    filtered out before bytes reach the extension. A decodable JSON object
    that is not OTLP is harmless (the reader yields zero rows for it).
    Shared by the cold whole-file reader and the incremental tail reader so
    the skip behaviour can never drift between the two paths.
    """
    if not line.strip():
        return False
    try:
        return isinstance(json.loads(line), dict)
    except json.JSONDecodeError:
        return False  # skip a partially-written trailing line


def _stage_lines(lines: Iterable[str], tmp_dir: str, tag: str) -> list[str]:
    """Write the readable lines of an iterable to sanitized staging file(s).

    Files are split below the reader's per-file size cap so one read_otlp_*
    call per staged file always succeeds. Unreadable lines are skipped
    (_line_is_readable). Returns the staged file paths.
    """
    staged: list[str] = []
    buf: list[str] = []
    buf_bytes = 0

    def flush() -> None:
        nonlocal buf, buf_bytes
        if not buf:
            return
        path = Path(tmp_dir) / f"{tag}_{len(staged)}.jsonl"
        path.write_text("".join(buf), encoding="utf-8")
        staged.append(path.as_posix())
        buf, buf_bytes = [], 0

    for line in lines:
        if not _line_is_readable(line):
            continue
        if buf and buf_bytes + len(line) > _STAGE_FILE_BUDGET:
            flush()
        buf.append(line if line.endswith("\n") else line + "\n")
        buf_bytes += len(line)
    flush()
    return staged


def _stage_stream(stream: str, telemetry_dir: Path, tmp_dir: str) -> list[str]:
    """Sanitize a stream's whole JSONL file(s) into staged file(s).

    Used by the cold path, which re-reads every matching file. Streams lines
    from disk (no full-file buffering) and shares the per-line guard via
    _stage_lines/_line_is_readable.
    """
    sources = sorted(glob.glob(str(telemetry_dir / SIGNAL_GLOBS[stream])))
    if not sources:
        return []

    def line_iter() -> Iterator[str]:
        for src in sources:
            with open(src, encoding="utf-8") as handle:
                yield from handle

    return _stage_lines(line_iter(), tmp_dir, stream)


def _schema_probe_chunk(telemetry_dir: Path, tmp_dir: str) -> str | None:
    """Write one small staged file from whichever stream has data, usable as a
    typed-schema probe for ANY read_otlp_* reader.

    The readers are cross-tolerant: each returns its own fixed typed schema — and
    zero rows — over a batch of any other signal (read_otlp_logs over a traces
    batch yields the logs columns, empty). So the first decodable batch of the
    first stream that has one is enough to seed an empty typed relation for ANY
    absent signal via `read_otlp_<reader>(probe) WHERE false` — drift-free, with
    no embedded schema. Reads at most that first batch — never the whole corpus —
    so it stays cheap on the hot path. Returns the staged path, or None when no
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
                    if not _line_is_readable(line):
                        continue  # blank/partial — try the next batch
                    staged = _stage_lines([line], tmp_dir, "schema_probe")
                    return staged[0] if staged else None
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
    """Write the embedded schema-probe sample to a staged file, return its path.

    The schema-source fallback when the telemetry dir offers no readable line of
    its own, so absent relations can be seeded empty even over a truly empty or
    absent directory (FR-1: no "table does not exist" ever)."""
    staged = _stage_lines([_EMBEDDED_PROBE_LINE], tmp_dir, "schema_probe")
    return staged[0] if staged else None


# gauge/sum split the scalar reading into int_value/double_value; the unified
# `metrics.value` coalesces them to one DOUBLE. histogram/exp_histogram have no
# scalar reading, so `value` surfaces their distribution `sum` (quoted — it is
# a column name, not the aggregate). metric_type tags each row by its origin
# sub-relation; the suffixes match the relation names (metrics_<type>).
_METRIC_VIEW_PARTS: tuple[tuple[str, str, str], ...] = (
    ("metrics_gauge", "gauge", "coalesce(double_value, CAST(int_value AS DOUBLE))"),
    ("metrics_sum", "sum", "coalesce(double_value, CAST(int_value AS DOUBLE))"),
    ("metrics_histogram", "histogram", '"sum"'),
    ("metrics_exp_histogram", "exp_histogram", '"sum"'),
)


def create_unified_metrics_view(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the `metrics` view as the union of whichever metric tables exist.

    Unions all four per-type sub-relations that are present — gauge, sum,
    histogram, exp_histogram — projecting the common columns plus a `metric_type`
    discriminator and `resource_attributes` (so worktree scoping and the census
    can key on it, SPEC-otelq-worktree-scoping; NULL for a sub-relation that
    lacks the column, e.g. a minimal test double). The unified `value` is each
    gauge/sum row's scalar reading (double_value, else int_value) and each
    histogram/exp_histogram row's `sum`. Self-guarding: builds from whichever
    sub-relations are present and no-ops when none are. Both the hot and cold
    paths expose all four sub-relations whenever metrics are present (absent
    types seeded empty) and none when metrics are absent, so they produce the
    identical `metrics` view — the FR-11 equivalence lever. Defined in exactly
    one place; reused by every relation builder and the test fixture.
    """
    have = _existing_relations(conn)
    parts: list[str] = []
    for tbl, mtype, value in _METRIC_VIEW_PARTS:
        if tbl not in have:
            continue
        ra = "resource_attributes" if _column_exists(conn, tbl, "resource_attributes") else "NULL"
        parts.append(
            f"SELECT time_unix_nano, service_name, name, unit, "
            f"{value} AS value, '{mtype}' AS metric_type, "
            f"{ra} AS resource_attributes FROM {tbl}"
        )
    if parts:
        conn.execute("CREATE OR REPLACE VIEW metrics AS " + " UNION ALL ".join(parts))


def _materialize(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    signal: str,
    staged: list[str],
) -> None:
    """Read each staged file with its own read_otlp_* call into one table.

    Each call adopts the reader's schema verbatim (_read_select). The result
    is materialised, so the staged files are no longer needed once this
    returns.
    """
    for index, path in enumerate(staged):
        select = _read_select(signal, path)
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
# The min/max `timestamp` among a result's rows, for the FR-29 response header;
# both are None for a zero-row result.
TimeRange = tuple[datetime | None, datetime | None]


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


def _column_exists(
    conn: duckdb.DuckDBPyConnection, relation: str, column: str
) -> bool:
    """True iff `relation` (table or view) exposes `column`. Lets the worktree
    helpers stay fail-friendly over minimal relations that omit the reader's
    `resource_attributes` column (they simply contribute no worktree tags)."""
    rows = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = ? AND column_name = ? LIMIT 1",
        [relation, column],
    ).fetchall()
    return bool(rows)


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


def _iso_utc(dt: datetime) -> str:
    """Render a naive UTC datetime as an explicit-UTC ISO-8601/RFC-3339 string
    (a trailing 'Z'), so the value itself asserts UTC — a naive `str(datetime)`
    (space separator, no offset) is indistinguishable from any other timezone
    and would leave FR-29's "all timestamps are UTC" notice unverifiable from
    the data alone (FR-16). Millisecond precision (3 decimals, fixed): a fixed
    presentation precision (FR-16 — the stored event-times are ns-exact) that
    keeps the value
    readable — sub-millisecond digits add width without adding signal an
    agent would act on."""
    return dt.isoformat(timespec="milliseconds") + "Z"


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
    stream_staged: dict[str, list[str]],
) -> set[str]:
    """Create _stage_<signal> = persisted pending parquet UNION this run's delta.

    The pending parquet already holds canonical-schema rows, so it is read
    as-is; the delta files go through the reader with the canonical projection
    (_read_select). Both arms share a column set, unioned by name. Returns the
    signals staged.
    """
    staged: set[str] = set()
    for stream in CURSOR_STREAMS:
        delta = stream_staged.get(stream, [])
        for signal in STREAM_SIGNALS[stream]:
            arms: list[str] = []
            pending = pending_path(telemetry_dir, signal)
            if pending.exists():
                arms.append(
                    f"SELECT * FROM read_parquet({_sql_str(pending.as_posix())})"
                )
            for path in delta:
                arms.append(_read_select(signal, path))
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
    stream_staged: dict[str, list[str]] = {}
    for stream in CURSOR_STREAMS:
        new_state: dict[str, FileState] = {}
        stream_staged[stream] = _stage_lines(
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

    staged = _build_staging(conn, telemetry_dir, stream_staged)

    # Advance each stream watermark to the max event-time ever seen.
    for stream in CURSOR_STREAMS:
        candidates: list[datetime] = []
        prev = _parse_ts(cursor["streams"][stream].get("max_event_ts_seen"))
        if prev:
            candidates.append(prev)
        for signal in STREAM_SIGNALS[stream]:
            if signal in staged:
                hi = _scalar(
                    conn.execute(
                        f"SELECT max({EVENT_TIME_COLUMNS[signal]}) "
                        f"FROM _stage_{signal}"
                    )
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
    event_col = EVENT_TIME_COLUMNS[signal]
    minutes = conn.execute(
        f"SELECT DISTINCT date_trunc('minute', {event_col}) AS m "
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
            f"WHERE date_trunc('minute', {event_col}) = TIMESTAMP '{_fmt_ts(m)}') "
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
    event_col = EVENT_TIME_COLUMNS[signal]
    where = f"date_trunc('minute', {event_col}) >= TIMESTAMP '{_fmt_ts(hot_floor)}'"
    if newly_sealed:
        excl = ", ".join(f"TIMESTAMP '{_fmt_ts(m)}'" for m in sorted(newly_sealed))
        where += f" AND date_trunc('minute', {event_col}) NOT IN ({excl})"
    candidate = f"SELECT * FROM _stage_{signal} WHERE {where}"
    # When a candidate minute already has a sealed partition (a genuine late/
    # out-of-order arrival, or a crash that rolled the cursor back and re-read the
    # minute), EXCEPT the partition's rows: a re-read of identical rows is removed
    # (no duplication — INV-2/EC-11), while a genuinely new late row is kept (so
    # the hot read still equals a full raw re-scan — FR-11/INV-4).
    minutes = conn.execute(
        f"SELECT DISTINCT date_trunc('minute', {event_col}) FROM ({candidate})"
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
            f"CREATE TABLE {prefix}{signal} AS "
            f"{_read_select(signal, probe)} WHERE false"
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
            # (P-1). max(event-time) in _finalize uses parquet zone-map stats, not a
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
        v = _scalar(
            conn.execute(f"SELECT max({EVENT_TIME_COLUMNS[s]}) FROM _all_{s}")
        )
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
            event_col = EVENT_TIME_COLUMNS[s]
            conn.execute(
                f"CREATE TABLE {s} AS SELECT * FROM _all_{s} "
                f"WHERE {event_col} >= TIMESTAMP '{_fmt_ts(lo)}' "
                f"AND {event_col} <= TIMESTAMP '{_fmt_ts(hi)}'"
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
        staged = _stage_stream(stream, telemetry_dir, tmp_dir)
        if not staged:
            continue
        for signal in STREAM_SIGNALS[stream]:
            _materialize(conn, f"_all_{signal}", signal, staged)
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
            staged = _stage_stream(stream, telemetry_dir, tmp_dir)
            if not staged:
                continue
            for signal in STREAM_SIGNALS[stream]:
                _materialize(conn, signal, signal, staged)
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


# The six commands whose results carry a clear OpenTelemetry signal and thus get
# the FR-29 response header; `sql` (no fixed signal) and the non-query commands
# (collector-config/troubleshoot/doctor, handled outside run_command) do not.
_HEADER_COMMANDS = frozenset({"summary", "errors", "slow", "trace", "logs", "metric"})
_HEADER_SIGNAL_ORDER = ("traces", "logs", "metrics")
# cmd_errors tags each row "span" or "log" (FR-4); map that to the plural
# Signal names the response header uses (FR-29).
_ERRORS_KIND_SIGNAL = {"span": "traces", "log": "logs"}


def _result_time_range(
    conn: duckdb.DuckDBPyConnection,
    command: str,
    columns: list[str],
    rows: list[Row],
) -> TimeRange:
    """The min/max `timestamp` among a result's rows, for the FR-29 response
    header. `summary`'s rows carry per-bucket `earliest`/`latest` instead of a
    single `timestamp` column (some buckets are zero-count with null bounds);
    `trace`'s rows carry neither (FR-6's columns describe the tree shape, not
    raw fields), so its range is looked up from `traces` by the returned rows'
    own `span_id`s — still strictly derived from the rows actually returned,
    not the query's search window. Not computed for `sql` (arbitrary column
    shapes, no response header to feed anyway)."""
    if not rows or command not in _HEADER_COMMANDS:
        return None, None
    if command == "summary":
        los = [r[3] for r in rows if r[3] is not None]
        his = [r[4] for r in rows if r[4] is not None]
        return (min(los) if los else None, max(his) if his else None)
    if "timestamp" in columns:
        idx = columns.index("timestamp")
        values = [r[idx] for r in rows]
        return (min(values), max(values))
    if command == "trace":
        span_ids = [r[-1] for r in rows]
        placeholders = ",".join("?" * len(span_ids))
        lo, hi = _one_row(
            conn.execute(
                f"SELECT min(start_time_unix_nano), max(start_time_unix_nano) "
                f"FROM traces "
                f"WHERE span_id IN ({placeholders})",
                span_ids,
            )
        )
        return (lo, hi)
    return None, None


def _result_signal(command: str, rows: list[Row]) -> str:
    """The FR-29 header's `<signal>` value: a fixed mapping for the five
    single-signal commands, or the set of signals actually represented among
    the returned rows for `summary`/`errors`, whose rows can span more than one
    OpenTelemetry signal (FR-3, FR-4)."""
    if command in ("slow", "trace"):
        return "traces"
    if command == "logs":
        return "logs"
    if command == "metric":
        return "metrics"
    if command == "summary":
        present = {r[0] for r in rows}
    elif command == "errors":
        present = {_ERRORS_KIND_SIGNAL[r[0]] for r in rows}
    else:
        return "n/a"
    ordered = [s for s in _HEADER_SIGNAL_ORDER if s in present]
    return ", ".join(ordered) if ordered else "n/a"


# json/jsonl/csv/table are self-describing shapes an LLM already recognizes;
# `compact` is otelq-specific, so its header line spells out the shape inline
# rather than relying on the reader already knowing the convention. No single
# de facto standard name matches exactly (pandas' `orient="split"` is the
# closest analog but also carries an `index` array and calls it `data`), so
# this stays a literal description rather than a borrowed, imprecise label.
_FORMAT_SHAPE_HINT = {
    "compact": (
        ', a {"columns":[...],"rows":[[...]]} object — column names once, '
        "each row a positional array"
    ),
}


def _uuid7() -> str:
    """Generate a UUIDv7 (RFC 9562): a 48-bit Unix-millisecond timestamp prefix
    followed by random bits, so ids sort by creation time yet stay collision-
    resistant. Used as the default `--session-id` (FR-33) so consecutive
    invocations of one investigation can share a time-ordered tag without the
    caller having to supply one. Delegates to the stdlib generator when it
    exists (Python 3.14+); otherwise assembles the layout by hand — no third-
    party dependency, keeping otelq's single-dep footprint (duckdb) intact."""
    import uuid

    generator = getattr(uuid, "uuid7", None)
    if generator is not None:
        return str(generator())
    unix_ms = time.time_ns() // 1_000_000
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF  # 12 bits
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)  # 62 bits
    value = (unix_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version 7
    value |= rand_a << 64
    value |= 0b10 << 62  # RFC 9562 variant
    value |= rand_b
    return str(uuid.UUID(int=value))


def resolve_session_id(args: argparse.Namespace) -> str:
    """The effective session id for this invocation (FR-33): the verbatim
    `--session-id` when supplied, else a freshly generated UUIDv7. Resolved once
    per run and reused for both the response header's `Session` line and the
    stderr session footer, so the id an answer is stamped with is exactly the id
    the footer tells the caller to reuse."""
    supplied = getattr(args, "session_id", None)
    return supplied if supplied else _uuid7()


# The stderr session footer (FR-33), printed after EVERY command's answer so an
# agent driving otelq is reminded — on every invocation — how to correlate the
# follow-up calls of one investigation. It lives on stderr, not stdout, so it
# never corrupts the machine-parseable payload (`sql`/json/csv/compact), mirroring
# otelq's other stderr guidance notices (truncation, cold-widen, friendly-empty).
def _session_footer(session_id: str) -> str:
    return (
        f"To track this ongoing analysis, include --session-id {session_id} "
        f"in any consecutive otelq invocations related to the current analysis."
    )


def _format_response_header(
    command: str,
    fmt: str,
    rows: list[Row],
    time_range: TimeRange,
    session_id: str,
    regex: str | None = None,
    regex_removed: int | None = None,
    worktree_banner: str | None = None,
) -> str:
    """Render the FR-29 response header: a fixed plain-text block naming the
    command, format, OpenTelemetry signal(s), UTC time range, and the FR-33
    `Session` id, so an LLM consumer cannot mistake a rendered `timestamp` for
    local time and can carry the session tag into follow-up calls. When
    `--regex` (FR-32) was supplied, two extra lines report the verbatim
    pattern and how many rows it removed, so a caller is never blind to what
    was filtered away — unlike post-hoc `grep` on rendered output. When worktree
    scoping engaged (FR-6), one `Worktree scope:` line names the active scope and
    how many other-worktree rows were hidden; absent otherwise, so untagged
    output is byte-identical (INV-2)."""
    lo, hi = time_range
    from_str = _iso_utc(lo) if lo is not None else "n/a"
    to_str = _iso_utc(hi) if hi is not None else "n/a"
    shape_hint = _FORMAT_SHAPE_HINT.get(fmt, "")
    lines = [
        "==========",
        f"otelq {command} response, format {fmt}{shape_hint}",
        f"OpenTelemetry signal: {_result_signal(command, rows)}",
        f"Time range: {from_str} - {to_str}",
    ]
    if worktree_banner is not None:
        lines.append(worktree_banner)
    if regex is not None:
        lines.append(f"Regex filter applied: {regex}")
        lines.append(f"Rows removed by regex: {regex_removed}")
    lines.append("IMPORTANT: all timestamps are UTC")
    lines.append(f"Session: {session_id}")
    lines.append("----------")
    return "\n".join(lines)


def _regex_cell_str(value: Any) -> str:
    """Render a cell the same way `format_output` eventually will (FR-16), so
    a `--regex` pattern matches what the caller will actually see rendered —
    not a naive `str(datetime)` form the output never shows."""
    return _iso_utc(value) if isinstance(value, datetime) else str(value)


def _apply_regex_filter(
    pattern: re.Pattern[str], rows: list[Row]
) -> tuple[list[Row], int]:
    """Keep only rows where `pattern` matches at least one cell's string form
    (FR-32). `None` cells are excluded from matching entirely — otherwise a
    stray `None` would stringify to the literal text "None" and could
    accidentally match a pattern that has nothing to do with a null value."""
    kept = [
        r
        for r in rows
        if any(pattern.search(_regex_cell_str(v)) for v in r if v is not None)
    ]
    return kept, len(rows) - len(kept)


def _resolve_regex_arg(args: argparse.Namespace) -> re.Pattern[str] | None:
    """Validate and compile `--regex` (FR-32): a real error for an unsupported
    command or a malformed pattern, never a silent no-op or a raw traceback."""
    pattern_text = getattr(args, "regex", None)
    if pattern_text is None:
        return None
    if args.command not in _HEADER_COMMANDS:
        supported = ", ".join(sorted(_HEADER_COMMANDS))
        raise SystemExit(
            f"otelq: --regex is not supported for '{args.command}' "
            f"(only: {supported})"
        )
    try:
        return re.compile(pattern_text)
    except re.error as exc:
        raise SystemExit(f"otelq: invalid --regex pattern: {exc}")


def run_command(
    args: argparse.Namespace, regex: re.Pattern[str] | None
) -> tuple[
    list[str], list[Row], TimeRange, int | None, tuple[list[str], list[Row]] | None, str | None
]:
    """Plan, build the connection, dispatch, and apply trace/metric cold-fallback.

    `regex` (already compiled and scope-validated by the caller, FR-32) is
    applied to each candidate result before the time range is computed, so
    `Time range` in the header reflects the rows actually returned. The
    fourth return value is the regex-removed row count, or `None` when no
    `--regex` was supplied. The fifth is `summary`'s service census as
    (columns, rows) (FR-4), or `None` for every other command. The sixth is the
    worktree-scope header banner (FR-6), or `None` when no worktree behavior
    engaged (⇒ untagged output stays byte-identical, INV-2)."""
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
        # Resolve worktree scope BEFORE dispatch so scoped commands see the
        # predicate on args; the banner is computed AFTER, over the same window.
        args._worktree_scope = _resolve_worktree_scope(conn, args)
        try:
            columns, rows = command(conn, args)
        except NoTelemetryError:
            if not fallback:
                raise
        else:
            if not (fallback and not rows):
                regex_removed = None
                if regex is not None:
                    rows, regex_removed = _apply_regex_filter(regex, rows)
                time_range = _result_time_range(conn, args.command, columns, rows)
                services = _maybe_service_rows(conn, args.command)
                banner = _worktree_banner(conn, args)
                return columns, rows, time_range, regex_removed, services, banner
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
        args._worktree_scope = _resolve_worktree_scope(conn, args)
        columns, rows = command(conn, args)
        regex_removed = None
        if regex is not None:
            rows, regex_removed = _apply_regex_filter(regex, rows)
        time_range = _result_time_range(conn, args.command, columns, rows)
        services = _maybe_service_rows(conn, args.command)
        banner = _worktree_banner(conn, args)
        return columns, rows, time_range, regex_removed, services, banner


def format_output(columns: list[str], rows: list[tuple[Any, ...]], fmt: str) -> str:
    # Render every `timestamp`/`earliest`/`latest` value as an explicit-UTC
    # ISO-8601 string up front, once, so all five formats (table/json/jsonl/
    # csv/compact) show a value that asserts UTC itself (FR-16) rather than a
    # naive `str(datetime)` that looks identical for any timezone.
    rows = [
        tuple(_iso_utc(v) if isinstance(v, datetime) else v for v in r) for r in rows
    ]
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
_TRACE_SLOW_NS = 1_000_000_000  # 1s in nanoseconds; the >1s / =<1s span split
# (duration_time_unix_nano is integer nanoseconds — ADR-010 native schema).
# The ns→ms divisor for the presented duration_ms output column.
_NS_PER_MS = 1_000_000


def _summary_agg(event_col: str) -> str:
    """Per-subset aggregates every summary row carries (count, span, distinct
    services), keyed on the relation's own event-time column (ADR-010)."""
    return (
        f"count(*), min({event_col}), max({event_col}), "
        f"count(DISTINCT service_name)"
    )


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


# --- Worktree scoping SQL + activation (SPEC-otelq-worktree-scoping) ----------


def _worktree_id_sql(column: str = "resource_attributes") -> str:
    """SQL that extracts a normalized `otelq.worktree.id` from a JSON
    `resource_attributes` column: the string value, with empty coerced to NULL
    so an empty tag reads as untagged (FR-11)."""
    path = f'$."{WORKTREE_ID_KEY}"'
    return f"NULLIF(json_extract_string({column}, '{path}'), '')"


def _worktree_branch_sql(column: str = "resource_attributes") -> str:
    path = f'$."{WORKTREE_BRANCH_KEY}"'
    return f"NULLIF(json_extract_string({column}, '{path}'), '')"


def _worktree_scope_clause(column: str = "resource_attributes") -> str:
    """A ready-to-paste SQL predicate that scopes an `otelq sql` query to the
    CURRENT worktree, **mine-or-untagged** (FR-9): the row's `otelq.worktree.id`
    is absent OR equals the reserved `$WORKTREE_ID` parameter, which otelq binds
    from the cwd's .env.local at query time (FR-13) — no literal id is embedded,
    so the snippet is byte-identical across worktrees and never needs
    hand-editing. Same shape as the parameterized `_worktree_predicate` the
    built-in commands apply (INV-5), and the single generator reused by
    `set_resource_attributes` (FR-3) so the snippet is identical wherever otelq
    emits it."""
    expr = _worktree_id_sql(column)
    return f"({expr} IS NULL OR {expr} = ${_RESERVED_SQL_PARAM})"



def _worktree_resource_union(conn: duckdb.DuckDBPyConnection) -> str | None:
    """A `SELECT resource_attributes` union over whichever of traces/logs/metrics
    both resolve and carry the column, or None. Defensive so the master switch
    and hidden-row census never raise on a minimal relation (FR-12)."""
    have = _existing_relations(conn)
    parts = [
        f"SELECT resource_attributes FROM {rel}"
        for rel in ("traces", "logs", "metrics")
        if rel in have and _column_exists(conn, rel, "resource_attributes")
    ]
    return " UNION ALL ".join(parts) if parts else None


def _worktree_tags_present(conn: duckdb.DuckDBPyConnection) -> bool:
    """The master switch (FR-1): True iff at least one row under the active
    window carries a non-empty `otelq.worktree.id`. Absent ⇒ every command is
    byte-identical to its pre-feature behavior (INV-2)."""
    union = _worktree_resource_union(conn)
    if union is None:
        return False
    expr = _worktree_id_sql()
    row = conn.execute(
        f"SELECT 1 FROM ({union}) WHERE {expr} IS NOT NULL LIMIT 1"
    ).fetchone()
    return row is not None


def _worktree_hidden_stats(
    conn: duckdb.DuckDBPyConnection, scope_id: str
) -> tuple[int, int]:
    """Window-level (rows_hidden, distinct_other_worktrees): the tagged rows from
    OTHER worktrees across traces∪logs∪metrics that scoping to `scope_id` hides
    (untagged rows are shown, so never counted). Uniform across scoped commands
    (FR-6)."""
    union = _worktree_resource_union(conn)
    if union is None:
        return 0, 0
    expr = _worktree_id_sql()
    row = conn.execute(
        f"SELECT count(*), count(DISTINCT {expr}) FROM ({union}) "
        f"WHERE {expr} IS NOT NULL AND {expr} <> ?",
        [scope_id],
    ).fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


@dataclass(frozen=True)
class _WorktreeScope:
    """Resolved scoping state for one command invocation.

    `active` ⇒ the command filters rows to `scope_id` (mine-or-untagged).
    `mode` drives the header banner: 'scope' (active), 'all' (--all-worktrees),
    'no-identity' (tags present but identity undefined), or 'off' (byte-identical,
    no banner)."""

    active: bool
    scope_id: str | None
    mode: str


def _resolve_worktree_scope(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> _WorktreeScope:
    """Decide whether/how a scoped command filters (FR-5, FR-7, FR-8). Identity
    is resolved (a git touch) ONLY on the opt-in path — tags present and no
    explicit flag — so untagged invocations pay no git cost and stay
    byte-identical."""
    if args.command not in _WORKTREE_SCOPED_COMMANDS:
        return _WorktreeScope(False, None, "off")
    if getattr(args, "all_worktrees", False):  # explicit global view (FR-7)
        mode = "all" if _worktree_tags_present(conn) else "off"
        return _WorktreeScope(False, None, mode)
    if not _worktree_tags_present(conn):  # master switch off (FR-1)
        return _WorktreeScope(False, None, "off")
    identity = resolve_worktree_identity()
    if identity is None:  # tagged telemetry but no resolvable identity (FR-8)
        return _WorktreeScope(False, None, "no-identity")
    return _WorktreeScope(True, identity, "scope")


def _worktree_predicate(args: argparse.Namespace) -> tuple[str, list[str]]:
    """The mine-or-untagged WHERE fragment for a scoped command, or ('', []) when
    scoping is inactive. Always includes the `IS NULL` branch so untagged rows
    are never hidden (INV-5)."""
    scope: _WorktreeScope | None = getattr(args, "_worktree_scope", None)
    if scope is None or not scope.active or scope.scope_id is None:
        return "", []
    expr = _worktree_id_sql()
    return f"({expr} IS NULL OR {expr} = ?)", [scope.scope_id]


def _worktree_banner(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> str | None:
    """The header banner line for a scoped command (FR-6), or None when no
    worktree behavior engaged (⇒ untagged output is byte-identical, INV-2)."""
    scope: _WorktreeScope | None = getattr(args, "_worktree_scope", None)
    if scope is None:
        return None
    if scope.mode == "off":
        return None
    if scope.mode == "all":
        return "Worktree scope: all worktrees (scoping disabled via --all-worktrees)"
    if scope.mode == "no-identity":
        return (
            "Worktree scope: none resolved (not in a git checkout and no "
            ".env.local OTEL_RESOURCE_ATTRIBUTES); showing all worktrees"
        )
    hidden, others = _worktree_hidden_stats(conn, scope.scope_id or "")
    return (
        f"Worktree scope: {scope.scope_id} — this worktree + untagged; "
        f"{hidden} row(s) from {others} other worktree(s) hidden "
        f"(use --all-worktrees to include)"
    )


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
    bucket = (
        f"CASE WHEN duration_time_unix_nano > {_TRACE_SLOW_NS} "
        f"THEN '>1s' ELSE '=<1s' END"
    )
    got = {
        r[0]: r[1:]
        for r in conn.execute(
            f"SELECT {bucket} AS b, {_summary_agg('start_time_unix_nano')} "
            f"FROM traces GROUP BY b"
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
            f"SELECT {level} AS lvl, {_summary_agg('time_unix_nano')} "
            f"FROM logs GROUP BY lvl"
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
            f"SELECT metric_type AS t, {_summary_agg('time_unix_nano')} "
            f"FROM metrics GROUP BY t"
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


# `summary`'s service-list second block (FR-3): the pivot of the per-signal
# view — one row per service, counted across all three signals — so a caller
# sees which services dominate the window and are worth zooming in on. Printed
# after the per-signal block in every --format, behind a plain-text delimiter
# that keeps the two format-rendered blocks unambiguous for a machine consumer.
_SUMMARY_SERVICE_COLUMNS = ["service", "count"]
# When worktree telemetry is present the census inserts worktree columns BETWEEN
# the leading service column and the trailing count (FR-4); it stays global
# (never scoped, INV-3).
_SUMMARY_SERVICE_WORKTREE_COLUMNS = [
    "service",
    "otelq.worktree.id",
    "otelq.worktree.branch",
    "count",
]
_SUMMARY_SERVICE_LABEL = "** List of services in telemetry data **"


def _summary_by_service(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[list[str], list[Row]]:
    """`summary`'s service census (FR-4), always GLOBAL (never scoped, INV-3).

    When worktree telemetry is present, group by
    (otelq.worktree.id, otelq.worktree.branch, service_name) with untagged rows
    rendered under `(untagged)`; otherwise keep the pre-feature (service, count)
    shape byte-for-byte (INV-2). Ordered by count desc then the grouping keys for
    determinism (byte-identical cached vs --no-cache). All three relations resolve
    under expose-empty (FR-1)."""
    if _worktree_tags_present(conn):
        wid = _worktree_id_sql()
        wbr = _worktree_branch_sql()
        rows = conn.execute(
            f"SELECT service_name, "
            f"COALESCE({wid}, '{_WORKTREE_UNTAGGED_LABEL}') AS worktree_id, "
            f"COALESCE({wbr}, '') AS worktree_branch, "
            f"count(*) AS n FROM ("
            "  SELECT service_name, resource_attributes FROM traces"
            "  UNION ALL SELECT service_name, resource_attributes FROM logs"
            "  UNION ALL SELECT service_name, resource_attributes FROM metrics"
            ") GROUP BY 1, 2, 3 ORDER BY n DESC, service_name, worktree_id, worktree_branch"
        ).fetchall()
        return _SUMMARY_SERVICE_WORKTREE_COLUMNS, rows
    rows = conn.execute(
        "SELECT service_name, count(*) AS n FROM ("
        "  SELECT service_name FROM traces"
        "  UNION ALL SELECT service_name FROM logs"
        "  UNION ALL SELECT service_name FROM metrics"
        ") GROUP BY service_name ORDER BY n DESC, service_name"
    ).fetchall()
    return _SUMMARY_SERVICE_COLUMNS, rows


def _maybe_service_rows(
    conn: duckdb.DuckDBPyConnection, command: str
) -> tuple[list[str], list[Row]] | None:
    """`summary`'s service census as (columns, rows) — the columns vary with the
    worktree master switch (FR-4) — or `None` for every other command. Computed
    here (not in `cmd_summary`) so it stays out of the command's own single-result
    contract and the direct-call summary tests."""
    return _summary_by_service(conn) if command == "summary" else None


def _sql_named_parameters(
    conn: duckdb.DuckDBPyConnection, query: str
) -> set[str]:
    """The DuckDB named parameters (`$name`) the query references, per DuckDB's
    own parser — a token inside a string literal or comment is therefore NOT
    counted (EC-8). Empty on any parse failure, so the caller runs the query
    unchanged and DuckDB surfaces the real syntax error (FR-13)."""
    import duckdb  # lazy; see TYPE_CHECKING note above

    try:
        statements = conn.extract_statements(query)
    except duckdb.Error:
        return set()
    names: set[str] = set()
    for statement in statements:
        names |= statement.named_parameters
    return names


def _execute_sql(
    conn: duckdb.DuckDBPyConnection, query: str, cwd: Path | None = None
) -> duckdb.DuckDBPyConnection | None:
    """Run a user `sql` query verbatim (FR-9). The one value otelq may supply is
    the reserved `$WORKTREE_ID` named parameter, and only when the query itself
    references it (FR-13): otelq binds it to the cwd's env-file worktree identity
    via DuckDB's native parameter binding — the query text is never rewritten.
    Referenced-but-unresolvable fails friendly and runs nothing; not referenced
    ⇒ the query runs untouched (preserves FR-1/INV-2, no env-file read).

    `execute()` really can return `None` for an empty / whitespace /
    comment-only query at runtime, even though the installed duckdb stub
    declares a non-Optional return type — this wrapper's own signature
    reflects the observed behavior instead of the stub's (EC-4, test_bug5)."""
    if _RESERVED_SQL_PARAM in _sql_named_parameters(conn, query):
        base = Path.cwd() if cwd is None else cwd
        worktree_id = _worktree_id_from_env_files(base)
        if not worktree_id:
            raise SystemExit(
                "otelq: query references $WORKTREE_ID but no otelq.worktree.id "
                "is set in .env.local / .env — run "
                "`otelq set_resource_attributes` (or set "
                "OTEL_RESOURCE_ATTRIBUTES) first"
            )
        return conn.execute(query, {_RESERVED_SQL_PARAM: worktree_id})
    return conn.execute(query)


def cmd_sql(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    import duckdb  # lazy; see TYPE_CHECKING note above

    # The query-history tables ride along as views in the sql escape hatch
    # (ADR-009); best-effort — a torn store must never break an unrelated
    # query. `dir` is absent when cmd_sql is driven directly over a bare
    # connection (tests/fixtures); there is no store to expose then.
    tdir = getattr(args, "dir", None)
    if tdir is not None:
        try:
            _history_create_views(conn, tdir)
        except duckdb.Error:
            pass
    try:
        result = _execute_sql(conn, args.query)
        # An empty / whitespace / comment-only query has no statement to run, so
        # result is None here, and result.description below would otherwise
        # raise AttributeError (not a duckdb.Error). Route it to the same
        # friendly SQL-error path as any other bad SQL (EC-4).
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
    # command queries the already-scoped relations. trace_id travels with every
    # row (FR-4): errors is the triage entry of an investigation and the trace
    # tree its localization step, so the pivot key to `trace <id>` (FR-6) must
    # not require a second sql lookup. A log with no trace context carries its
    # raw (empty) value.
    columns = ["kind", "timestamp", "service_name", "label", "detail", "trace_id"]
    # Worktree scoping (FR-5): AND the mine-or-untagged predicate onto each arm's
    # own WHERE; the param repeats once per arm, in union order.
    pred, pred_params = _worktree_predicate(args)
    scope = f" AND {pred}" if pred else ""
    arms: list[str] = []
    params: list[str] = []
    if _has_rows(conn, "traces"):
        arms.append(
            "SELECT 'span' AS kind, start_time_unix_nano AS timestamp, "
            "service_name, name AS label, status_status_message AS detail, "
            "trace_id FROM traces WHERE status_code = 2" + scope
        )
        params.extend(pred_params)
    if _has_rows(conn, "logs"):
        # FR-4: match case-insensitively. severity_text carries inconsistent
        # casing in practice (e.g. "Error"), so fold before comparing (cf. FR-2).
        arms.append(
            "SELECT 'log' AS kind, time_unix_nano AS timestamp, service_name, "
            "severity_text AS label, body AS detail, trace_id "
            "FROM logs WHERE upper(severity_text) IN ('ERROR', 'FATAL')" + scope
        )
        params.extend(pred_params)
    if not arms:
        return columns, []
    # FR-4 orders newest-first; the trailing keys are a deterministic tie-breaker
    # so equal-timestamp rows render in the same order on the hot (parquet) and
    # cold (raw) paths — byte-identical cached vs --no-cache (B-7). ORDER BY in
    # SQL (not a Python sort) also tolerates null label/detail without raising.
    query = (
        " UNION ALL ".join(arms)
        + " ORDER BY timestamp DESC, kind, service_name, label, detail, trace_id"
    )
    rows = _limited(conn, query, params, getattr(args, "top", _DEFAULT_TOP))
    return columns, rows


def cmd_slow(
    conn: duckdb.DuckDBPyConnection, args: argparse.Namespace
) -> CommandResult:
    _require(conn, "traces")
    columns = ["timestamp", "service_name", "span_name", "duration_ms", "trace_id"]
    pred, pred_params = _worktree_predicate(args)  # FR-5
    where = f" WHERE {pred}" if pred else ""
    rows = _limited(
        conn,
        # FR-5 orders by duration desc; the trailing keys (all present on every
        # traces relation) are a deterministic tie-breaker so
        # equal-duration spans — and which ones the LIMIT keeps — match on the hot
        # (parquet) and cold (raw) paths — byte-identical cached vs --no-cache.
        f"SELECT start_time_unix_nano AS timestamp, service_name, "
        f"name AS span_name, "
        f"duration_time_unix_nano // {_NS_PER_MS} AS duration_ms, trace_id "
        f"FROM traces{where} "
        f"ORDER BY duration_time_unix_nano DESC, start_time_unix_nano DESC, "
        f"trace_id, service_name, name",
        pred_params,
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
        f"SELECT span_id, parent_span_id, name AS span_name, service_name, "
        f"duration_time_unix_nano // {_NS_PER_MS} AS duration_ms, status_code, "
        f"start_time_unix_nano AS timestamp "
        f"FROM traces WHERE trace_id = ? ORDER BY start_time_unix_nano",
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
    pred, pred_params = _worktree_predicate(args)  # FR-5
    if pred:
        where.append(pred)
        params.extend(pred_params)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    columns = ["timestamp", "service_name", "severity_text", "body", "trace_id"]
    rows = _limited(
        conn,
        # FR-7 orders newest-first; the trailing keys are a deterministic
        # tie-breaker so rows sharing a timestamp render in the same order on the
        # hot (parquet) and cold (raw) paths — byte-identical cached vs --no-cache.
        f"SELECT time_unix_nano AS timestamp, service_name, severity_text, "
        f"body, trace_id "
        f"FROM logs{clause} "
        f"ORDER BY time_unix_nano DESC, service_name, severity_text, body, "
        f"trace_id",
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
    pred, pred_params = _worktree_predicate(args)  # FR-5
    scope = f" AND {pred}" if pred else ""
    rows = _limited(
        conn,
        # FR-8 orders ascending by event-time; the trailing keys are a
        # deterministic tie-breaker (name is filtered to a constant) so
        # equal-timestamp points render in the same order on the hot (parquet)
        # and cold (raw) paths — byte-identical cached vs --no-cache.
        f"SELECT time_unix_nano AS timestamp, service_name, "
        f"name AS metric_name, metric_type, value, "
        f"unit AS metric_unit FROM metrics WHERE name = ?{scope} "
        f"ORDER BY time_unix_nano, service_name, value, metric_type, unit",
        [args.name, *pred_params],
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
            timestamps: ALL timestamps — printed by otelq and written into a
              `sql` query — are UTC. Write `sql` timestamp literals bare
              ('YYYY-MM-DD HH:MM:SS') or 'Z'-suffixed ('...T10:00:00Z'); never
              a non-Z offset (e.g. +02:00) — DuckDB silently drops it instead
              of converting, so the comparison would be silently wrong.

            argument order:
              --dir / --format / --all / --no-cache / --since / --regex /
              --verbose are GLOBAL flags and must come BEFORE the subcommand:
                otelq --since 10m --format compact errors
              (not: otelq errors --since 10m). Per-command flags (--top, --service,
              --level, --grep) go AFTER the subcommand.

            output format (pick the fewest tokens the consumer can parse):
              --format compact  DEFAULT. BEST for agents/LLMs: a single
                                {"columns":[...],"rows":[[...]]} object — column
                                names once, each row a positional array. Lossless
                                and the smallest machine format (no repeated keys).
                                Reconstruct rows with zip(columns, row).
              --format json     a JSON array of per-row objects; use only when a
              --format jsonl    consumer needs self-describing rows / streaming.
              --format csv      spreadsheet/interchange.
              --format table    for a human reading the terminal, not for parsing.

            time window (filters by each record's own event-time):
              (default)            a recent window (the cache's hot window)
              --since Ns|Nm|Nh|Nd  only the trailing window, e.g. 30s, 10m, 2h, 1d
              --all                the full captured history (no window)
              `trace` ignores the window — a trace id is looked up across all
              history, and a unique id prefix is accepted.

            row limits:
              errors / slow / logs / metric cap output with --top N and print a
              one-line notice to stderr when the result was truncated.

            worktree scoping (opt-in; engages only when telemetry carries
              otelq.worktree.id tags — otherwise every command is unchanged):
              errors / slow / logs / metric default to the CURRENT worktree
              (plus untagged rows) and print a `Worktree scope:` header line.
              --all-worktrees   include every worktree (disable scoping)
              (GLOBAL; `summary`, `trace`, and `sql` are never scoped). To
              inspect one specific other worktree, filter in `sql` on
              otelq.worktree.id. Run `set_resource_attributes` to write the
              git-derived tags into ./.env.local for the launcher to source.

            regex filtering (summary/errors/slow/trace/logs/metric only):
              --regex PATTERN  keep only rows matching PATTERN in some cell.
                               Applied BEFORE rendering, so JSON escaping/CSV
                               quoting/table padding never affect precision —
                               precise field-level matching, not `| grep` on
                               already-rendered text. The response header
                               reports the verbatim pattern and how many rows
                               it removed, so you're never blind to what was
                               filtered (unlike piping through grep). Standard
                               Python re syntax, case-sensitive by default —
                               use inline (?i) for case-insensitive. Applies to
                               the same already --top-capped result; raise
                               --top to search further. Not supported for sql
                               (use WHERE col ~ 'pattern' — DuckDB has native
                               regex) or collector-config/doctor/troubleshoot.

            sql views (for `otelq sql "<query>"`):
              data model: the duckdb-otlp v0.6.0 reader schema, adopted
              verbatim (see the duckdb-otlp project docs for full semantics).
              Below is a curated subset. Explore the full live
              schema with standard DuckDB introspection, e.g.
              sql "DESCRIBE traces" or sql "PRAGMA table_info('logs')" — it
              reveals extra columns (span_attributes/log_attributes/
              metric_attributes, resource_attributes, scope_attributes, ...)
              carrying whatever custom OTel tags an app actually emits.
              IMPORTANT — isolate your `sql` to this worktree: unlike the built-in
              commands, `sql` is NEVER auto-scoped, so on a shared Collector it
              sees EVERY concurrent worktree's rows. Scope a query to THIS
              worktree (its own rows OR untagged infra) by AND-ing the
              mine-or-untagged predicate on otelq.worktree.id into your WHERE.
              Reference the reserved $WORKTREE_ID parameter — otelq binds it to
              THIS worktree's id from .env.local at query time (never rewriting
              your text), so the snippet is identical across worktrees, e.g.
                sql "SELECT * FROM logs WHERE
                     (NULLIF(json_extract_string(resource_attributes,
                     '$.\"otelq.worktree.id\"'), '') IS NULL
                     OR NULLIF(json_extract_string(resource_attributes,
                     '$.\"otelq.worktree.id\"'), '') = $WORKTREE_ID)"
              Run `set_resource_attributes` to print THIS worktree's exact id and
              a ready-to-paste copy of that predicate — no need to hand-build it.
              traces   start_time_unix_nano (event-time),
                       duration_time_unix_nano (ns), trace_id, span_id,
                       parent_span_id, service_name, name (span name), kind,
                       status_code (0=unset,1=ok,2=error), status_status_message
              logs     time_unix_nano (event-time), trace_id, service_name,
                       severity_text, severity_number, body
              metrics  time_unix_nano (event-time), service_name, name,
                       metric_type, value, unit
                       (metric_type: gauge|sum|histogram|exp_histogram;
                       value = gauge/sum's double_value else int_value,
                       the sum of histogram/exp)
              per-type metric relations (metrics unions whichever are present):
                metrics_gauge, metrics_sum               int_value, double_value
                metrics_histogram, metrics_exp_histogram  count, sum, min, max
                       (+ bucket_counts/explicit_bounds, or scale/zero_count/…)
              (the OTel Summary metric type is unsupported by the reader extension)
              *_unix_nano event-time columns are naive UTC TIMESTAMP_NS — see
              "timestamps" above for the
              literal convention when filtering on them.
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
        default="compact",
        help="output format (default: compact, the fewest-token format for "
        "agents; pass --format table for a human-readable view)",
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
    # --regex is a GLOBAL flag: like --format, it applies to the result of
    # whichever query command runs. See SPEC-otelq-cli FR-11/FR-32.
    parser.add_argument(
        "--regex",
        help="keep only rows matching this pattern in some cell (summary/errors/"
        "slow/trace/logs/metric only); reported in the response header",
    )
    # --session-id is a GLOBAL flag: it tags a run of related invocations that
    # make up one investigation, echoed verbatim in the response header and the
    # stderr session footer so consecutive calls can be correlated. Omitted -> a
    # fresh time-ordered UUIDv7 is generated per invocation. See FR-33.
    parser.add_argument(
        "--session-id",
        help="tag this and consecutive related invocations with a shared id "
        "(default: a generated UUIDv7); echoed in the header and session footer",
    )
    # Worktree scoping (SPEC-otelq-worktree-scoping): a single GLOBAL opt-out.
    # By default a scoped command (errors/slow/logs/metric) filters to the current
    # worktree ONLY when the telemetry carries otelq.worktree.id tags (FR-1).
    # --all-worktrees opts out of that filtering; to target one specific worktree,
    # filter in `sql` on otelq.worktree.id (FR-7 / FR-9).
    parser.add_argument(
        "--all-worktrees",
        action="store_true",
        help="include every worktree's telemetry (disable default worktree scoping)",
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
    p_history = sub.add_parser(
        "history",
        help="ranked past-query history — the templates most likely to crack "
        "an investigation (triage assistant; also as sql views "
        "history_queries/history_invocations)",
    )
    p_history.add_argument(
        "--top", type=_non_negative_int, default=10, help="rows to show"
    )
    p_triage = sub.add_parser(
        "triage",
        help="start or continue an investigation from history: auto-runs the "
        "most likely next query when the evidence is strong (Markov step over "
        "past sessions), suggests the follow-up invocation, or admits it "
        "doesn't know and lists the top templates",
    )
    p_triage.add_argument(
        "--top",
        type=_non_negative_int,
        default=20,
        help="templates listed when no candidate is convincing",
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
    sub.add_parser(
        "set_resource_attributes",
        help="write git-derived otelq.worktree.id/branch into ./.env.local "
        "(opt-in worktree tagging; source it before launching the app)",
    )
    p_help = sub.add_parser("help", help="show help for otelq or a command")
    p_help.add_argument(
        "topic", nargs="?", help="command to show help for (omit for general help)"
    )
    return parser


# --- query-history triage store (ADR-009) ------------------------------------
# otelq records every telemetry-interrogating invocation in a consumer-owned
# `.otelq-history/` subtree of the target telemetry dir (CONTRACT v1.1) so an
# LLM agent can mine past investigations: which queries run most, which ones
# tend to END a troubleshooting burst (the terminal-query success proxy), and
# what typically follows what. The write path is one atomic O_APPEND line into
# journal.jsonl; a janitor at the END of the invocation — after the answer has
# already been printed, mirroring ADR-008's "answer first, then maintain" — and
# only if it wins the store's own O_EXCL lock, compacts the journal into two
# Parquet tables and applies the retention rules. Merging dedupes on the full
# invocation row, so re-consuming journal lines after a lost race or crash is
# idempotent. Recording is strictly best-effort: any failure is swallowed and
# can never change a command's result or exit code.

HISTORY_DIRNAME = ".otelq-history"
_HISTORY_JOURNAL = "journal.jsonl"
_HISTORY_QUERIES = "queries.parquet"
_HISTORY_INVOCATIONS = "invocations.parquet"
_HISTORY_AUDIT = "audit.jsonl"
# Commands recorded in history: the telemetry-interrogating ones only. Meta
# commands (doctor/troubleshoot/collector-config/help) and the history read
# surface itself are excluded — the latter also bounds the feedback loop
# (reading history never generates history).
_HISTORY_COMMANDS = frozenset(
    {"summary", "sql", "errors", "slow", "trace", "logs", "metric"}
)
# Any of these (case-insensitive) in OTELQ_HISTORY disables recording;
# anything else — including unset/empty — leaves it on.
_HISTORY_DISABLE_VALUES = frozenset({"0", "false", "off", "no"})

# Per-command --top parser defaults: a --top equal to its default is omitted
# from the canonical invocation string so `errors` and `errors --top 50` are
# one template, not two.
_HISTORY_TOP_DEFAULTS = {"errors": 50, "slow": 20, "logs": 50, "metric": 50}

# Normalisation: raw invocations rarely repeat verbatim (trace ids, quoted SQL
# literals); templates do, and template identity is what makes frequency and
# transition statistics meaningful. Quoted SQL literals ('' escapes included)
# and long hex ids collapse to '?'; everything else — including --regex/--grep
# patterns and --since windows — stays verbatim, because those values ARE the
# reusable recipe.
_HISTORY_SQL_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_HISTORY_HEX_ID_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")


class _HistoryConfig(NamedTuple):
    """Janitor business rules (ADR-009 decision 5). Every threshold is
    env-overridable so tests can trigger the janitor without waiting out
    wall-clock rules."""

    min_age_hours: int  # rows must be older than this to be removable
    keep_min: int  # never shrink the query table below this many templates
    max_rows: int  # a query whose every run returned more rows is a flooder
    stale_days: int  # a query unused this long is removable
    half_life_days: int  # recency decay: an invocation this old counts as 0.5


_HISTORY_DEFAULTS = _HistoryConfig(
    min_age_hours=24,
    keep_min=500,
    max_rows=500,
    stale_days=30,
    half_life_days=7,
)


class _TriageConfig(NamedTuple):
    """`triage` decision thresholds (ADR-009 amendment). A candidate next
    query is AUTO-RUN only with both evidence (decayed transition weight) and
    dominance (its share of all observed transitions from the anchor); the
    softer suggest_* pair gates the printed next-query suggestion. All
    env-overridable so tests can force either behaviour deterministically."""

    evidence: float  # min decayed weight to act (≈ 2 recent observations)
    share: float  # min fraction of transitions that agree
    suggest_evidence: float
    suggest_share: float


_TRIAGE_DEFAULTS = _TriageConfig(
    evidence=2.0, share=0.5, suggest_evidence=1.0, suggest_share=0.4
)


def _history_int_env(name: str, default: int) -> int:
    """A non-negative int from the environment, or the default on anything
    unparsable/negative — a bad env var must never break recording."""
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


def _history_float_env(name: str, default: float) -> float:
    """A non-negative float from the environment, defaulting like
    _history_int_env."""
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


def _history_config() -> _HistoryConfig:
    d = _HISTORY_DEFAULTS
    return _HistoryConfig(
        min_age_hours=_history_int_env("OTELQ_HISTORY_MIN_AGE_HOURS", d.min_age_hours),
        keep_min=_history_int_env("OTELQ_HISTORY_KEEP_MIN", d.keep_min),
        max_rows=_history_int_env("OTELQ_HISTORY_MAX_ROWS", d.max_rows),
        stale_days=_history_int_env("OTELQ_HISTORY_STALE_DAYS", d.stale_days),
        half_life_days=max(
            1, _history_int_env("OTELQ_HISTORY_HALF_LIFE_DAYS", d.half_life_days)
        ),
    )


def _triage_config() -> _TriageConfig:
    d = _TRIAGE_DEFAULTS
    return _TriageConfig(
        evidence=_history_float_env("OTELQ_TRIAGE_EVIDENCE", d.evidence),
        share=_history_float_env("OTELQ_TRIAGE_SHARE", d.share),
        suggest_evidence=_history_float_env(
            "OTELQ_TRIAGE_SUGGEST_EVIDENCE", d.suggest_evidence
        ),
        suggest_share=_history_float_env(
            "OTELQ_TRIAGE_SUGGEST_SHARE", d.suggest_share
        ),
    )


def _history_enabled() -> bool:
    return (
        os.environ.get("OTELQ_HISTORY", "").strip().lower()
        not in _HISTORY_DISABLE_VALUES
    )


def history_dir(telemetry_dir: Path) -> Path:
    return telemetry_dir / HISTORY_DIRNAME


def _history_raw(args: argparse.Namespace) -> str:
    """The canonical invocation string: the semantic parts of the command line,
    in fixed order. Presentation flags (--format/--verbose) and perf flags
    (--no-cache) are excluded — they don't change what was asked."""
    parts: list[str] = []
    since = getattr(args, "since", None)
    if since:
        parts += ["--since", str(since)]
    if getattr(args, "all", False):
        parts.append("--all")
    regex = getattr(args, "regex", None)
    if regex is not None:
        parts += ["--regex", str(regex)]
    parts.append(args.command)
    if args.command == "sql":
        parts.append(str(args.query))
    elif args.command == "trace":
        parts.append(str(args.trace_id))
    elif args.command == "metric":
        parts.append(str(args.name))
    if args.command == "logs":
        for flag in ("service", "level", "grep"):
            value = getattr(args, flag, None)
            if value is not None:
                parts += [f"--{flag}", str(value)]
    top = getattr(args, "top", None)
    if top is not None and top != _HISTORY_TOP_DEFAULTS.get(args.command):
        parts += ["--top", str(top)]
    return " ".join(parts)


def _history_normalize(command: str, raw: str) -> str:
    """Collapse an invocation to its template (see the regex notes above)."""
    norm = re.sub(r"\s+", " ", raw).strip()
    if command == "sql":
        norm = _HISTORY_SQL_LITERAL_RE.sub("'?'", norm)
    norm = _HISTORY_HEX_ID_RE.sub("?", norm)
    if command == "trace":
        # trace ids are volatile even when short (a unique prefix is accepted);
        # the template is the act of pivoting to a trace, not the id itself.
        norm = re.sub(r"(\btrace )\S+", r"\1?", norm)
    return norm


def _history_qid(norm: str) -> str:
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _history_append(path: Path, line: str) -> None:
    """Append one serialised line with a single atomic write. O_APPEND makes
    the seek+write atomic per call and one small write is not split, so
    concurrent otelq invocations never interleave lines; there is no external
    rotator to race (unlike the Collector's files)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


# The row count of the current invocation's result, noted by _dispatch for the
# recording hook in main(). A plain module-level slot is valid because otelq is
# a single-shot, single-threaded CLI: one invocation, one result.
_history_last_rows: int | None = None


def _history_note_rows(n: int) -> None:
    global _history_last_rows
    _history_last_rows = n


def _history_record(args: argparse.Namespace, exit_code: int, elapsed_ns: int) -> None:
    """Append this invocation to the journal and run the janitor. Best-effort:
    never raises, never fabricates a telemetry root, never records meta
    commands or a disabled/absent store."""
    try:
        if args.command not in _HISTORY_COMMANDS or not _history_enabled():
            return
        if not args.dir.is_dir():
            return
        raw = _history_raw(args)
        norm = _history_normalize(args.command, raw)
        entry = {
            "ts": _fmt_ts(_utc_now()),
            "qid": _history_qid(norm),
            # Only an EXPLICITLY supplied --session-id is stored (ground truth
            # for sessionisation). A generated id is an offer in the footer,
            # not a correlation the caller asserted — storing it would make
            # every unflagged call a singleton session and break the time-gap
            # heuristic that chains casual usage.
            "session_id": (
                args.session_id
                if getattr(args, "_session_supplied", False)
                else None
            ),
            "command": args.command,
            "raw": raw,
            "norm": norm,
            "rows_returned": _history_last_rows if _history_last_rows is not None else 0,
            "duration_ms": round(elapsed_ns / 1_000_000.0, 3),
            "exit_code": exit_code,
        }
        hdir = history_dir(args.dir)
        hdir.mkdir(parents=True, exist_ok=True)
        _history_append(
            hdir / _HISTORY_JOURNAL,
            json.dumps(entry, separators=(",", ":")) + "\n",
        )
        _history_janitor(args.dir)
    except Exception:
        pass


def _history_janitor(telemetry_dir: Path) -> None:
    """Compact the journal into the Parquet tables and apply retention — only
    if we win the store's own single-writer lock (same O_EXCL + stale-reap
    machinery as the cache, in a separate directory so the two never contend).
    Losing the race just defers compaction to the next invocation."""
    hdir = history_dir(telemetry_dir)
    fd = _acquire_lock(hdir)
    if fd is None:
        return
    try:
        _history_compact(hdir)
    finally:
        _release_lock(hdir, fd)


# Fixed journal schema for read_json: explicit types (no inference surprises),
# malformed lines skipped, missing keys NULL.
_HISTORY_JOURNAL_COLUMNS = (
    "{ts: 'TIMESTAMP', qid: 'VARCHAR', session_id: 'VARCHAR', "
    "command: 'VARCHAR', raw: 'VARCHAR', "
    "norm: 'VARCHAR', rows_returned: 'BIGINT', duration_ms: 'DOUBLE', "
    "exit_code: 'INTEGER'}"
)


def _history_compact(hdir: Path) -> None:
    """Merge journal lines into queries.parquet / invocations.parquet, apply
    the retention rules, truncate the consumed journal, and audit-log what
    happened. Caller holds the store lock.

    Rule semantics (ADR-009 decision 5): a query template is REMOVABLE only
    when its last use is older than min_age_hours AND it is bad — every run
    returned 0 rows (never produced signal), every run returned more than
    max_rows (always floods context), or it was last used over stale_days ago.
    Victims are taken in badness order (0-row, flooder, stale), oldest first,
    and never below keep_min surviving templates. Removing a template removes
    its invocation rows."""
    journal = hdir / _HISTORY_JOURNAL
    q_path = hdir / _HISTORY_QUERIES
    inv_path = hdir / _HISTORY_INVOCATIONS
    try:
        journal_size = journal.stat().st_size
    except OSError:
        journal_size = 0
    if journal_size == 0 and not q_path.exists():
        return  # nothing recorded yet

    import duckdb  # lazy; see TYPE_CHECKING note above

    cfg = _history_config()
    conn = duckdb.connect(database=":memory:")
    try:
        inv_arms: list[str] = []
        if inv_path.exists():
            # SELECT * + BY NAME below: a parquet written before the
            # session_id column existed still merges (missing column -> NULL).
            inv_arms.append(
                f"SELECT * FROM read_parquet({_sql_str(inv_path.as_posix())})"
            )
        if journal_size:
            inv_arms.append(
                f"SELECT qid, ts, session_id, rows_returned, duration_ms, "
                f"exit_code "
                f"FROM read_json({_sql_str(journal.as_posix())}, "
                f"format='newline_delimited', ignore_errors=true, "
                f"columns={_HISTORY_JOURNAL_COLUMNS}) WHERE qid IS NOT NULL"
            )
        if not inv_arms:
            return
        conn.execute(
            "CREATE TABLE _inv_stage AS "
            + " UNION ALL BY NAME ".join(f"(FROM ({arm}))" for arm in inv_arms)
        )
        has_session = _scalar(
            conn.execute(
                "SELECT count(*) FROM pragma_table_info('_inv_stage') "
                "WHERE name = 'session_id'"
            )
        )
        if not has_session:
            # Store predates session ids entirely (old parquet, empty journal).
            conn.execute("ALTER TABLE _inv_stage ADD COLUMN session_id VARCHAR")
        # Full-row DISTINCT: a journal line re-consumed after a skipped truncate
        # is byte-identical, so the merge is idempotent (no double-counting).
        conn.execute(
            "CREATE TABLE _inv AS SELECT DISTINCT qid, ts, session_id, "
            "rows_returned, duration_ms, exit_code FROM _inv_stage"
        )
        meta_arms: list[str] = []
        if q_path.exists():
            meta_arms.append(
                f"SELECT qid, command, norm, raw_example AS raw, "
                f"first_seen AS ts FROM read_parquet({_sql_str(q_path.as_posix())})"
            )
        if journal_size:
            meta_arms.append(
                f"SELECT qid, command, norm, raw, ts "
                f"FROM read_json({_sql_str(journal.as_posix())}, "
                f"format='newline_delimited', ignore_errors=true, "
                f"columns={_HISTORY_JOURNAL_COLUMNS}) WHERE qid IS NOT NULL"
            )
        # first_seen = the earliest ts ever associated with the template;
        # raw_example = the raw string of its LATEST use (arg_max), so
        # suggestions stay concrete and current.
        conn.execute(
            "CREATE TABLE _q AS "
            "SELECT m.qid, m.command, m.norm, m.raw_example, m.first_seen, "
            "       s.use_count, s.last_used, s.max_rows_ret, s.min_rows_ret "
            "FROM ("
            "  SELECT qid, any_value(command) AS command, any_value(norm) AS norm, "
            "         arg_max(raw, ts) AS raw_example, min(ts) AS first_seen "
            f"  FROM ({' UNION ALL '.join(meta_arms)}) GROUP BY qid"
            ") m JOIN ("
            "  SELECT qid, count(*) AS use_count, max(ts) AS last_used, "
            "         max(rows_returned) AS max_rows_ret, "
            "         min(rows_returned) AS min_rows_ret "
            "  FROM _inv GROUP BY qid"
            ") s USING (qid)"
        )

        total = int(_scalar(conn.execute("SELECT count(*) FROM _q")) or 0)
        removed = 0
        may_remove = total - cfg.keep_min
        if may_remove > 0:
            now = _utc_now()
            age_cut = _fmt_ts(now - timedelta(hours=cfg.min_age_hours))
            stale_cut = _fmt_ts(now - timedelta(days=cfg.stale_days))
            conn.execute(
                "CREATE TABLE _victims AS SELECT qid FROM ("
                "  SELECT qid, "
                "    CASE WHEN max_rows_ret = 0 THEN 1 "
                f"        WHEN min_rows_ret > {cfg.max_rows} THEN 2 "
                "         ELSE 3 END AS badness, last_used "
                "  FROM _q "
                f" WHERE last_used < TIMESTAMP '{age_cut}' "
                "    AND (max_rows_ret = 0 "
                f"        OR min_rows_ret > {cfg.max_rows} "
                f"        OR last_used < TIMESTAMP '{stale_cut}') "
                "  ORDER BY badness, last_used "
                f" LIMIT {may_remove})"
            )
            removed = int(_scalar(conn.execute("SELECT count(*) FROM _victims")) or 0)
            if removed:
                conn.execute(
                    "DELETE FROM _q WHERE qid IN (SELECT qid FROM _victims)"
                )
                conn.execute(
                    "DELETE FROM _inv WHERE qid NOT IN (SELECT qid FROM _q)"
                )

        if journal_size == 0 and removed == 0:
            return  # nothing changed; skip the Parquet rewrite entirely
        for table, target in (("_q", q_path), ("_inv", inv_path)):
            tmp = target.with_suffix(target.suffix + ".tmp")
            conn.execute(
                f"COPY {table} TO {_sql_str(tmp.as_posix())} (FORMAT PARQUET)"
            )
            os.replace(tmp, target)
        if journal_size:
            try:
                # Truncate only when no line landed since we read; a concurrent
                # append leaves the journal intact and the idempotent merge
                # re-consumes those lines next round.
                if journal.stat().st_size == journal_size:
                    os.truncate(journal, 0)
            except OSError:
                pass
        audit = {
            "ts": _fmt_ts(_utc_now()),
            "merged_bytes": journal_size,
            "queries_total": total - removed,
            "queries_removed": removed,
        }
        _history_append(
            hdir / _HISTORY_AUDIT, json.dumps(audit, separators=(",", ":")) + "\n"
        )
    finally:
        conn.close()


# Stable empty-view schemas so `sql` users see the history relations even
# before any history exists (expose-empty, matching FR-1's spirit).
_HISTORY_VIEW_SCHEMAS = {
    "history_queries": (
        "CAST(NULL AS VARCHAR) AS qid, CAST(NULL AS VARCHAR) AS command, "
        "CAST(NULL AS VARCHAR) AS norm, CAST(NULL AS VARCHAR) AS raw_example, "
        "CAST(NULL AS TIMESTAMP) AS first_seen, CAST(NULL AS BIGINT) AS use_count, "
        "CAST(NULL AS TIMESTAMP) AS last_used, CAST(NULL AS BIGINT) AS max_rows_ret, "
        "CAST(NULL AS BIGINT) AS min_rows_ret"
    ),
    "history_invocations": (
        "CAST(NULL AS VARCHAR) AS qid, CAST(NULL AS TIMESTAMP) AS ts, "
        "CAST(NULL AS VARCHAR) AS session_id, "
        "CAST(NULL AS BIGINT) AS rows_returned, CAST(NULL AS DOUBLE) AS duration_ms, "
        "CAST(NULL AS INTEGER) AS exit_code"
    ),
}
_HISTORY_VIEW_FILES = {
    "history_queries": _HISTORY_QUERIES,
    "history_invocations": _HISTORY_INVOCATIONS,
}


def _history_create_views(
    conn: duckdb.DuckDBPyConnection, telemetry_dir: Path
) -> None:
    """Expose the history tables as `sql` views (ADR-009 decision 7). Lazy
    parquet views are fine here: `sql` keeps external file access on purpose
    (see _seal_external_access). Absent files yield empty, correctly-typed
    views so recipes never fail on a fresh store."""
    hdir = history_dir(telemetry_dir)
    for view, filename in _HISTORY_VIEW_FILES.items():
        path = hdir / filename
        if path.exists():
            conn.execute(
                f"CREATE OR REPLACE VIEW {view} AS "
                f"SELECT * FROM read_parquet({_sql_str(path.as_posix())})"
            )
        else:
            conn.execute(
                f"CREATE OR REPLACE VIEW {view} AS "
                f"SELECT {_HISTORY_VIEW_SCHEMAS[view]} WHERE false"
            )


_HISTORY_NO_STORE_MSG = (
    "no query history yet — history accumulates in .otelq-history/ as otelq "
    "commands run"
)


def _history_sessions_cte(inv_path: Path, cfg: _HistoryConfig) -> str:
    """The shared WITH-prefix over invocations: sessionise, flag terminals,
    and attach the recency weight. Used by `history`'s ranking and by every
    `triage` analysis so the session model can never drift between them.

    Sessions are EXPLICIT session ids ONLY (ADR-009 amendment). Timestamps are
    never used to infer session membership: concurrent agent sessions hitting
    one store interleave in time, so any gap heuristic would chain unrelated
    investigations together. A row recorded without a supplied --session-id
    belongs to NO session — it still carries the recency weight (frequency
    evidence for ranking) but can never be terminal, transition, or
    session-opening evidence.

    Per-row derived columns: `is_terminal` (last row of its explicit session;
    always FALSE for session-less rows), `usable` (returned 1..max_rows rows —
    neither silence nor a context flood), and `w` — the exponential recency
    weight 0.5^(age/half_life), so yesterday counts ~1 and last month barely
    at all."""
    hl_seconds = cfg.half_life_days * 86400.0
    now_lit = _fmt_ts(_utc_now())
    return (
        "WITH inv AS ("
        f"  SELECT * FROM read_parquet({_sql_str(inv_path.as_posix())})"
        "), term AS ("
        "  SELECT qid, ts, session_id, rows_returned, "
        "    CASE WHEN session_id IS NULL THEN FALSE "
        "         ELSE ts = MAX(ts) OVER (PARTITION BY session_id) "
        "    END AS is_terminal, "
        f"   CASE WHEN rows_returned BETWEEN 1 AND {cfg.max_rows} "
        "         THEN 1 ELSE 0 END AS usable, "
        f"   POWER(0.5, GREATEST(0, date_diff('second', ts, "
        f"     TIMESTAMP '{now_lit}')) / {hl_seconds}) AS w"
        "  FROM inv)"
    )


# Per-template scoring (ADR-009 amendment): recency-decayed frequency times
# Laplace-smoothed terminal-success. rf = Σw (recent uses count ~1, old ~0);
# wf = Σw over invocations that ENDED their session with a usable row count;
# smoothing (wf+1)/(rf+2) keeps a 1-for-1 fluke below a 9-for-10 workhorse and
# gives never-successful templates a floor instead of zero. The product ranks
# "frequent AND working, recently" above either property alone.
_HISTORY_SCORE_CTE = (
    ", per AS ("
    "  SELECT qid, SUM(w) AS rf, "
    "    SUM(w * CAST(is_terminal AS INTEGER) * usable) AS wf, "
    "    count(*) AS uses_raw, "
    "    SUM(CASE WHEN is_terminal AND usable = 1 THEN 1 ELSE 0 END) AS wins_raw "
    "  FROM term GROUP BY qid"
    "), scored AS ("
    "  SELECT qid, rf, wf, uses_raw, wins_raw, "
    "    rf * (wf + 1.0) / (rf + 2.0) AS score "
    "  FROM per)"
)


def _history_report(telemetry_dir: Path, top: int) -> tuple[list[str], list[Row]]:
    """The `history` command: past query templates ranked by the decayed
    frequency × smoothed-success score (see _HISTORY_SCORE_CTE). terminal_pct
    stays the raw lifetime rate — the human-readable evidence behind the
    score."""
    q_path = history_dir(telemetry_dir) / _HISTORY_QUERIES
    inv_path = history_dir(telemetry_dir) / _HISTORY_INVOCATIONS
    columns = [
        "rank", "score", "terminal_pct", "uses", "last_used", "command", "query",
    ]
    if not q_path.exists() or not inv_path.exists():
        return columns, []

    import duckdb  # lazy; see TYPE_CHECKING note above

    cfg = _history_config()
    conn = duckdb.connect(database=":memory:")
    try:
        rows = conn.execute(
            _history_sessions_cte(inv_path, cfg)
            + _HISTORY_SCORE_CTE
            + " SELECT CAST(row_number() OVER win AS BIGINT) AS rank, "
            "  CAST(round(scored.score, 3) AS DOUBLE) AS score, "
            "  CAST(round(100.0 * scored.wins_raw / scored.uses_raw) AS BIGINT)"
            "    AS terminal_pct, "
            "  q.use_count AS uses, "
            "  strftime(q.last_used, '%Y-%m-%dT%H:%M:%S') || 'Z' AS last_used, "
            "  q.command, q.raw_example AS query "
            f"FROM scored JOIN read_parquet({_sql_str(q_path.as_posix())}) q "
            "  USING (qid) "
            "WINDOW win AS (ORDER BY scored.score DESC, q.last_used DESC) "
            f"ORDER BY rank LIMIT {top}"
        ).fetchall()
    except duckdb.Error:
        return columns, []  # torn/foreign parquet: an empty report, never a crash
    finally:
        conn.close()
    return columns, rows


# --- triage (ADR-009 amendment): act on the history, don't just report it ----

_TRIAGE_NO_HISTORY_MSG = (
    "triage: no query history yet — start with `summary` (the RCA guide's "
    "grounding step); history accumulates as otelq commands run"
)
_TRIAGE_NO_CANDIDATE_MSG = (
    "triage: no strong candidate from history, and this session is already "
    "grounded (summary ran) — top templates below; pick one that matches the "
    "symptom, or proceed with RCA step 2 (errors/slow/logs/metric)"
)


class _TriageCandidate(NamedTuple):
    qid: str
    raw_example: str
    norm: str
    command: str
    evidence: float  # decayed observation weight backing this candidate
    share: float  # its fraction of all observations from this anchor


def _triage_concrete(candidate: _TriageCandidate) -> bool:
    """A template is auto-runnable only when normalisation introduced no '?'
    placeholders — i.e. the stored example IS the template. Re-running someone
    else's stale trace id or SQL literal verbatim would query noise."""
    return candidate.norm == re.sub(r"\s+", " ", candidate.raw_example).strip()


def _triage_pick(
    rows: list[Row], evidence: float, share: float
) -> _TriageCandidate | None:
    """Rows: (qid, raw_example, norm, command, evidence, total). Returns the
    top candidate iff it clears both thresholds."""
    if not rows:
        return None
    qid, raw_example, norm, command, ev, total = rows[0]
    ev = float(ev or 0.0)
    total = float(total or 0.0)
    cand = _TriageCandidate(
        qid=str(qid),
        raw_example=str(raw_example),
        norm=str(norm),
        command=str(command),
        evidence=ev,
        share=(ev / total) if total > 0 else 0.0,
    )
    if cand.evidence >= evidence and cand.share >= share:
        return cand
    return None


def _triage_next_candidates(
    conn: duckdb.DuckDBPyConnection,
    base_cte: str,
    q_path: Path,
    from_qid: str,
) -> list[Row]:
    """First-order Markov step: adjacent (within-session) successors of
    from_qid, decay-weighted, best first — each weighted by the successor's
    own smoothed success so a frequently-followed dead end ranks below a
    slightly-rarer resolver."""
    return conn.execute(
        base_cte
        + _HISTORY_SCORE_CTE
        + ", steps AS ("
        "  SELECT qid, session_id, ts, "
        "    LEAD(qid) OVER (PARTITION BY session_id ORDER BY ts, qid) "
        "      AS next_qid, "
        "    LEAD(w) OVER (PARTITION BY session_id ORDER BY ts, qid) "
        "      AS next_w "
        "  FROM term WHERE session_id IS NOT NULL"
        "), trans AS ("
        "  SELECT next_qid AS qid, SUM(next_w) AS tw FROM steps "
        f" WHERE qid = {_sql_str(from_qid)} AND next_qid IS NOT NULL "
        "  GROUP BY 1"
        ") "
        "SELECT trans.qid, q.raw_example, q.norm, q.command, trans.tw, "
        "  SUM(trans.tw) OVER () AS total "
        f"FROM trans JOIN read_parquet({_sql_str(q_path.as_posix())}) q "
        "  USING (qid) "
        "JOIN scored USING (qid) "
        "ORDER BY trans.tw * scored.score DESC, trans.tw DESC LIMIT 5"
    ).fetchall()


def _triage_starter_candidates(
    conn: duckdb.DuckDBPyConnection, base_cte: str, q_path: Path
) -> list[Row]:
    """Fresh-start analysis: which template OPENS sessions that end
    successfully? evidence = decayed weight of successful sessions it started;
    share = its success ratio as an opener."""
    return conn.execute(
        base_cte
        + ", outcome AS ("
        "  SELECT *, MAX(CAST(is_terminal AS INTEGER) * usable) "
        "      OVER (PARTITION BY session_id) AS won, "
        "    ts = MIN(ts) OVER (PARTITION BY session_id) AS is_first "
        "  FROM term WHERE session_id IS NOT NULL"
        "), starters AS ("
        "  SELECT qid, SUM(w * won) AS sw, SUM(w) AS fw FROM outcome "
        "  WHERE is_first GROUP BY qid"
        ") "
        "SELECT starters.qid, q.raw_example, q.norm, q.command, starters.sw, "
        "  starters.sw / (CASE WHEN starters.fw > 0 THEN starters.fw ELSE 1 END) "
        "    * starters.sw AS _shareprod "
        f"FROM starters JOIN read_parquet({_sql_str(q_path.as_posix())}) q "
        "  USING (qid) "
        "ORDER BY starters.sw DESC LIMIT 5"
    ).fetchall()


def _triage_anchor(
    conn: duckdb.DuckDBPyConnection, base_cte: str, session_id: str | None
) -> str | None:
    """The template qid this triage continues from, or None for a fresh
    start. ONLY an explicitly supplied session id can anchor — its session's
    latest row, regardless of age (the caller asserted continuity). Recency
    must never anchor: concurrent agent sessions interleave in one store, so
    'the latest row' can belong to someone else's investigation entirely."""
    if session_id is None:
        return None
    row = conn.execute(
        base_cte
        + " SELECT qid FROM term "
        f"WHERE session_id = {_sql_str(session_id)} "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return str(row[0]) if row is not None else None


def _triage_session_ran_summary(
    conn: duckdb.DuckDBPyConnection, base_cte: str, q_path: Path, session_id: str
) -> bool:
    """Whether the anchor's session already contains a `summary` invocation —
    the gate on triage's grounded fallback (a session grounds at most once)."""
    count = _scalar(
        conn.execute(
            base_cte
            + " SELECT count(*) FROM term "
            f"JOIN read_parquet({_sql_str(q_path.as_posix())}) q USING (qid) "
            f"WHERE term.session_id = {_sql_str(session_id)} "
            "AND q.command = 'summary'"
        )
    )
    return bool(count)


# The synthetic grounding candidate: when history offers nothing convincing
# and the session has not been grounded yet, triage runs the RCA guide's own
# step 1 instead of shrugging (`summary` is concrete by construction). Its qid
# is the real template hash, so the post-run suggestion lookup still works —
# past sessions that grounded with summary vote on what to run next.
_TRIAGE_GROUND_CANDIDATE = _TriageCandidate(
    qid=_history_qid("summary"), raw_example="summary", norm="summary",
    command="summary", evidence=0.0, share=0.0,
)


def _triage_suggestion_line(
    telemetry_dir: Path, session_id: str, candidate: _TriageCandidate
) -> str:
    """The full copy-paste next invocation, session id included — printed as
    the LAST line of triage output so an agent can chain without thinking."""
    return (
        f"triage next suggestion (adapt any '?' parts): "
        f"otelq --dir {telemetry_dir} --session-id {session_id} "
        f"{candidate.raw_example}"
    )


def _run_triage(args: argparse.Namespace, session_supplied: bool) -> int:
    """`otelq triage` (ADR-009 amendment): decide from history whether we are
    mid-investigation (Markov next-step from the anchor query) or starting
    fresh (best session-opener), AUTO-RUN the winning candidate when the
    evidence clears the thresholds and the template is concrete, then print
    the suggested follow-up as the last output line. With no convincing
    candidate it says so and dumps the ranked template list instead of
    guessing."""
    hdir = history_dir(args.dir)
    q_path = hdir / _HISTORY_QUERIES
    inv_path = hdir / _HISTORY_INVOCATIONS
    if not q_path.exists() or not inv_path.exists():
        print(_TRIAGE_NO_HISTORY_MSG, file=sys.stderr)
        return 0

    import duckdb  # lazy; see TYPE_CHECKING note above

    cfg = _history_config()
    tcfg = _triage_config()
    conn = duckdb.connect(database=":memory:")
    summary_ran = False  # has THIS session already been grounded?
    try:
        base_cte = _history_sessions_cte(inv_path, cfg)
        try:
            anchor_qid = _triage_anchor(
                conn, base_cte, args.session_id if session_supplied else None
            )
            if anchor_qid is not None:
                # The anchor exists only for a SUPPLIED session id, so the
                # chain, the auto-run, and the suggestion already share
                # args.session_id — no adoption step.
                summary_ran = _triage_session_ran_summary(
                    conn, base_cte, q_path, args.session_id
                )
                candidates = _triage_next_candidates(
                    conn, base_cte, q_path, anchor_qid
                )
            else:
                candidates = _triage_starter_candidates(conn, base_cte, q_path)
            picked = _triage_pick(candidates, tcfg.evidence, tcfg.share)
            suggested = picked or _triage_pick(
                candidates, tcfg.suggest_evidence, tcfg.suggest_share
            )
        except duckdb.Error:
            picked = suggested = None  # torn store: fall through to the dump
    finally:
        conn.close()

    if picked is not None and _triage_concrete(picked):
        return _triage_autorun(args, picked)
    if suggested is not None:
        # Confident enough to point, not to run (soft evidence, or a template
        # with '?' placeholders that only the caller can fill).
        print(_triage_suggestion_line(args.dir, args.session_id, suggested))
        return 0
    if not summary_ran:
        # No convincing candidate, and this session has not been grounded yet
        # (a fresh session trivially hasn't): run the RCA guide's step 1 for
        # the caller instead of refusing and telling them to run it themselves.
        return _triage_autorun(
            args,
            _TRIAGE_GROUND_CANDIDATE,
            banner=(
                "triage: no strong history candidate — grounding with "
                f"`summary` (RCA step 1; session {args.session_id})"
            ),
        )
    print(_TRIAGE_NO_CANDIDATE_MSG, file=sys.stderr)
    columns, rows = _history_report(args.dir, args.top)
    print(format_output(columns, rows, args.format))
    return 0


def _triage_autorun(
    args: argparse.Namespace,
    picked: _TriageCandidate,
    banner: str | None = None,
) -> int:
    """Execute the picked template as a full otelq invocation: banner, the
    command's normal output (header, payload), a manual history record (so the
    chain advances), then the next-step suggestion as the last line."""
    argv = [
        "--dir", str(args.dir),
        "--format", args.format,
        "--session-id", args.session_id,
        *shlex.split(picked.raw_example),
    ]
    try:
        inner = build_parser().parse_args(argv)
    except SystemExit:
        # A stored template that no longer parses (flag renamed, etc.) — point
        # instead of run, never crash triage over stale history.
        print(_triage_suggestion_line(args.dir, args.session_id, picked))
        return 0
    if inner.command not in _HISTORY_COMMANDS:
        print(_triage_suggestion_line(args.dir, args.session_id, picked))
        return 0
    print(
        banner
        or f"triage: running `{picked.raw_example}` (session {args.session_id})"
    )
    t0 = time.monotonic_ns()
    try:
        code = _dispatch(inner)
    except SystemExit as exc:
        # The stored query failed at runtime (e.g. stale SQL against a changed
        # schema): report and leave the caller in charge.
        print(f"triage: candidate failed: {exc}", file=sys.stderr)
        return 0
    _history_record(inner, code, time.monotonic_ns() - t0)

    # Second Markov step: with the picked query now the anchor, is there a
    # reasonably likely follow-up? Printed last, full syntax, session included.
    hdir = history_dir(args.dir)
    q_path = hdir / _HISTORY_QUERIES
    inv_path = hdir / _HISTORY_INVOCATIONS

    import duckdb  # lazy; see TYPE_CHECKING note above

    cfg = _history_config()
    tcfg = _triage_config()
    conn = duckdb.connect(database=":memory:")
    try:
        nxt = _triage_pick(
            _triage_next_candidates(
                conn, _history_sessions_cte(inv_path, cfg), q_path, picked.qid
            ),
            tcfg.suggest_evidence,
            tcfg.suggest_share,
        )
    except duckdb.Error:
        nxt = None
    finally:
        conn.close()
    if nxt is not None:
        print()
        print(_triage_suggestion_line(args.dir, args.session_id, nxt))
    return code


def _dispatch(args: argparse.Namespace) -> int:
    # Whether the caller EXPLICITLY passed --session-id (before resolve_
    # session_id backfills a generated one): triage anchors to a supplied
    # session's own history; a generated id can't have any.
    session_supplied = getattr(args, "session_id", None) is not None
    # Resolve the session id once up front (FR-33) and stash it back on `args`
    # so the response header (below) and the stderr session footer (main) stamp
    # and advertise the exact same id — including a generated UUIDv7 default.
    # Suppliedness rides along for the history record (only explicit ids are
    # stored) and for triage's anchor lookup.
    args._session_supplied = session_supplied
    args.session_id = resolve_session_id(args)
    regex = _resolve_regex_arg(args)
    if args.command == "collector-config":
        print(render_collector_config())
        return 0
    if args.command == "set_resource_attributes":
        # A meta command (FR-3): no --dir/connection. Writes the git-derived
        # worktree keys into the cwd's .env.local for the launcher to source.
        return _run_set_resource_attributes(Path.cwd())
    if args.command == "troubleshoot":
        print(render_troubleshooting())
        return 0
    if args.command == "doctor":
        rows, ok = doctor_report(args.dir)
        print(format_output(["check", "status", "detail"], rows, args.format))
        return 0 if ok else 1
    if args.command == "history":
        columns, rows = _history_report(args.dir, args.top)
        if not rows and not (history_dir(args.dir) / _HISTORY_QUERIES).exists():
            # Fail FRIENDLY: a fresh store is normal, not an error.
            print(_HISTORY_NO_STORE_MSG, file=sys.stderr)
            return 0
        print(format_output(columns, rows, args.format))
        return 0
    if args.command == "triage":
        return _run_triage(args, session_supplied)
    try:
        columns, rows, time_range, regex_removed, services, worktree_banner = (
            run_command(args, regex)
        )
    except NoTelemetryError as exc:
        _history_note_rows(0)
        print(exc, file=sys.stderr)
        return 0
    _history_note_rows(len(rows))
    if args.command in _HEADER_COMMANDS:
        print(
            _format_response_header(
                args.command,
                args.format,
                rows,
                time_range,
                args.session_id,
                args.regex,
                regex_removed,
                worktree_banner,
            )
        )
    print(format_output(columns, rows, args.format))
    if services is not None:
        # summary's second block (FR-3/FR-4): a plain-text delimiter makes the two
        # format-rendered blocks unambiguous for a machine consumer, in every
        # --format. Without worktree tags the columns/rows are byte-identical to
        # summary's output before this block existed (INV-2).
        service_columns, service_rows = services
        print()
        print(_SUMMARY_SERVICE_LABEL)
        print(format_output(service_columns, service_rows, args.format))
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
    t0 = time.monotonic_ns()
    try:
        code = _dispatch(args)
        # Session footer (FR-33): reminds the caller — on EVERY command — how to
        # correlate follow-up invocations. On stderr (never stdout) so it can't
        # corrupt the machine-parseable answer. A leading blank line sets it off
        # from the rendered answer above. Flush stdout first so the footer truly
        # trails the answer even when stdout is block-buffered (piped, not a tty)
        # while stderr is unbuffered. `_dispatch` has stashed the resolved id.
        sys.stdout.flush()
        print("\n" + _session_footer(args.session_id), file=sys.stderr)
        # Record AFTER the answer is fully printed (ADR-009: history is
        # maintenance, kept off the critical path by ordering — the same rule
        # ADR-008 applies to cache sealing). Only commands that completed
        # normally are recorded; _history_record is best-effort throughout.
        _history_record(args, code, time.monotonic_ns() - t0)
        return code
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
