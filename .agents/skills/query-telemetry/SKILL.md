---
name: query-telemetry
description: "Use when debugging backend or frontend behaviour, or verifying that instrumentation works — captures OTEL traces, logs, and metrics from the local dev Collector and queries them with the otelq CLI."
---

# Query Telemetry

Inspect real OpenTelemetry signals from the running system. Use this to close
the loop after a change: run the code, then confirm from telemetry that it
behaved as intended.

## The loop

1. **Start the dev stack** (the Collector starts with it): `just otel-up`
2. **Enable export:** ensure `OTEL_ENABLED=true` in `.env`, then (re)start the
   app(s) you are exercising.
3. **Reproduce** the behaviour (hit an endpoint, run a flow, run a test).
4. **Query:** `just otelq --format json <command>` (the `--format` flag goes
   *before* the subcommand — see Commands below)
5. **Inspect** the JSON, then iterate.

## Commands

Run `just otelq <command>`. For parseable output add `--format json`.

> **Argument order matters.** `--format` is a *global* flag, so it must come
> **before** the subcommand — correct: `just otelq --format json errors`;
> wrong: `just otelq errors --format json`. The wrong order fails with
> `otelq: error: unrecognized arguments: --format json`. Subcommand-specific
> flags (`--since`, `--top`, `--service`, etc.) still go *after* the
> subcommand. (`table` is the default format; `csv` is also available.)

- `summary` — counts and time span per signal; the "is anything captured?" check
- `errors [--since 10m]` — error-status spans and ERROR/FATAL logs
- `slow [--top 20]` — slowest spans by duration
- `trace <trace_id>` — every span of one trace, as a parent/child tree
- `logs [--service X] [--level ERROR] [--grep text]` — filtered log records
- `metric <name>` — time series for one metric
- `sql "<query>"` — ad-hoc SQL over the views `traces`, `logs`, `metrics`,
  `metrics_gauge`, `metrics_sum`. Use the dedicated recipe:
  `just otelq-sql "SELECT count(*) FROM traces"`. (`just otelq sql "..."` breaks
  on any whitespace due to variadic word-splitting; `uv run otelq.py
  sql "..."` also works as a fallback.)

Always prefer `--format json` so output is parsed structurally, not scraped.

## Schema cheat-sheet (for `sql`)

- `traces`: `timestamp`, `duration` (nanoseconds), `trace_id`, `span_id`,
  `parent_span_id`, `service_name`, `span_name`, `span_kind`,
  `status_code` (0=unset, 1=ok, 2=error), `status_message`
- `logs`: `timestamp`, `trace_id`, `service_name`, `severity_text`, `body`
- `metrics`: `timestamp`, `service_name`, `metric_name`, `metric_type`,
  `value`, `metric_unit`

## Troubleshooting

- **Empty output / "no telemetry captured":** the Collector is not running
  (`just otel-up`), or apps started with `OTEL_ENABLED=false`. Fix both, then
  reproduce again.
- **Stale data:** `just otel-clean` clears `telemetry/` before a fresh run.
