---
name: target-project-setup
description: "Use from the otelq repo to idempotently wire otelq into your project on the same host — adds otelq's file-export pipeline to your project's existing OpenTelemetry Collector (or scaffolds one) only when missing or drifted, verifies the wiring, and installs the otelq query skill into the target so its AI agent can drive the otelq CLI. Asks for the target project's absolute path."
---

# Integrate otelq with your project's Collector

**Direction (important):** this skill runs **from the otelq repository** and
operates on a **different target project elsewhere on the same host**. otelq is
the tool; the *target project* is where the integration happens. You are not
integrating a Collector into otelq — you are adding otelq's file-export pipeline
to the **target project's** Collector so otelq can read what that project emits.

**Before doing anything, ask the user for the absolute path to the target
project** (the project whose Collector should be wired up), e.g.
`/Users/me/dev/my-service`. Call it `$TARGET`. Every file you **edit** in the steps
below lives under `$TARGET` — **never** edit files inside the otelq repo for this
task. (One step *reads* a file from the otelq repo — the canonical query skill — and
copies it into `$TARGET`; reading the otelq repo is fine, editing it is not.) The
only otelq command you run is `otelq collector-config` (and the read-only `doctor` /
`summary` checks).

otelq reads OTLP signals as JSONL from a shared `telemetry/` directory — that
directory is the entire contract (see
`context/contract/CONTRACT-telemetry-directory.md`). Any Collector that writes the
right files works; otelq does not need to own the Collector.

**Idempotence requirement:** treat this skill as safe to run repeatedly. If
`$TARGET` is already compliant with these setup instructions, the correct result
is **no file edits, no duplicate config entries, no new commit, and no Collector
restart**. Inspect first, compare against the canonical otelq fragments/skill, and
only change files that are missing required setup or have drifted from the current
contract. When everything already matches, report that the target is already wired
for otelq and proceed only to optional read-only verification (`doctor` /
`summary`) if the user wants it.

There are **two cases**, both covered here:

- **Integrated (the normal case)** — `$TARGET` already runs a Collector as part of
  its architecture. Add otelq's three `file/<signal>` exporters to *that* Collector
  (Path A). otelq never starts, stops, or cleans it.
- **No Collector yet** — `$TARGET` has no Collector. Scaffold a minimal one that
  `$TARGET` owns, modelled on otelq's reference config (Path B). (If the user only
  wants to *try* otelq with no app at all, the bundled standalone demo in the otelq
  repo — `just otel-demo` — is simpler; this skill is for wiring a real project.)

In integrated mode, **otelq never manages `$TARGET`'s Collector** — it does not
start, stop, or clean it. otelq only reads the telemetry dir and owns the
`.otelq-cache/` subtree. Do **not** run `just otel-up` / `just otel-clean` against
an integrated setup (those target otelq's *bundled* Collector and `otel-clean`
truncates the JSONL files).

## Running otelq

Throughout this skill, **`otelq …`** is shorthand for running the CLI straight
from PyPI with `uvx` — no clone, no global install:

```
uvx otelq …
```

The first run downloads otelq and fetches the DuckDB `otlp` community extension
(network once, then cached). For the `doctor` / `summary` checks below, pass
`--dir $TARGET/telemetry` so otelq reads the target project's output folder. To
pin a version: `uvx otelq@0.1.0 …`.

## Step 0 — Ask for `$TARGET`, secure its working tree, then preflight

1. **Get `$TARGET`** (absolute path) and confirm it exists.

2. **Secure the target's working tree before editing anything.** This skill changes
   files in `$TARGET`, adds a commit, runs a verify probe, and reverts it, so start
   from a known git state. Inspect it first:

   ```sh
   git -C "$TARGET" rev-parse --is-inside-work-tree   # is it a git repo at all?
   git -C "$TARGET" branch --show-current             # current branch
   git -C "$TARGET" status --porcelain                # any uncommitted changes?
   ```

   Then **advise the user and act on their choice before progressing**:
   - **Uncommitted changes present** → advise **committing (or stashing) them first**,
     so their in-flight work is saved and the tree is clean before otelq touches
     anything — otherwise your edits and the verify probe entangle with theirs.
   - **Either way** → recommend doing the integration on a **dedicated branch**
   (e.g. `target-project-setup`) rather than on their working branch, so the whole
     change set — including the probe's commit and revert — stays isolated and is
     trivial to review or drop.
   - **Not a git repo** → say so: the commit/revert verify model needs git. Offer to
     `git init`, or proceed only if the user accepts undoing the probe by hand.

   **Do not edit any file in `$TARGET` until the user has committed/stashed and/or
   switched branch as they prefer** (or has explicitly chosen to proceed on the
   current branch as-is).

