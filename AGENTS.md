# otelq development instructions

**otelq** is a single-file Python CLI (`otelq.py`) that queries OpenTelemetry traces/logs/metrics captured by a local dev Collector, plus a `context/` documentation system. There is no server, no web app, no backend modules — just the CLI, the Collector, and the docs.

**1. Bootstrap the environment** (one command, idempotent):
```
just otel-up
```
Creates `.telemetry/` and brings up the dev OTel Collector (OTLP gRPC `:4317` / HTTP `:4318`). The Collector writes captured signals to the bind-mounted `.telemetry/{traces,logs,metrics}.jsonl`; `otelq` reads from there. Point your instrumented process at `localhost:4317`/`:4318`, exercise it, then query.

**2. Find the canonical truth.** All design decisions live in [`context/`](./context/CONTEXT.md). Read [CONTEXT.md](./context/CONTEXT.md) **first** — it routes you to the right doc by task type. Document precedence is **ADR > CONTRACT > SPEC > PRD** when in conflict.

**3. Pick the right skill for your task**:
- Capturing + inspecting telemetry to verify a change works → `otelq` (the canonical loop: emit signals at the Collector, then query them with `otelq`)
- Creating or updating context docs (PRD/SPEC/ADR/CONTRACT) → invoke the context-engineer skill.

**4. Architectural non-negotiables** (deviations require an ADR):
- **CLI-ONLY** — no server, no daemon, no long-running process, no MCP. Low latency on a single invocation is the whole point; anything that adds a hop or a warm-process assumption is out.
- **OpenTelemetry-only, via the Collector file seam** — otelq never talks to instrumented processes directly. The bind-mounted `.telemetry/` directory is the contract between the Collector and the CLI (see **CONTRACT-telemetry-directory** / **ADR-001**). No bespoke ingestion paths.
- **The justfile is the single execution gateway** — never invent parallel scripts or call `otelq.py`/`docker compose` ad hoc in a way that bypasses the recipes.
- **EXACT DuckDB pin** — never floated (see **ADR-003**). otelq depends on the `otlp` *community* extension, which is built per DuckDB version and lags releases; an unpinned bump can land on a version with no published extension and break every command.
- **Fail FRIENDLY, not silent** — no telemetry captured ⇒ a clear human-readable message and **exit 0**, never a stack trace. Errors are explanatory, not raw tracebacks.
- **Strict typing** throughout — pyright `strict` passes clean (enforced via `[tool.pyright]`), full type hints, and **no** `# type: ignore` / `# pyright: ignore`. Explicit `Any` is confined to the two genuinely dynamic boundaries — parsed OTLP-JSON payloads and DuckDB result rows — and must never spread beyond them.
- **No solution-name leakage** — keep code neutral; do not embed any external/solution project name in otelq code or filenames.

**5. Common pitfalls** (cost you cycles if missed):
- `--format` is a **GLOBAL** flag and must precede the subcommand: `just otelq --format json summary` ✅, not `just otelq summary --format json` ❌.
- The `duckdb-otlp` community extension is fetched from `community-extensions.duckdb.org` on **first run** (network once, then cached). A first invocation on a fresh machine needs connectivity.
- The `otlp` community extension **may lag DuckDB releases** — this is why the pin is exact (see ADR-003). Confirm the extension exists for a target version before any bump.
- `just otel-clean` **stops the Collector before truncating** the active jsonl files, then restarts it. The Collector holds those fds open; `rm`-ing or live-truncating them while it runs orphans the fd / leaves a NUL hole and silently loses low-volume logs/metrics. Use the recipe; don't clear `.telemetry/` by hand.

**6. Code-intelligence tooling — if present, initialize it in *this* worktree.** Serena (symbol-level LSP intelligence) and CodeGraph (call/dependency graph) are optional developer tools for working *on* this repo. They are **not** part of otelq's architecture — the CLI-ONLY / no-MCP rule in §4 governs what otelq **is**, not what you use to edit it. Neither is required; if the binaries are absent, carry on with `rg`/read.

When either **is** available, initialize it in the worktree you are actually working in, **before** relying on its answers:
- **Their indexes are per-directory, and a linked git worktree inherits none of them.** This is the same trap as the telemetry store (**ADR-011** / **SPEC-otelq-cli FR-45**): an un-indexed worktree does not error, it returns *plausible but incomplete* results — no callers found, symbol not defined anywhere — which reads exactly like a real finding. Treat "no results" from an unverified index as unknown, not as evidence.
- **Serena** — `.serena/project.yml` is committed, so configuration carries across worktrees; the LSP cache does not. Warm it with `serena project index`. Check with `serena project health-check`.
- **CodeGraph** — `.codegraph/` is per-worktree and gitignored, so it must be built locally: `codegraph init`. Refresh after significant edits with `codegraph sync`, and confirm freshness with `codegraph status` — a stale graph is the same failure mode as an absent one.

---

# Dev Workflow
Execute `just` to see the available recipes.


---

# Canonical truth

Read [CONTEXT.md](./context/CONTEXT.md). The rules in CONTEXT.md **must** be followed.