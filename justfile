default:
    @just --list

# ── Collector & query (otelq) ────────────────────────────────────────────────

# Start the dev OTel Collector (OTLP gRPC :4317 / HTTP :4318)
otel-up:
    mkdir -p telemetry
    docker compose --profile otel up -d
    @echo "OTel Collector listening on localhost:4317 (gRPC) / localhost:4318 (HTTP)"

# Quick test with synthetic data — no app needed. Starts the Collector, then runs
# otelgen generators that push ~15s of traces and logs through it, populating
# telemetry/ so otelq (and the query-telemetry skill) can be tried on a fresh clone.
otel-demo:
    mkdir -p telemetry
    docker compose --profile otel up -d
    @echo "Generating ~15s of synthetic traces + logs via otelgen (one-shot)..."
    docker compose --profile demo up
    -docker compose --profile demo rm -fs >/dev/null 2>&1
    @echo "Done. Query it:  just otelq summary  |  just otelq slow  |  just otelq --format json logs"

# Stop the dev OTel Collector (and remove any demo generator containers)
otel-down:
    docker compose --profile otel --profile demo down

# Reset captured telemetry: empty the active files in place, drop rotated
# backups and the otelq parquet cache. The dev Collector keeps
# telemetry/{traces,logs,metrics}.jsonl open; `rm`-ing them while it runs
# orphans those fds — high-volume traces reappear on the next 50MB rotation,
# but low-volume logs/metrics never rotate, so their files never come back and
# `just otelq logs` then reports "no telemetry captured". So: stop, clear
# content in place (keep the inode, never unlink), restart.
[unix]
otel-clean:
    #!/usr/bin/env bash
    set -euo pipefail
    collector=otel-collector
    running=$(docker ps -q -f "name=^${collector}$" 2>/dev/null || true)
    [ -n "$running" ] && docker stop "$collector" >/dev/null
    # Active files: empty in place. Truncating while the Collector holds the fd
    # would leave a multi-KB NUL hole (its file exporter is not O_APPEND — it
    # writes at a tracked offset), which is why the stop above is required.
    for sig in traces logs metrics; do : > "telemetry/${sig}.jsonl"; done
    # Rotated backups (<signal>-<timestamp>.jsonl) are not held open -> remove.
    find telemetry -maxdepth 1 -type f -name '*.jsonl' \
        ! -name traces.jsonl ! -name logs.jsonl ! -name metrics.jsonl -delete
    rm -rf telemetry/.otelq-cache
    [ -n "$running" ] && docker start "$collector" >/dev/null
    echo "Reset telemetry/ (emptied active files in place; removed backups + .otelq-cache)"

# Reset captured telemetry: empty the active files in place, drop rotated
# backups and the otelq parquet cache. See the [unix] variant above for why the
# Collector is stopped first (held-open file exporters; orphaned-fd footgun).
[windows]
otel-clean:
    @$c = "otel-collector"; $running = (docker ps -q -f "name=^$c$"); if ($running) { docker stop $c | Out-Null }; foreach ($sig in 'traces','logs','metrics') { Clear-Content -Path "telemetry/$sig.jsonl" -ErrorAction SilentlyContinue }; Get-ChildItem -Path telemetry -Filter *.jsonl -File | Where-Object { $_.Name -notin 'traces.jsonl','logs.jsonl','metrics.jsonl' } | Remove-Item -Force -ErrorAction SilentlyContinue; Remove-Item -Path "telemetry/.otelq-cache" -Recurse -Force -ErrorAction SilentlyContinue; if ($running) { docker start $c | Out-Null }; Write-Host "Reset telemetry/ (emptied active files in place; removed backups + .otelq-cache)"

# Query captured telemetry, e.g. `just otelq summary`
otelq *ARGS:
    uv run otelq.py {{ARGS}}

# Run an ad-hoc SQL query against captured telemetry
otelq-sql QUERY:
    uv run otelq.py sql "{{QUERY}}"

# Run the otelq test suite
otelq-test:
    uv run --with pytest --with "duckdb==1.5.3" pytest tests/ -v

# Lint the project
lint:
    uvx ruff check .
