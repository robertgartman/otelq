---
name: otelq
description: "Query logs, metrics, and traces captured from the OpenTelemetry feed using the otelq CLI."
---

# Query Telemetry

Inspect real OpenTelemetry signals to close the loop after a change: run the
code, then confirm from telemetry that it behaved as intended.

## Run

```
uvx otelq --dir .telemetry --format compact summary
```

`--dir .telemetry` is **required** — `uvx` runs otelq from an isolated build, so
the default path won't resolve to your project. Point it at the Collector's
output folder (the bind-mounted `.telemetry/` at the project root). Start with
`summary` when you don't yet know what to query. Pin a version with
`uvx otelq@<version> …`.

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

`--format` is a **global** flag: it (and `--all`, `--no-cache`, `--since`) goes
**before** the subcommand; per-command flags (`--top`, `--service`, `--level`,
`--grep`) go **after**:

```
uvx otelq --dir .telemetry --since 10m --format compact errors --top 20
```

## Commands

`summary`, `errors`, `slow`, `trace <id>`, `logs`, `metric <name>`,
`sql "<query>"`. Narrow the window with `--since 30s|10m|2h|1d` (or `--all` for
full history); cap rows with `--top N`. Full reference (incl. the `sql` view
columns):

```
uvx otelq --dir .telemetry --help
```

## Not seeing data?

```
uvx otelq --dir .telemetry troubleshoot
```

