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
created: 2026-10-25
last_updated: 2026-10-25
related_documents:
  - ADR-002-pep723-uv-single-file-distribution
  - ADR-007-dual-collector-standalone-and-integrated
  - ADR-012-exit-codes-as-public-contract
  - SPEC-otelq-cli
  - PRD-otelq
supersedes: null
superseded_by: null
retrieval_priority: high
ai_summary: "The otelq package is the single versioned delivery artifact: it embeds, installs, version-pins and verifies its own agent skill; plugin manifests are derived packaging."
semantic_tags:
  - otelq
  - distribution
  - agent-skill
  - skill-md
  - versioning
  - release
  - plugin
  - uvx
---

# ADR-016 — The CLI Carries Its Own Versioned Agent Skill

## Context

[ADR-002](ADR-002-pep723-uv-single-file-distribution.md) made otelq a single
file distributed through PyPI and stated that "the agent integration is a thin
skill that calls the CLI". It did not decide how that skill reaches a project,
or how the skill and the CLI stay in step. In practice otelq became three
artifacts that move independently:

- **The CLI**, run as `uvx otelq`. Unpinned, `uvx` resolves the newest release
  on first use and then reuses that cached environment indefinitely, so the
  version a machine runs is whatever happened to be fetched first.
- **The query skill**, a `SKILL.md` that teaches an agent the commands and the
  investigation loop. The setup flow copies it from a clone of the otelq
  repository into the target project, where it is frozen at the version of that
  clone.
- **The setup skill**, run by a human to wire a project's Collector and install
  the query skill.

Nothing ties these together. A skill can document flags the cached CLI does not
have, or omit commands it does have, and neither the agent nor the user is told.
Adopting otelq also requires cloning its repository even though the CLI itself
needs no clone.

The surrounding ecosystem constrains the choice:

- **The skill format is standardized; its packaging is not.** The Agent Skills
  specification defines only the per-skill folder (`SKILL.md` with `name` and
  `description`). Where agents discover skills is per vendor: Codex, Cursor,
  Gemini CLI and Copilot read a project's `.agents/skills/`, while Claude Code
  reads `.claude/skills/`.
- **Plugin formats are vendor-specific bundles.** A Claude Code plugin reaches
  only Claude Code surfaces, auto-update is off by default for third-party
  marketplaces, and a project-level enablement does not install the plugin for
  teammates. Codex has a separate, similar format.
- **Skill registries are indexes, not package managers.** They copy `SKILL.md`
  folders from a git repository and carry no version handshake with the tool the
  skill drives.

## Decision

**The otelq package is the single versioned delivery artifact for everything an
agent needs.** The skill is part of the CLI's release, not a sibling of it.

1. **The canonical query-skill text is embedded in the CLI.** The version of the
   skill a project receives is, by construction, the version of the CLI that
   installed it. The repository's own skill files are generated from that
   embedded text and must be identical to it.
2. **The skill pins the CLI.** Every otelq invocation the skill teaches names the
   exact otelq version, and the skill carries that version as a machine-readable
   marker. A project's skill therefore runs the CLI it was written for,
   independent of any machine's `uvx` cache or globally installed copy.
3. **The CLI installs and refreshes the skill.** Installing into a project, and
   upgrading later, is one otelq invocation with no clone of the otelq
   repository. Installation is idempotent and writes the cross-agent
   `.agents/skills/` location plus a thin Claude Code pointer.
4. **Drift is reported, never silent.** `doctor` reports when a project's
   installed skill names a different version than the running CLI, or differs
   from the canonical text for its version. This is a diagnostic, not a verdict:
   it never changes `doctor`'s exit code
   ([ADR-012](ADR-012-exit-codes-as-public-contract.md)).
5. **One version number, moved by release automation.** A release bumps the CLI
   version, the version pins inside the repository's skill files, and any plugin
   manifest together. Tests fail when any of them disagree.
6. **Plugin manifests are derived packaging.** A plugin manifest may exist as a
   convenience entry point, but it is never a source of truth for skill content
   or version, and it exposes only the human-run setup skill. The query skill
   belongs in the target project, where every teammate and every agent receives
   it with the repository.

The setup skill stays human-invoked. It is a one-time, consented operation on
another project ([ADR-007](ADR-007-dual-collector-standalone-and-integrated.md)),
so an agent must not load it on its own initiative.

## Alternatives Considered

- **A Claude Code plugin as the primary channel.** Rejected. It reaches one
  vendor's agents, needs a per-user install even when enabled in project
  settings, updates only on request for third-party marketplaces, and adds a
  third version number without solving the CLI pin. It offers nothing otelq
  needs today: otelq has no hooks, servers or bundled executables to ship.
- **Skill registries and their installers as the primary channel.** Rejected as
  primary, kept as compatible. A registry copy is frozen at install time and has
  no way to learn the version of the CLI it drives. The repository layout stays
  discoverable by such installers, so they remain a valid way to obtain the
  setup skill.
- **Status quo: copy the skill from a clone.** Rejected. It requires a clone to
  adopt, leaves the CLI version to the `uvx` cache, and gives no signal when the
  copy goes stale.
- **Ship the skill as a separate data file in the wheel.** Rejected. It would
  break the single-file form of
  [ADR-002](ADR-002-pep723-uv-single-file-distribution.md), where `otelq.py` run
  from a URL or a checkout must behave exactly like the installed package.

## Consequences

- **`otelq.py` carries several hundred lines of skill text.** The single-file
  property of ADR-002 is preserved, at the cost of a larger file. The skill text
  is edited in `otelq.py`; the repository's skill files are regenerated from it.
- **Upgrading is explicit and atomic.** A project moves to a new otelq by
  re-running the install with the newer CLI, which updates the skill and its pin
  together. Until then it keeps running the version it was written for.
- **The CLI gains a command that writes into a project.** It writes only the two
  skill files, and only under a target directory the caller names. A write that
  cannot complete is a failure to answer under ADR-012, never a partial success.
- **Release automation owns more files.** The release tooling rewrites the
  version pins inside the skill files and the plugin manifest; tests guarding
  that every pin equals the CLI version are the backstop when a pin is missed.
- **The setup flow no longer depends on a clone for the skill step.** Other
  setup steps that copy reference files keep a version-pinned source.
- **Behavior is specified in
  [SPEC-otelq-cli](../spec/SPEC-otelq-cli.md)** (the `skill` command and the
  `doctor` skill check); this ADR governs only how the skill is delivered and
  versioned.
