# Security Policy

## Supported versions

otelq is pre-1.0 (alpha). Security fixes are applied to the latest released
version on the `main` branch only.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Report privately through GitHub's
[**"Report a vulnerability"**](https://github.com/robertgartman/otelq/security/advisories/new)
button on the repository's **Security** tab (Private Vulnerability Reporting).
If you cannot use that channel, email **robert.gartman.sweden@gmail.com** with
the details.

Please include:

- a description of the issue and its impact;
- the version / commit affected;
- steps to reproduce, ideally with a minimal example.

You can expect an acknowledgement within a few days. Because this is a
single-maintainer hobby project, please allow reasonable time for a fix before
any public disclosure.

## Scope notes

otelq is a local, CLI-only tool: it reads OTLP JSONL telemetry files written by
a developer's own Collector and runs in-process DuckDB queries against them. It
has no network listener and no server component. The most relevant security
considerations are therefore:

- handling of untrusted telemetry file contents (parsing, SQL views);
- the integrity of the pinned `duckdb` version and the `duckdb-otlp` community
  extension it loads (see [`context/adr/ADR-003`](context/adr/ADR-003-duckdb-otlp-extension-pin-governance.md));
- the supply chain of the CI workflows under `.github/workflows/`.
