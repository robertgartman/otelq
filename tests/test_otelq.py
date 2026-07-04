# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "duckdb>=1.1.0"]
# ///
"""Tests for otelq. Run: just otelq-test"""

import sys
from argparse import Namespace
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import otelq  # noqa: E402

TESTDATA = Path(__file__).resolve().parent / "testdata"


@pytest.fixture
def synth_conn() -> duckdb.DuckDBPyConnection:
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
        -- duration is integer milliseconds (extension unit): 5 / 90 / 12 ms.
        INSERT INTO traces VALUES
          ('2026-05-22 10:00:00',0,5,'trace-a','span-a1','','','checkout-api',
           '','','GET /orders',2,1,'','','','','','','','',0,0,0,0),
          ('2026-05-22 10:00:01',0,90,'trace-a','span-a2','span-a1','',
           'checkout-api','','','SELECT orders',3,1,'','','','','','','','',0,0,0,0),
          ('2026-05-22 10:00:02',0,12,'trace-b','span-b1','','','catalog-api',
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
    # histogram/exp_histogram mirror the duckdb-otlp reader columns; the unified
    # `metrics` view surfaces their `sum` as `value` (they have no scalar value).
    conn.execute(
        """
        CREATE TABLE metrics_histogram (
            timestamp TIMESTAMP_MS, start_timestamp BIGINT, metric_name VARCHAR,
            metric_description VARCHAR, metric_unit VARCHAR, count BIGINT, sum DOUBLE,
            min DOUBLE, max DOUBLE, bucket_counts VARCHAR, explicit_bounds VARCHAR,
            service_name VARCHAR, service_namespace VARCHAR, service_instance_id VARCHAR,
            resource_attributes VARCHAR, scope_name VARCHAR, scope_version VARCHAR,
            scope_attributes VARCHAR, metric_attributes VARCHAR, flags INTEGER,
            exemplars_json VARCHAR, aggregation_temporality INTEGER);
        INSERT INTO metrics_histogram
          (timestamp, metric_name, metric_unit, count, sum, min, max, service_name,
           aggregation_temporality)
        VALUES
          ('2026-05-22 10:00:00','http.server.duration','ms',10,123.5,1.0,50.0,
           'checkout-api',2);
        """
    )
    conn.execute(
        """
        CREATE TABLE metrics_exp_histogram (
            timestamp TIMESTAMP_MS, start_timestamp BIGINT, metric_name VARCHAR,
            metric_description VARCHAR, metric_unit VARCHAR, count BIGINT, sum DOUBLE,
            min DOUBLE, max DOUBLE, scale INTEGER, zero_count BIGINT,
            zero_threshold DOUBLE, positive_offset INTEGER,
            positive_bucket_counts VARCHAR, negative_offset INTEGER,
            negative_bucket_counts VARCHAR, service_name VARCHAR,
            service_namespace VARCHAR, service_instance_id VARCHAR,
            resource_attributes VARCHAR, scope_name VARCHAR, scope_version VARCHAR,
            scope_attributes VARCHAR, metric_attributes VARCHAR, flags INTEGER,
            exemplars_json VARCHAR, aggregation_temporality INTEGER);
        INSERT INTO metrics_exp_histogram
          (timestamp, metric_name, metric_unit, count, sum, min, max, scale,
           zero_count, service_name, aggregation_temporality)
        VALUES
          ('2026-05-22 10:00:01','rpc.server.duration','ms',5,42.0,2.0,20.0,2,0,
           'catalog-api',2);
        """
    )
    otelq.create_unified_metrics_view(conn)
    return conn


def test_format_output_json() -> None:
    out = otelq.format_output(["a", "b"], [(1, "x")], "json")
    import json as _json

    assert _json.loads(out) == [{"a": 1, "b": "x"}]


def test_format_output_csv() -> None:
    out = otelq.format_output(["a", "b"], [(1, "x")], "csv")
    assert out == "a,b\r\n1,x"


def test_format_output_table_empty() -> None:
    assert otelq.format_output(["a"], [], "table") == "(no rows)"


def _summary_conn(
    *,
    traces: list[tuple[str, int, str]] | None = None,
    logs: list[tuple[str, int, str, str]] | None = None,
    with_metrics: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Minimal in-memory conn for summary tests: only the columns cmd_summary
    reads (traces.duration, logs.severity_number, the metrics view)."""
    conn = duckdb.connect(":memory:")
    if traces is not None:
        conn.execute(
            "CREATE TABLE traces "
            "(timestamp TIMESTAMP_MS, duration BIGINT, service_name VARCHAR)"
        )
        if traces:
            conn.executemany("INSERT INTO traces VALUES (?, ?, ?)", traces)
    if logs is not None:
        conn.execute(
            "CREATE TABLE logs (timestamp TIMESTAMP_MS, severity_number INTEGER, "
            "severity_text VARCHAR, service_name VARCHAR)"
        )
        if logs:
            conn.executemany("INSERT INTO logs VALUES (?, ?, ?, ?)", logs)
    if with_metrics:
        conn.execute(
            "CREATE TABLE metrics_gauge (timestamp TIMESTAMP_MS, service_name VARCHAR, "
            "metric_name VARCHAR, metric_unit VARCHAR, value DOUBLE)"
        )
        conn.execute("INSERT INTO metrics_gauge VALUES ('2026-05-22 10:00:00','svc','m','1',1.0)")
        otelq.create_unified_metrics_view(conn)
    return conn


def test_summary_breakdown_rows(synth_conn: duckdb.DuckDBPyConnection) -> None:
    # AC-2 / FR-3: details column + per-signal breakdown, scoped per row.
    columns, rows = otelq.cmd_summary(synth_conn, Namespace())
    assert columns == ["signal", "details", "count", "earliest", "latest", "services"]
    count = {(r[0], r[1]): r[2] for r in rows}
    # traces (5/90/12 ms) all fall in =<1s
    assert count[("traces", ">1s")] == 0
    assert count[("traces", "=<1s")] == 3
    # logs: one INFO (sevnum 9), one ERROR (sevnum 17)
    assert count[("logs", "INFO")] == 1
    assert count[("logs", "ERROR")] == 1
    # metrics: one row per type, scoped to that type (2 gauge, 1 sum, 1 histogram,
    # 1 exp_histogram)
    assert count[("metrics", "gauge")] == 2
    assert count[("metrics", "sum")] == 1
    assert count[("metrics", "histogram")] == 1
    assert count[("metrics", "exp_histogram")] == 1
    # the four metric-type rows appear in canonical order
    assert [r[1] for r in rows if r[0] == "metrics"] == [
        "gauge",
        "sum",
        "histogram",
        "exp_histogram",
    ]
    # per-subset services: ERROR logs came only from catalog-api
    err = next(r for r in rows if r[:2] == ("logs", "ERROR"))
    assert err[5] == 1


def test_summary_zero_count_skeleton(synth_conn: duckdb.DuckDBPyConnection) -> None:
    # AC-23 / EC-11: present signals show their full skeleton, zeros included.
    _columns, rows = otelq.cmd_summary(synth_conn, Namespace())
    levels = [r[1] for r in rows if r[0] == "logs"]
    assert levels == ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
    by = {(r[0], r[1]): r for r in rows}
    for lvl in ("TRACE", "DEBUG", "WARN", "FATAL"):
        row = by[("logs", lvl)]
        assert row[2] == 0  # count
        assert row[3] is None and row[4] is None  # earliest / latest
        assert row[5] == 0  # services
    assert by[("traces", ">1s")][2] == 0  # both buckets present, >1s empty


def test_summary_level_from_severity_number() -> None:
    # AC-24 / EC-12: level comes from severity_number, not mixed-case text.
    conn = _summary_conn(
        logs=[
            ("2026-05-22 10:00:00", 9, "Info", "svc"),
            ("2026-05-22 10:00:01", 9, "info", "svc"),
        ]
    )
    count = {(r[0], r[1]): r[2] for r in otelq.cmd_summary(conn, Namespace())[1]}
    assert count[("logs", "INFO")] == 2
    assert ("logs", "UNSET") not in count  # no out-of-range record


def test_summary_unset_row_only_when_present() -> None:
    # AC-24 / EC-12: out-of-range severities surface as an UNSET row.
    conn = _summary_conn(
        logs=[
            ("2026-05-22 10:00:00", 9, "Info", "svc"),
            ("2026-05-22 10:00:01", 0, "", "svc"),  # 0 -> UNSET
            ("2026-05-22 10:00:02", 99, "WAT", "svc"),  # out of range -> UNSET
        ]
    )
    count = {(r[0], r[1]): r[2] for r in otelq.cmd_summary(conn, Namespace())[1]}
    assert count[("logs", "INFO")] == 1
    assert count[("logs", "UNSET")] == 2


def test_summary_absent_signal_has_no_rows() -> None:
    # AC-25 / FR-3: only metrics present -> the four metric-type rows (one gauge
    # row of real data, the other three at zero), and NO zero-count trace/log
    # skeleton (a wholly-absent signal contributes no rows).
    conn = _summary_conn(with_metrics=True)  # only metrics present (one gauge)
    _columns, rows = otelq.cmd_summary(conn, Namespace())
    assert [r[0] for r in rows] == ["metrics"] * 4  # no traces/logs rows at all
    count = {(r[0], r[1]): r[2] for r in rows}
    assert count[("metrics", "gauge")] == 1
    assert count[("metrics", "sum")] == 0
    assert count[("metrics", "histogram")] == 0
    assert count[("metrics", "exp_histogram")] == 0


def test_summary_raises_when_empty() -> None:
    conn = duckdb.connect(":memory:")
    with pytest.raises(otelq.NoTelemetryError):
        otelq.cmd_summary(conn, Namespace())


def test_sql_passthrough(synth_conn: duckdb.DuckDBPyConnection) -> None:
    columns, rows = otelq.cmd_sql(
        synth_conn, Namespace(query="SELECT count(*) AS n FROM traces")
    )
    assert columns == ["n"]
    assert rows == [(3,)]


def test_all_relations_resolve_and_metric_types(
    synth_conn: duckdb.DuckDBPyConnection,
) -> None:
    # AC-1 / FR-1, FR-2: every exposed relation resolves via `sql`, the `metrics`
    # view exposes the FR-2 columns, and it spans all four metric types.
    for rel in (
        "traces",
        "logs",
        "metrics",
        "metrics_gauge",
        "metrics_sum",
        "metrics_histogram",
        "metrics_exp_histogram",
    ):
        cols, _rows = otelq.cmd_sql(
            synth_conn, Namespace(query=f"SELECT * FROM {rel} LIMIT 1")
        )
        assert cols, f"{rel} did not resolve"
    cols, _ = otelq.cmd_sql(synth_conn, Namespace(query="SELECT * FROM metrics LIMIT 1"))
    assert {
        "timestamp",
        "service_name",
        "metric_name",
        "metric_type",
        "value",
        "metric_unit",
    } <= set(cols)
    _c, rows = otelq.cmd_sql(
        synth_conn, Namespace(query="SELECT DISTINCT metric_type FROM metrics")
    )
    assert {r[0] for r in rows} == {"gauge", "sum", "histogram", "exp_histogram"}


def test_metrics_view_value_is_value_or_sum(
    synth_conn: duckdb.DuckDBPyConnection,
) -> None:
    # FR-2: metrics.value carries the scalar `value` for gauge/sum and the `sum`
    # for histogram/exp_histogram (which have no scalar value).
    _c, rows = otelq.cmd_sql(
        synth_conn,
        Namespace(
            query="SELECT metric_type, value FROM metrics "
            "WHERE metric_type IN ('histogram', 'exp_histogram') ORDER BY metric_type"
        ),
    )
    got = dict(rows)
    assert got["exp_histogram"] == 42.0  # the exp_histogram row's sum
    assert got["histogram"] == 123.5  # the histogram row's sum


def test_integration_reads_real_fixture() -> None:
    """connect() + the duckdb-otlp extension read genuine Collector output."""
    conn = otelq.connect(TESTDATA)
    _columns, rows = otelq.cmd_summary(conn, Namespace())
    assert sum(r[2] for r in rows if r[0] == "traces") > 0


def test_errors_finds_error_span_and_log(
    synth_conn: duckdb.DuckDBPyConnection,
) -> None:
    _columns, rows = otelq.cmd_errors(synth_conn, Namespace(since=None))
    kinds = sorted(r[0] for r in rows)
    assert kinds == ["log", "span"]
    assert all(r[2] == "catalog-api" for r in rows)


def test_errors_matches_mixed_case_severity_text() -> None:
    # FR-4 regression: ERROR/FATAL logs must match case-insensitively. Real
    # exporters store mixed-case text (e.g. "Error"/"Fatal"); a case-sensitive
    # filter silently dropped them.
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE logs (timestamp TIMESTAMP_MS, trace_id VARCHAR, "
        "service_name VARCHAR, severity_text VARCHAR, severity_number INTEGER, "
        "body VARCHAR)"
    )
    conn.execute(
        "INSERT INTO logs VALUES "
        "('2026-05-22 10:00:00','t1','app','Error',17,'boom'),"
        "('2026-05-22 10:00:01','t2','app','Fatal',21,'dead'),"
        "('2026-05-22 10:00:02','t3','app','Info',9,'fine')"
    )
    _columns, rows = otelq.cmd_errors(conn, Namespace(since=None))
    assert sorted(r[3] for r in rows) == ["Error", "Fatal"]


def test_slow_orders_by_duration_desc(
    synth_conn: duckdb.DuckDBPyConnection,
) -> None:
    _columns, rows = otelq.cmd_slow(synth_conn, Namespace(top=2))
    assert len(rows) == 2
    assert rows[0][2] == "SELECT orders"  # 90ms span first
    assert rows[0][3] >= rows[1][3]  # duration_ms descending


def test_slow_and_summary_use_millisecond_duration() -> None:
    # FR-3/FR-5 regression: the duckdb-otlp extension reports duration in ms.
    # A 2000 ms span must land in the ">1s" bucket and show as 2000 ms in `slow`.
    # The old code treated duration as ns: it never crossed the 1e9 threshold
    # (so ">1s" was always empty) and divided by 1e6 (so `slow` showed 0.0 ms).
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE traces (timestamp TIMESTAMP_MS, duration BIGINT, "
        "service_name VARCHAR, span_name VARCHAR, trace_id VARCHAR)"
    )
    conn.execute(
        "INSERT INTO traces VALUES "
        "('2026-05-22 10:00:00',2000,'app','slow-op','t1'),"  # 2s  -> >1s
        "('2026-05-22 10:00:01',12,'app','fast-op','t2')"     # 12ms -> =<1s
    )
    summary = {(r[0], r[1]): r[2] for r in otelq.cmd_summary(conn, Namespace())[1]}
    assert summary[("traces", ">1s")] == 1
    assert summary[("traces", "=<1s")] == 1
    cols, rows = otelq.cmd_slow(conn, Namespace(top=1))
    assert rows[0][cols.index("span_name")] == "slow-op"
    assert rows[0][cols.index("duration_ms")] == 2000


def test_trace_returns_tree_for_one_trace(
    synth_conn: duckdb.DuckDBPyConnection,
) -> None:
    _columns, rows = otelq.cmd_trace(synth_conn, Namespace(trace_id="trace-a"))
    assert len(rows) == 2
    assert rows[0][0] == 0 and rows[1][0] == 1  # depth: root then child


def test_trace_unknown_id_raises(synth_conn: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(otelq.NoTelemetryError):
        otelq.cmd_trace(synth_conn, Namespace(trace_id="does-not-exist"))


def test_logs_filter_by_service(synth_conn: duckdb.DuckDBPyConnection) -> None:
    _columns, rows = otelq.cmd_logs(
        synth_conn, Namespace(service="catalog-api", level=None, grep=None)
    )
    assert len(rows) == 1
    assert rows[0][1] == "catalog-api"


def test_logs_filter_by_level(synth_conn: duckdb.DuckDBPyConnection) -> None:
    _columns, rows = otelq.cmd_logs(
        synth_conn, Namespace(service=None, level="error", grep=None)
    )
    assert len(rows) == 1
    assert rows[0][2] == "ERROR"


def test_logs_filter_by_level_mixed_case_text() -> None:
    # FR-7 regression: --level must match severity_text case-insensitively.
    # Real exporters store mixed-case text (e.g. "Info"); `--level INFO` once
    # missed these because only the input was folded, not the column.
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE logs (timestamp TIMESTAMP_MS, trace_id VARCHAR, "
        "service_name VARCHAR, severity_text VARCHAR, severity_number INTEGER, "
        "body VARCHAR)"
    )
    conn.execute(
        "INSERT INTO logs VALUES "
        "('2026-05-22 10:00:00','t1','app','Info',9,'a'),"
        "('2026-05-22 10:00:01','t2','app','Info',9,'b')"
    )
    _columns, rows = otelq.cmd_logs(
        conn, Namespace(service=None, level="INFO", grep=None)
    )
    assert len(rows) == 2
    assert all(r[2] == "Info" for r in rows)


def test_logs_filter_by_grep(synth_conn: duckdb.DuckDBPyConnection) -> None:
    _columns, rows = otelq.cmd_logs(
        synth_conn, Namespace(service=None, level=None, grep="save")
    )
    assert len(rows) == 1
    assert "save" in rows[0][3].lower()  # body column


def test_metric_returns_time_series(
    synth_conn: duckdb.DuckDBPyConnection,
) -> None:
    _columns, rows = otelq.cmd_metric(synth_conn, Namespace(name="db.pool.in_use"))
    assert [r[4] for r in rows] == [4.0, 7.0]  # value column, time-ordered


def test_integration_timestamps_are_scaled() -> None:
    """Timestamps from real Collector output must be in 2026, not year ~58358.

    The duckdb-otlp extension stores nanoseconds in a TIMESTAMP_MS column;
    without the divide-by-1000 correction every timestamp renders as ~year 58358.
    This test uses the real fixture to guard that register_views applies the fix.
    """
    conn = otelq.connect(TESTDATA)
    row = conn.execute("SELECT min(timestamp) FROM traces").fetchone()
    assert row is not None
    assert row[0].year == 2026


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


def _resource(service: str) -> dict[str, Any]:
    return {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]}


def make_span(
    ts: datetime,
    trace_id: str = "t1",
    span_id: str = "s1",
    parent: str = "",
    name: str = "GET /x",
    service: str = "app-test",
    kind: int = 2,
    status_code: int = 0,
    status_msg: str = "",
    duration_ms: int = 5,
) -> dict[str, Any]:
    end = ts + timedelta(milliseconds=duration_ms)
    span: dict[str, Any] = {
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


def make_log(
    ts: datetime,
    service: str = "app-test",
    severity: str = "INFO",
    sevnum: int = 9,
    body: str = "hi",
    trace_id: str = "",
) -> dict[str, Any]:
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


def make_gauge(
    ts: datetime,
    name: str = "db.pool",
    unit: str = "{c}",
    value: float = 4.0,
    service: str = "app-test",
) -> dict[str, Any]:
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


def make_sum(
    ts: datetime,
    name: str = "reqs",
    unit: str = "{r}",
    value: int = 42,
    service: str = "app-test",
) -> dict[str, Any]:
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


def make_histogram(
    ts: datetime,
    name: str = "http.server.duration",
    unit: str = "ms",
    count: int = 10,
    total: float = 123.5,
    service: str = "app-test",
) -> dict[str, Any]:
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
                                "histogram": {
                                    "aggregationTemporality": 2,
                                    "dataPoints": [
                                        {
                                            "startTimeUnixNano": _ns(ts),
                                            "timeUnixNano": _ns(ts),
                                            "count": str(count),
                                            "sum": total,
                                            "bucketCounts": ["0", "1", "2", "3", "4"],
                                            "explicitBounds": [1.0, 5.0, 10.0, 25.0],
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


def make_exp_histogram(
    ts: datetime,
    name: str = "rpc.server.duration",
    unit: str = "ms",
    count: int = 5,
    total: float = 42.0,
    service: str = "app-test",
) -> dict[str, Any]:
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
                                "exponentialHistogram": {
                                    "aggregationTemporality": 2,
                                    "dataPoints": [
                                        {
                                            "startTimeUnixNano": _ns(ts),
                                            "timeUnixNano": _ns(ts),
                                            "count": str(count),
                                            "sum": total,
                                            "scale": 2,
                                            "zeroCount": "0",
                                            "positive": {
                                                "offset": 0,
                                                "bucketCounts": ["1", "2", "2"],
                                            },
                                            "negative": {
                                                "offset": 0,
                                                "bucketCounts": [],
                                            },
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


def write_jsonl(
    path: Path, objs: Iterable[dict[str, Any]], append: bool = False
) -> None:
    text = "".join(_json.dumps(o) + "\n" for o in objs)
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        fh.write(text)


@pytest.fixture
def temp_telemetry(tmp_path: Path) -> Path:
    d = tmp_path / ".telemetry"
    d.mkdir()
    return d


import re as _re  # noqa: E402

# The FR-29 response header (====== ... ------) that precedes the FR-10 payload
# for summary/errors/slow/trace/logs/metric, in every --format. A no-op on
# output that carries no header (sql/doctor/help/etc.), so tests that just need
# the bare payload can apply this unconditionally.
_HEADER_RE = _re.compile(r"\A==========\n.*?\n----------\n", _re.DOTALL)


def _strip_header(out: str) -> str:
    """Strip the FR-29 response header from otelq stdout, leaving the bare
    FR-10 payload for tests that parse or compare it directly."""
    return _HEADER_RE.sub("", out, count=1)


def _summary_first_block(out: str) -> str:
    """summary emits a labeled service second block (FR-3); return only the
    first (per-signal) block's payload for tests that parse it as one object."""
    return _strip_header(out).split("\n\n" + otelq._SUMMARY_SERVICE_LABEL, 1)[0]


def _run(dirpath: Path, *argv: str) -> str:
    """Run the CLI in-process; return its stdout string."""
    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    with redirect_stdout(buf):
        otelq.main(["--dir", str(dirpath), "--format", "json", *argv])
    return buf.getvalue()


# --- fabrication smoke test (validates OTLP shapes against the real extension) -


def test_fabricated_corpus_roundtrips(temp_telemetry: Path) -> None:
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "traces.jsonl",
        [make_span(base, status_code=2, status_msg="boom")],
    )
    write_jsonl(
        temp_telemetry / "logs.jsonl", [make_log(base, severity="ERROR", sevnum=17)]
    )
    write_jsonl(
        temp_telemetry / "metrics.jsonl",
        [make_gauge(base), make_sum(base), make_histogram(base), make_exp_histogram(base)],
    )
    conn = otelq.connect(temp_telemetry)
    by: dict[str, int] = {}
    for r in otelq.cmd_summary(conn, Namespace())[1]:  # sum the per-signal breakdown
        by[r[0]] = by.get(r[0], 0) + r[2]
    assert by == {"traces": 1, "logs": 1, "metrics": 4}  # one of each metric type
    row = conn.execute("SELECT min(timestamp) FROM traces").fetchone()
    assert row is not None
    assert row[0].year == 2026


def test_metrics_absent_types_resolve_empty_via_cli(temp_telemetry: Path) -> None:
    # FR-1: a gauge+sum-only corpus still resolves metrics_histogram /
    # metrics_exp_histogram — to 0 rows, never a catalog error — and the result is
    # byte-identical cached vs --no-cache (the keystone, expose-empty path).
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "metrics.jsonl", [make_gauge(base), make_sum(base)])
    for rel in ("metrics_histogram", "metrics_exp_histogram"):
        cached = _run(temp_telemetry, "sql", f"SELECT count(*) AS n FROM {rel}")
        nocache = _run(temp_telemetry, "--no-cache", "sql", f"SELECT count(*) AS n FROM {rel}")
        assert _json.loads(cached) == [{"n": 0}]  # resolves empty, not an error
        assert cached == nocache, f"cached != --no-cache for {rel}"


