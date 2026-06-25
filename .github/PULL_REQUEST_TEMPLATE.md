<!--
Thanks for contributing to otelq! Please read CONTRIBUTING.md first.
Keep PRs scoped to one area of work.
-->

## Summary

<!-- What does this change and why? -->

## Related issue

<!-- e.g. Closes #123 -->

## Maintainer workflow

<!--
Confirm this change follows the public-repo maintainer flow in CONTRIBUTING.md:
feature branch -> PR -> checks green -> merge via GitHub UI / VS Code PR extension / gh CLI.
If this PR is an exception, explain why in Summary.
-->

## Checklist

- [ ] `uvx pyright` is clean (strict: 0 errors / 0 warnings / 0 informations)
- [ ] `just lint` (ruff) is clean
- [ ] `just otelq-test` passes
- [ ] New / changed behavior has tests
- [ ] User-facing changes are reflected in the README and the relevant `context/spec/` doc
- [ ] No `# type: ignore` / `# pyright: ignore`; no casual `duckdb` pin bump (see ADR-003)
- [ ] Branch + PR follow CONTRIBUTING.md maintainer workflow (or exception explained)
