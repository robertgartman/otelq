# Contributing to otelq

Thanks for your interest in otelq. Issues and pull requests are welcome at
[github.com/robertgartman/otelq](https://github.com/robertgartman/otelq).

This is a deliberately small, single-file CLI. A few project-specific rules keep
it that way — please read these before opening a PR.

## Ground rules

- **The `justfile` is the single execution gateway.** Run tasks through `just`
  (`just lint`, `just otelq-test`, `just otel-up`, …) rather than ad-hoc
  commands, so everyone runs the same thing.
- **Strict typing, no escapes.** `pyright` runs in strict mode and must stay at
  0 errors / 0 warnings / 0 informations. Do not add `# type: ignore` or
  `# pyright: ignore`. Explicit `Any` is allowed *only* at the OTLP-JSON and
  DuckDB-result-row boundaries — never spread it further.
- **The `duckdb` pin is exact and load-bearing.** It is pinned to the same exact
  version in both `otelq.py` (the PEP 723 header) and `pyproject.toml`, and the
  two must stay in sync. The `duckdb-otlp` community extension is built per
  DuckDB version, so bumping the pin follows the governance in
  [`context/adr/ADR-003`](context/adr/ADR-003-duckdb-otlp-extension-pin-governance.md) —
  validate the extension build first. Do not bump it casually.
- **Stay standalone.** otelq was extracted from a monorepo; do not reintroduce
  any application-specific bits (named services, framework-specific span
  filters, etc.).
- **Read the docs first.** See [`AGENTS.md`](AGENTS.md) and
  [`context/CONTEXT.md`](context/CONTEXT.md). Doc precedence is
  ADR > CONTRACT > SPEC > PRD. Behavior changes should be reflected in the
  relevant SPEC.

## Development setup

You need [Docker](https://www.docker.com/) (for the dev Collector) and
[uv](https://docs.astral.sh/uv/) (for the CLI and the toolchain).

```sh
uv sync --extra dev        # populate .venv (duckdb + stubs, pytest, ruff, editable otelq)
```

## Before you open a pull request

Run the full local check. All three must be clean:

```sh
uvx pyright                              # strict mode: 0 errors / 0 warnings / 0 informations
just lint                                # ruff check (uvx ruff check .)
just otelq-test                          # pytest suite
```

The first test run downloads the `duckdb-otlp` extension and needs network once;
it is cached afterwards.

## Pull request checklist

- [ ] `pyright`, `ruff`, and the test suite all pass locally.
- [ ] New or changed behavior has tests.
- [ ] User-facing behavior changes are reflected in the README and the relevant
      `context/spec/` document.
- [ ] No `# type: ignore` / `# pyright: ignore`; no casual `duckdb` pin bump.
- [ ] Commits are scoped to one area of work with a clear message.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