def test_metrics_four_types_value_is_sum_via_cli(temp_telemetry: Path) -> None:
    # FR-2: with all four metric types present, the `metrics` view spans them and
    # value = sum for histogram/exp_histogram; cached == --no-cache.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "metrics.jsonl",
        [
            make_gauge(base, value=4.0),
            make_sum(base, value=42),
            make_histogram(base, total=123.5),
            make_exp_histogram(base, total=42.0),
        ],
    )
    types = _run(temp_telemetry, "sql", "SELECT DISTINCT metric_type FROM metrics")
    assert {r["metric_type"] for r in _json.loads(types)} == {
        "gauge",
        "sum",
        "histogram",
        "exp_histogram",
    }
    hist = "SELECT metric_type, value FROM metrics WHERE metric_type = 'histogram'"
    cached = _run(temp_telemetry, "sql", hist)
    assert _json.loads(cached) == [{"metric_type": "histogram", "value": 123.5}]
    assert cached == _run(temp_telemetry, "--no-cache", "sql", hist)


def test_absent_signals_resolve_empty_metrics_only(temp_telemetry: Path) -> None:
    # FR-1 universal expose-empty: on a metrics-only corpus, traces/logs (and the
    # metrics view) still RESOLVE — to 0 rows, never a catalog error — byte-
    # identical cached vs --no-cache; summary shows only metric rows; slow/trace/
    # errors name the gap (present: metrics) rather than blaming the collector.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "metrics.jsonl", [make_gauge(base), make_sum(base)])
    for rel in ("traces", "logs"):
        cached = _run(temp_telemetry, "sql", f"SELECT count(*) AS n FROM {rel}")
        nocache = _run(temp_telemetry, "--no-cache", "sql", f"SELECT count(*) AS n FROM {rel}")
        assert _json.loads(cached) == [{"n": 0}]  # resolves empty, not an error
        assert cached == nocache, f"cached != --no-cache for {rel}"
    rows = _json.loads(_summary_first_block(_run(temp_telemetry, "summary")))
    assert {r["signal"] for r in rows} == {"metrics"}  # no trace/log skeleton
    for argv in (["slow"], ["trace", "deadbeef"], ["errors"]):
        out, err = _run_both(temp_telemetry, *argv)
        assert out.strip() == ""  # the command yields no rows on stdout
        assert "present: metrics" in err  # names the gap, not the generic text
        assert otelq._NO_TELEMETRY_MSG not in err


