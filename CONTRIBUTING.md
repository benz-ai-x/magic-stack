# Contributing to Magic Stack

Thanks for your interest in contributing! Magic Stack is a pure-Python macOS menu-bar app hosting an SSH tunnel proxy (HTTP→SOCKS5), a TLS capture mode (mitmproxy), and the Suanpan AI routing gateway (Anthropic Messages API → multi-LLM backends).

[English](CONTRIBUTING.md) · 欢迎中文交流（issue / PR 中文均可）

## Setup

```bash
git clone https://github.com/benz-ai-x/magic-stack.git
cd magic-stack
pip3 install -r requirements-dev.txt
python3 app.py   # dev mode
```

- Runtime floor is Python ≥3.9; the packaging toolchain needs 3.12 (mitmproxy ≥12).
- The Suanpan gateway deps are optional at runtime — the app starts fine without them and tells you what to install.

## Testing

```bash
# Python tests — must run via python3 -m (bare pytest=3.9 crashes on suanpan type hints)
python3 -m pytest --cov tests/

# Settings-UI JS tests (node --test glob form; directory mode fails on node ≥26)
node --test tests/js/*.test.mjs
```

Both suites must pass before a PR can merge. Coverage omits are declared in `.coveragerc`.

## Conventions

- **One module, one home** — every responsibility has a single owning module (see the [module map](docs/agents/architecture.md)). New behavior goes into the owning module or a new one; don't fork logic across sites.
- **Docs are guarded** — `tests/test_docs_drift.py` pins the [module inventory](docs/agents/architecture.md) and [settings navigation](docs/agents/ui.md). Moving/adding modules or settings views means updating the corresponding reference.
- **Domain vocabulary** — terms like tunnel, capture mode, per-request origin binding, and routing priority are defined in [`CONTEXT.md`](CONTEXT.md). Use them consistently.
- **Commits** — Conventional Commits with a scope and a Chinese summary, e.g. `fix(tunnel): relay 写端断开后停止转发`. Releases are tagged `vX.Y.Z` (version lives in `build.sh`).
- **ADRs** — decisions with lasting consequences get an ADR under `docs/adr/`.

## Pull requests

1. Keep PRs focused; reference the issue number (`#NN`).
2. Describe the user-visible change in the PR body — most fixes follow a diagnose → root-cause → test → fix rhythm, and the tests are the interesting part.
3. New features should come with tests and, where user-visible, a CHANGELOG entry.

## Reporting issues

Include: macOS version, app version (menu → About), relevant log lines (`~/Library/Logs/MagicProxy.log`), and steps to reproduce. For the AI gateway, `~/.suanpan/logs/usage.jsonl` (redact API keys!) is often decisive.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
