---
name: otelq
description: "Capture and query local OpenTelemetry traces, logs, and metrics from the dev Collector with the otelq CLI."
allowed-tools: Bash, Read
user-invocable: true
---

# Query Telemetry

First, read the canonical instructions at `.agents/skills/otelq/SKILL.md` and follow them exactly.

This skill captures OTEL traces, logs, and metrics from the local dev Collector and queries them with the otelq CLI to verify or debug system behaviour.

Note: `--dir` is optional — otelq resolves the store itself ($OTELQ_DIR, else
the nearest `.telemetry/` at or above the cwd), and from a linked git worktree it
reads the main checkout's store automatically, since all worktrees share that one
Collector output path. Every response header reports which store it read and why.