def test_empty_dir_resolves_empty_and_stays_friendly(temp_telemetry: Path) -> None:
    # FR-1/FR-18: a truly EMPTY dir still resolves every relation (0 rows, never a
    # catalog error — seeded from the embedded schema probe), byte-identical cached
    # vs --no-cache; yet the built-in commands still emit the generic friendly
    # message and exit 0, because presence is by row count (_has_rows), and no
    # zero-count skeleton is emitted.
    for rel in ("traces", "logs", "metrics", "metrics_histogram"):
        cached = _run(temp_telemetry, "sql", f"SELECT count(*) AS n FROM {rel}")
        nocache = _run(temp_telemetry, "--no-cache", "sql", f"SELECT count(*) AS n FROM {rel}")
        assert _json.loads(cached) == [{"n": 0}], f"{rel} should resolve to 0 rows"
        assert cached == nocache, f"cached != --no-cache for {rel}"
    for argv in (["summary"], ["slow"], ["errors"], ["metric", "x"]):
        out, err = _run_both(temp_telemetry, *argv)
        assert out.strip() == ""  # no rows / no skeleton on stdout
        assert otelq._NO_TELEMETRY_MSG in err  # generic friendly text, nothing present


# =============================================================================
# N4 regression tests — one focused test per original bug. Each pins the FIXED
# behavior: reverting the corresponding fix would fail the test.
# =============================================================================


def test_bug1_metrics_gauge_and_sum_queryable_cache_equals_nocache(
    temp_telemetry: Path,
) -> None:
    # BUG-1: on a gauge-only corpus, both metrics_gauge and metrics_sum must be
    # queryable — gauge has rows, sum resolves to 0 (never a catalog error) — and
    # byte-identical cached vs --no-cache.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "metrics.jsonl",
        [make_gauge(base), make_gauge(base + timedelta(seconds=5))],
    )
    for rel, n in (("metrics_gauge", 2), ("metrics_sum", 0)):
        cached = _run(temp_telemetry, "sql", f"SELECT count(*) AS n FROM {rel}")
        nocache = _run(temp_telemetry, "--no-cache", "sql", f"SELECT count(*) AS n FROM {rel}")
        assert _json.loads(cached) == [{"n": n}], f"{rel} expected {n}"
        assert cached == nocache, f"cached != --no-cache for {rel}"


def test_bug2_slow_top_negative_rejected_at_parse() -> None:
    # BUG-2: `slow --top -1` must be rejected by argparse (exit 2, "must be >= 0"),
    # NOT reach DuckDB as LIMIT -1 (an uncaught BinderException traceback).
    import io as _io
    from contextlib import redirect_stderr

    parser = otelq.build_parser()
    err = _io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stderr(err):
        parser.parse_args(["slow", "--top", "-1"])
    assert exc.value.code == 2
    assert "must be >= 0" in err.getvalue()
    assert parser.parse_args(["slow", "--top", "0"]).top == 0  # zero is valid
    with pytest.raises(SystemExit):  # a non-int still errors
        parser.parse_args(["slow", "--top", "abc"])


def test_bug3_logs_grep_is_literal_substring(temp_telemetry: Path) -> None:
    # BUG-3: --grep is a literal, case-insensitive SUBSTRING match, NOT an ILIKE
    # pattern — `_` and `%` must match themselves, not act as wildcards.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "logs.jsonl",
        [
            make_log(base + timedelta(seconds=0), body="user_id matched"),
            make_log(base + timedelta(seconds=1), body="userXid mismatch"),
            make_log(base + timedelta(seconds=2), body="100% done"),
            make_log(base + timedelta(seconds=3), body="100zzdone"),
        ],
    )

    def bodies(*flags: str) -> list[str]:
        out = _strip_header(_run(temp_telemetry, "logs", *flags))
        return [r["body"] for r in _json.loads(out)]

    # '_' is literal: matches "user_id matched" only, not "userXid mismatch"
    assert bodies("--grep", "user_id") == ["user_id matched"]
    # '%' is literal: matches "100% done" only, not "100zzdone"
    assert bodies("--grep", "100%") == ["100% done"]
    # case-insensitive
    assert bodies("--grep", "USER_ID") == ["user_id matched"]


