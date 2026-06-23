# otelq

**Feed your agentic development setup with Open Telemetry**

otelq is a tiny CLI that queries the OpenTelemetry traces, logs, and metrics your app emits locally. A stock OpenTelemetry Collector captures those signals to plain JSONL files on disk, and otelq reads them directly with DuckDB — no Jaeger, no SigNoz, no Grafana, no server, and no UI. It is built for the inner development loop and for AI coding agents doing close-the-loop verification: run the app, then ask "did the request error?", "what was slow?", "show me trace X" straight from the terminal.

## Architecture

```
  your app  --OTLP-->  Collector (Docker)  --writes-->  ./telemetry/*.jsonl  --read by-->  otelq (host CLI)
```

The bind-mounted directory is the entire contract. The Collector writes OTLP signals as `traces.jsonl`, `logs.jsonl`, and `metrics.jsonl`; otelq reads those same files. There is no network coupling between the Collector and the CLI — the shared directory is the API.

## Quickstart

```sh
# 1. Start the dev Collector (OTLP gRPC :4317 / HTTP :4318)
just otel-up

# 2. Point the app you are debugging at the Collector and run it
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
#    ...then run your app so it emits telemetry...

# 3. Query what was captured
just otelq summary
just otelq --format json errors
just otelq slow --top 10
just otelq trace <trace_id>
```

Run `just otel-clean` to reset captured telemetry (empties the active JSONL files in place, drops rotated backups, and clears the otelq cache). Run `just otel-down` to stop the Collector.

## Install / run options

**(a) Zero-install, ad-hoc** — `otelq.py` is a [PEP 723](https://peps.python.org/pep-0723/) single-file script. `uv` provisions Python and DuckDB on the fly:

```sh
uv run otelq.py summary
```

**(b) Installed CLI** (after the first PyPI release):

```sh
uvx otelq summary          # ephemeral, no install
pipx install otelq         # persistent install
```

**(c) Clone the repo** for the full dev setup — the Collector compose file and the `justfile`:

```sh
git clone https://github.com/robertgartman/otelq
cd otelq
just otel-up
```

## Commands

| Command   | What it does                                              |
|-----------|-----------------------------------------------------------|
| `summary` | Counts and time span per signal                           |
| `errors`  | Error spans and ERROR/FATAL logs                          |
| `slow`    | Slowest spans (`--top N`)                                 |
| `trace`   | All spans of one trace, as a tree (`trace <trace_id>`)    |
| `logs`    | Filtered log records (`--service`, `--level`, `--grep`)   |
| `metric`  | Time series for one metric (`metric <name>`)              |
| `sql`     | Ad-hoc SQL over the `traces`/`logs`/`metrics` views       |

**Argument-order rule:** `--format` (`table` \| `json` \| `csv`) is a global flag and goes **before** the subcommand. The same applies to `--all` and `--no-cache`. The `--since` window (e.g. `10m`, `2h`, `1d`) goes after the subcommand.

```sh
uv run otelq.py --format json errors        # correct
uv run otelq.py errors --format json         # WRONG: --format is global
uv run otelq.py logs --level ERROR --since 30m
```

See [`context/spec/SPEC-otelq-cli`](context/spec/SPEC-otelq-cli.md) for the full, authoritative command behavior.

## DuckDB pin note

The sole runtime dependency is pinned exactly: `duckdb==1.5.3`. This is deliberate. otelq reads OTLP JSONL via the community [`duckdb-otlp`](https://github.com/smithclay/duckdb-otlp) extension, which is built per DuckDB version — a floating DuckDB would silently fail to load the extension. CI runs an extension-probe step that loads the extension against the pinned version so the pin and the published extension stay in lockstep. See [`context/adr/ADR-003`](context/adr/ADR-003-duckdb-otlp-extension-pin-governance.md) for the decision and trade-offs.

## Agentic engineering

This repo is built to be driven by AI coding agents:

- **[`AGENTS.md`](AGENTS.md)** — start here. The entry point for agents working in this repo.
- **[`context/CONTEXT.md`](context/CONTEXT.md)** — the documentation system (PRD / SPEC / ADR / CONTRACT routing rules).
- **[`.agents/skills/query-telemetry`](.agents/skills/query-telemetry/SKILL.md)** — the query-telemetry skill: capture OTEL signals from the dev Collector and query them with otelq. A `.claude` shim (`.claude/skills/query-telemetry`) mirrors it for Claude Code.

The `.claude-plugin` manifest (`.claude-plugin/plugin.json`, `marketplace.json`) is an early distribution path for shipping otelq and its skill as an installable plugin.

## Requirements

- **Docker** — to run the dev OpenTelemetry Collector.
- **[uv](https://docs.astral.sh/uv/)** — to run the CLI (it provisions Python and DuckDB; no separate Python setup needed).

## Contributing

```sh
just lint          # ruff
just otelq-test    # pytest suite
```

Issues and pull requests welcome at [github.com/robertgartman/otelq](https://github.com/robertgartman/otelq).

## License

MIT © 2026 Robert Gartman. See [`LICENSE`](LICENSE).
