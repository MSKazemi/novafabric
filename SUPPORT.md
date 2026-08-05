# Support

Where to get help, and what you can expect back.

## Response commitments

These are promises, not aspirations. **If we miss one, ping the thread** — that
is not rude, it is the system working as intended.

| You do this | We respond within |
|---|---|
| Open a bug report | 3 business days |
| Open a feature request | 5 business days |
| Open a pull request | 5 business days for a first review |
| Ask a question in Discussions | 1 week |
| Report a security vulnerability | **72 hours** for critical — see [SECURITY.md](SECURITY.md) |
| Apply as a design partner | 2 weeks |

NovaFabric is currently maintained by one person, and these windows are set to
what one person can actually sustain — not to what sounds impressive. If that
changes, this table changes with it.

**What we don't promise:** a fix on a deadline, support for unsupported
configurations, or a response to direct messages and email. Use issues and
Discussions — a public answer helps the next person with the same problem.

---

## Documentation first

Most questions are answered here:

- **Getting started:** [docs/getting-started.md](docs/getting-started.md)
- **Concepts** — the five primitives, the replay modes: [docs/concepts.md](docs/concepts.md)
- **Architecture** — the subsystem map: [docs/architecture.md](docs/architecture.md)
- **CLI reference:** [docs/cli-reference.md](docs/cli-reference.md)
- **Python API:** [docs/python-api.md](docs/python-api.md)
- **Comparisons** — how NovaFabric relates to other tools: [docs/comparison.md](docs/comparison.md)
- **FAQ:** [in the README](README.md#faq)
- **Website:** <https://novafabric.ai>

## Ask a question

| What you have | Where it goes |
|---|---|
| "Is this supposed to work like this?" | [Discussions → Q&A](https://github.com/novafabric/novafabric/discussions/categories/q-a) |
| "This is broken" | [Bug report](https://github.com/novafabric/novafabric/issues/new?template=bug_report.yml) |
| "NovaFabric should be able to…" | [Feature request](https://github.com/novafabric/novafabric/issues/new?template=feature_request.yml) |
| "The docs are wrong / unclear" | [Documentation issue](https://github.com/novafabric/novafabric/issues/new?template=documentation.yml) |
| "Here's what I built with it" | [Discussions → Show and tell](https://github.com/novafabric/novafabric/discussions/categories/show-and-tell) |
| "I found a vulnerability" | [SECURITY.md](SECURITY.md) — **never** a public issue |

When reporting a problem, include:

- The NovaFabric version (`nova --version`)
- Your OS and Python version
- The exact command you ran and the **full** output, not just the last line
- A minimal reproduction if you have one

> **Check your paste for secrets.** NovaFabric redacts capsules; it does not
> redact terminal output you paste into an issue.

## Security

Do **not** open a public issue for a vulnerability. Use the private disclosure
process in [SECURITY.md](SECURITY.md) — GitHub private vulnerability reporting is
enabled on this repository, so [*Security → Report a
vulnerability*](https://github.com/novafabric/novafabric/security/advisories/new)
works directly.

## Contributing

Want to fix it yourself? [CONTRIBUTING.md](CONTRIBUTING.md) has a 15-minute path
from clone to pull request, and
[good first issues](https://github.com/novafabric/novafabric/labels/good%20first%20issue)
are kept stocked and specified.

## Commercial / research collaboration

NovaFabric is maintained by
[Mohsen Seyedkazemi Ardebili](https://github.com/MSKazemi) as part of the
[NovaFabric](https://github.com/novafabric) open-source lab. There is no
commercial offering and no hosted service. For research collaboration, see the
[design partner program](docs/governance/design-partners.md) or the contact links
on <https://novafabric.ai>.