def test_bug4_dir_pointing_at_file_is_friendly(tmp_path: Path) -> None:
    # BUG-4: --dir pointing at a regular file exits non-zero with an "is not a
    # directory" message and NO traceback (not a deep NotADirectoryError).
    f = tmp_path / "notadir"
    f.write_text("hi", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:  # clean SystemExit, not a traceback
        otelq.main(["--dir", str(f), "summary"])
    assert "is not a directory" in str(exc.value)
    assert exc.value.code != 0


def test_bug5_empty_sql_query_is_friendly(synth_conn: duckdb.DuckDBPyConnection) -> None:
    # BUG-5: an empty/whitespace SQL string raises SystemExit "otelq: SQL error"
    # (a friendly message), NOT an AttributeError from result=None.
    for q in ("", "   ", "\n\t"):
        with pytest.raises(SystemExit) as exc:
            otelq.cmd_sql(synth_conn, Namespace(query=q))
        assert "otelq: SQL error" in str(exc.value)
    cols, rows = otelq.cmd_sql(synth_conn, Namespace(query="SELECT 1 AS one"))
    assert cols == ["one"] and rows == [(1,)]  # a valid query still works


def test_bug6_logs_order_deterministic_across_cache_paths(temp_telemetry: Path) -> None:
    # BUG-6: logs sharing an identical timestamp must order deterministically
    # (the trailing tie-breaker), so cached output == --no-cache byte-for-byte.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    same = base + timedelta(seconds=5)
    write_jsonl(
        temp_telemetry / "logs.jsonl",
        [
            make_log(same, service="svc-c", body="ccc"),
            make_log(same, service="svc-a", body="aaa"),
            make_log(same, service="svc-b", body="bbb"),
            make_log(base + timedelta(seconds=1), service="svc-a", body="earlier"),
        ],
    )
    cached = _run(temp_telemetry, "logs")
    nocache = _run(temp_telemetry, "--no-cache", "logs")
    assert cached == nocache  # tie-breaker makes equal-timestamp order deterministic
    assert len(_json.loads(_strip_header(cached))) == 4


# --- AC-1 / FR-1, FR-5: sealing produces per-minute partitions ----------------


def test_ac1_seals_complete_minutes(temp_telemetry: Path) -> None:
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


def test_ac11_cached_equals_no_cache(temp_telemetry: Path) -> None:
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    spans: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []  # all four metric types, interleaved
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
        metrics.append(make_gauge(t, value=float(i)))
        metrics.append(make_sum(t, value=i * 2))
        metrics.append(make_histogram(t, total=float(i * 10)))
        metrics.append(make_exp_histogram(t, total=float(i * 100)))
    write_jsonl(temp_telemetry / "traces.jsonl", spans)
    write_jsonl(temp_telemetry / "logs.jsonl", logs)
    write_jsonl(temp_telemetry / "metrics.jsonl", metrics)
    for argv in (
        ["summary"],
        ["errors"],
        ["slow"],
        ["logs"],
        ["metric", "db.pool"],
        ["metric", "http.server.duration"],  # a histogram metric (value = sum)
        ["trace", trace_hex("t3")],
        ["sql", "SELECT metric_type, count(*) c FROM metrics GROUP BY 1 ORDER BY 1"],
        ["sql", "SELECT count(*) FROM metrics_histogram"],
        ["sql", "SELECT count(*) FROM metrics_exp_histogram"],
    ):
        cached = _run(temp_telemetry, *argv)
        nocache = _run(temp_telemetry, "--no-cache", *argv)
        assert cached == nocache, (
            f"cached != --no-cache for {argv}\n{cached}\n---\n{nocache}"
        )


# --- AC-17 / FR-17: --no-cache writes nothing ---------------------------------


def test_ac17_no_cache_leaves_cache_untouched(temp_telemetry: Path) -> None:
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


def test_ac2_incremental_no_rebytes(temp_telemetry: Path) -> None:
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


def test_ac13_portable_file_ops() -> None:
    src = Path(str(otelq.__file__)).read_text()
    assert "fcntl" not in src
    assert "os.rename(" not in src  # must use os.replace
    assert "os.replace(" in src
    # no text-mode tell() offset tracking; the tail reader is binary
    assert ".tell()" not in src


# --- AC-16 / FR-16: otel-clean removes the cache subtree ----------------------


def test_ac16_otel_clean_recipe_removes_cache() -> None:
    justfile = (Path(__file__).resolve().parents[1] / "justfile").read_text()
    assert "rm -rf .telemetry/.otelq-cache" in justfile


import os as _os  # noqa: E402
import time as _time  # noqa: E402


def _build(
    dirpath: Path,
    route: str = "HOT",
    window_min: int | None = 30,
    use_cache: bool = True,
) -> duckdb.DuckDBPyConnection:
    win = None if window_min is None else timedelta(minutes=window_min)
    return otelq.build_connection(dirpath, otelq.Plan(route, win, use_cache))


def _run_both(dirpath: Path, *argv: str) -> tuple[str, str]:
    import io as _io
    from contextlib import redirect_stderr, redirect_stdout

    out, err = _io.StringIO(), _io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        otelq.main(["--dir", str(dirpath), "--format", "json", *argv])
    return out.getvalue(), err.getvalue()


def _signal_count(dirpath: Path, signal: str, *argv: str) -> int:
    # summary breaks a signal into several rows (trace buckets, log levels), so
    # the signal's total is the sum of its rows' counts.
    return _sum_signal(_run(dirpath, *argv), signal)


def _sum_signal(out: str, signal: str = "traces") -> int:
    """Total count for a signal across summary's per-row breakdown."""
    rows = _json.loads(_summary_first_block(out))
    return sum(r["count"] for r in rows if r["signal"] == signal)


def _minutes_per_minute(base: datetime, n: int) -> list[dict[str, Any]]:
    return [
        make_span(base + timedelta(minutes=i), trace_id=f"t{i}", span_id=f"s{i}")
        for i in range(n)
    ]


# --- AC-3 / FR-3 / EC-1: rotation mid-stream loses/duplicates nothing ---------


def test_ac3_rotation_no_gap_no_dup(temp_telemetry: Path) -> None:
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
    assert _sum_signal(cached) == 9  # all 9 spans, none lost/duplicated


# --- AC-5 / FR-5 / EC-2: cold start seals only the hot window ------------------


def test_ac5_cold_start_seals_only_hot_window(temp_telemetry: Path) -> None:
    base = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 90))
    _build(temp_telemetry)
    sealed = sorted((temp_telemetry / CACHE / "traces").glob("*.parquet"))
    assert 20 < len(sealed) < 40, f"expected ~30 sealed minutes, got {len(sealed)}"
    keys = [otelq.parse_minute_key(p.stem) for p in sealed]
    assert all(k is not None for k in keys)
    oldest = min(k for k in keys if k is not None)
    floor = (base + timedelta(minutes=55)).replace(tzinfo=None)
    assert oldest >= floor  # nothing older than the window


# --- AC-6 / FR-6: a later run evicts partitions that fell out of the window ----


def test_ac6_eviction_drops_stale_partitions(temp_telemetry: Path) -> None:
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
    assert all(m is not None and m >= floor for m in sealed)


# --- AC-9 / FR-9: recent-by-default, --all widens -----------------------------


def test_ac9_recent_default_vs_all(temp_telemetry: Path) -> None:
    base = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 90))
    default = _signal_count(temp_telemetry, "traces", "summary")
    widened = _signal_count(temp_telemetry, "traces", "--all", "summary")
    assert widened == 90
    assert default < widened  # default reports only the hot window
    assert 25 <= default <= 35


# --- AC-8 / FR-8: --since beyond the window reaches old data (cold path) -------


def test_ac8_since_beyond_window_is_cold(temp_telemetry: Path) -> None:
    base = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 90))
    far = _signal_count(temp_telemetry, "traces", "--since", "120m", "summary")
    assert far == 90


# --- AC-10 / FR-10: trace lookup falls back to cold for an old id -------------


def test_ac10_trace_cold_fallback(temp_telemetry: Path) -> None:
    base = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", _minutes_per_minute(base, 90))
    # t5 is ~85 minutes old: absent from the hot cache, found via cold fallback
    out = _run(temp_telemetry, "trace", trace_hex("t5"))
    assert len(_json.loads(_strip_header(out))) == 1
    assert out == _run(temp_telemetry, "--no-cache", "trace", trace_hex("t5"))


# --- AC-14 / FR-14 / EC-7, EC-8: version mismatch self-wipes and rebuilds ------


def test_ac14_version_mismatch_self_heals(temp_telemetry: Path) -> None:
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
    assert _signal_count(temp_telemetry, "traces", "summary") == 6


# --- AC-15 / FR-15 / EC-4, EC-5: partial line + oversized batch are skipped ----


def test_ac15_robust_tail_parsing(temp_telemetry: Path) -> None:
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
    assert _sum_signal(out) == 5  # only the valid spans
    assert "exceeds the 2048-row" in err  # oversized batch warned + skipped


# --- AC-18 / INV-6: raw files are never modified ------------------------------


def test_ac18_raw_files_unmodified(temp_telemetry: Path) -> None:
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    traces = temp_telemetry / "traces.jsonl"
    write_jsonl(traces, _minutes_per_minute(base, 30))
    before = traces.read_bytes()
    _build(temp_telemetry)
    _run(temp_telemetry, "summary")
    assert traces.read_bytes() == before


# --- AC-20 / EC-6: zeroed st_ino still disambiguates two files -----------------


