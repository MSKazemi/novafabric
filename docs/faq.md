# FAQ — the longer answers

The [README FAQ](../README.md#faq) answers the ten questions people ask *before*
trying NovaFabric: what it is, what a capsule is, whether it phones home, whether it
is production-ready.

**This page answers the questions people ask after that** — while evaluating it,
while it is misbehaving, and while deciding whether to contribute. Nothing here is
repeated from the README, so the two pages cannot drift apart.

- [Evaluating it](#evaluating-it)
- [Using it](#using-it)
- [When something goes wrong](#when-something-goes-wrong)
- [Trust, security, and data](#trust-security-and-data)
- [The project itself](#the-project-itself)
- [Contributing](#contributing)

---

## Evaluating it

### Should I use this, honestly?

Use it if you need to **prove, replay, or compare a run that already happened** —
audit evidence, reproducibility, regression triage, "what changed between Tuesday
and today".

Do **not** use it if what you want is live dashboards and alerting. That is a
different job and other tools do it better. The honest comparison, including where
NovaFabric loses, is in [How NovaFabric compares](comparison.md). A wrong
recommendation costs you days and costs this project the only currency it has.

### What is the fastest way to find out if it fits?

An afternoon, not a reading session. The five-step evaluation — including the two
steps that actually matter (prove the redaction works; break NovaFabric and confirm
your workload survives) — is in
[For platform teams](for-platform-teams.md#how-to-run-a-real-evaluation-in-an-afternoon).

### How much does capture slow my run down?

Measured numbers, with the command and hardware stated so you can reproduce them
rather than take our word, are in [Benchmarks](benchmarks.md). Re-run them on your
own hardware before quoting them to anyone — that is what they are for.

### Can I use it in a commercial product?

Yes. Apache-2.0, including patent grant. You do not owe this project anything for
using it commercially, and there is no paid tier to upgrade to.

### Is it safe to depend on with one maintainer?

That is the correct question to ask, and the answer is "with your eyes open". The
risk is real; the mitigations are that capsules are plain folders readable without
this tool, there is no hosted dependency to lose, and the path to becoming a
maintainer is written down and open. The full discussion, including the case for
*not* adopting yet, is in
[For platform teams](for-platform-teams.md#the-risks-we-would-raise-if-we-were-you).

---

## Using it

### Which of the 32 extras do I actually need?

Run `nova doctor --check-extras`. It lists each extra as complete or incomplete,
names the distributions missing from each, and prints the exact
`pip install 'novafabric[<extra>]'` command. It exits 0 either way — most installs
deliberately omit most extras, so incompleteness is information, not failure.

### Do I need server mode?

Almost certainly not, to start. Local mode — capture, validate, replay, diff,
lineage — is the stable core and needs nothing but the CLI. Server mode exists for
multi-user and cluster-scale deployments and is `experimental`. Postgres is never
required for local mode.

### Can I capture something that is not Python?

Yes. `nova capture <command>` wraps *any* command. For capturing model traffic from
a non-Python client, use `nova api-proxy` or `nova mcp-proxy`, which work at the wire
level and are language-agnostic.

### Where does everything get written?

Local mode writes to `~/.novafabric/` — capsules under `capsules/<ulid>/`. Replays
land in `.novafabric/replays/` relative to where you run the command. Both paths are
configurable; see the [CLI reference](cli-reference.md).

### Can I move a capsule to another machine?

That is the entire point. A capsule is a self-contained folder. Copy it, tar it, put
it in object storage, email it to an auditor — it validates and replays anywhere the
CLI runs, offline.

### Which replay mode should I use?

| Mode | Use when |
|---|---|
| `exact` | The run was deterministic and you want a byte-comparable re-run |
| `mocked` | You want the original recorded responses replayed back, with no network |
| `semantic` | You accept a different-but-equivalent model response |
| `forensic` | You want to inspect without executing anything — read-only, no network, no subprocess |

NovaFabric does **not** claim exact replay of remote LLM calls. A remote model is not
deterministic; `mocked` and `semantic` exist precisely because pretending otherwise
would be dishonest.

---

## When something goes wrong

### ~30 tests fail right after I clone and set up

You ran `uv sync` instead of `uv sync --all-extras`. A plain sync strips the optional
extras and those tests fail with import errors that look like your fault but are not.
Re-sync with `--all-extras`.

### `nova validate` warns about my `environment` value

The conventional set is `{development, test, staging, production}`. A custom value
like `ci` is legitimate and recorded verbatim, but validate warns on anything outside
that set — deliberately, because a warning on every single run just teaches people to
ignore warnings.

### The test suite hangs

There is a 300-second `pytest-timeout` cap so a hang fails by name rather than
running forever. Read which test the timeout names; do not restart blindly.

### A command printed `pip install 'novafabric'` with no extra in it

That was a real bug, fixed in the `nova doctor --check-extras` work: Rich read
`[serve]` as a markup tag and silently deleted it. If you are on a version that still
does this, the extra name you want is the one named in the surrounding text — and
please [open an issue](https://github.com/MSKazemi/novafabric/issues/new/choose) so
we know it survived somewhere.

### Something else

Search [existing issues](https://github.com/MSKazemi/novafabric/issues) first, then
ask in [Discussions](https://github.com/MSKazemi/novafabric/discussions) — questions
belong there, and you will get a faster answer than in an issue. Open an issue when
you have something actionable: a bug with a reproduction, or a defined feature.

---

## Trust, security, and data

### How do I verify redaction actually happened?

Do not trust it — test it. Put a fake secret in the environment, capture a run, then
grep the capsule for that string. Every capsule also carries a redaction proof
recording that redaction ran. If you find a leak, that is a security issue: follow
[SECURITY.md](../SECURITY.md) and do **not** open a public issue.

### What does "signed" actually mean here?

Different things at different layers, and it is worth being precise:

- **Capsules** can carry in-toto DSSE attestations, Sigstore signatures, and RFC 3161
  trusted timestamps. See [Trust surfaces](trust-surfaces.md).
- **Container images** are signed keylessly with cosign and carry SLSA build
  provenance plus an SBOM as OCI artifacts.
- **Air-gap bundles** are one tar whose members are inventoried in a DSSE-signed
  manifest.

A signature proves origin and integrity. It does not prove the contents are *correct*
— nothing does, and any tool claiming otherwise is selling something.

### Does NovaFabric certify us as compliant?

No, and it will refuse to pretend to. NovaFabric emits conformance **receipts** —
evidence that a control was exercised, with provenance — never verdicts. A verdict is
an auditor's job. [Assurance cases](assurance-cases.md) explains the distinction and
why it is load-bearing.

### Is there telemetry?

None. No accounts, no phone-home, no update check, no crash reporting. A consequence
worth stating plainly: we have no idea who uses this unless they tell us, which is
why [ADOPTERS.md](../ADOPTERS.md) exists and why it is genuinely useful to add
yourself.

---

## The project itself

### Why "capsule" and not "trace"?

A trace is a row in someone's database, and it answers "what happened". A capsule is
a portable folder you own, and it answers "can this be replayed, compared, and
proven". The noun is different because the unit of value is different.

### When is v1.0, and what changes?

v1.0 is the **schema freeze** — the point at which the on-disk Run Capsule and
Evidence Bundle formats become a stable contract. Until then, treat capsule internals
as internal. What should be in v1.0 is being argued about in the open right now, in
[What should v1.0 be?](https://github.com/MSKazemi/novafabric/discussions/10) — if you
have a stake in the format, that is the moment to argue, not after.

### Why is nearly everything labelled `experimental`?

Because it is, and the project has a standing rule against blurring the line. Every
feature carries exactly one of: **works today**, **experimental**, **planned**, or
**future design**. Take the labels literally — they are maintained on purpose, and
an over-claiming doc is treated here as a bug.

### Who pays for this?

Nobody. There is no company, no funding, and no revenue. That is also why the most
useful thing an organization can do for NovaFabric is not money but people — see
[the bus-factor discussion](for-platform-teams.md#the-risks-we-would-raise-if-we-were-you).

---

## Contributing

### I want to help but I do not know this codebase

That is the normal case and it is planned for.
[Good first issues](https://github.com/MSKazemi/novafabric/labels/good%20first%20issue)
each name the files to touch, the acceptance criteria, the tests to write, and an
honest difficulty estimate — so you never have to guess where to start.
[CONTRIBUTING.md](../CONTRIBUTING.md) gets you from clone to pull request in about 15
minutes.

### Is a documentation or typo fix worth opening?

Yes, genuinely, and they are reviewed like real contributions. A doc that lied to you
is a bug report about the project's honesty, which this project cares about more than
most.

### Do I need to ask before starting?

No — for anything labelled `good first issue` or for fixing something that annoyed
you, just open the PR. Do ask first (in an issue or discussion) before large work,
public API changes, or a new dependency; that is where you would otherwise write 800
lines that get rejected on a design point.

### Can I contribute without writing code?

Yes, and it counts the same. Documentation, translations, bug reports with
reproductions, design feedback, reviews, and talks are all listed in
[CONTRIBUTORS.md](../CONTRIBUTORS.md), and they all count toward the
[maintainer path](governance/maintainer-criteria.md).

### Can I use an AI assistant to write my contribution?

Yes — see the policy in [AGENTS.md](../AGENTS.md) and
[CONTRIBUTING.md](../CONTRIBUTING.md). The requirement is not *how* you wrote it but
that **you understand it, you tested it, and you can respond to review on it**. A PR
its own author cannot explain wastes the reviewer's time, and that is the only thing
being guarded against.
