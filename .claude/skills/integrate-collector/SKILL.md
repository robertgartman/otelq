---
name: integrate-collector
description: "Run from the otelq repo to wire otelq into your project on the same host: add otelq's file-export pipeline to your project's existing OpenTelemetry Collector (or scaffold one) so otelq can read its telemetry, then verify the wiring. Asks for the target project's absolute path first."
allowed-tools: Bash, Read, Edit, Write
user-invocable: true
---

# Integrate otelq with your project's Collector

First, read the canonical instructions at `.agents/skills/integrate-collector/SKILL.md` and follow them exactly. The summary below is only an index.

**Direction:** this skill runs from the otelq repo and operates on a **different target project elsewhere on the same host**. Before doing anything, **ask the user for the absolute path to that target project**, then do all file reads/edits under that path — never edit the otelq repo for this task. Run otelq via `uvx` — `otelq …` is shorthand for `uvx --from git+https://github.com/robertgartman/otelq otelq …` (no clone, no install). The otelq commands you run are `otelq collector-config` (prints the file-export fragment) and the read-only `otelq --dir <target>/telemetry doctor` / `summary` checks.

**Flow:** preflight (uv / docker / git) → detect the Collector → **Path A** tee the `file/*` exporters into an existing Collector, or **Path B** scaffold one the target owns → `mkdir` + bind-mount `./telemetry` → gitignore it → present the plan and confirm → verify.

**Do not leave `telemetrygen` or the `demo` profile in the target project.** The verify step *may* use a synthetic `telemetrygen` probe, but only as a **temporary, ephemeral-to-be-reverted** Compose service that runs on the Collector's network and is removed afterward — never a permanent fixture. Before running it, check the teed pipelines' exporters: if any real backend (`otlp`/`otlphttp`/`prometheus`/…) shares the pipeline, the synthetic data reaches it too — warn the user. Prefer verifying with the target's own app traffic when that is easy.
