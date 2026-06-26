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
  - ADR-002-pep723-uv-single-file-distribution
  - ADR-003-duckdb-otlp-extension-pin-governance
  - ADR-004-collector-in-docker-bind-mount
  - ADR-005-incremental-parquet-cache
  - CONTRACT-telemetry-directory
  - SPEC-otelq-cli
supersedes: null
superseded_by: null
ai_summary: "Foundational split: the Dockerized Collector appends OTLP-JSONL to a host bind-mounted dir; a host-side CLI re-reads those files on demand — no server, port, daemon, or DB."
semantic_tags:
  - otelq
  - telemetry
  - bind-mount
  - decoupling
  - collector
  - observability
  - file-export
---

# ADR-001 — Host CLI Reads Bind-Mounted Telemetry Files

## Context

The dev/CI telemetry path captures OpenTelemetry signals (traces, logs, metrics)
during a normal development session and lets a human or a coding agent inspect
them with SQL. The capture half — an OTel Collector running in Docker — and the
query half — an ad-hoc tool — have fundamentally different lifecycles and
operational properties:

- **Capture is durable and long-lived.** The Collector runs while the dev stack
  is up, continuously appending signals. The data it produces must outlive any
  single query, outlive the query tool's process, and ideally outlive the
  Collector container itself.
- **Query is ephemeral and bursty.** A query happens when someone (or an agent)
  asks a question, returns an answer, and exits. It owns no state between
  invocations.

The constraint is to keep this dev/CI path *lightweight*: no database server, no
UI stack, no persistent service, and no telemetry pipeline that must itself be
operated. The capture side and the query side must be coupled as loosely as
possible so that either can be replaced, restarted, or removed without
disturbing the other, and so that the captured data is reachable by any host
tool — not only by the tool that happens to ship today.

This is the foundational decision on which the entire project rests; every other
ADR in this set is a refinement of, or a constraint inside, the seam it
establishes.

## Decision

Split the system along a **filesystem seam**:

1. The Collector runs inside Docker and writes OTLP-JSONL via its `fileexporter`
   into a directory that is **bind-mounted from the host**. This is the durable
   half: an append-only file stream at a stable host path.
2. A **host-side CLI** re-reads those files on demand to answer queries. This is
   the ephemeral compute half: it attaches to the files, runs a query in-process,
   prints the result, and exits — holding no state and serving no requests.

There is **no server, no listening port, no long-running daemon, and no
persistent database** in this path. The Collector appends; the CLI re-reads.
The two communicate only through the bytes on disk at the bind-mount path.

Because the files live on the host (not inside the container's writable layer),
they **survive `docker compose down`** and remain readable by any host process at
a predictable location. The CLI is therefore responsible only for *locating* that
directory — it exposes a `--dir` flag with a sane default (`<cwd>/telemetry`, the
telemetry directory under the current working directory), so the common case
needs no argument and an unusual layout is still reachable. (A cwd-relative
default also keeps an installed copy — `uvx`/`pipx` — usable: a script-relative
default would point into the install location, e.g. site-packages.)

## Alternatives Considered

- **Query inside the Collector container.** Rejected. It re-couples the durable
  and ephemeral halves into one process and one lifecycle: stopping or upgrading
  the Collector would take the query capability with it, and the captured data
  would be trapped behind the container boundary. It also forces query tooling to
  live in, and be shipped with, the Collector image.
- **A long-running query server / HTTP API.** Rejected. It reintroduces exactly
  what the lightweight goal forbids: a persistent service, a port to bind and
  protect, a process to supervise and restart, and an API surface to version. The
  query workload is bursty and stateless; a standing server is pure overhead for
  it. (A latency argument against an always-on negotiated API surface is made in
  [ADR-002](ADR-002-pep723-uv-single-file-distribution.md).)
- **`docker exec` into a container for each query.** Rejected. It makes every
  query depend on a *running* container, defeating the "data survives
  `docker compose down`" property, and tightly binds the host workflow to Docker's
  presence and to the container's internal toolchain and filesystem. The whole
  point is that the data is reachable *without* a container in the loop.

## Consequences

- **The bind-mount directory layout and the OTLP-JSONL file convention become a
  public contract.** The host path, the per-signal file naming, and the
  size-rotation scheme are no longer an internal Collector detail; any host tool
  may rely on them. This contract is recorded in
  [CONTRACT-telemetry-directory](../contract/CONTRACT-telemetry-directory.md), and
  the Collector-side of the seam (Dockerization and the bind mount) is decided in
  [ADR-004](ADR-004-collector-in-docker-bind-mount.md).
- **The CLI must locate the directory robustly.** A `--dir` flag with a
  sensible cwd-relative default (`<cwd>/telemetry`) is required so the tool works
  with zero configuration in the normal case and remains usable for non-standard
  layouts.
  The behavioral details of the CLI are specified in
  [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md).
- **Re-reading on demand is the read model.** Because the CLI re-attaches to the
  files on every invocation rather than holding them open, the file producer
  (Collector) and the file consumer (CLI) never contend for a handle, and either
  may be started, stopped, or replaced independently. The cost of repeatedly
  re-scanning the corpus is what later motivates the incremental cache in
  [ADR-005](ADR-005-incremental-parquet-cache.md) — an accelerator layered *on*
  this seam, not a change *to* it.
- **The query tool stays a distributable artifact, not a deployment.** Because
  nothing in this path is a service, the tool can be shipped as a single file or
  package and run anywhere the bind-mount path is reachable; its distribution
  form is decided in [ADR-002](ADR-002-pep723-uv-single-file-distribution.md).
