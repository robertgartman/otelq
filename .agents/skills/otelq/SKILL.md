---
name: otelq
description: "Query logs, metrics, and traces captured from the OpenTelemetry feed using the otelq CLI."
---

# Query Telemetry

Inspect real OpenTelemetry signals from the running system. Use this to close
the loop after a change: run the code, then confirm from telemetry that it
behaved as intended.

## Running otelq
```
uvx --from git+https://github.com/robertgartman/otelq otelq --dir telemetry ...
```

- **`--dir telemetry` is required.** It points otelq at the Collector's output
  folder — the `telemetry/` directory at this project's root (the bind-mounted
  dir set up when otelq was wired in). Because `uvx` runs otelq from an isolated
  build, the default would not resolve to your project, so always pass `--dir`.
  Adjust the path if your Collector writes elsewhere.
- To pin a version, append a ref: `…/otelq@v0.1.0 otelq …`.

## Discovering commands

```
uvx --from git+https://github.com/robertgartman/otelq otelq --help
```

## Troubleshoot

```
uvx --from git+https://github.com/robertgartman/otelq otelq --troubleshoot
```

