---
name: target-project-setup
description: "Run from the otelq repo to wire otelq into your project on the same host: add otelq's file-export pipeline to your project's existing OpenTelemetry Collector (or scaffold one) so otelq can read its telemetry, verify the wiring, and install the otelq query skill into the target so its AI agent can drive the otelq CLI. Asks for the target project's absolute path first."
allowed-tools: Bash, Read, Edit, Write
user-invocable: true
---

# Integrate otelq with your project's Collector

The canonical, authoritative instructions live in `.agents/skills/target-project-setup/SKILL.md`. **Read that file and follow it exactly.** This Claude Code entry is only a pointer — all guidance is maintained in the canonical file so the two cannot drift.
