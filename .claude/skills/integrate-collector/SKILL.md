---
name: integrate-collector
description: "Run from the otelq repo to wire otelq into your project on the same host: add otelq's file-export pipeline to your project's existing OpenTelemetry Collector (or scaffold one) so otelq can read its telemetry, then verify the wiring. Asks for the target project's absolute path first."
allowed-tools: Bash, Read, Edit, Write
user-invocable: true
---

# Integrate otelq with your project's Collector

The canonical, authoritative instructions live in `.agents/skills/integrate-collector/SKILL.md`. **Read that file and follow it exactly.** This Claude Code entry is only a pointer — all guidance is maintained in the canonical file so the two cannot drift.
