# Translating NovaFabric

**Translations are real contributions and are credited exactly like code.** They are
also, right now, among the highest-leverage things anyone can contribute: every word
of this project is currently in English, and a reader who bounces off the README
never reaches the parts that would have convinced them.

This page is the whole specification. It is short on purpose.

- **Want to claim a language?** Comment on
  [the translation issue](https://github.com/MSKazemi/novafabric/issues/73) so two
  people do not translate the same file.
- **Your language is not listed?** Say which one. It gets added. That is not an
  exception — it is the point.

---

## What to translate, and in what order

Translate in this order. Each step is independently useful, and **stopping after
step 1 is a complete, welcome contribution** — do not feel you have signed up for
the whole list.

| Order | File | Why this order |
|---|---|---|
| 1 | `README.md` → `README.<lang>.md` | The front door. The single highest-value file by a wide margin. |
| 2 | [`docs/getting-started.md`](../getting-started.md) | Converts a curious reader into someone who has actually run it. |
| 3 | [`docs/concepts.md`](../concepts.md) | The five primitives — the vocabulary everything else assumes. |
| 4 | [`docs/faq.md`](../faq.md) | Answers the questions that otherwise become unanswered issues. |
| 5 | [`CONTRIBUTING.md`](../../CONTRIBUTING.md) | Turns a reader into a contributor in their own language. |

**Do not translate** these, ever: `CHANGELOG.md`, `ROADMAP.md`, the release notes, the
ADRs, or `docs/cli-reference.md`. They change too often, and a stale translation of a
reference document is worse than no translation — it tells people something untrue
about the software with the authority of documentation.

### Naming

Use the BCP 47 tag, lowercase language + optional uppercase region:

```
README.zh-CN.md    README.ja.md    README.es.md
README.pt-BR.md    README.de.md    README.ko.md
```

Root-level READMEs sit at the repository root. Everything else mirrors the English
path with the tag before the extension: `docs/getting-started.ja.md`.

---

## The rules that matter

### Never translate

A reader must be able to copy anything in a code block and have it work. Leave
untouched:

- Commands, flags, and subcommands — `nova capture`, `--mode forensic`, `uv sync --all-extras`
- File paths, environment variables, JSON/YAML keys
- Everything inside a fenced code block, **including the terminal output**
- The five primitive names: **Asset Registry**, **Run Capsule**, **Replay**,
  **Lineage**, **Evidence Bundle**
- Product and project names

### Always translate

Prose, headings, prose inside table cells, image `alt` text, and admonition labels.

### The status labels are load-bearing

This project labels every feature with exactly one of **works today**,
**experimental**, **planned**, or **future design**, and treats blurring that line as
a bug. Translations must preserve the distinction precisely.

If your language has no crisp equivalent — and for "experimental" many do not — write
your best translation and put the English in parentheses:

> 実験的 (experimental)

**Never soften "experimental" into a word that sounds finished.** A reader who adopts
an experimental subsystem because the translation implied stability has been misled
by us, and that is the specific failure this project exists to prevent.

### Keep links working

- Internal links to English files stay pointing at the English files unless a
  translation of that file exists.
- Translating a heading changes its anchor. Re-check every in-page link afterwards —
  this is the most common defect in translation PRs.

### Machine translation

**A machine-translated first pass is fine and expected.** Nobody is asking you to
type 800 lines by hand.

What is required is that a person who speaks the language has **read and corrected**
the result. The failure mode being guarded against is a translation no fluent speaker
has ever read — those are worse than nothing, because they look official.

If you are not a fluent or native speaker, **say so plainly in the pull request**. It
does not disqualify your contribution; it tells the reviewer what kind of review it
needs, and it is the honest thing to do.

---

## The language switcher

Every README carries the same switcher line directly under the title. When you add a
language, add it to **every** existing README, not only your own — otherwise your
translation is unreachable from the others.

```markdown
**[English](README.md)** · [简体中文](README.zh-CN.md) · [日本語](README.ja.md)
```

Write each language's name **in that language** (日本語, not "Japanese"). Keep the
list alphabetical by tag after English, which always comes first.

---

## Keeping translations from going stale

This is the part most projects get wrong, so the rule here is deliberately modest:

**A translation is a snapshot, and it is allowed to lag.** We would rather have a
three-month-old Japanese README than none. What we will not do is pretend it is
current.

Every translated file therefore ends with a footer naming the English commit it was
translated from:

```markdown
---

*Translated from the English [README.md](README.md) at commit `abc1234`.
Documentation changes fast in a v0.x project — if this page contradicts the English
version, the English version is authoritative. Spotted drift?
[Open an issue](https://github.com/MSKazemi/novafabric/issues/new/choose) or send a
patch; both are welcome.*
```

Get the commit with `git rev-parse --short HEAD` when you start.

Nobody is on the hook for perpetual maintenance of a file they translated once. If a
translation drifts badly and nobody updates it, we will mark it stale rather than
delete it — a stale translation with a warning still helps a reader more than an
English-only page.

---

## Checklist for your pull request

- [ ] File named with the correct BCP 47 tag.
- [ ] Code blocks, commands, flags, and paths are byte-identical to the English.
- [ ] The four status labels are preserved unambiguously.
- [ ] The language switcher is updated in **every** README.
- [ ] No in-page anchor link is broken.
- [ ] The "translated from commit" footer is present.
- [ ] You said whether you are a fluent speaker.

Then open the PR. If anything above was unclear, that is a defect in **this page** —
please say so, and it gets fixed.
