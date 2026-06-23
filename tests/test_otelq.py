# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "duckdb>=1.1.0"]
# ///
"""Tests for otelq. Run: just otelq-test"""

import sys
from argparse import Namespace
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import otelq  # noqa: E402

TESTDATA = Path(__file__).resolve().parent / "testdata"


@pytest.fixture
def synth_conn():
    """In-memory DuckDB with the duckdb-otlp schema and known rows."""
    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE traces (
            timestamp TIMESTAMP_MS, end_timestamp BIGINT, duration BIGINT,
            trace_id VARCHAR, span_id VARCHAR, parent_span_id VARCHAR,
            trace_state VARCHAR, service_name VARCHAR, service_namespace VARCHAR,
            service_instance_id VARCHAR, span_name VARCHAR, span_kind INTEGER,
            status_code INTEGER, status_message VARCHAR, resource_attributes VARCHAR,
            scope_name VARCHAR, scope_version VARCHAR, scope_attributes VARCHAR,
            span_attributes VARCHAR, events_json VARCHAR, links_json VARCHAR,
            dropped_attributes_count INTEGER, dropped_events_count INTEGER,
            dropped_links_count INTEGER, flags INTEGER);
        INSERT INTO traces VALUES
          ('2026-05-22 10:00:00',0,5000000,'trace-a','span-a1','','','checkout-api',
           '','','GET /orders',2,1,'','','','','','','','',0,0,0,0),
          ('2026-05-22 10:00:01',0,90000000,'trace-a','span-a2','span-a1','',
           'checkout-api','','','SELECT orders',3,1,'','','','','','','','',0,0,0,0),
          ('2026-05-22 10:00:02',0,12000000,'trace-b','span-b1','','','catalog-api',
           '','','POST /products',2,2,'boom','','','','','','','',0,0,0,0);
        """
    )
    conn.execute(
        """
        CREATE TABLE logs (
            timestamp TIMESTAMP_MS, observed_timestamp BIGINT, trace_id VARCHAR,
            span_id VARCHAR, service_name VARCHAR, service_namespace VARCHAR,
            service_instance_id VARCHAR, severity_number INTEGER, severity_text VARCHAR,
            body VARCHAR, resource_attributes VARCHAR, scope_name VARCHAR,
            scope_version VARCHAR, scope_attributes VARCHAR, log_attributes VARCHAR);
        INSERT INTO logs VALUES
          ('2026-05-22 10:00:00',0,'trace-a','span-a1','checkout-api','','',9,
           'INFO','order received','','','','',''),
          ('2026-05-22 10:00:02',0,'trace-b','span-b1','catalog-api','','',17,
           'ERROR','product save failed','','','','','');
        """
    )
    conn.execute(
        """
        CREATE TABLE metrics_gauge (
            timestamp TIMESTAMP_MS, start_timestamp BIGINT, metric_name VARCHAR,
            metric_description VARCHAR, metric_unit VARCHAR, value DOUBLE,
            service_name VARCHAR, service_namespace VARCHAR, service_instance_id VARCHAR,
            resource_attributes VARCHAR, scope_name VARCHAR, scope_version VARCHAR,
            scope_attributes VARCHAR, metric_attributes VARCHAR, flags INTEGER,
            exemplars_json VARCHAR);
        INSERT INTO metrics_gauge VALUES
          ('2026-05-22 10:00:00',0,'db.pool.in_use','','{connections}',4,
           'checkout-api','','','','','','','',0,''),
          ('2026-05-22 10:00:05',0,'db.pool.in_use','','{connections}',7,
           'checkout-api','','','','','','','',0,'');
        """
    )
    conn.execute(
        """
        CREATE TABLE metrics_sum (
            timestamp TIMESTAMP_MS, start_timestamp BIGINT, metric_name VARCHAR,
            metric_description VARCHAR, metric_unit VARCHAR, value DOUBLE,
            service_name VARCHAR, service_namespace VARCHAR, service_instance_id VARCHAR,
            resource_attributes VARCHAR, scope_name VARCHAR, scope_version VARCHAR,
            scope_attributes VARCHAR, metric_attributes VARCHAR, flags INTEGER,
            exemplars_json VARCHAR, aggregation_temporality INTEGER,
            is_monotonic BOOLEAN);
        INSERT INTO metrics_sum VALUES
          ('2026-05-22 10:00:00',0,'http.server.requests','','{requests}',42,
           'checkout-api','','','','','','','',0,'',2,true);
        """
    )
    otelq.create_unified_metrics_view(conn)
    return conn


def test_format_output_json():
    out = otelq.format_output(["a", "b"], [(1, "x")], "json")
    import json as _json

    assert _json.loads(out) == [{"a": 1, "b": "x"}]


def test_format_output_csv():
    out = otelq.format_output(["a", "b"], [(1, "x")], "csv")
    assert out == "a,b\r\n1,x"


def test_format_output_table_empty():
    assert otelq.format_output(["a"], [], "table") == "(no rows)"


def test_summary_counts(synth_conn):
    columns, rows = otelq.cmd_summary(synth_conn, Namespace())
    by_signal = {r[0]: r[1] for r in rows}
    assert by_signal == {"traces": 3, "logs": 2, "metrics": 3}


def test_summary_raises_when_empty():
    conn = duckdb.connect(":memory:")
    with pytest.raises(otelq.NoTelemetryError):
        otelq.cmd_summary(conn, Namespace())


def test_sql_passthrough(synth_conn):
    columns, rows = otelq.cmd_sql(
        synth_conn, Namespace(query="SELECT count(*) AS n FROM traces")
    )
    assert columns == ["n"]
    assert rows == [(3,)]


def test_integration_reads_real_fixture():
    """connect() + the duckdb-otlp extension read genuine Collector output."""
    conn = otelq.connect(TESTDATA)
    columns, rows = otelq.cmd_summary(conn, Namespace())
    by_signal = {r[0]: r[1] for r in rows}
    assert by_signal.get("traces", 0) > 0


def test_errors_finds_error_span_and_log(synth_conn):
    columns, rows = otelq.cmd_errors(synth_conn, Namespace(since=None))
    kinds = sorted(r[0] for r in rows)
    assert kinds == ["log", "span"]
    assert all(r[2] == "catalog-api" for r in rows)


def test_slow_orders_by_duration_desc(synth_conn):
    columns, rows = otelq.cmd_slow(synth_conn, Namespace(top=2))
    assert len(rows) == 2
    assert rows[0][2] == "SELECT orders"  # 90ms span first
    assert rows[0][3] >= rows[1][3]  # duration_ms descending


def test_trace_returns_tree_for_one_trace(synth_conn):
    columns, rows = otelq.cmd_trace(synth_conn, Namespace(trace_id="trace-a"))
    assert len(rows) == 2
    assert rows[0][0] == 0 and rows[1][0] == 1  # depth: root then child


def test_trace_unknown_id_raises(synth_conn):
    with pytest.raises(otelq.NoTelemetryError):
        otelq.cmd_trace(synth_conn, Namespace(trace_id="does-not-exist"))


def test_logs_filter_by_service(synth_conn):
    columns, rows = otelq.cmd_logs(
        synth_conn, Namespace(service="catalog-api", level=None, grep=None)
    )
    assert len(rows) == 1
    assert rows[0][1] == "catalog-api"


def test_logs_filter_by_level(synth_conn):
    columns, rows = otelq.cmd_logs(
        synth_conn, Namespace(service=None, level="error", grep=None)
    )
    assert len(rows) == 1
    assert rows[0][2] == "ERROR"


def test_logs_filter_by_grep(synth_conn):
    columns, rows = otelq.cmd_logs(
        synth_conn, Namespace(service=None, level=None, grep="save")
    )
    assert len(rows) == 1
    assert "save" in rows[0][3].lower()  # body column


def test_metric_returns_time_series(synth_conn):
    columns, rows = otelq.cmd_metric(synth_conn, Namespace(name="db.pool.in_use"))
    assert [r[4] for r in rows] == [4.0, 7.0]  # value column, time-ordered


def test_integration_timestamps_are_scaled():
    """Timestamps from real Collector output must be in 2026, not year ~58358.

    The duckdb-otlp extension stores nanoseconds in a TIMESTAMP_MS column;
    without the divide-by-1000 correction every timestamp renders as ~year 58358.
    This test uses the real fixture to guard that register_views applies the fix.
    """
    conn = otelq.connect(TESTDATA)
    earliest = conn.execute("SELECT min(timestamp) FROM traces").fetchone()[0]
    assert earliest.year == 2026


# =============================================================================
# Incremental parquet cache (SPEC-otelq-incremental-cache)
# =============================================================================
import hashlib as _hashlib  # noqa: E402
import json as _json  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

CACHE = otelq.CACHE_DIRNAME


def trace_hex(label: str) -> str:
    """Deterministic 32-hex-char (16-byte) trace id from a label."""
    return _hashlib.sha1(label.encode()).hexdigest()[:32]


def span_hex(label: str) -> str:
    """Deterministic 16-hex-char (8-byte) span id from a label."""
    return _hashlib.sha1(label.encode()).hexdigest()[:16]


def _ns(dt: datetime) -> str:
    """Event-time as a nanosecond OTLP timeUnixNano string."""
    return str(int(dt.timestamp() * 1_000_000_000))


def _resource(service: str) -> dict:
    return {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]}


def make_span(
    ts,
    trace_id="t1",
    span_id="s1",
    parent="",
    name="GET /x",
    service="app-test",
    kind=2,
    status_code=0,
    status_msg="",
    duration_ms=5,
):
    end = ts + timedelta(milliseconds=duration_ms)
    span = {
        "traceId": trace_hex(trace_id),
        "spanId": span_hex(span_id),
        "parentSpanId": span_hex(parent) if parent else "",
        "name": name,
        "kind": kind,
        "startTimeUnixNano": _ns(ts),
        "endTimeUnixNano": _ns(end),
        "attributes": [],
        "flags": 0,
        "status": {"code": status_code, "message": status_msg} if status_code else {},
    }
    return {
        "resourceSpans": [
            {
                "resource": _resource(service),
                "scopeSpans": [{"scope": {"name": "test"}, "spans": [span]}],
            }
        ]
    }


def make_log(ts, service="app-test", severity="INFO", sevnum=9, body="hi", trace_id=""):
    return {
        "resourceLogs": [
            {
                "resource": _resource(service),
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": _ns(ts),
                                "observedTimeUnixNano": _ns(ts),
                                "severityNumber": sevnum,
                                "severityText": severity,
                                "body": {"stringValue": body},
                                "traceId": trace_id,
                                "attributes": [],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def make_gauge(ts, name="db.pool", unit="{c}", value=4.0, service="app-test"):
    return {
        "resourceMetrics": [
            {
                "resource": _resource(service),
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": name,
                                "unit": unit,
                                "description": "",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": _ns(ts),
                                            "startTimeUnixNano": _ns(ts),
                                            "asDouble": value,
                                            "attributes": [],
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }


def make_sum(ts, name="reqs", unit="{r}", value=42, service="app-test"):
    return {
        "resourceMetrics": [
            {
                "resource": _resource(service),
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": name,
                                "unit": unit,
                                "description": "",
                                "sum": {
                                    "aggregationTemporality": 2,
                                    "isMonotonic": True,
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": _ns(ts),
                                            "startTimeUnixNano": _ns(ts),
                                            "asInt": value,
                                            "attributes": [],
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }


def write_jsonl(path: Path, objs, append=False):
    text = "".join(_json.dumps(o) + "\n" for o in objs)
    mode = "a" if append else "w"
    with open(path, mode, encoding="utf-8") as fh:
        fh.write(text)


@pytest.fixture
def temp_telemetry(tmp_path):
    d = tmp_path / "telemetry"
    d.mkdir()
    return d


def _run(dirpath, *argv):
    """Run the CLI in-process; return its stdout string."""
    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    with redirect_stdout(buf):
        otelq.main(["--dir", str(dirpath), "--format", "json", *argv])
    return buf.getvalue()


# --- fabrication smoke test (validates OTLP shapes against the real extension) -


def test_fabricated_corpus_roundtrips(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "traces.jsonl",
        [make_span(base, status_code=2, status_msg="boom")],
    )
    write_jsonl(
        temp_telemetry / "logs.jsonl", [make_log(base, severity="ERROR", sevnum=17)]
    )
    write_jsonl(temp_telemetry / "metrics.jsonl", [make_gauge(base), make_sum(base)])
    conn = otelq.connect(temp_telemetry)
    by = {r[0]: r[1] for r in otelq.cmd_summary(conn, Namespace())[1]}
    assert by == {"traces": 1, "logs": 1, "metrics": 2}
    assert conn.execute("SELECT min(timestamp) FROM traces").fetchone()[0].year == 2026


# --- AC-1 / FR-1, FR-5: sealing produces per-minute partitions ----------------


def test_ac1_seals_complete_minutes(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    spans = [
        make_span(base + timedelta(seconds=i * 10), trace_id=f"t{i}", span_id=f"s{i}")
        for i in range(30)
    ]  # 5 min of spans
    write_jsonl(temp_telemetry / "traces.jsonl", spans)
    otelq.build_connection(
        temp_telemetry, otelq.Plan("HOT", timedelta(minutes=30), True)
    )
    sealed = sorted((temp_telemetry / CACHE / "traces").glob("*.parquet"))
    # watermark ~12:04:50 -> seal_high ~12:01:50 -> minutes 12:00, 12:01 seal
    assert [p.stem for p in sealed] == ["2026-06-22T12-00", "2026-06-22T12-01"]
    assert (temp_telemetry / CACHE / otelq.PENDING_DIRNAME / "traces.parquet").exists()
    assert (temp_telemetry / CACHE / otelq.CURSOR_FILENAME).exists()


# --- AC-11 / FR-11 / INV-1, INV-4: cached == --no-cache (the keystone) ---------


def test_ac11_cached_equals_no_cache(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    spans, logs, gauges = [], [], []
    for i in range(20):  # 20 minutes, all inside the 30-min hot window
        t = base + timedelta(minutes=i, seconds=5)
        err = i % 5 == 0
        spans.append(
            make_span(
                t,
                trace_id=f"t{i}",
                span_id=f"s{i}",
                status_code=2 if err else 0,
                status_msg="boom",
                duration_ms=i + 1,
            )
        )
        logs.append(
            make_log(
                t,
                severity="ERROR" if err else "INFO",
                sevnum=17 if err else 9,
                body=f"msg{i}",
            )
        )
        gauges.append(make_gauge(t, value=float(i)))
    write_jsonl(temp_telemetry / "traces.jsonl", spans)
    write_jsonl(temp_telemetry / "logs.jsonl", logs)
    write_jsonl(temp_telemetry / "metrics.jsonl", gauges)
    for argv in (
        ["summary"],
        ["errors"],
        ["slow"],
        ["logs"],
        ["metric", "db.pool"],
        ["trace", trace_hex("t3")],
    ):
        cached = _run(temp_telemetry, *argv)
        nocache = _run(temp_telemetry, "--no-cache", *argv)
        assert cached == nocache, (
            f"cached != --no-cache for {argv}\n{cached}\n---\n{nocache}"
        )


# --- AC-17 / FR-17: --no-cache writes nothing ---------------------------------


def test_ac17_no_cache_leaves_cache_untouched(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "traces.jsonl",
        [
            make_span(
                base + timedelta(seconds=i * 10), trace_id=f"t{i}", span_id=f"s{i}"
            )
            for i in range(12)
        ],
    )
    _run(temp_telemetry, "--no-cache", "summary")
    assert not (temp_telemetry / CACHE).exists()


# --- AC-2 / FR-2: a second run reads no new raw bytes -------------------------


def test_ac2_incremental_no_rebytes(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "traces.jsonl",
        [
            make_span(
                base + timedelta(seconds=i * 10), trace_id=f"t{i}", span_id=f"s{i}"
            )
            for i in range(30)
        ],
    )
    otelq.build_connection(
        temp_telemetry, otelq.Plan("HOT", timedelta(minutes=30), True)
    )
    cur1 = _json.loads((temp_telemetry / CACHE / otelq.CURSOR_FILENAME).read_text())
    off1 = next(iter(cur1["streams"]["traces"]["files"].values()))["bytes_consumed"]
    size = (temp_telemetry / "traces.jsonl").stat().st_size
    assert off1 == size  # consumed to EOF
    otelq.build_connection(
        temp_telemetry, otelq.Plan("HOT", timedelta(minutes=30), True)
    )
    cur2 = _json.loads((temp_telemetry / CACHE / otelq.CURSOR_FILENAME).read_text())
    off2 = next(iter(cur2["streams"]["traces"]["files"].values()))["bytes_consumed"]
    assert off2 == off1  # nothing new consumed


# --- AC-13 / FR-13: cross-platform source guards ------------------------------


def test_ac13_portable_file_ops():
    src = Path(str(otelq.__file__)).read_text()
    assert "fcntl" not in src
    assert "os.rename(" not in src  # must use os.replace
    assert "os.replace(" in src
    # no text-mode tell() offset tracking; the tail reader is binary
    assert ".tell()" not in src


# --- AC-16 / FR-16: otel-clean removes the cache subtree ----------------------


def test_ac16_otel_clean_recipe_removes_cache():
    justfile = (Path(__file__).resolve().parents[1] / "justfile").read_text()
    assert "rm -rf telemetry/.otelq-cache" in justfile


import os as _os  # noqa: E402
import time as _time  # noqa: E402


def _build(dirpath, route="HOT", window_min=30, use_cache=True):
    win = None if window_min is None else timedelta(minutes=window_min)
    return otelq.build_connection(dirpath, otelq.Plan(route, win, use_cache))


def _run_both(dirpath, *argv):
    import io as _io
    from contextlib import redirect_stderr, redirect_stdout

    out, err = _io.StringIO(), _io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        otelq.main(["--dir", str(dirpath), "--format", "json", *argv])
    return out.getvalue(), err.getvalue()


def _signal_count(dirpath, signal, *argv):
    out = _run(dirpath, *argv)
    for row in _json.loads(out):
        if row["signal"] == signal:
            return row["count"]
    return 0


def _minutes_per_minute(base, n):
    return [
        make_span(base + timedelta(minutes=i), trace_id=f"t{i}", span_id=f"s{i}")
        for i in range(n)
    ]


# --- AC-3 / FR-3 / EC-1: rotation mid-stream loses/duplicates nothing ---------


def test_ac3_rotation_no_gap_no_dup(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    traces = temp_telemetry / "traces.jsonl"
    write_jsonl(traces, _minutes_per_minute(base, 6))  # minutes 0..5
    _build(temp_telemetry)  # seals some, advances cursor
    # rotate: the active file becomes a backup, a fresh active file appears
    traces.rename(temp_telemetry / "traces-2026-06-22T12-05-00.000.jsonl")
    write_jsonl(
        traces,
        [
            make_span(
                base + timedelta(minutes=6 + i),
                trace_id=f"t{6 + i}",
                span_id=f"s{6 + i}",
            )
            for i in range(3)
        ],
    )  # 6,7,8
    cached = _run(temp_telemetry, "summary")  # HOT route — exercises the cache
    nocache = _run(temp_telemetry, "--no-cache", "summary")
    assert cached == nocache
    assert _json.loads(cached)[0]["count"] == 9  # all 9 spans, none lost/duplicated


# --- AC-5 / FR-5 / EC-2: cold start seals only the hot window ------------------


def test_ac5_cold_start_seals_only_hot_window(temp_telemetry):
    base = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 90))
    _build(temp_telemetry)
    sealed = sorted((temp_telemetry / CACHE / "traces").glob("*.parquet"))
    assert 20 < len(sealed) < 40, f"expected ~30 sealed minutes, got {len(sealed)}"
    oldest = min(otelq.parse_minute_key(p.stem) for p in sealed)
    floor = (base + timedelta(minutes=55)).replace(tzinfo=None)
    assert oldest >= floor  # nothing older than the window


# --- AC-6 / FR-6: a later run evicts partitions that fell out of the window ----


def test_ac6_eviction_drops_stale_partitions(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    traces = temp_telemetry / "traces.jsonl"
    write_jsonl(traces, _minutes_per_minute(base, 6))  # minutes 0..5
    _build(temp_telemetry)
    assert otelq.sealed_path(temp_telemetry, "traces", base).exists()
    # append data 40 minutes later; the watermark jumps, evicting the old minutes
    write_jsonl(
        traces,
        [
            make_span(
                base + timedelta(minutes=40 + i),
                trace_id=f"t{40 + i}",
                span_id=f"s{40 + i}",
            )
            for i in range(5)
        ],
        append=True,
    )
    _build(temp_telemetry)
    assert not otelq.sealed_path(temp_telemetry, "traces", base).exists()  # evicted
    sealed = [
        otelq.parse_minute_key(p.stem)
        for p in (temp_telemetry / CACHE / "traces").glob("*.parquet")
    ]
    floor = (base + timedelta(minutes=10)).replace(tzinfo=None)
    assert all(m >= floor for m in sealed)


# --- AC-9 / FR-9: recent-by-default, --all widens -----------------------------


def test_ac9_recent_default_vs_all(temp_telemetry):
    base = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 90))
    default = _signal_count(temp_telemetry, "traces", "summary")
    widened = _signal_count(temp_telemetry, "traces", "--all", "summary")
    assert widened == 90
    assert default < widened  # default reports only the hot window
    assert 25 <= default <= 35


# --- AC-8 / FR-8: --since beyond the window reaches old data (cold path) -------


def test_ac8_since_beyond_window_is_cold(temp_telemetry):
    base = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 90))
    far = _signal_count(temp_telemetry, "traces", "summary", "--since", "120m")
    assert far == 90


# --- AC-10 / FR-10: trace lookup falls back to cold for an old id -------------


def test_ac10_trace_cold_fallback(temp_telemetry):
    base = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 90))
    # t5 is ~85 minutes old: absent from the hot cache, found via cold fallback
    out = _run(temp_telemetry, "trace", trace_hex("t5"))
    assert len(_json.loads(out)) == 1
    assert out == _run(temp_telemetry, "--no-cache", "trace", trace_hex("t5"))


# --- AC-14 / FR-14 / EC-7, EC-8: version mismatch self-wipes and rebuilds ------


def test_ac14_version_mismatch_self_heals(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 6))
    _build(temp_telemetry)
    cdir = temp_telemetry / CACHE
    (cdir / otelq.CURSOR_FILENAME).write_text(_json.dumps({"version": 999}))
    stray = cdir / "traces" / "junk.parquet.tmp"
    stray.write_text("garbage")
    _build(temp_telemetry)  # must wipe + rebuild rather than crash
    cur = _json.loads((cdir / otelq.CURSOR_FILENAME).read_text())
    assert cur["version"] == otelq.CURSOR_SCHEMA_VERSION
    assert not stray.exists()  # wiped
    assert _json.loads(_run(temp_telemetry, "summary"))[0]["count"] == 6


# --- AC-15 / FR-15 / EC-4, EC-5: partial line + oversized batch are skipped ----


def test_ac15_robust_tail_parsing(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    traces = temp_telemetry / "traces.jsonl"
    write_jsonl(traces, _minutes_per_minute(base, 5))  # 5 valid spans
    # one indivisible batch of 2049 spans (exceeds the read_otlp 2048 limit)
    big = {
        "resourceSpans": [
            {
                "resource": _resource("app-test"),
                "scopeSpans": [
                    {
                        "spans": [
                            make_span(base)["resourceSpans"][0]["scopeSpans"][0][
                                "spans"
                            ][0]
                            for _ in range(2049)
                        ]
                    }
                ],
            }
        ]
    }
    with open(traces, "a", encoding="utf-8") as fh:
        fh.write(_json.dumps(big) + "\n")
        fh.write('{"resourceSpans": [ {"partial"')  # truncated trailing line
    out, err = _run_both(temp_telemetry, "--all", "summary")
    assert _json.loads(out)[0]["count"] == 5  # only the valid spans
    assert "exceeds the 2048-row" in err  # oversized batch warned + skipped


# --- AC-18 / INV-6: raw files are never modified ------------------------------


def test_ac18_raw_files_unmodified(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    traces = temp_telemetry / "traces.jsonl"
    write_jsonl(traces, _minutes_per_minute(base, 30))
    before = traces.read_bytes()
    _build(temp_telemetry)
    _run(temp_telemetry, "summary")
    assert traces.read_bytes() == before


# --- AC-20 / EC-6: zeroed st_ino still disambiguates two files -----------------


def test_ac20_zeroed_st_ino_disambiguates(temp_telemetry, monkeypatch):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 5))
    write_jsonl(
        temp_telemetry / "traces-2026-06-22T11-00-00.000.jsonl",
        [
            make_span(
                base + timedelta(minutes=5 + i),
                trace_id=f"t{5 + i}",
                span_id=f"s{5 + i}",
            )
            for i in range(5)
        ],
    )
    real_stat = _os.stat

    def zeroed(path, *a, **k):
        s = real_stat(path, *a, **k)
        vals = list(s)
        vals[1] = 0  # st_ino -> 0; fingerprint+size must disambiguate
        return _os.stat_result(vals)

    monkeypatch.setattr(otelq.os, "stat", zeroed)
    cached = _run(temp_telemetry, "summary")  # HOT route — uses the inode/fp cursor
    monkeypatch.undo()
    nocache = _run(temp_telemetry, "--no-cache", "summary")
    assert cached == nocache
    assert _json.loads(cached)[0]["count"] == 10  # both files read, none collided


# --- AC-12 / FR-12 / INV-5: lock contention still answers, skips sealing -------


def test_ac12_lock_contention_reads_without_sealing(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 40))
    cdir = temp_telemetry / CACHE
    cdir.mkdir(parents=True)
    (cdir / otelq.LOCK_FILENAME).write_text(str(_os.getpid()))  # live holder
    cached = _run(temp_telemetry, "summary")
    nocache = _run(temp_telemetry, "--no-cache", "summary")
    assert cached == nocache  # answered correctly despite losing the lock
    assert not (cdir / otelq.CURSOR_FILENAME).exists()  # no sealing happened
    assert not (cdir / "traces").exists()


# --- FR-11/INV-4 regression: late arrival into an already-sealed minute --------
# (the bug the adversarial review found; AC-11's in-order data missed it)


def test_late_arrival_to_sealed_minute_stays_queryable(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    traces = temp_telemetry / "traces.jsonl"
    write_jsonl(traces, _minutes_per_minute(base, 6))  # minute 12:00 will seal
    _build(temp_telemetry)
    assert otelq.sealed_path(temp_telemetry, "traces", base).exists()
    # append a late span whose event-time falls in the already-sealed minute 12:00
    write_jsonl(
        traces,
        [make_span(base + timedelta(seconds=30), trace_id="late", span_id="late")],
        append=True,
    )
    cached = _run(temp_telemetry, "summary")  # HOT route
    nocache = _run(temp_telemetry, "--no-cache", "summary")
    assert cached == nocache  # late record must remain on the hot path
    assert _json.loads(cached)[0]["count"] == 7


def test_clock_skew_outlier_does_not_drop_records(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    traces = temp_telemetry / "traces.jsonl"
    # a single far-future span yanks the watermark to 12:10, sealing 12:00 early
    write_jsonl(
        traces,
        [
            make_span(base + timedelta(seconds=10), trace_id="a", span_id="a"),
            make_span(base + timedelta(seconds=20), trace_id="b", span_id="b"),
            make_span(
                base + timedelta(minutes=10), trace_id="future", span_id="future"
            ),
        ],
    )
    _build(temp_telemetry)
    # a normal-time span for the already-sealed minute 12:00 arrives next run
    write_jsonl(
        traces,
        [make_span(base + timedelta(seconds=30), trace_id="c", span_id="c")],
        append=True,
    )
    cached = _run(temp_telemetry, "summary")  # HOT route
    assert cached == _run(temp_telemetry, "--no-cache", "summary")
    assert _json.loads(cached)[0]["count"] == 4


# --- AC-19 / INV-2 / EC-11: crash between seal and cursor advance --------------


def test_ac19_crash_immutability_and_no_loss(temp_telemetry):
    """A crash between sealing and the cursor advance (simulated by a cursor
    rollback) keeps sealed partitions immutable (INV-2) and loses no record. A
    pathological full rollback may transiently OVER-count the recent unsealed
    minutes — a documented, otel-clean-recoverable bound (SPEC EC-11); equivalence
    is exact in normal operation (no crash)."""
    import hashlib as _h

    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 6))
    _build(temp_telemetry)
    sealed = sorted((temp_telemetry / CACHE / "traces").glob("*.parquet"))
    assert sealed
    before = {p.name: _h.sha256(p.read_bytes()).hexdigest() for p in sealed}
    # simulate a crash that sealed partitions but died before _write_cursor:
    # roll the traces stream offsets back to 0 (keep version valid -> no wipe)
    cpath = temp_telemetry / CACHE / otelq.CURSOR_FILENAME
    cur = _json.loads(cpath.read_text())
    for key in cur["streams"]["traces"]["files"]:
        cur["streams"]["traces"]["files"][key]["bytes_consumed"] = 0
    cpath.write_text(_json.dumps(cur))
    _build(temp_telemetry)  # re-reads already-sealed minutes; must stay idempotent
    after = {
        p.name: _h.sha256(p.read_bytes()).hexdigest()
        for p in sorted((temp_telemetry / CACHE / "traces").glob("*.parquet"))
    }
    for name, digest in before.items():
        assert after.get(name) == digest, f"sealed partition {name} mutated (INV-2)"
    cached_n = _json.loads(_run(temp_telemetry, "summary"))[0]["count"]
    cold_n = _json.loads(_run(temp_telemetry, "--no-cache", "summary"))[0]["count"]
    assert (
        cached_n >= cold_n
    )  # no data LOSS (a full rollback may transiently over-count)


# --- FR-12/INV-5 regression: live writer's lock is never reaped ----------------


def test_live_lock_reaped_only_past_hard_ceiling(temp_telemetry):
    cdir = temp_telemetry / CACHE
    cdir.mkdir(parents=True)
    lock = cdir / otelq.LOCK_FILENAME
    lock.write_text(str(_os.getpid()))  # a live pid
    # live + within the hard ceiling => never reaped (genuine long catch-up seal)
    old = _time.time() - otelq._LOCK_STALE_SECS - 100
    _os.utime(lock, (old, old))
    assert otelq._reap_if_stale(lock) is False
    assert lock.exists()
    # live + past the hard ceiling => reaped (pid almost certainly reused/wedged)
    ancient = _time.time() - otelq._LOCK_HARD_STALE_SECS - 100
    _os.utime(lock, (ancient, ancient))
    assert otelq._reap_if_stale(lock) is True
    assert not lock.exists()
    # dead pid + old mtime => reaped
    lock.write_text("2147483646")
    _os.utime(lock, (old, old))
    assert otelq._reap_if_stale(lock) is True
    assert not lock.exists()


# --- FR-11 regression: lock-loser's stale-offset tail must not double-count ----


def test_tail_does_not_double_count_sealed(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 8))
    _build(temp_telemetry)  # seals minutes + writes pending + advances cursor
    cdir = temp_telemetry / CACHE
    cpath = cdir / otelq.CURSOR_FILENAME
    cur = _json.loads(cpath.read_text())
    for key in cur["streams"]["traces"]["files"]:
        cur["streams"]["traces"]["files"][key]["bytes_consumed"] = 0  # stale offset
    cpath.write_text(_json.dumps(cur))
    (cdir / otelq.LOCK_FILENAME).write_text(str(_os.getpid()))  # force the reader path
    cached = _run(temp_telemetry, "summary")  # hot: sealed ∪ pending ∪ bounded tail
    assert cached == _run(temp_telemetry, "--no-cache", "summary")
    assert _json.loads(cached)[0]["count"] == 8  # not 16 — tail bounded past sealed


# --- FR-11 regression: sub-minute window boundary (found by the live check) ----


def test_subminute_window_boundary_equivalence(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    # 13s spacing over ~37 min, so the 30-min window lower bound (now - 30min)
    # lands mid-minute — the minute straddling it must not be evicted away.
    spans = [
        make_span(base + timedelta(seconds=i * 13), trace_id=f"t{i}", span_id=f"s{i}")
        for i in range(170)
    ]
    write_jsonl(temp_telemetry / "traces.jsonl", spans)
    cached = _run(temp_telemetry, "summary")
    nocache = _run(temp_telemetry, "--no-cache", "summary")
    assert cached == nocache  # cache must serve the sub-minute window tail exactly


# --- FR-11 regression: byte-identical late records must not be collapsed -------
# (EXCEPT vs EXCEPT ALL — the second adversarial pass found this)


def test_identical_late_records_kept(temp_telemetry):
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    logs_path = temp_telemetry / "logs.jsonl"
    write_jsonl(
        logs_path,
        [make_log(base + timedelta(minutes=i), body=f"m{i}") for i in range(8)],
    )  # minute 12:00 seals
    _build(temp_telemetry)
    dup = make_log(base + timedelta(seconds=30), body="dup")  # in sealed minute 12:00
    write_jsonl(logs_path, [dup, dup], append=True)  # two BYTE-IDENTICAL late logs
    cached = _run(temp_telemetry, "summary")  # HOT route
    nocache = _run(temp_telemetry, "--no-cache", "summary")
    assert cached == nocache  # EXCEPT ALL keeps both; plain EXCEPT would drop one
    by = {r["signal"]: r["count"] for r in _json.loads(cached)}
    assert by["logs"] == 10  # 8 originals + 2 identical late records


# --- error-message clarity: name the missing signal, don't cry "collector down" -
# The generic "no telemetry captured — is the collector running?" misleads when
# only ONE signal is absent: it sent a whole debugging session chasing the
# collector when traces were flowing fine and only logs.jsonl was missing.


def test_require_names_missing_signal_when_others_present():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE traces(timestamp TIMESTAMP)")  # traces present, no logs
    with pytest.raises(otelq.NoTelemetryError) as exc:
        otelq.cmd_logs(conn, Namespace(service=None, level=None, grep=None))
    msg = str(exc.value)
    assert msg != otelq._NO_TELEMETRY_MSG  # not the misleading generic text
    assert "logs" in msg  # names the missing signal
    assert "traces" in msg  # names what IS present


def test_require_keeps_generic_message_when_nothing_present():
    conn = duckdb.connect(":memory:")  # no relations at all -> collector likely down
    with pytest.raises(otelq.NoTelemetryError) as exc:
        otelq.cmd_logs(conn, Namespace(service=None, level=None, grep=None))
    assert str(exc.value) == otelq._NO_TELEMETRY_MSG


def test_errors_names_gap_when_only_metrics_present():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE metrics(timestamp TIMESTAMP)")  # metrics only
    with pytest.raises(otelq.NoTelemetryError) as exc:
        otelq.cmd_errors(conn, Namespace())
    msg = str(exc.value)
    assert msg != otelq._NO_TELEMETRY_MSG
    assert "metrics" in msg  # names the present signal so the gap is obvious