def test_ac20_zeroed_st_ino_disambiguates(
    temp_telemetry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    def zeroed(path: Any, *a: Any, **k: Any) -> _os.stat_result:
        s = real_stat(path, *a, **k)
        vals = list(s)
        vals[1] = 0  # st_ino -> 0; fingerprint+size must disambiguate
        return _os.stat_result(vals)

    monkeypatch.setattr(otelq.os, "stat", zeroed)
    cached = _run(temp_telemetry, "summary")  # HOT route — uses the inode/fp cursor
    monkeypatch.undo()
    nocache = _run(temp_telemetry, "--no-cache", "summary")
    assert cached == nocache
    assert _sum_signal(cached) == 10  # both files read, none collided


# --- AC-12 / FR-12 / INV-5: lock contention still answers, skips sealing -------


def test_ac12_lock_contention_reads_without_sealing(temp_telemetry: Path) -> None:
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


def test_late_arrival_to_sealed_minute_stays_queryable(temp_telemetry: Path) -> None:
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
    assert _sum_signal(cached) == 7


def test_clock_skew_outlier_does_not_drop_records(temp_telemetry: Path) -> None:
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
    assert _sum_signal(cached) == 4


# --- AC-19 / INV-2 / EC-11: crash between seal and cursor advance --------------


def test_ac19_crash_immutability_and_no_loss(temp_telemetry: Path) -> None:
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
    cached_n = _signal_count(temp_telemetry, "traces", "summary")
    cold_n = _signal_count(temp_telemetry, "traces", "--no-cache", "summary")
    assert (
        cached_n >= cold_n
    )  # no data LOSS (a full rollback may transiently over-count)


# --- FR-12/INV-5 regression: live writer's lock is never reaped ----------------


def test_live_lock_reaped_only_past_hard_ceiling(temp_telemetry: Path) -> None:
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


def test_tail_does_not_double_count_sealed(temp_telemetry: Path) -> None:
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
    assert _sum_signal(cached) == 8  # not 16 — tail bounded past sealed


# --- FR-11 regression: sub-minute window boundary (found by the live check) ----


def test_subminute_window_boundary_equivalence(temp_telemetry: Path) -> None:
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


def test_identical_late_records_kept(temp_telemetry: Path) -> None:
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
    assert _sum_signal(cached, "logs") == 10  # 8 originals + 2 identical late records


# --- error-message clarity: name the missing signal, don't cry "collector down" -
# The generic "no telemetry captured — is the collector running?" misleads when
# only ONE signal is absent: it sent a whole debugging session chasing the
# collector when traces were flowing fine and only logs.jsonl was missing.


def test_require_names_missing_signal_when_others_present() -> None:
    conn = duckdb.connect(":memory:")
    # traces has DATA (a row), no logs at all. "Present" = has rows, not mere
    # table existence (every relation can now be seeded empty), so insert a row.
    conn.execute("CREATE TABLE traces(timestamp TIMESTAMP)")
    conn.execute("INSERT INTO traces VALUES (TIMESTAMP '2026-05-22 10:00:00')")
    with pytest.raises(otelq.NoTelemetryError) as exc:
        otelq.cmd_logs(conn, Namespace(service=None, level=None, grep=None))
    msg = str(exc.value)
    assert msg != otelq._NO_TELEMETRY_MSG  # not the misleading generic text
    assert "logs" in msg  # names the missing signal
    assert "traces" in msg  # names what IS present


def test_require_keeps_generic_message_when_nothing_present() -> None:
    conn = duckdb.connect(":memory:")  # no relations at all -> collector likely down
    with pytest.raises(otelq.NoTelemetryError) as exc:
        otelq.cmd_logs(conn, Namespace(service=None, level=None, grep=None))
    assert str(exc.value) == otelq._NO_TELEMETRY_MSG


def test_errors_names_gap_when_only_metrics_present() -> None:
    conn = duckdb.connect(":memory:")
    # metrics has DATA (a row), no traces/logs. "Present" = has rows (FR-19), so
    # insert a row rather than relying on an empty table as a presence proxy.
    conn.execute("CREATE TABLE metrics(timestamp TIMESTAMP)")
    conn.execute("INSERT INTO metrics VALUES (TIMESTAMP '2026-05-22 10:00:00')")
    with pytest.raises(otelq.NoTelemetryError) as exc:
        otelq.cmd_errors(conn, Namespace())
    msg = str(exc.value)
    assert msg != otelq._NO_TELEMETRY_MSG
    assert "metrics" in msg  # names the present signal so the gap is obvious


# --- collector-config: reference producer fragment ---------------------------
# `otelq collector-config` emits the file-export settings to merge into an
# existing Collector (the "integrated" setup). It is GENERATED from this
# module's pinned constants so it can never drift from the .telemetry/ contract;
# the drift guard below ties those constants back to the shipped dev config.


def test_collector_config_emits_pinned_values() -> None:
    out = otelq.render_collector_config()
    assert f"max_megabytes: {otelq.ROTATION_MAX_MEGABYTES}" in out
    assert f"max_backups: {otelq.ROTATION_MAX_BACKUPS}" in out
    for sig in ("traces", "logs", "metrics"):
        assert f"path: {otelq.COLLECTOR_MOUNT_PATH}/{sig}.jsonl" in out
        assert f"file/{sig}:" in out
    assert "-contrib" in out  # the file exporter is contrib-only — must be flagged


def test_collector_config_matches_dev_yaml() -> None:
    """Drift guard: the generated fragment's contract values must match the
    shipped reference Collector config."""
    dev_yaml = (Path(__file__).resolve().parents[1] / "otel-collector-dev.yaml").read_text()
    assert f"max_megabytes: {otelq.ROTATION_MAX_MEGABYTES}" in dev_yaml
    assert f"max_backups: {otelq.ROTATION_MAX_BACKUPS}" in dev_yaml
    for sig in ("traces", "logs", "metrics"):
        assert f"path: {otelq.COLLECTOR_MOUNT_PATH}/{sig}.jsonl" in dev_yaml


# --- doctor: validate a telemetry dir against the contract -------------------


def test_doctor_ok_on_valid_dir(temp_telemetry: Path) -> None:
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", [make_span(base)])
    write_jsonl(temp_telemetry / "logs.jsonl", [make_log(base)])
    write_jsonl(temp_telemetry / "metrics.jsonl", [make_gauge(base)])
    rows, ok = otelq.doctor_report(temp_telemetry)
    assert ok
    status = {r[0]: r[1] for r in rows}
    assert status["traces"] == "OK"
    assert status["logs"] == "OK"
    assert status["metrics"] == "OK"


def test_doctor_fails_on_missing_dir(tmp_path: Path) -> None:
    rows, ok = otelq.doctor_report(tmp_path / "does-not-exist")
    assert not ok
    assert rows[0][1] == "FAIL"


def test_doctor_fails_on_no_telemetry(temp_telemetry: Path) -> None:
    rows, ok = otelq.doctor_report(temp_telemetry)  # empty dir, no jsonl
    assert not ok
    assert any(r[0] == "telemetry" and r[1] == "FAIL" for r in rows)


def test_doctor_fails_on_malformed_jsonl(temp_telemetry: Path) -> None:
    (temp_telemetry / "traces.jsonl").write_text("not json at all\n", encoding="utf-8")
    rows, ok = otelq.doctor_report(temp_telemetry)
    assert not ok
    assert any(r[0] == "traces" and r[1] == "FAIL" for r in rows)


def test_doctor_fails_on_wrong_signal_key(temp_telemetry: Path) -> None:
    # a logs payload written into traces.jsonl: valid JSON, wrong top-level key
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", [make_log(base)])
    rows, ok = otelq.doctor_report(temp_telemetry)
    assert not ok
    detail = {r[0]: r[2] for r in rows}
    assert "resourceSpans" in detail["traces"]


def test_doctor_partial_capture_is_ok(temp_telemetry: Path) -> None:
    # only traces flowing: logs/metrics WARN but the dir is still usable.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", [make_span(base)])
    rows, ok = otelq.doctor_report(temp_telemetry)
    assert ok
    status = {r[0]: r[1] for r in rows}
    assert status["traces"] == "OK"
    assert status["logs"] == "WARN"
    assert status["metrics"] == "WARN"


def test_doctor_exit_codes_via_main(temp_telemetry: Path, tmp_path: Path) -> None:
    import io as _io
    from contextlib import redirect_stdout

    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", [make_span(base)])
    with redirect_stdout(_io.StringIO()):
        assert otelq.main(["--dir", str(temp_telemetry), "doctor"]) == 0
        assert otelq.main(["--dir", str(tmp_path / "nope"), "doctor"]) == 1


def test_collector_config_via_main_prints_fragment() -> None:
    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    with redirect_stdout(buf):
        assert otelq.main(["collector-config"]) == 0
    assert "exporters:" in buf.getvalue()


# --- troubleshoot: the capture loop + fixes, moved out of the query skill ----
# `otelq troubleshoot` is the single home for the loop/fixes guidance so the
# query skill can stay a thin pointer instead of restating it.


def test_troubleshoot_emits_loop_and_fixes() -> None:
    out = otelq.render_troubleshooting()
    assert "capture" in out and "loop" in out  # the loop
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in out  # export step
    assert "Stale data" in out  # a fix
    assert "doctor" in out  # points at the real verification command


def test_troubleshoot_via_main_prints() -> None:
    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    with redirect_stdout(buf):
        assert otelq.main(["troubleshoot"]) == 0
    assert "capture" in buf.getvalue()


def test_help_epilog_documents_argument_order_and_sql_schema() -> None:
    # The query skill points agents at `otelq --help`; the reference it needs
    # (global-flags-first rule + sql view schema) must actually be there.
    help_text = otelq.build_parser().format_help()
    assert "GLOBAL flags" in help_text
    assert "BEFORE the subcommand" in help_text
    # FR-30: the UTC timestamp convention must be stated early/prominently, not
    # buried only in the sql cheat-sheet — it precedes "argument order:".
    assert help_text.index("timestamps:") < help_text.index("argument order:")
    assert "UTC" in help_text and "non-Z offset" in help_text
    # The help must steer agents to the token-efficient machine format (AC-37).
    assert "compact" in help_text
    for view in (
        "traces",
        "logs",
        "metrics",
        "metrics_gauge",
        "metrics_sum",
        "metrics_histogram",
        "metrics_exp_histogram",
    ):
        assert view in help_text


def test_readme_help_dump_matches_live_help() -> None:
    # Drift guard: the README's `otelq --help` dump (## Commands) must match
    # the real output, so a future flag/epilog change is caught here instead
    # of the README silently rotting — as it had, before this test existed.
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    marker = "```text\n"
    start = readme.index(marker, readme.index("## Commands")) + len(marker)
    end = readme.index("\n```", start)
    dumped = readme[start:end]

    # The README shows a generic <cwd>/.telemetry placeholder for --dir's
    # default; the real absolute path is a different length and wraps the
    # help text differently, so patch it before rendering rather than after.
    # argparse also wraps "usage:"/options text to the terminal width (via
    # shutil.get_terminal_size(), which honors COLUMNS) — pin it to the width
    # the README was dumped at (80) so this test is deterministic regardless
    # of the width of whatever terminal/CI runner actually executes it.
    original_default = otelq.DEFAULT_DIR
    original_columns = _os.environ.get("COLUMNS")
    otelq.DEFAULT_DIR = Path("<cwd>/.telemetry")
    _os.environ["COLUMNS"] = "80"
    try:
        live = otelq.build_parser().format_help()
    finally:
        otelq.DEFAULT_DIR = original_default
        if original_columns is None:
            _os.environ.pop("COLUMNS", None)
        else:
            _os.environ["COLUMNS"] = original_columns
    # Compare word sequences, not exact line breaks: argparse's usage-line
    # wrapping of the subparsers "..." token has changed across Python
    # versions (observed: ubuntu-latest and macos-latest CI runners resolve
    # different Python patch versions via uv and wrap it differently even at
    # the same pinned COLUMNS), which is incidental formatting, not content
    # drift. Word-order still catches a real flag/help-text change.
    assert dumped.split() == live.split()


def test_bare_otelq_prints_full_help() -> None:
    # FR-22: a bare `otelq` (no command) prints the full help and exits 0,
    # rather than the terse argparse "required: command" error.
    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    with redirect_stdout(buf):
        assert otelq.main([]) == 0
    out = buf.getvalue()
    assert "usage: otelq" in out and "summary" in out and "GLOBAL flags" in out


def test_help_command_prints_general_help() -> None:
    # FR-22: `otelq help` == the global help.
    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    with redirect_stdout(buf):
        assert otelq.main(["help"]) == 0
    assert "usage: otelq" in buf.getvalue() and "GLOBAL flags" in buf.getvalue()


def test_help_command_topic_prints_subcommand_help() -> None:
    # FR-22: `otelq help <command>` prints that command's own help.
    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    with redirect_stdout(buf):
        assert otelq.main(["help", "slow"]) == 0
    out = buf.getvalue()
    assert "otelq slow" in out and "--top" in out


def test_help_command_unknown_topic_errors() -> None:
    # FR-22 / EC-13: `otelq help <unknown>` reports argparse's invalid-choice
    # error (exit 2), not a silent success.
    import io as _io
    from contextlib import redirect_stderr

    buf = _io.StringIO()
    with redirect_stderr(buf):
        assert otelq.main(["help", "not-a-command"]) == 2
    assert "invalid choice" in buf.getvalue()


def test_default_dir_is_cwd_relative() -> None:
    # FR-12 / ADR-001: the default telemetry dir is <cwd>/.telemetry, resolved from
    # the current working directory — NOT the script's install location. A
    # script-relative default put it under site-packages for `uvx ... otelq`.
    assert otelq.DEFAULT_DIR == Path.cwd() / ".telemetry"
    parsed = otelq.build_parser().parse_args(["summary"])
    assert parsed.dir == Path.cwd() / ".telemetry"


# --- broken pipe: `otelq ... | head` must exit cleanly, not dump a traceback ---


def test_main_handles_broken_pipe() -> None:
    import io as _io
    from contextlib import redirect_stdout

    class _BrokenStream(_io.TextIOBase):
        def write(self, s: str) -> int:
            raise BrokenPipeError()

    with redirect_stdout(_BrokenStream()):
        rc = otelq.main(["collector-config"])  # prints, so it hits the broken write
    assert rc == 0  # swallowed, no traceback escaped


# --- review findings: F-1..F-5, P-5, D-2, D-4, B-1 --------------------------


def _run_err(dirpath: Path, *argv: str) -> tuple[str, str]:
    """Run the CLI in-process; return (stdout, stderr)."""
    import io as _io
    from contextlib import redirect_stderr, redirect_stdout

    out, err = _io.StringIO(), _io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        otelq.main(["--dir", str(dirpath), "--format", "json", *argv])
    return out.getvalue(), err.getvalue()


def test_f3_version_matches_pyproject() -> None:
    # F-3: `otelq --version` reports __version__, kept in lockstep with the
    # packaged version in pyproject.toml so an agent can name the exact build.
    import tomllib

    pyproject = Path(otelq.__file__).resolve().parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["version"] == otelq.__version__


def test_f3_version_flag_prints_and_exits_zero() -> None:
    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        otelq.main(["--version"])
    assert exc.value.code == 0
    assert f"otelq {otelq.__version__}" in buf.getvalue()


def test_f1_top_caps_rows_and_warns_on_stderr(temp_telemetry: Path) -> None:
    # F-1: --top caps a command's rows and, only when it actually truncated,
    # prints a one-line notice to STDERR (never stdout, so json stays parseable).
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "logs.jsonl",
        [make_log(base + timedelta(seconds=i), body=f"m{i}") for i in range(6)],
    )
    out, err = _run_err(temp_telemetry, "logs", "--top", "2")
    assert len(_json.loads(_strip_header(out))) == 2
    assert "truncated to 2 rows" in err
    # Under the cap: full result, no notice on stderr.
    out2, err2 = _run_err(temp_telemetry, "logs", "--top", "50")
    assert len(_json.loads(_strip_header(out2))) == 6
    assert "truncated" not in err2


