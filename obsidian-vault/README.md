# Triage knowledge vault

Open this folder as an Obsidian vault. It is the long-term memory of the CI triage agent
and the Verification Agent: what the team has learned about failures, written so people
can read and edit it and the agent can match it against new failures.

| Folder | Holds | Written by |
|---|---|---|
| `issues/` | One note per reported ACT/Sail issue (the "Reported ACT/Sail Issues" sheet) | `ci/triage/tools/sync_issue_notes.py`, then curated by a person |
| `verifications/` | One note per experiment that confirmed or ruled out a root cause | The Verification Agent or a person, from `templates/verification.md` |
| `boards/` | Per-board overview linking the notes that apply | People |
| `templates/` | Note templates (never matched) | People |

## How the triage agent uses a note

For every failed test (`ci/triage/vault.py`):

1. A note **matches** when its `boards` include the board (or are empty), one of its `tests`
   globs matches the test name, and every `signals` substring appears in the failure
   evidence. A note with neither `tests` nor `signals` never matches.
2. A matching note with `auto: true` that names the board and the test **explains the
   failure without AI**: the workbook shows its verdict, the issue link and how to treat it.
   Only a person's verdict in the portal outranks it.
3. Any other matching note goes into the AI question as context (at most two).

Notes are read on every run and not copied into the SQLite memory, so editing a note
takes effect on the next triage.

## Frontmatter

```yaml
id: ACT-1828                 # file name without .md
title: ...
url: https://github.com/riscv/riscv-arch-test/issues/1828
state: open                  # GitHub state, kept current by sync_issue_notes.py
resolution: open-known-limitation   # open | fixed-upstream | test-dropped | duplicate | ...
fixed_in: ""                 # PR, release or config option that fixed it
cause: test-limitation       # test-limitation | reference-model | spec-version |
                             # implementation-choice | framework-bug | hardware-timing
boards: [vf2_jh7110]
tests: ["Sstvecd*"]          # case-insensitive globs
signals: []                  # lowercase substrings that must all be in the evidence
auto: true
verdict: Test or ACT issue   # a portal Verdict value
curated: true                # false = stub from the sync tool, never matches until curated
```

Body sections, in this order: summary, `## What fails`, `## What the maintainers concluded`,
`## How triage treats a match`, `## Related` (`[[ID]]` links).

## Keeping it current

```bash
python3 ci/triage/tools/sync_issue_notes.py --dry-run   # what would change
python3 ci/triage/tools/sync_issue_notes.py             # update states, add stubs
```

New issues arrive as stubs (`curated: false`, no tests or signals). A person (or an
agent, reviewed like code) reads the thread and fills in the frontmatter and sections.
Set `auto: true` only when the board and test names alone identify the issue.
