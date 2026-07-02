default:
    @just --list

# ── Collector & query (otelq) ────────────────────────────────────────────────

# Start the dev OTel Collector (OTLP gRPC :4317 / HTTP :4318)
otel-up:
    mkdir -p .telemetry
    docker compose --profile otel up -d
    @echo "OTel Collector listening on localhost:4317 (gRPC) / localhost:4318 (HTTP)"

# Quick test with synthetic data — no app needed. Starts the Collector (with NO
# published host ports, via compose.demo.yaml, so it never collides with another
# collector on 4317), then runs telemetrygen generators that push ~15s of a
# nuanced mix — fast + slow (>1s) traces, metrics, and logs at all six severity
# levels (TRACE/DEBUG/INFO/WARN/ERROR/FATAL) — through it over the Docker network,
# populating .telemetry/ so otelq (and the otelq skill) can be tried on a fresh
# clone with every view non-empty.
otel-demo:
    mkdir -p .telemetry
    docker compose -f compose.yaml -f compose.demo.yaml --profile otel up -d
    @echo "Generating ~15s of synthetic telemetry via telemetrygen (fast+slow traces, metrics, all six log levels; one-shot)..."
    docker compose -f compose.yaml -f compose.demo.yaml --profile demo up
    -docker compose -f compose.yaml -f compose.demo.yaml --profile demo rm -fs >/dev/null 2>&1
    @echo "Waiting for the Collector to flush its final batch (5s batch timeout)..."
    sleep 7
    @echo "Done. Query it:  just otelq summary  |  just otelq slow  |  just otelq errors  |  just otelq metric gen"

# Stop the dev OTel Collector — both the standalone project and the demo project.
otel-down:
    -docker compose --profile otel down
    -docker compose -f compose.yaml -f compose.demo.yaml --profile otel --profile demo down

# Reset captured telemetry: empty the active files in place, drop rotated
# backups and the otelq parquet cache. The dev Collector keeps
# .telemetry/{traces,logs,metrics}.jsonl open; `rm`-ing them while it runs
# orphans those fds — high-volume traces reappear on the next 50MB rotation,
# but low-volume logs/metrics never rotate, so their files never come back and
# `just otelq logs` then reports "no telemetry captured". So: stop, clear
# content in place (keep the inode, never unlink), restart.
[unix]
otel-clean:
    #!/usr/bin/env bash
    set -euo pipefail
    # Stop EVERY collector that may hold .telemetry/*.jsonl open: the standalone
    # `just otel-up` container (otel-collector) AND the demo container
    # (otelq-demo-collector, from compose.demo.yaml). Truncating a file a live
    # collector still holds open leaves an orphaned fd / NUL hole (see below), so
    # any running collector for this dir must be stopped first.
    running=$(docker ps -q -f "name=^otel-collector$" -f "name=^otelq-demo-collector$" 2>/dev/null || true)
    [ -n "$running" ] && docker stop $running >/dev/null
    # Active files: empty in place. Truncating while the Collector holds the fd
    # would leave a multi-KB NUL hole (its file exporter is not O_APPEND — it
    # writes at a tracked offset), which is why the stop above is required.
    for sig in traces logs metrics; do : > ".telemetry/${sig}.jsonl"; done
    # Rotated backups (<signal>-<timestamp>.jsonl) are not held open -> remove.
    find .telemetry -maxdepth 1 -type f -name '*.jsonl' \
        ! -name traces.jsonl ! -name logs.jsonl ! -name metrics.jsonl -delete
    rm -rf .telemetry/.otelq-cache
    [ -n "$running" ] && docker start $running >/dev/null
    echo "Reset .telemetry/ (emptied active files in place; removed backups + .otelq-cache)"

# Reset captured telemetry: empty the active files in place, drop rotated
# backups and the otelq parquet cache. See the [unix] variant above for why the
# Collector is stopped first (held-open file exporters; orphaned-fd footgun).
[windows]
otel-clean:
    @$running = (docker ps -q -f "name=^otel-collector$" -f "name=^otelq-demo-collector$"); if ($running) { docker stop $running | Out-Null }; foreach ($sig in 'traces','logs','metrics') { Clear-Content -Path ".telemetry/$sig.jsonl" -ErrorAction SilentlyContinue }; Get-ChildItem -Path .telemetry -Filter *.jsonl -File | Where-Object { $_.Name -notin 'traces.jsonl','logs.jsonl','metrics.jsonl' } | Remove-Item -Force -ErrorAction SilentlyContinue; Remove-Item -Path ".telemetry/.otelq-cache" -Recurse -Force -ErrorAction SilentlyContinue; if ($running) { docker start $running | Out-Null }; Write-Host "Reset .telemetry/ (emptied active files in place; removed backups + .otelq-cache)"

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
