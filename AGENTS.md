# AGENTS.md

A README for coding agents working in this repository. Humans should read
[CONTRIBUTING.md](CONTRIBUTING.md) — it has a 15-minute path from clone to pull
request. This file exists so an agent does not have to infer the conventions from
the code and get them subtly wrong.

Everything here is a real constraint of this project, not a style preference.

---

## Setup

```bash
uv sync --all-extras
```

**`--all-extras` is required.** A plain `uv sync` strips the optional extras and
~30 tests fail with import errors that look unrelated to whatever you changed.
This is the single most common wasted debugging session in this repo.

## Gates — all four must pass

```bash
make test-fast     # ~90 s, parallel, skips integration + testcontainers tiers
make lint          # ruff check src tests scripts
make typecheck     # mypy src (strict)
make check-links   # every relative link in a public doc resolves
```

Before a release, or when you have touched storage or the server:
`make test-par` (full scope + coverage, ~5 min). **Coverage must stay at or above
90%.** Never run the ~11.6K-test suite serially during development.

For any CLI change, also smoke-test `uv run nova --help` and the affected
sub-command.

---

## What will get a change rejected

These are invariants, not preferences. Breaking one means the change gets
reverted regardless of how good the rest is.

1. **A capsule must verify with no server, no network, and no database.**
2. **Capture never blocks the user's workload by default.** If a NovaFabric
   component fails, the workload continues and the failure is recorded.
3. **No secrets past the redaction boundary** — never log or store prompts,
   tokens, or environment variables outside the redacted capsule.
4. **Full prompt/response capture is opt-in**, never a default.
5. **No telemetry and no update checks.** Ever. Do not add one "behind a flag".
6. **Core local-mode features must work with no internet.**
7. **Compute-node hot paths never write to a database or a graph** — they write
   to a local spool.
8. **Schema changes are additive and optional first.** A new field must not break
   validation of an existing capsule.
9. **Postgres is never required for local mode.** SQLite is the local default.
10. **Two top-level formats only** — Run Capsule and Evidence Bundle.

Full architectural context: [docs/architecture.md](docs/architecture.md).

---

## Things that are true here and are not true everywhere

- **Not every path in a maintainer's working tree is in this repository.** Some
  design and internal material is kept outside it. The rule that matters to you:
  **a public document may only link to something the public git tracks** — and
  `make check-links` enforces exactly that, checking git membership rather than
  whether a file happens to exist locally. If it rejects a link you can open,
  the link is still wrong; the file is not part of what people clone.

  The same rule applies to tests and CI: a test that reads a path outside the
  repository passes for its author and fails for everyone else. Two guards in
  `tests/docs/test_tests_are_runnable_from_a_public_clone.py` enforce it, and
  they exist because that failure has happened repeatedly.
- **Never write terminal output from memory.** Run the command and paste what it
  actually printed. Invented CLI output in documentation has been caught here
  more than once.
- **A source-tree test run cannot see a packaging defect.** Three separate
  released-install bugs got through a green suite because the tests ran from the
  source tree where repo-root fallbacks resolve. If you change packaging, build
  the wheel and install it into a clean venv.
- **`tests/<name>/__init__.py` shadows an installed distribution** via
  `pythonpath=tests`. Check before naming a new test package; this silently
  disabled the coverage gate for six weeks.
- **A 300 s `pytest-timeout` makes hangs fail by name.** Read the timeout
  failure; do not restart blindly.
- Tests are hermetic — an autouse fixture strips ambient `NOVAFABRIC_*` vars, so
  a test never touches a developer's real capsule store. Do not work around it.

---

## Code style

- Typed Pydantic models for data; dataclasses for internal state
- Small modules, pure functions, explicit named exception classes
- No hidden global state, no import-time IO
- Structured logging, never `print`
- Bounded queues, retries, and recursion

Avoid: large magical classes, silent failures, unbounded queues, speculative
abstractions, premature backends.

**Comments explain *why*, not *what*.** A comment that restates the code is
noise; a comment recording why a non-obvious choice was made is the most
valuable thing in the file. Match the density of the surrounding code.

---

## When a change needs an RFC instead of a pull request

New CLI command or default flag · schema change · storage format change · new
runtime dependency · anything touching the security posture · governance change.

See [the RFC process](docs/governance/rfc-process.md). If you are unsure,
[ask in Discussions](https://github.com/novafabric/novafabric/discussions)
rather than guessing — asking is always cheaper than writing the wrong document.

---

## If you are an AI agent opening a pull request

- **Say so in the PR description.** Not because AI-assisted contributions are
  unwelcome — they are welcome — but because a reviewer allocates attention
  differently, and that is a reasonable thing for them to want to know.
- **Do not open a PR you have not run the gates on.** An unverified PR costs a
  maintainer more time than it saves.
- **One change per PR.** A PR that fixes a bug and also reformats four files is
  much harder to review than two PRs.
- **If a test fails and you cannot fix it, say that in the PR** rather than
  deleting or skipping the test. A failing test that is reported honestly is
  useful; a deleted one is a defect with the evidence removed.
- Include the gate output in the PR description.