def test_f2_since_accepts_seconds_unit() -> None:
    # F-2: --since gains a seconds unit (Ns) alongside m/h/d.
    assert otelq._parse_since("45s") == timedelta(seconds=45)
    assert otelq._parse_since("2m") == timedelta(minutes=2)
    with pytest.raises(SystemExit):
        otelq._parse_since("10x")


def test_f2_since_seconds_windows_end_to_end(temp_telemetry: Path) -> None:
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "logs.jsonl",
        [
            make_log(base, body="oldest"),
            make_log(base + timedelta(seconds=10), body="mid"),
            make_log(base + timedelta(seconds=40), body="newest"),
        ],
    )
    # Anchor is the max event-time (base+40s); a 30s window keeps mid+newest.
    out = _strip_header(_run(temp_telemetry, "--since", "30s", "logs"))
    bodies = [r["body"] for r in _json.loads(out)]
    assert "oldest" not in bodies
    assert set(bodies) == {"mid", "newest"}
    assert _run(temp_telemetry, "--since", "30s", "logs") == _run(
        temp_telemetry, "--no-cache", "--since", "30s", "logs"
    )


def _raw_span(ts: datetime, tid: str, sid: str, name: str = "GET /x") -> dict[str, Any]:
    """A span with an explicit (already-hex) trace id, for prefix-match tests."""
    end = ts + timedelta(milliseconds=5)
    span: dict[str, Any] = {
        "traceId": tid,
        "spanId": sid,
        "parentSpanId": "",
        "name": name,
        "kind": 2,
        "startTimeUnixNano": _ns(ts),
        "endTimeUnixNano": _ns(end),
        "attributes": [],
        "flags": 0,
        "status": {},
    }
    return {
        "resourceSpans": [
            {
                "resource": _resource("app-test"),
                "scopeSpans": [{"scope": {"name": "test"}, "spans": [span]}],
            }
        ]
    }


def test_f4_trace_prefix_resolves_and_flags_ambiguity(temp_telemetry: Path) -> None:
    # F-4: `trace` accepts a unique id prefix; two matches raise a friendly error.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    tid_a1 = "aaaa" + "0" * 27 + "1"  # shares the "aaaa..." prefix with tid_a2
    tid_a2 = "aaaa" + "0" * 27 + "2"
    tid_b = "bbbb" + "0" * 28
    write_jsonl(
        temp_telemetry / "traces.jsonl",
        [
            _raw_span(base, tid_a1, "1" + "0" * 15, name="span-a1"),
            _raw_span(base + timedelta(seconds=1), tid_a2, "2" + "0" * 15, name="span-a2"),
            _raw_span(base + timedelta(seconds=2), tid_b, "3" + "0" * 15, name="span-b"),
        ],
    )
    # A unique prefix resolves to the one trace (its single span).
    rows = _json.loads(_strip_header(_run(temp_telemetry, "--all", "trace", "bbbb")))
    assert [r["span_name"].strip() for r in rows] == ["span-b"]
    # An exact id still works.
    exact = _json.loads(_strip_header(_run(temp_telemetry, "--all", "trace", tid_a1)))
    assert [r["span_name"].strip() for r in exact] == ["span-a1"]
    # An ambiguous prefix is a friendly SystemExit, not a silent pick.
    with pytest.raises(SystemExit) as exc:
        otelq.main(["--dir", str(temp_telemetry), "--all", "trace", "aaaa"])
    assert "ambiguous" in str(exc.value)


def test_p5_format_json_compact_and_jsonl() -> None:
    # P-5: json is compact (no spaces) and a jsonl format streams one object/line.
    cols = ["a", "b"]
    rows = [(1, 2), (3, 4)]
    assert otelq.format_output(cols, rows, "json") == '[{"a":1,"b":2},{"a":3,"b":4}]'
    assert otelq.format_output(cols, rows, "jsonl") == '{"a":1,"b":2}\n{"a":3,"b":4}'


def test_p5_format_jsonl_via_cli(temp_telemetry: Path) -> None:
    import io as _io
    from contextlib import redirect_stdout

    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "logs.jsonl", [make_log(base), make_log(base + timedelta(seconds=1))])
    buf = _io.StringIO()
    with redirect_stdout(buf):
        otelq.main(["--dir", str(temp_telemetry), "--format", "jsonl", "logs"])
    lines = [ln for ln in _strip_header(buf.getvalue()).splitlines() if ln.strip()]
    assert len(lines) == 2
    for ln in lines:
        assert isinstance(_json.loads(ln), dict)  # each line is a standalone object


def test_p5_format_compact_columns_rows() -> None:
    # P-5 (AC-37): compact declares columns once, rows as positional arrays;
    # losslessly reconstructs to the same objects `json` emits.
    cols = ["a", "b"]
    rows = [(1, 2), (3, 4)]
    out = otelq.format_output(cols, rows, "compact")
    assert out == '{"columns":["a","b"],"rows":[[1,2],[3,4]]}'
    parsed = _json.loads(out)
    reconstructed = [dict(zip(parsed["columns"], r)) for r in parsed["rows"]]
    assert reconstructed == _json.loads(otelq.format_output(cols, rows, "json"))


def test_p5_format_compact_via_cli(temp_telemetry: Path) -> None:
    import io as _io
    from contextlib import redirect_stdout

    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "logs.jsonl", [make_log(base), make_log(base + timedelta(seconds=1))])
    buf = _io.StringIO()
    with redirect_stdout(buf):
        otelq.main(["--dir", str(temp_telemetry), "--format", "compact", "logs"])
    obj: dict[str, Any] = _json.loads(_strip_header(buf.getvalue()))
    assert set(obj) == {"columns", "rows"}
    columns: list[str] = obj["columns"]
    rows: list[list[Any]] = obj["rows"]
    assert len(rows) == 2
    for row in rows:
        assert len(row) == len(columns)  # positional arrays align to columns


def test_d2_sql_boundary_locks_builtins_not_sql(tmp_path: Path) -> None:
    # D-2: built-in commands run with filesystem access revoked; `sql` keeps it as
    # a documented escape hatch.
    csv_file = tmp_path / "data.csv"
    csv_file.write_text("n\n1\n2\n", encoding="utf-8")
    read = f"SELECT count(*) AS c FROM read_csv('{csv_file}')"

    locked = duckdb.connect(":memory:")
    otelq._seal_external_access(locked, "summary")
    with pytest.raises(duckdb.Error):
        locked.execute(read)

    allowed = duckdb.connect(":memory:")
    otelq._seal_external_access(allowed, "sql")
    row = allowed.execute(read).fetchone()
    assert row is not None and row[0] == 2


def test_d4_doctor_reports_cache_health(temp_telemetry: Path) -> None:
    # D-4: doctor surfaces cache writability without ever failing a valid dir.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", [make_span(base)])
    rows, ok = otelq.doctor_report(temp_telemetry)
    assert ok
    assert any(r[0] == "cache writable" and r[1] == "OK" for r in rows)


def test_d4_and_b1_doctor_flags_clock_skew(temp_telemetry: Path) -> None:
    # D-4 + B-1: a far-future watermark in the cursor is flagged (WARN), never a
    # FAIL — queries still answer via the clamped window.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", [make_span(base)])
    cache = temp_telemetry / CACHE
    cache.mkdir()
    far = otelq._fmt_ts(otelq._future_ceiling() + timedelta(days=5))
    payload: dict[str, Any] = {
        "version": otelq.CURSOR_SCHEMA_VERSION,
        "streams": {
            s: {"files": {}, "max_event_ts_seen": far} for s in otelq.CURSOR_STREAMS
        },
    }
    (cache / otelq.CURSOR_FILENAME).write_text(_json.dumps(payload), encoding="utf-8")
    rows, ok = otelq.doctor_report(temp_telemetry)
    assert ok  # skew is a WARN, not a FAIL
    assert any(r[0] == "clock skew" and r[1] == "WARN" for r in rows)