3. **Preflight the host** — fail fast with a fix if any is missing:
   - `uv` / `uvx` on PATH (`uv --version`) — runs otelq.
   - `docker` and `docker compose` working (`docker compose version`).

4. **Detect the Collector.** Search `$TARGET` for a Compose service using
   `otel/opentelemetry-collector*` (look in `compose.yaml`, `compose.yml`,
   `docker-compose.yml`, and any `-f`-referenced overlays).
   - Found → **Path A** (integrate).
   - None found → **Path B** (scaffold), after confirming with the user that
     `$TARGET` really has no Collector (it may run one outside Compose — k8s, a
     binary, a managed/agent Collector; in that case add the `file/*` exporters and
     a host-mounted dir to *that* Collector by hand using `otelq collector-config`
     as the reference, and skip Compose-specific steps).

---

## Path A — Integrate into an existing Collector

1. **Locate the Collector service** and the config file it mounts (its `--config`
   path, e.g. `otel-collector.yaml`).

2. **Ensure the image is `-contrib`.** The `file` exporter exists only in
   `otel/opentelemetry-collector-contrib`, **not** the core
   `otel/opentelemetry-collector` image. If `$TARGET` runs core, switch its service
   to the `-contrib` image at the same tag — otherwise the config below fails to
   start.

3. **Get the canonical fragment** (run from the otelq repo):

   ```sh
   otelq collector-config
   ```

   This prints the exact `file/*` exporters (with the pinned rotation settings) and
   the pipeline wiring. It is generated from otelq's own constants, so it always
   matches the contract — prefer it over hand-writing the config.

4. **Reconcile any existing `file/*` exporters — otelq owns these definitions.**
   Before merging, check whether `$TARGET`'s config **already defines** `file/traces`,
   `file/logs`, or `file/metrics` exporter blocks (a prior integration, a hand-rolled
   setup, or an older otelq version may have left them). If so, **diff each existing
   block against the canonical fragment from step 3** — the `path`, `flush_interval`,
   and `rotation` (`max_megabytes` / `max_backups`) settings in particular.
   - **Identical** → leave it; nothing to do for that exporter.
   - **Differs in any field** → **replace the whole exporter block with otelq's
     canonical version.** otelq is the master for these definitions: the rotation
     and path settings are generated from otelq's own constants and the contract
     depends on them (e.g. `send_batch_max_size`/rotation interplay, the exact
     `/telemetry/<signal>.jsonl` paths otelq reads). A stale local copy that merely
     *looks* wired up can silently break `doctor`/queries, so do not preserve the
     target's drifted values — overwrite them.

   This reconcile is about the exporter **definitions** only. Pipeline membership is
   still additive (next step) — never drop the target's *other* exporters.

5. **Merge it — tee, don't replace.** Ensure the three `file/*` exporters exist
   (added fresh, or reconciled per step 4), then **append** `file/traces` /
   `file/logs` / `file/metrics` to the **existing**
   `service.pipelines.<signal>.exporters` lists. Keep every exporter that is
   already there:

   ```yaml
   exporters: [otlphttp/their-backend, file/traces]   # add file/traces; keep the rest
   ```

   A Collector pipeline fans every received item out to **all** of its exporters,
   so this tee is what makes otelq see the data — and is also why the verify probe
   needs the blast-radius check below.

6. **Create the host telemetry dir and bind-mount it** into the Collector service:

   ```sh
   mkdir -p "$TARGET/telemetry"          # create it first, or Docker makes it root-owned
   ```
   ```yaml
   volumes:
     - ./telemetry:/telemetry            # host dir : the contract mount path
   ```

