---
name: query-telemetry
description: "Query logs, metrics, and traces captured from the OpenTelemetry feed using the otelq CLI."
---

# Query Telemetry

Inspect real OpenTelemetry signals from the running system. Use this to close
the loop after a change: run the code, then confirm from telemetry that it
behaved as intended.

## Running otelq

Throughout this skill, **`otelq …`** is shorthand for running the CLI straight
from the repo with `uvx` — no `just`, no global install, no clone:

```
uvx --from git+https://github.com/robertgartman/otelq otelq --dir telemetry …
```

- `uvx` fetches and runs otelq in an isolated environment. The **first** run
  clones + builds it, and otelq then fetches the DuckDB `otlp` community
  extension; both are cached, so later runs are fast (and the build needs
  network once).
- `--dir telemetry` is required. It is a *global* flag and points otelq at the
  Collector's output folder (the repo's `telemetry/`). Because `uvx` runs otelq
  from an isolated build, the default would not resolve to the repo — always
  pass `--dir`.
- To pin a version, append a ref: `…/otelq@v0.1.0 otelq …`.

The Collector itself is still brought up and reset with `just` (see The loop) —
only the otelq *queries* run through `uvx`.

## The loop

1. **Start the dev stack** (the Collector starts with it): `just otel-up`
2. **Enable export:** ensure `OTEL_ENABLED=true` in `.env`, then (re)start the
   app(s) you are exercising.
3. **Reproduce** the behaviour (hit an endpoint, run a flow, run a test).
4. **Query:** `otelq --format json <command>` (the `--format` flag goes *before*
   the subcommand — see Commands below).
5. **Inspect** the JSON, then iterate.

## Commands

Run `otelq <command>` (the shorthand above). For parseable output add
`--format json`.

> **Argument order matters.** `--dir` and `--format` are *global* flags, so they
> must come **before** the subcommand — correct:
> `otelq --format json errors`; wrong: `otelq errors --format json`. The wrong
> order fails with `otelq: error: unrecognized arguments: --format json`.
> Subcommand-specific flags (`--since`, `--top`, `--service`, etc.) still go
> *after* the subcommand. (`table` is the default format; `csv` is also
> available.)

- `summary` — counts and time span per signal; the "is anything captured?" check
- `errors [--since 10m]` — error-status spans and ERROR/FATAL logs
- `slow [--top 20]` — slowest spans by duration
- `trace <trace_id>` — every span of one trace, as a parent/child tree
- `logs [--service X] [--level ERROR] [--grep text]` — filtered log records
- `metric <name>` — time series for one metric
- `sql "<query>"` — ad-hoc SQL over the views `traces`, `logs`, `metrics`,
  `metrics_gauge`, `metrics_sum`, e.g.
  `otelq sql "SELECT count(*) FROM traces"`.

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
