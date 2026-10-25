---
name: target-project-setup
description: "Wire otelq into a project on this host: add otelq's file-export pipeline to the project's OpenTelemetry Collector (or scaffold one), verify the wiring, and install the version-pinned otelq query skill. Asks for the target project's absolute path first."
allowed-tools: Bash, Read, Edit, Write
disable-model-invocation: true
---

# Integrate otelq with your project's Collector

The canonical, authoritative instructions live in the otelq repository at
`.agents/skills/target-project-setup/SKILL.md`. **Read that file and follow it
exactly.**

- Loaded from the otelq Claude Code plugin, the repository root is
  `${CLAUDE_PLUGIN_ROOT}`, so read
  `${CLAUDE_PLUGIN_ROOT}/.agents/skills/target-project-setup/SKILL.md`.
- Loaded from a clone of the otelq repository, the path is relative to the
  clone's root.

This Claude Code entry is only a pointer, so the two cannot drift. It is
human-invoked only: it edits another project, so the agent never loads it on
its own initiative.