7. **Gitignore the captured telemetry** in `$TARGET` (mirror otelq's own rule), so
   neither the Collector output nor otelq's cache is committed:

   ```gitignore
   # otelq: local telemetry capture + query cache — never committed
   /telemetry/*
   !/telemetry/.gitkeep
   ```

If every exporter definition, pipeline membership entry, bind mount, telemetry
directory, and gitignore rule already matches this section, make no edits for Path
A and record the integration as already compliant. Then go to **Present the plan**.

---

## Path B — Scaffold a Collector `$TARGET` owns

`$TARGET` has no Collector. Stand up a minimal one **owned by `$TARGET`** (not by
otelq), modelled on otelq's reference producer:

1. Copy otelq's `otel-collector-dev.yaml` into `$TARGET` as the starting Collector
   config (it is the contract-locked reference: OTLP in → three `file/*` exporters
   with the pinned rotation). Drop the `debug` exporter if the project does not want
   console spam.
2. Add a Collector service to `$TARGET`'s Compose (create `compose.yaml` if absent),
   using the `otel/opentelemetry-collector-contrib` image, mounting that config and
   `./telemetry:/telemetry`, and publishing the OTLP ports the app needs
   (`4317`/`4318`). `otelq collector-config` documents the same exporters/mount for
   cross-reference.
3. `mkdir -p "$TARGET/telemetry"` and add the **same gitignore** rule as Path A
   step 7.

If `$TARGET` already has this scaffold with the canonical config, service, mount,
telemetry directory, and gitignore rule, make no edits for Path B and record the
scaffold as already compliant.

The app then points `OTEL_EXPORTER_OTLP_ENDPOINT` at this Collector. Because this
Collector is `$TARGET`'s, `$TARGET` owns its lifecycle — otelq still only reads the
directory.

---

## Present the plan, then apply

Before editing, **show the user the concrete changes** (which files, the exporter +
pipeline edits, the bind mount, the gitignore, and the otelq query skill copied in —
see below) and ask whether to **apply
step-by-step (confirm each)** or **run to the end**. If the inspection found no
changes because `$TARGET` is already compliant, say so explicitly and skip the
apply/restart path. Otherwise, make the edits and have `$TARGET`'s Collector
**(re)start to load the new config** — a one-time, consented setup action on the
project's own service. This does not change the lifecycle boundary: otelq's
*runtime* (the CLI) still never starts, stops, or cleans that Collector.

## Verify the wiring

Offer the user two ways to prove telemetry actually lands:

- **(a) Their own app** — exercise `$TARGET`'s app so it emits real telemetry. No
  synthetic data, nothing to revert. Prefer this when it is easy.
- **(b) Synthetic probe** — a throwaway `telemetrygen` run, useful when running the
  app is inconvenient. It is committed-then-reverted so it leaves nothing *committed*
  in `$TARGET` (the only residue is gitignored synthetic capture you can clear or
  filter). Use the safe sequence below.

Either way, the check is:

```sh
otelq --dir "$TARGET/telemetry" doctor      # exit 0 + OK rows = wiring is good
otelq --dir "$TARGET/telemetry" summary     # see counts per signal
```

`doctor` is read-only — it only validates files already on disk (files present,
valid OTLP/JSON, correct signal per file); it never generates data. On an **empty**
dir it exits non-zero ("no `*.jsonl` found"), which is expected until something
emits.

### Synthetic probe (option b) — commit → probe → revert

1. **Commit the integration first**, so it is saved before anything temporary is
   added (confirm with the user — this writes to `$TARGET`'s history):

   ```sh
   git -C "$TARGET" add -A && git -C "$TARGET" commit -m "Integrate otelq file-export pipeline"
   ```

2. **Check the blast radius.** Look at the exporter list of each pipeline you teed
   into (from the merged config you just wrote):
   - Pipeline exports to **only `file/*`** (plus `debug`/`logging`) → the probe is
     isolated; synthetic data reaches nothing but the files. Run it freely.
   - Pipeline **also has real exporters** (`otlp`, `otlphttp`, `prometheus`, …) →
     **warn the user**: because the pipeline fans out to all exporters, the
     synthetic spans/metrics **will also be shipped to those backends** for the
     duration of the probe. Recommend running the probe only against a dev/staging
     Collector, and keep the distinctive `--service otelq-probe` tag (below) so the
     data is easy to identify and filter downstream. If that is not acceptable, use
     option (a) instead.

3. **Add a temporary `telemetrygen` service to `$TARGET`'s Compose** (an
   *uncommitted* edit on top of the commit). It runs **inside the same Compose
   project network as the Collector** and reaches it **by service name** — no host
   ports required (this mirrors otelq's `otel-demo`). Replace `<collector-service>`
   with the Collector's actual service name in `$TARGET`:

   ```yaml
   telemetrygen-probe:
     # same pinned image otelq's compose.yaml uses
     image: ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen@sha256:6bb6a56e325b8e8a600a9d6a5acb7e08a96cfefac08ddab052af540633abce77
     restart: "no"        # one-shot: emit, then exit
     command:
       ["traces", "--otlp-endpoint", "<collector-service>:4317", "--otlp-insecure",
        "--duration", "15s", "--rate", "5", "--service", "otelq-probe"]
   ```

   Repeat the service for `metrics` and `logs` (swap the first arg) to cover all
   three signals, exactly as the demo does.

4. **Run the probe** (Collector already running from the restart above):

   ```sh
   docker compose -f <compose-file> up telemetrygen-probe        # + the metrics/logs ones
   sleep 7                                                       # let the Collector flush its batch
   ```

5. **Verify** with the `doctor` + `summary` commands above, and show the user a real
   query result, e.g. `otelq --dir "$TARGET/telemetry" slow --top 5`. Synthetic rows
   carry `service.name = otelq-probe`.

6. **Revert the probe** — drop the temporary service so `$TARGET` is back at the
   integration commit:

   ```sh
   git -C "$TARGET" checkout -- <compose-file>   # remove the telemetrygen-probe service
   rm -rf "$TARGET/telemetry/.otelq-cache"        # otelq's own cache — safe to delete, not held open
   ```

   The synthetic capture left in `telemetry/*.jsonl` is tagged
   `service.name = otelq-probe` and is gitignored, so it is never committed and is
   trivial to filter out. **Do not truncate those active files while the Collector
   is running** — its `file` exporter writes at a tracked offset (not `O_APPEND`),
   so truncating under it leaves a corrupt NUL hole (the hazard `just otel-clean`
   avoids by stopping the Collector first). To actually clear it, let `$TARGET`'s
   owner restart its Collector and empty the files while it is down — otelq must not
   stop a Collector it does not own.

## Install the otelq query skill into `$TARGET`

Wiring the Collector only gets telemetry onto disk. For `$TARGET`'s AI coding agent
to actually *use* otelq — to know the `otelq` commands and the query loop — the
target project needs the **otelq query skill** as well. Without it the CLI is wired
up but the agent has no instructions for it, so the `otelq` command is effectively
dead weight in that project. Install a **verbatim copy** of this repo's canonical
query skill, `.agents/skills/otelq/SKILL.md`, into `$TARGET`.

This is the one step that **reads** files inside the otelq repo (the canonical
skill and its Claude Code shim) and **writes** them into `$TARGET` — it still never
*edits* anything in the otelq repo, and otelq stays the master for the skill's
content.

This skill is **agnostic about which AI coding agent `$TARGET` uses**, and that is
exactly why placing the file on disk is *not* the finish line: each agent discovers
skills in a different place, so a skill that is present but in a directory the
agent never scans is invisible — wired-up-but-dead, the same failure mode as a
wired Collector with no skill at all. So drive this step from the agent, and treat
**"the agent can actually load it"** as the acceptance criterion (verified below),
not "the file exists."

1. **Ask the user which AI coding agent `$TARGET` uses** (Claude Code, Cursor,
   Windsurf, Copilot, Codex, another, or several) — *then derive the skills
   architecture from that answer*, rather than only asking for a path. From the
   otelq repo we cannot know the target's agent, and the right install layout
   depends entirely on it.

2. **Pick the skills architecture for that agent.** otelq is the master for the
   skill *content*; how it is surfaced is per-agent:
   - **The canonical body always lives at `.agents/skills/otelq/SKILL.md`** — the
     cross-agent `.agents/skills` standard, project-scoped inside `$TARGET` so it
     travels with the repo. Install it there regardless of agent; it is the single
     source of truth a shim can point at.
   - **If the chosen agent reads `.agents/skills` natively**, that one file is
     enough — no shim.
   - **If the chosen agent has its own skills dir** (e.g. **Claude Code →
     `.claude/skills/otelq/SKILL.md`**), it will **not** see `.agents/skills`, so
     also install a **thin pointer shim** in the agent's own dir whose body just
     says *"read the canonical instructions at `.agents/skills/otelq/SKILL.md` and
     follow them exactly."* This is the pattern the otelq repo uses on itself: see
     `.claude/skills/otelq/SKILL.md` — copy it verbatim as the shim. It keeps a
     single canonical body while making the skill discoverable. Match whatever shim
     convention `$TARGET` already uses for its other skills if it has one.
   - **For multiple agents**, install one canonical body plus one shim per agent
     that needs its own dir.
   - **An agent you do not recognise** → ask the user for that agent's
     project-scoped skills directory and the shape it expects, rather than guessing.

   Whatever the layout, keep the trailing `otelq/SKILL.md` shape (a directory named
   `otelq` containing `SKILL.md`) and install **project-scoped inside `$TARGET`**
   (never a global/user skills dir).

3. **Copy verbatim** from the otelq repo into the chosen location(s) under `$TARGET`.
   Do not rewrite, trim, or "adapt" the content — otelq is the master for both the
   canonical skill and the shim, exactly as it is for the `collector-config`
   fragment, so a copy that drifts is a bug. If a destination file already exists and
   is byte-for-byte identical, leave it untouched; if it exists but differs, replace
   it with the canonical copy:

   ```sh
   # canonical body (always)
   DEST="$TARGET/.agents/skills/otelq"
   mkdir -p "$DEST"
   cp .agents/skills/otelq/SKILL.md "$DEST/SKILL.md"

   # plus a per-agent shim when the agent has its own dir, e.g. Claude Code:
   SHIM="$TARGET/.claude/skills/otelq"
   mkdir -p "$SHIM"
   cp .claude/skills/otelq/SKILL.md "$SHIM/SKILL.md"
   ```

4. **Acceptance check — confirm the agent can discover it, not just that the file
   landed.** `git -C "$TARGET" status` should show the new skill/shim file(s) (so
   they commit alongside the integration). Then verify discoverability *for the
   named agent*: the skill sits in a directory **that agent actually scans**
   (canonical body in `.agents/skills` only suffices for agents that read it; Claude
   Code needs the `.claude/skills` shim). Note that a freshly added skill usually
   requires the agent to **reload/restart its session** before it appears, and that
   `$TARGET`'s git status should be clean afterwards (idempotent: a byte-identical
   skill already present is a no-op, not a re-copy). Only once the skill resolves in
   the user's actual agent is this step done — then tell the user the `otelq` command
   is usable by `$TARGET`'s agent.

## When something is off

- `doctor` says **no `*.jsonl` found** — the Collector is not writing: confirm the
  bind mount, that the `file/*` exporters are in the **active** pipelines, and that
  the image is `-contrib`.
- `doctor` reports a **FAIL** on a signal — the file is present but not
  contract-valid OTLP/JSON; check the exporter `path` and that nothing else writes
  to that file.
- A signal shows **WARN (no files)** — that signal simply is not being emitted yet;
  not an error if the app (or probe) does not produce it.
- The probe's `telemetrygen` can't reach the Collector — confirm both are in the
  **same Compose project** (so service-name DNS resolves) and that you used the
  Collector's **service** name, not its `container_name`.
- The skill file is present but **the agent never offers/loads otelq** — it is in a
  directory that agent does not scan. Most common with Claude Code, which reads
  `.claude/skills` and **not** `.agents/skills`: add the thin `.claude/skills/otelq`
  shim pointing at the canonical body (see the install step), then reload the agent
  session so it re-scans skills.
