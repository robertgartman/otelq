# Read Me First (Agent Quickstart)

> **You are an AI agent working in this repo.** Read this section before doing anything else. The rest of this file is reference material.

**otelq** is a single-file Python CLI (`otelq.py`) that queries OpenTelemetry traces/logs/metrics captured by a local dockerized dev Collector, plus a `context/` documentation system. There is no server, no web app, no backend modules — just the CLI, the Collector, and the docs.

**1. Bootstrap the environment** (one command, idempotent):
```
just otel-up
```
Creates `telemetry/` and brings up the dev OTel Collector (OTLP gRPC `:4317` / HTTP `:4318`). The Collector writes captured signals to the bind-mounted `telemetry/{traces,logs,metrics}.jsonl`; `otelq` reads from there. Point your instrumented process at `localhost:4317`/`:4318`, exercise it, then query.

**2. Find the canonical truth.** All design decisions live in [`context/`](./context/CONTEXT.md). Read [CONTEXT.md](./context/CONTEXT.md) **first** — it routes you to the right doc by task type. Document precedence is **ADR > CONTRACT > SPEC > PRD** when in conflict.

**3. Pick the right skill for your task**:
- Capturing + inspecting telemetry to verify a change works → `otelq` (the canonical loop: emit signals at the Collector, then query them with `otelq`)
- Creating or updating context docs (PRD/SPEC/ADR/CONTRACT) → follow [CONTEXT.md](./context/CONTEXT.md) discipline (right type, right folder, frontmatter, precedence)

**4. Architectural non-negotiables** (deviations require an ADR):
- **CLI-ONLY** — no server, no daemon, no long-running process, no MCP. Low latency on a single invocation is the whole point; anything that adds a hop or a warm-process assumption is out.
- **OpenTelemetry-only, via the Collector file seam** — otelq never talks to instrumented processes directly. The bind-mounted `telemetry/` directory is the contract between the Collector and the CLI (see **CONTRACT-telemetry-directory** / **ADR-001**). No bespoke ingestion paths.
- **The justfile is the single execution gateway** — never invent parallel scripts or call `otelq.py`/`docker compose` ad hoc in a way that bypasses the recipes.
- **EXACT DuckDB pin** — `duckdb==1.5.3`, never floated (see **ADR-003**). otelq depends on the `otlp` *community* extension, which is built per DuckDB version and lags releases; an unpinned bump can land on a version with no published extension and break every command.
- **Fail FRIENDLY, not silent** — no telemetry captured ⇒ a clear human-readable message and **exit 0**, never a stack trace. Errors are explanatory, not raw tracebacks.
- **Strict typing** throughout — pyright `strict` passes clean (enforced via `[tool.pyright]`), full type hints, and **no** `# type: ignore` / `# pyright: ignore`. Explicit `Any` is confined to the two genuinely dynamic boundaries — parsed OTLP-JSON payloads and DuckDB result rows — and must never spread beyond them.
- **No solution-name leakage** — keep code neutral; do not embed any external/solution project name in otelq code or filenames.

**5. Common pitfalls** (cost you cycles if missed):
- `--format` is a **GLOBAL** flag and must precede the subcommand: `just otelq --format json summary` ✅, not `just otelq summary --format json` ❌.
- The `duckdb-otlp` community extension is fetched from `community-extensions.duckdb.org` on **first run** (network once, then cached). A first invocation on a fresh machine needs connectivity.
- The `otlp` community extension **lags DuckDB releases** — this is why the pin is exact (see ADR-003). Confirm the extension exists for a target version before any bump.
- `just otel-clean` **stops the Collector before truncating** the active jsonl files, then restarts it. The Collector holds those fds open; `rm`-ing or live-truncating them while it runs orphans the fd / leaves a NUL hole and silently loses low-volume logs/metrics. Use the recipe; don't clear `telemetry/` by hand.

---

# Dev Workflow

| Command | What it does |
|---------|--------------|
| `just otel-up` | Create `telemetry/` and start the dev Collector (OTLP `:4317`/`:4318`). |
| `just otel-down` | Stop the Collector. |
| `just otelq <cmd>` | Run the CLI, e.g. `just otelq summary`, `just otelq logs --since 10m`. |
| `just otelq-sql "<query>"` | Run an ad-hoc SQL query against captured telemetry. |
| `just otelq-test` | Run the test suite (`tests/`) under the pinned DuckDB. |
| `just otel-clean` | Reset captured telemetry: stop Collector → empty active jsonl in place → drop rotated backups + the parquet cache → restart. **Destructive and unrecoverable** (`telemetry/` is gitignored). |
| `just lint` | Lint with Ruff. |

The CLI lives in `otelq.py` (single file). Tests live in `tests/`. The Collector config is `otel-collector-dev.yaml`; the stack is defined in `compose.yaml` (profile `otel`).

Typical verify-a-change loop: `just otel-up` → run your instrumented code against `localhost:4317` → `just otelq summary` / `just otelq-sql "..."` to confirm the signals look right → `just otel-clean` between runs to start from a clean slate.

---

# Adding Context Docs

Read [CONTEXT.md](./context/CONTEXT.md) first; it is the authoritative routing guide. Quick rules:

- Place each doc under `context/<type>/` per its type: PRDs in `context/prd/`, SPECs in `context/spec/`, ADRs in `context/adr/`, CONTRACTs in `context/contract/`. Follow the naming patterns (`PRD-<name>.md`, `SPEC-<name>.md`, `ADR-NNN-<short-title>.md`, `CONTRACT-<name>.md`).
- **A document answers exactly one** Decision-Matrix question. PRD = *why*; SPEC = *what exactly the system does*; ADR = *why this technical approach*; CONTRACT = *how systems exchange data*. Don't mix product vision or architectural rationale into a SPEC.
- **Every SPEC functional requirement (FR) needs at least one acceptance criterion** that traces to it; every edge case should also have AC coverage.
- **ADR numbers are sequential, zero-padded, and gap-free**; check the highest existing number before assigning. ADR/SPEC requirement IDs (FR/INV/AC/EC) are **append-only**. Never modify an accepted ADR — supersede it with a new one.
- Fill all required frontmatter fields; use `2026-06-23`-style `YYYY-MM-DD` dates for `created`/`last_updated`. Validate that `related_documents`/`depends_on` references exist.
