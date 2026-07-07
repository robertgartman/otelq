---
doc_type: adr
authoritative: true
stability: stable
status: active
decision_scope: architecture
audience:
  - ai
  - engineering
must_not_contain:
  - feature_requirements
  - implementation_walkthroughs
  - reversible_decisions
created: 2026-06-23
last_updated: 2026-06-23
related_documents:
  - ADR-001-host-cli-reads-bind-mounted-files
  - ADR-003-duckdb-otlp-extension-pin-governance
  - SPEC-otelq-cli
supersedes: null
superseded_by: null
ai_summary: "Distribute otelq as a single PEP 723 file (uv run / pinned raw URL) and as a PyPI package (uvx/pipx); stay CLI-only — no frozen binary, no MCP server."
semantic_tags:
  - otelq
  - pep723
  - uv
  - distribution
  - cli
  - pyinstaller
  - mcp
  - packaging
---

# ADR-002 — PEP 723 / uv Single-File Distribution

## Context

The query tool established in
[ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md) — `otelq` — is a
stateless host-side reader of bind-mounted telemetry files. It is meant to be
reached for casually, including by coding agents in a change → run → observe loop,
so its *acquisition cost* and *invocation latency* matter as much as its
behavior. Two distinct usage modes must both be cheap:

- **Ad-hoc, zero-install.** Run the tool directly from a checkout, or from a
  pinned URL, with nothing to install first.
- **Installed, on the `PATH`.** A developer who uses it regularly wants
  `otelq` to just be a command.

The tool has effectively one runtime dependency (`duckdb`), which must be pinned
exactly for reasons decided in
[ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md). Reproducibility of
the tool therefore reduces almost entirely to reproducibility of that single
pin, wherever and however the tool is run.

Separately, because coding agents consume the tool, there is pressure to expose
it as an MCP server or a structured API. That pressure must be weighed against
latency: an agent already speaks fluent text and shell.

## Decision

Distribute `otelq` as a **single [PEP 723](https://peps.python.org/pep-0723/)
file** — `otelq.py` with an inline `# /// script` dependency block — and make
that one file usable three ways:

1. **`uv run otelq.py`** directly from a checkout.
2. **`uv run <pinned-raw-URL>`** — run the exact file from a pinned source
   reference without cloning.
3. **A PyPI package** built with `hatchling`, exposing `[project.scripts]
   otelq = ...`, so that **`uvx otelq`** and **`pipx install otelq`** work for
   installed use.

The tool stays **CLI-only**. It is deliberately *not* wrapped in an MCP server or
any persistent API surface. This is a latency decision, consistent with
[ADR-001](ADR-001-host-cli-reads-bind-mounted-files.md)'s no-server stance: for an
LLM client, the text round-trip "invoke a CLI, read its stdout" is faster and
simpler than negotiating an MCP/API handshake and transport, and it requires no
standing process. The agent integration is therefore a thin skill that calls the
CLI, not a server the agent connects to.

The single runtime dependency is pinned **exactly and identically** in both
distribution forms: the PEP 723 inline block and the package `pyproject` both
declare `duckdb==1.5.4`. The two pins **must be kept in sync** — they are the
same decision expressed twice, governed by
[ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md).

## Alternatives Considered

- **Freeze to a self-contained binary (PyInstaller / Nuitka).** Rejected on
  hard technical grounds, not preference. DuckDB is **officially unsupported under
  PyInstaller**, and there is a known crash where `LOAD`-ing a DuckDB extension
  from inside a frozen bundle fails — and loading the `otlp` extension is the
  tool's core operation (see
  [ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md)). On macOS a frozen
  artifact additionally incurs the hardened-runtime / quarantine (Gatekeeper) tax,
  requiring signing and notarization to run cleanly. A frozen binary would thus be
  both fragile (extension loading) and operationally heavy (code-signing) for no
  gain over a `uv`-run script whose only dependency is a single pinned wheel.
- **A heavyweight CLI framework.** Rejected. The tool is a small set of
  subcommands over a fixed query surface; a large framework adds dependencies,
  startup cost, and packaging weight that work against the single-file,
  fast-to-invoke goal. The standard library is sufficient.
- **An MCP server / structured API in front of the tool.** Rejected for the
  latency reason in the Decision: a standing negotiated surface is slower to reach
  and heavier to operate than a stateless CLI, for a workload that is inherently
  one-shot.

## Consequences

- **Zero-install ad-hoc use is a first-class mode.** Anyone — human or agent —
  can run the exact tool from a pinned reference without a clone or an install
  step, which is what makes it cheap enough to use as a feedback sense.
- **Reproducibility rests entirely on the exact pin.** Because the tool carries
  its dependency inline, "which `otelq`" is fully determined by the pinned
  `duckdb` version; this is why that pin is governed rigorously in
  [ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md), and why the PEP 723
  block and `pyproject` must never drift apart.
- **Pinned source references must point at an immutable tag, not a branch.** When
  distributing via a raw URL, the URL **must** reference a tag (or other immutable
  ref), because `uv` caches by URL: a branch URL can serve a stale cached file
  even after the branch moves, silently running an old tool. A tagged URL is
  content-stable and sidesteps the URL-cache staleness entirely.
- **Two pins to maintain.** The cost of supporting both the single-file and the
  packaged forms is one duplicated dependency declaration that must be updated
  together on any bump; the pin-bump checklist that enforces this lives in
  [ADR-003](ADR-003-duckdb-otlp-extension-pin-governance.md).
- **The CLI's command surface is specified separately.** What the commands do,
  their flags, and their output contract are defined in
  [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md); this ADR governs only *how the tool
  is distributed and invoked*.