def test_b1_window_anchor_clamped_to_ceiling(temp_telemetry: Path) -> None:
    # B-1: a single far-future record must not push the query window past all real
    # data. The window's upper anchor is clamped to wall-clock + tolerance, so a
    # record near the ceiling stays visible while the poison record is excluded;
    # --all (windowless) still sees both. Identical hot vs cold (FR-11).
    # aware UTC so make_log's naive-vs-aware timestamp conversion matches the
    # naive-UTC ceiling the clamp computes at query time.
    ceiling = datetime.now(timezone.utc) + otelq.MAX_FUTURE_SKEW
    in_window = ceiling - timedelta(minutes=5)  # within the default hot window
    poison = ceiling + timedelta(days=10)  # implausible outlier
    write_jsonl(
        temp_telemetry / "logs.jsonl",
        [make_log(in_window, body="real"), make_log(poison, body="poison")],
    )

    def count(*argv: str) -> int:
        return _json.loads(_run(temp_telemetry, *argv, "sql", "SELECT count(*) AS n FROM logs"))[0]["n"]

    assert count() == 1  # default window: poison clamped out, real record kept
    assert count("--all") == 2  # windowless: both present
    windowed = _run(temp_telemetry, "sql", "SELECT count(*) AS n FROM logs")
    assert windowed == _run(temp_telemetry, "--no-cache", "sql", "SELECT count(*) AS n FROM logs")


def test_b5_single_quote_in_dir_path_is_sql_safe(tmp_path: Path) -> None:
    # B-5 / SPEC-cli FR-28, EC-21, AC-36: a --dir whose path contains a single
    # quote must not break the SQL that splices raw-file paths. The query must
    # still return rows and stay identical cached vs --no-cache (FR-11).
    d = tmp_path / "o'brien" / ".telemetry"
    d.mkdir(parents=True)
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(d / "logs.jsonl", [make_log(base, body="hi"), make_log(base, body="yo")])
    cached = _run(d, "--all", "sql", "SELECT count(*) AS n FROM logs")
    nocache = _run(d, "--all", "--no-cache", "sql", "SELECT count(*) AS n FROM logs")
    assert _json.loads(cached) == [{"n": 2}]  # no SQL breakage on the quoted path
    assert cached == nocache


# =============================================================================
# FR-29 / AC-38..AC-42: the response header
# =============================================================================


def _run_fmt(dirpath: Path, fmt: str, *argv: str) -> str:
    """Run the CLI in-process with an explicit --format; return stdout."""
    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    with redirect_stdout(buf):
        otelq.main(["--dir", str(dirpath), "--format", fmt, *argv])
    return buf.getvalue()


def test_ac38_response_header_shape_and_placement(temp_telemetry: Path) -> None:
    # AC-38: summary/errors/slow/trace/logs/metric print a fixed header before
    # the FR-10 rendering, for every --format; the payload after it is
    # unchanged from the header-less rendering of the same rows.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "logs.jsonl", [make_log(base, body="hi")])
    conn = otelq.connect(temp_telemetry)
    columns, rows = otelq.cmd_logs(conn, Namespace(service=None, level=None, grep=None))
    for fmt in ("table", "json", "jsonl", "csv", "compact"):
        out = _run_fmt(temp_telemetry, fmt, "logs")
        lines = out.splitlines()
        assert lines[0] == "=" * 10
        assert lines[1].startswith(f"otelq logs response, format {fmt}")
        assert lines[2] == "OpenTelemetry signal: logs"
        assert lines[3].startswith("Time range: ") and " - " in lines[3]
        assert lines[4] == "IMPORTANT: all timestamps are UTC"
        assert lines[5] == "-" * 10
        assert _strip_header(out) == otelq.format_output(columns, rows, fmt) + "\n"


def test_ac39_no_header_on_non_governed_commands(temp_telemetry: Path) -> None:
    # AC-39: sql (no fixed signal) and the non-query commands print no header.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "logs.jsonl", [make_log(base)])
    sql_out = _run(temp_telemetry, "sql", "SELECT count(*) AS n FROM logs")
    assert not sql_out.startswith("==========")

    import io as _io
    from contextlib import redirect_stdout

    for argv in (
        ["--dir", str(temp_telemetry), "doctor"],
        ["collector-config"],
        ["troubleshoot"],
    ):
        buf = _io.StringIO()
        with redirect_stdout(buf):
            otelq.main(argv)
        assert not buf.getvalue().startswith("==========")


def test_ac40_zero_row_time_range_is_na(temp_telemetry: Path) -> None:
    # AC-40: a zero-row result (an unknown metric name) still prints the
    # header, with Time range: n/a - n/a rather than failing or omitting it.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "metrics.jsonl", [make_gauge(base, name="db.pool")])
    out = _run(temp_telemetry, "metric", "does.not.exist")
    lines = out.splitlines()
    assert lines[2] == "OpenTelemetry signal: metrics"
    assert lines[3] == "Time range: n/a - n/a"
    assert _json.loads(_strip_header(out)) == []


def test_ac41_summary_and_errors_signal_lists_present_signals(
    temp_telemetry: Path,
) -> None:
    # AC-41: summary/errors rows can span more than one signal; the header
    # lists whichever of traces/logs/metrics are actually present, comma-joined
    # in that fixed order.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", [make_span(base, status_code=2)])
    write_jsonl(
        temp_telemetry / "logs.jsonl", [make_log(base, severity="ERROR", sevnum=17)]
    )
    summary_out = _run(temp_telemetry, "summary")
    assert summary_out.splitlines()[2] == "OpenTelemetry signal: traces, logs"
    errors_out = _run(temp_telemetry, "errors")
    assert errors_out.splitlines()[2] == "OpenTelemetry signal: traces, logs"


def test_ac41_errors_signal_is_traces_only_when_no_error_logs(
    temp_telemetry: Path,
) -> None:
    # AC-41 (single-signal case): a traces-only-error corpus (no error/fatal
    # logs) makes errors's header signal field read "traces" alone.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", [make_span(base, status_code=2)])
    write_jsonl(
        temp_telemetry / "logs.jsonl", [make_log(base, severity="INFO", sevnum=9)]
    )
    out = _run(temp_telemetry, "errors")
    assert out.splitlines()[2] == "OpenTelemetry signal: traces"


def test_ac42_errors_zero_rows_signal_is_na(temp_telemetry: Path) -> None:
    # AC-42: errors's signal is derived from the returned rows (unlike the
    # fixed single-signal commands), so a zero-row result reads n/a for the
    # signal too, not just the time range.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", [make_span(base, status_code=0)])
    write_jsonl(
        temp_telemetry / "logs.jsonl", [make_log(base, severity="INFO", sevnum=9)]
    )
    out = _run(temp_telemetry, "errors")
    lines = out.splitlines()
    assert lines[2] == "OpenTelemetry signal: n/a"
    assert lines[3] == "Time range: n/a - n/a"
    assert _json.loads(_strip_header(out)) == []


# =============================================================================
# FR-30 / AC-43, AC-44: sql timestamp-literal UTC convention
# =============================================================================


def test_ac43_sql_timestamp_literal_utc_convention(temp_telemetry: Path) -> None:
    # FR-30/EC-24: timestamp columns are naive UTC. A bare or Z-suffixed
    # literal must match a known-UTC record; a literal carrying an explicit
    # non-Z offset for the SAME instant must NOT match, because DuckDB
    # silently discards the offset instead of converting it — pinning the
    # documented footgun against otelq's own relations (not an isolated
    # DuckDB sanity check), so a future DuckDB upgrade that changes this
    # parsing behavior is caught rather than silently masked.
    base = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)  # == 12:00 at +02:00
    write_jsonl(temp_telemetry / "logs.jsonl", [make_log(base, body="hi")])

    def count(literal: str) -> int:
        out = _run(
            temp_telemetry,
            "sql",
            f"SELECT count(*) AS n FROM logs WHERE timestamp = '{literal}'",
        )
        return _json.loads(out)[0]["n"]

    assert count("2026-07-01 10:00:00") == 1  # bare UTC literal matches
    assert count("2026-07-01T10:00:00Z") == 1  # Z-suffixed literal matches
    # Same instant, correctly converted, written with an explicit +02:00
    # offset: DuckDB discards the offset instead of applying it, so this must
    # NOT match — the silently-wrong result the convention warns against.
    assert count("2026-07-01T12:00:00+02:00") == 0


_UTC_TS_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_ac45_timestamps_render_explicit_utc(temp_telemetry: Path) -> None:
    # FR-16/EC-25: every rendered timestamp — in every format, plus summary's
    # earliest/latest and the FR-29 header's Time range — is an explicit-UTC
    # ISO-8601 string (a trailing Z), never a naive str(datetime) that looks
    # identical for any timezone.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "logs.jsonl", [make_log(base, body="hi")])

    for fmt in ("table", "json", "jsonl", "csv", "compact"):
        out = _run_fmt(temp_telemetry, fmt, "logs")
        lines = out.splitlines()
        from_str, to_str = lines[3].removeprefix("Time range: ").split(" - ")
        assert _UTC_TS_RE.match(from_str) and _UTC_TS_RE.match(to_str)
        payload = _strip_header(out)
        if fmt == "json":
            ts = _json.loads(payload)[0]["timestamp"]
        elif fmt == "jsonl":
            ts = _json.loads(payload.splitlines()[0])["timestamp"]
        elif fmt == "compact":
            obj = _json.loads(payload)
            ts = obj["rows"][0][obj["columns"].index("timestamp")]
        elif fmt == "csv":
            ts = payload.splitlines()[1].split(",")[0]
        else:  # table
            ts = payload.splitlines()[2].split()[0]
        assert _UTC_TS_RE.match(ts), f"{fmt}: {ts!r} is not explicit-UTC ISO-8601"

    # summary's earliest/latest columns too
    write_jsonl(temp_telemetry / "traces.jsonl", [make_span(base, status_code=0)])
    rows = _json.loads(_summary_first_block(_run(temp_telemetry, "summary")))
    for r in rows:
        if r["count"] > 0:
            assert _UTC_TS_RE.match(r["earliest"])
            assert _UTC_TS_RE.match(r["latest"])


