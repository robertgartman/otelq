# otelq

[![CI](https://github.com/robertgartman/otelq/actions/workflows/ci.yml/badge.svg)](https://github.com/robertgartman/otelq/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/robertgartman/otelq/badge)](https://scorecard.dev/viewer/?uri=github.com/robertgartman/otelq)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Feed your agentic development setup with Open Telemetry**

otelq is a tiny CLI that queries the OpenTelemetry traces, logs, and metrics your app emits locally. A stock OpenTelemetry Collector captures those signals to plain JSONL files on disk, and otelq reads them directly with DuckDB — no Jaeger, no SigNoz, no Grafana, no server, and no UI. It is built for the inner development loop and for AI coding agents doing close-the-loop verification: run the app, then ask "did the request error?", "what was slow?", "show me trace X" straight from the terminal.

## Architecture

```
  your app  --OTLP-->  Collector (Docker)  --writes-->  ./telemetry/*.jsonl  --read by-->  otelq (host CLI)
```

The bind-mounted directory is the entire contract. The Collector writes OTLP signals as `traces.jsonl`, `logs.jsonl`, and `metrics.jsonl`; otelq reads those same files. There is no network coupling between the Collector and the CLI — the shared directory is the API.

### Collector: standalone or integrated

The Collector is interchangeable — otelq is a pure consumer of the telemetry directory, so any conformant producer works. There are two setups (see [`context/adr/ADR-007`](context/adr/ADR-007-dual-collector-standalone-and-integrated.md)):

- **Standalone** — `just otel-up` runs otelq's bundled Collector. Best for a project with no telemetry stack, or a quick demo.
- **Integrated** — another project on the same host already runs its own Collector. Add otelq's file-export pipeline to *that project's* Collector instead of running a second one. The direction matters: you work **from the otelq repo** and integrate otelq **into the target project** (identified by its absolute path, e.g. `/Users/me/dev/my-service`) — not the other way around.

  ```sh
  otelq collector-config                      # run in the otelq repo: prints the exporters + pipeline wiring
  # ...paste the fragment into the TARGET project's Collector config, bind-mount its ./telemetry, restart...
  otelq --dir /Users/me/dev/my-service/telemetry doctor    # verify the target's wiring satisfies the contract
  ```

  `collector-config` is generated from otelq's pinned constants, so it never drifts from the contract. The `file` exporter requires the `*-contrib` Collector image. In integrated mode otelq never manages the target's Collector — the `just otel-*` recipes (especially `otel-clean`) are standalone-only. The **integrate-collector** skill automates this and asks for the target project's path; see below.

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

### Try it without an app (demo)

No app to instrument yet? Generate synthetic telemetry with one command:

```sh
just otel-demo        # starts the Collector, then runs otelgen for ~15s
just otelq summary    # now there is traces + logs data to query
just otelq slow --top 5
```

`just otel-demo` brings up the Collector and runs [otelgen](https://github.com/krzko/otelgen) (the `demo` Compose profile) to push ~15s of synthetic **traces and logs** through it, populating `telemetry/`. It's the fastest way to exercise otelq — and the `query-telemetry` skill — on a fresh clone. (Metrics aren't generated: otelgen's metrics CLI is broken upstream at the pinned version.)

> The demo generators live **only in this repo** as a testing aid. They are **not** part of integrating otelq into another project — that path adds only the Collector's file exporters (see [Collector: standalone or integrated](#collector-standalone-or-integrated)), never otelgen.

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
| `collector-config` | Print the file-export fragment to add to an existing Collector |
| `doctor`  | Check that a telemetry dir satisfies the contract (`--dir`) |

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
- **[`.agents/skills/integrate-collector`](.agents/skills/integrate-collector/SKILL.md)** — the integrate-collector skill: run from this repo to wire otelq's file-export pipeline into *another* project's existing Collector (the integrated setup above). It asks for the target project's absolute path and verifies the result with `otelq doctor`.

The `.claude-plugin` manifest (`.claude-plugin/plugin.json`, `marketplace.json`) is an early distribution path for shipping otelq and its skill as an installable plugin.

## Requirements

- **Docker** — to run the dev OpenTelemetry Collector.
- **[uv](https://docs.astral.sh/uv/)** — to run the CLI (it provisions Python and DuckDB; no separate Python setup needed).

## Contributing

```sh
just lint          # ruff
just otelq-test    # pytest suite
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full setup, the project-specific
rules (strict typing, the load-bearing `duckdb` pin, the `justfile` gateway), and
the PR checklist. Participation is governed by the
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md); report vulnerabilities per
[`SECURITY.md`](SECURITY.md). Issues and pull requests welcome at
[github.com/robertgartman/otelq](https://github.com/robertgartman/otelq).

## License

MIT © 2026 Robert Gartman. See [`LICENSE`](LICENSE).
