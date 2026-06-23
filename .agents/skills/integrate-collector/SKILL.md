---
name: integrate-collector
description: "Use from the otelq repo to wire otelq into your project on the same host — adds otelq's file-export pipeline to your project's existing OpenTelemetry Collector so otelq can read its telemetry. Asks for the target project's absolute path."
---

# Integrate otelq with your project's Collector

**Direction (important):** this skill runs **from the otelq repository** and
operates on a **different target project elsewhere on the same host**. otelq is
the tool; the *target project* is where the integration happens. You are not
integrating a Collector into otelq — you are adding otelq's file-export pipeline
to the **target project's** Collector so otelq can read what that project emits.

**Before doing anything, ask the user for the absolute path to the target
project** (the project whose Collector should be wired up), e.g.
`/Users/me/dev/my-service`. Call it `$TARGET`. Every file you read or edit in the
steps below lives under `$TARGET` — **never** edit files inside the otelq repo for
this task. The only otelq command you run is `otelq collector-config` (run via
`uvx` — see **Running otelq** below), which just *prints* the fragment to paste
into `$TARGET`.

otelq reads OTLP signals as JSONL from a shared `telemetry/` directory — that
directory is the entire contract (see
`context/contract/CONTRACT-telemetry-directory.md`). Any Collector that writes the
right files works; otelq does not need to own the Collector.

There are **two setups**:

- **Standalone** — otelq runs its own bundled Collector (`just otel-up`) inside the
  otelq repo. Best for a demo or a project with no Collector. This skill is **not**
  for that case.
- **Integrated** — `$TARGET` already runs a Collector as part of its architecture.
  Add otelq's file-export pipeline to *that* Collector. This skill covers this case.

In integrated mode, **otelq never manages `$TARGET`'s Collector** — it does not
start, stop, or clean it. otelq only reads the telemetry dir and owns the
`.otelq-cache/` subtree. Do **not** run `just otel-up` / `just otel-clean` against
an integrated setup (those target otelq's *bundled* Collector and `otel-clean`
truncates the JSONL files).

**Never add telemetrygen or the demo to `$TARGET`.** The synthetic-telemetry
generators (the `demo` Compose profile / `just otel-demo`) exist **only in the
otelq repo** as a way to try otelq without instrumenting an app. They are a testing
aid for this project, not part of any integration. Into `$TARGET` you add **only**
the three `file/*` exporters, the pipeline wiring, and the bind mount — nothing
else. The real telemetry comes from `$TARGET`'s own application; telemetrygen has
no place there.

## Running otelq

Throughout this skill, **`otelq …`** is shorthand for running the CLI straight
from GitHub with `uvx` — no clone, no global install:

```
uvx --from git+https://github.com/robertgartman/otelq otelq …
```

The first run builds otelq and fetches the DuckDB `otlp` community extension
(network once, then cached). For the `doctor` / `summary` checks below, pass
`--dir $TARGET/telemetry` so otelq reads the target project's output folder. To
pin a version, append a ref: `…/otelq@v0.1.0 otelq …`.

## Steps

0. **Ask for `$TARGET`.** Get the absolute path to the target project from the
   user. Confirm it exists and contains a Collector setup (a compose file with an
   `otel/opentelemetry-collector*` service). All paths below are under `$TARGET`.

1. **Find the target's Collector.** In `$TARGET`'s `docker-compose.yml` /
   `compose.yaml`, locate the `otel/opentelemetry-collector*` service and the
   config file it mounts (the `--config` path, e.g. `otel-collector.yaml`).

2. **Check the image is `-contrib`.** The `file` exporter exists only in
   `otel/opentelemetry-collector-contrib`, **not** the core
   `otel/opentelemetry-collector` image. If `$TARGET` runs core, switch its service
   to the `-contrib` image (same tag) — otherwise the config below fails to start.

3. **Get the canonical fragment** (run from the otelq repo):

   ```sh
   otelq collector-config
   ```

   This prints the exact `file/*` exporters (with the pinned rotation settings) and
   the pipeline wiring to add. It is generated from otelq's own constants, so it
   always matches the contract — prefer it over hand-writing the config.

4. **Merge it into `$TARGET`'s Collector config.** Add the three `file/*`
   exporters, and append `file/traces` / `file/logs` / `file/metrics` to the
   **existing** `service.pipelines.<signal>.exporters` lists (keep the project's
   current exporters — you are adding a tee, not replacing).

5. **Bind-mount a host telemetry dir** into `$TARGET`'s Collector service in
   compose:

   ```yaml
   volumes:
     - ./telemetry:/telemetry
   ```

6. **Restart `$TARGET`'s Collector**, exercise its app so it emits telemetry, then
   **verify** (run from anywhere; point `--dir` at the target's telemetry dir):

   ```sh
   otelq --dir $TARGET/telemetry doctor     # exit 0 and OK rows = wiring is good
   otelq --dir $TARGET/telemetry summary
   ```

   `doctor` checks the dir against the contract (files present, valid OTLP/JSON,
   correct signal per file). A `FAIL` row names what is wrong. `--dir` is a global
   flag and points otelq at the **target project's** telemetry directory; set it on
   every query.

## When something is off

- `doctor` says **no `*.jsonl` found** — the target's Collector is not writing:
  confirm the bind mount, that the `file/*` exporters are in the active pipelines,
  and that the image is `-contrib`.
- `doctor` reports a **FAIL** on a signal — the file is present but not
  contract-valid OTLP/JSON; check the exporter `path` and that nothing else writes
  to that file.
- A signal shows **WARN (no files)** — that signal simply is not being emitted yet;
  not an error if the app does not produce it.
