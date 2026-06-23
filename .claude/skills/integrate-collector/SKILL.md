---
name: integrate-collector
description: "Run from the otelq repo to wire otelq into ANOTHER project on the same host: add otelq's file-export pipeline to that project's existing OpenTelemetry Collector so otelq can read its telemetry. Asks for the target project's absolute path first."
allowed-tools: Bash, Read, Edit
user-invocable: true
---

# Integrate otelq with another project's Collector

First, read the canonical instructions at `.agents/skills/integrate-collector/SKILL.md` and follow them exactly.

**Direction:** this skill runs from the otelq repo and operates on a **different target project elsewhere on the same host**. Before doing anything, **ask the user for the absolute path to that target project**, then do all file reads/edits under that path — never edit the otelq repo for this task. The only otelq-repo command is `otelq collector-config`, which prints the fragment to paste into the target. Verify with `otelq --dir <target>/telemetry doctor`.

**Never add otelgen or the `demo` profile to the target project** — the synthetic-telemetry demo (`just otel-demo`) lives only in the otelq repo. Into the target you add only the `file/*` exporters, pipeline wiring, and bind mount.