def test_ac46_compact_is_the_default_format(temp_telemetry: Path) -> None:
    # FR-10/EC-26: omitting --format defaults to compact (the fewest-token
    # format) — otelq's primary consumer is an AI agent; --format table
    # remains available as the explicit human-reading opt-in.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "logs.jsonl", [make_log(base, body="hi")])

    import io as _io
    from contextlib import redirect_stdout

    buf = _io.StringIO()
    with redirect_stdout(buf):
        otelq.main(["--dir", str(temp_telemetry), "logs"])  # no --format
    default_out = buf.getvalue()

    assert default_out == _run_fmt(temp_telemetry, "compact", "logs")
    payload = _strip_header(default_out)
    obj = _json.loads(payload)
    assert set(obj) == {"columns", "rows"}  # the compact shape, not a table

    table_out = _run_fmt(temp_telemetry, "table", "logs")
    assert table_out != default_out  # table remains an explicit opt-in


def test_ac47_compact_header_names_its_shape(temp_telemetry: Path) -> None:
    # FR-29/EC-27: json/jsonl/csv/table are self-describing shapes an LLM
    # already recognizes; compact is otelq-specific, so its header format line
    # spells out the shape inline. No other format gets the suffix.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "logs.jsonl", [make_log(base, body="hi")])

    shape_hint = (
        'a {"columns":[...],"rows":[[...]]} object — column names once, '
        "each row a positional array"
    )
    compact_line = _run_fmt(temp_telemetry, "compact", "logs").splitlines()[1]
    assert compact_line == f"otelq logs response, format compact, {shape_hint}"

    for fmt in ("table", "json", "jsonl", "csv"):
        line = _run_fmt(temp_telemetry, fmt, "logs").splitlines()[1]
        assert line == f"otelq logs response, format {fmt}"
        assert shape_hint not in line


def test_ac48_sql_schema_discovery_documented_and_works(temp_telemetry: Path) -> None:
    # FR-31/EC-28: --help documents that the sql views cheat-sheet is a
    # curated subset and points at DESCRIBE/PRAGMA for the full live schema;
    # a live DESCRIBE actually returns more columns than the cheat-sheet lists.
    help_text = otelq.build_parser().format_help()
    assert "DESCRIBE" in help_text and "PRAGMA" in help_text

    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "traces.jsonl", [make_span(base)])
    out = _run(temp_telemetry, "sql", "DESCRIBE traces")
    described_columns = {r["column_name"] for r in _json.loads(out)}
    documented_columns = {
        "timestamp",
        "duration",
        "trace_id",
        "span_id",
        "parent_span_id",
        "service_name",
        "span_name",
        "span_kind",
        "status_code",
        "status_message",
    }
    assert documented_columns <= described_columns  # cheat-sheet is a subset
    assert "span_attributes" in described_columns  # the undocumented escape hatch


# =============================================================================
# FR-32 / AC-49..AC-55: --regex result filtering
# =============================================================================


def test_ac49_regex_filters_rows_and_reports_in_header(temp_telemetry: Path) -> None:
    # FR-32/EC-29: only matching rows are kept, and the header gains two
    # lines naming the verbatim pattern and the removed-row count; with no
    # --regex, neither line appears.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "logs.jsonl",
        [
            make_log(base, body="boom error here"),
            make_log(base + timedelta(seconds=1), body="all good"),
        ],
    )
    out = _run(temp_telemetry, "--regex", "error", "logs")
    lines = out.splitlines()
    assert lines[4] == "Regex filter applied: error"
    assert lines[5] == "Rows removed by regex: 1"
    payload = _json.loads(_strip_header(out))
    assert len(payload) == 1 and payload[0]["body"] == "boom error here"

    out_no_regex = _run(temp_telemetry, "logs")
    assert "Regex filter applied" not in out_no_regex
    assert "Rows removed by regex" not in out_no_regex


def test_ac50_regex_matches_any_cell(temp_telemetry: Path) -> None:
    # FR-32: a row is kept if the pattern matches ANY cell, not just a
    # designated "message" column.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "logs.jsonl",
        [
            make_log(base, service="special-svc", body="normal text"),
            make_log(
                base + timedelta(seconds=1), service="other-svc", body="normal text"
            ),
        ],
    )
    out = _run(temp_telemetry, "--regex", "special-svc", "logs")
    payload = _json.loads(_strip_header(out))
    assert len(payload) == 1
    assert payload[0]["service_name"] == "special-svc"


def test_ac51_malformed_regex_is_a_real_error(temp_telemetry: Path) -> None:
    # FR-32/EC-30: a malformed pattern is a real error, not a raw traceback.
    with pytest.raises(SystemExit) as exc:
        otelq.main(["--dir", str(temp_telemetry), "--regex", "(", "logs"])
    assert "invalid --regex pattern" in str(exc.value)


def test_ac52_regex_rejected_outside_supported_commands(temp_telemetry: Path) -> None:
    # FR-32/EC-31: --regex is rejected as a real error for sql/doctor/
    # collector-config/troubleshoot, not silently ignored.
    for argv in (
        ["sql", "SELECT 1"],
        ["doctor"],
        ["collector-config"],
        ["troubleshoot"],
    ):
        with pytest.raises(SystemExit) as exc:
            otelq.main(["--dir", str(temp_telemetry), "--regex", "x", *argv])
        assert "not supported for" in str(exc.value)


def test_ac53_regex_matches_pre_render_raw_value(temp_telemetry: Path) -> None:
    # FR-32: filtering happens against the raw cell value, before --format
    # json's escaping — a pattern matching only the unescaped quote form
    # still keeps the row (it would NOT match the JSON-escaped \"hi\" form).
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(temp_telemetry / "logs.jsonl", [make_log(base, body='say "hi" now')])
    out = _run_fmt(temp_telemetry, "json", "--regex", '"hi"', "logs")
    payload = _json.loads(_strip_header(out))
    assert len(payload) == 1
    assert payload[0]["body"] == 'say "hi" now'


def test_ac54_regex_case_sensitive_and_skips_none_cells(temp_telemetry: Path) -> None:
    # FR-32: case-sensitive by default (inline (?i) opts in to insensitive);
    # None cells are excluded from matching, not stringified into "None".
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "logs.jsonl",
        [make_log(base, severity="INFO", sevnum=9, body="UPPER CASE")],
    )
    out = _run(temp_telemetry, "--regex", "upper case", "logs")
    assert _json.loads(_strip_header(out)) == []
    out_ci = _run(temp_telemetry, "--regex", "(?i)upper case", "logs")
    assert len(_json.loads(_strip_header(out_ci))) == 1

    # summary's zero-count skeleton rows (e.g. WARN/ERROR/...) have
    # earliest/latest = None; "None" must not match those rows. (First block
    # only — the service second block is independent of --regex, FR-3.)
    out_none = _run(temp_telemetry, "--regex", "None", "summary")
    assert _json.loads(_summary_first_block(out_none)) == []


def test_ac55_regex_operates_on_already_capped_result(temp_telemetry: Path) -> None:
    # FR-32/FR-23: the filter runs on the already --top-capped, fully-ordered
    # result — a match beyond the cap is not found unless --top is raised.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "logs.jsonl",
        [
            make_log(base, body="the target needle"),  # oldest
            make_log(base + timedelta(seconds=1), body="filler 1"),
            make_log(base + timedelta(seconds=2), body="filler 2"),
        ],
    )
    # newest-first order excludes the oldest ("needle") row once capped to 2.
    out = _run(temp_telemetry, "--regex", "needle", "logs", "--top", "2")
    assert _json.loads(_strip_header(out)) == []
    # Raising --top recovers the match.
    out2 = _run(temp_telemetry, "--regex", "needle", "logs", "--top", "3")
    assert len(_json.loads(_strip_header(out2))) == 1


def test_ac56_errors_rows_carry_trace_id_for_pivot(temp_telemetry: Path) -> None:
    # FR-4/FR-6: every errors row carries trace_id, so the triage→localization
    # pivot to `trace <id>` needs no intermediate sql lookup.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "traces.jsonl",
        [make_span(base, trace_id="t-err", span_id="s-err", status_code=2, status_msg="boom")],
    )
    write_jsonl(
        temp_telemetry / "logs.jsonl",
        [make_log(base, severity="ERROR", sevnum=17, trace_id=trace_hex("t-err"))],
    )
    rows = _json.loads(_strip_header(_run(temp_telemetry, "errors")))
    assert len(rows) == 2
    assert all("trace_id" in r for r in rows)
    span_row = next(r for r in rows if r["kind"] == "span")
    assert span_row["trace_id"] == trace_hex("t-err")
    # The carried id pivots straight into the trace tree (FR-6).
    tree = _json.loads(_strip_header(_run(temp_telemetry, "trace", span_row["trace_id"])))
    assert [s["span_name"].strip() for s in tree] == ["GET /x"]


def test_ac57_summary_service_list_second_block(temp_telemetry: Path) -> None:
    # FR-3/EC-32: summary emits a labeled service second block in every format,
    # with per-service totals across all signals ordered by count desc; the
    # first block is byte-identical to summary without the second block.
    base = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_jsonl(
        temp_telemetry / "traces.jsonl",
        [make_span(base + timedelta(seconds=i), service="busy-svc") for i in range(3)],
    )
    write_jsonl(
        temp_telemetry / "logs.jsonl",
        [
            make_log(base, service="busy-svc", body="a"),
            make_log(base + timedelta(seconds=1), service="quiet-svc", body="b"),
        ],
    )
    label = otelq._SUMMARY_SERVICE_LABEL

    # --format json: block 2 is rendered in the same format (an array of objects).
    out = _run(temp_telemetry, "summary")
    assert label in out
    first, second = _strip_header(out).split("\n\n" + label + "\n", 1)
    assert {r["signal"] for r in _json.loads(first)} == {"traces", "logs"}
    svc = _json.loads(second)  # per-service totals across ALL signals, count desc
    assert svc == [
        {"service": "busy-svc", "count": 4},  # 3 spans + 1 log
        {"service": "quiet-svc", "count": 1},  # 1 log
    ]

    # --format compact: block 2 is a compact object in the same shape as block 1.
    cout = _run_fmt(temp_telemetry, "compact", "summary")
    csecond = _strip_header(cout).split("\n\n" + label + "\n", 1)[1]
    assert _json.loads(csecond) == {
        "columns": ["service", "count"],
        "rows": [["busy-svc", 4], ["quiet-svc", 1]],
    }

    # Present in every format; the first block matches the second-block-free render.
    for fmt in ("table", "json", "jsonl", "csv", "compact"):
        assert label in _run_fmt(temp_telemetry, fmt, "summary")

    # errors (a non-summary command) never emits the service block.
    assert label not in _run(temp_telemetry, "errors")
