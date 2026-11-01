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
created: 2026-11-01
last_updated: 2026-11-01
related_documents:
  - ADR-016-cli-carries-versioned-agent-skill
  - ADR-007-dual-collector-standalone-and-integrated
  - ADR-002-pep723-uv-single-file-distribution
  - SPEC-otelq-cli
  - PRD-otelq
supersedes: null
superseded_by: null
retrieval_priority: high
ai_summary: "Project setup becomes a user-invoked mode of the otelq skill whose guidance the pinned CLI prints; the separate setup skill and the plugin are retired."
semantic_tags:
  - otelq
  - agent-skill
  - project-setup
  - collector
  - versioning
  - consent
  - plugin
---

# ADR-018 — Project Setup Ships Inside the otelq Skill

## Context

[ADR-016](ADR-016-cli-carries-versioned-agent-skill.md) made the otelq package
the single versioned artifact for the query skill, but left the **setup skill**
— the human-run flow that wires a project's Collector for otelq — as a separate
`SKILL.md` in the otelq repository, reached through a clone or a Claude Code
plugin. That leaves two artifacts that move independently of the CLI a project
actually runs:

- The setup skill a user loads is whatever the clone or the plugin cache holds,
  not the version the project's query skill pins. Setup guidance and the
  Collector contract it targets can therefore disagree with the CLI the project
  uses.
- The setup runs from *outside* the target project and has to be told its path.
  Most of its safety text exists only to stop the agent from editing the otelq
  clone it was loaded from.
- The plugin exists only to carry the setup skill. It reaches one vendor's
  agents and adds a third version number.

Meanwhile the query skill is already in the target project, pinned to the CLI,
and installed by one command.

## Decision

1. **Project setup is a mode of the otelq skill.** The query skill gains a
   `project-setup` mode that a user enters explicitly (in Claude Code,
   `/otelq project-setup`). Setup runs from inside the project being wired,
   which is identified as the project the agent is working in and confirmed
   with the user, not passed as a path into another repository.
2. **The setup guidance is embedded in the CLI and printed on demand.** The
   query skill does not carry the setup text; it tells the agent to obtain it
   from the version-pinned CLI. The guidance a project receives is therefore,
   by construction, the guidance for the otelq version that project pins, and
   the per-query context cost of the skill does not grow.
3. **Setup stays a consented, user-invoked operation.** Because the mode now
   lives in a skill the agent may load on its own, the guarantee ADR-016 drew
   from a non-model-invocable setup skill is carried by the skill text and the
   guidance: the agent enters setup only on the user's explicit request, explains
   the proposed setup, and changes nothing until the user explicitly approves the
   plan or amends it.
4. **Installing the skill advertises setup.** The command that installs or
   refreshes the skill tells the user that project setup is available and how to
   invoke it, so the one install command is the whole onboarding entry point.
5. **The separate setup skill and the Claude Code plugin are retired.** ADR-016
   decision 6 allowed a plugin manifest only as packaging for the setup skill;
   with that skill gone there is nothing for it to package.

This amends ADR-016 decision 6 and its closing statement that the setup skill
stays a separate, non-model-invocable skill. All other ADR-016 decisions stand.
It also amends the location clause of
[ADR-007](ADR-007-dual-collector-standalone-and-integrated.md): the integration
still operates on the target project and otelq still never manages that
project's Collector, but the setup now runs from within it.

## Alternatives Considered

- **Inline the setup text in the query skill.** Rejected. The query skill is
  loaded for every telemetry question; several hundred lines of one-time setup
  would cost context on every query and put Collector-editing instructions in
  front of an agent that only wanted to read a trace.
- **Install the setup text as a second file beside the query skill.** Rejected.
  It is one more committed file per project that can be edited or drift and
  that `doctor` would have to check, for text that is needed rarely and is
  always available from the pinned CLI.
- **Keep the separate setup skill and the plugin (status quo).** Rejected. It
  keeps setup guidance out of step with the CLI version a project runs, needs a
  clone or a vendor plugin to adopt, and requires the setup to address the
  project from outside.
- **Keep a minimal plugin that only bootstraps the skill.** Rejected. The
  bootstrap is already one `uvx` command that works for every agent; a plugin
  would add a vendor-specific path and a version number to do the same thing.

## Consequences

- **Onboarding is one command, then one invocation.** Installing the skill in a
  project, then entering `project-setup` from that project's agent, replaces
  cloning otelq or installing a plugin. Re-running the install with a newer CLI
  moves the CLI pin, the query skill, and the setup guidance together.
- **The consent guarantee is textual, not mechanical.** No agent setting stops
  the model from loading the otelq skill, so the explicit-approval rule in the
  skill and the guidance is the safeguard. Setup still edits files and restarts
  the project's Collector only after the user approves.
- **Agents without argument passing** enter the mode when the user asks for
  otelq project setup by name; the guidance is identical.
- **Existing plugin installs stop receiving updates** and should be removed by
  the user; the repository no longer publishes a marketplace entry.
- **`otelq.py` carries the setup guidance as well as the query skill.** The
  single-file property of ADR-002 is preserved; release tooling no longer owns a
  separate setup file or plugin manifest.
- **Behavior is specified in [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md)** (the
  `skill print project-setup` form, the install hint, and the skill's
  project-setup mode).
