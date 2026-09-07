# CLAUDE.md

Nightly job that renders a one-page next-day dashboard matching the Rocketbook
Flex planner page, which the user copies into paper by hand. Read README.md for the
user-facing view; this file is the things that will bite you.

## Hard constraints — violating these fails silently

- **Always `/usr/bin/python3`. Never pyenv, never a venv.** Secrets live in the
  macOS Keychain and the ACL is granted to that exact binary. Under a different
  interpreter macOS shows a password prompt — and launchd has no terminal, so
  the nightly job hangs forever with no error and a stale page. This is the
  single most damaging mistake available in this project.
- **Standard library only.** No pip, no requirements.txt. The job must survive
  OS and toolchain upgrades untouched. `httpjson.py` is the HTTP layer; extend
  it rather than reaching for requests.
- **A failed enrichment must never cost the page.** Wrap optional steps
  (PDF rendering, branch listing, board order) so they degrade with a note on
  stderr and carry on. Precedent: `fetch/github.py` catches 403 on branches;
  `render/build.py` catches PDF failures.

## Two shells — you cannot test most of this

Claude reaches this folder through a sandboxed Linux VM. Its network is behind
an egress allowlist:

    api.github.com       reachable
    api.anthropic.com    reachable
    api.atlassian.com    BLOCKED
    *.googleapis.com     BLOCKED
    Keychain             absent entirely (`security` does not exist)

The VM is only where files get written. The job runs under macOS launchd,
outside that proxy. **So you cannot run `check`, `fetch`, `rank` or `run`
yourself.** Write the code, verify syntax with `ast.parse`, then hand the user one
command to run in Terminal. Do not claim something works because it parsed.

When a data file looks stale or a field is missing, compare mtimes before
debugging the code — it is usually a fetch that predates the patch.

## Design decisions worth preserving

- **The model chooses *what*, code supplies *facts*.** `rank.py` decides which
  items make the page. Jira status and PR ownership shown on those lines are
  looked up from the raw bundle at render time (`render/build.py: index()`),
  never carried through the model. A hallucinated owner on a page he acts on
  before coffee is the failure mode this prevents. Keep new metadata on this
  side of the line.
- **Board order comes from the Agile API, not JQL.** `ORDER BY Rank` on a JQL
  search ignores the board's filter and sprint scope and picks the wrong "top
  of To Do" card. Use `fetch/jira.py: board_issues()`. `sprint_id` stays null
  and `use_active_sprint` true so sprint rollover needs no maintenance.
- **Capacity is the whole point.** The paper page holds 3 priorities and 14
  checkboxes. Ranking is a cutting problem, not a listing problem. It is also
  told to leave 3–4 lines empty for handwritten personal items — filling all 14
  with work is a failure, not thoroughness.
- **Priority order, his rule:** slot 1 is production/sprint-critical; slots 2
  and 3 are people personally blocked on him. Politeness does not reorder this.
- **`out/index.html` stays out of `publish/`.** The Evernote sync folder watches
  `publish/`; a file that changes nightly would create a duplicate note daily.

## Signals the correlation produces

`correlate.py` joins Jira to branches and PRs by ticket key — neither system
knows about the other, and this is where the page says things the boards cannot:

    claimed_but_nothing_exists   In Progress, no branch, no PR — not started
    waiting_on_review            PR open and idle — he is not the blocker
    done_but_pr_open             ticket closed, PR still open
    branch_only                  branch exists, no PR yet
    unlinked_prs                 his PRs with no ticket at all

Rocket Chat emails are parsed in `fetch/google.py: classify_mail()`. Trust
`direct_mention` (a literal @handle in the body), never the subject line —
Rocket Chat stamps "You have been mentioned" on channel-wide traffic too.

## Working style he expects

One command at a time. Facts and limitations before capabilities. No padding.
When something is wrong, say so plainly — including when the wrong thing is
something you built or claimed. He will check your work and he is usually right.

## Profiles

Two pages from one codebase. `dashboard.py <cmd> [profile]`; no profile = work.

    work      config.json          Jira, GitHub, Gmail/Calendar/Tasks via API
                                   -> out/, data/, publish/       Sun-Thu 21:00
    personal  config.personal.json IMAP mail, Apple Reminders, Google ICS feeds
                                   -> out-personal/ etc.          daily 21:05

Each profile's paths come from its own `output` block, so they never collide.
Prompts live in `prompts/<profile>.md` - the two reason about different things
and should not be merged.

Personal-source notes:
- Mail is IMAP + a Gmail app password, NOT the API. A personal account cannot
  use the work Internal OAuth app, and an External one caps refresh tokens at
  seven days in Testing. imaplib is stdlib; this needs no Cloud project.
- Only VIP and watch-tagged messages get a body fetch. `watch.kind` is
  "deadline" (auction closing, payment due - can reach PRIORITIES) or
  "informational" (shipping - at most one NOTES line, never a task).
- Reminders come from AppleScript PLURAL accessors (`name of every reminder
  ...`). JXA reads one property at a time and times out. ~30s; verified working
  under launchd, so the TCC grant does carry across from Terminal.
- Calendar reads Google's secret ICS URLs and expands RRULEs for one day at a
  time. `"source": "local"` switches back to Calendar.app, which still works.
  Unsupported: BYSETPOS and similar - those events silently will not appear.
- Most of his reminders are months overdue. The personal prompt treats age as a
  signal, not a flag, and forbids scolding. Do not "helpfully" reinstate a
  nag - that was a deliberate decision.

## When the user says the pages stopped arriving

Check whether the PDFs exist on disk BEFORE debugging anything in this project.
Evernote sync folders import only while the Evernote app is running, and it does
not backfill what it missed while closed - so a perfectly healthy pipeline looks
identical to a broken one from the user's side. That has happened once already.
The other silent killer is the mini being asleep at 21:00; `pmset repeat` holds
only one schedule and anything else that sets one replaces ours.

## What is not versioned

Configs, fetched data, output, logs and the generated plists are all gitignored -
the repo holds code and prompts only. A clone is not a restorable setup, and
Keychain secrets exist nowhere but the Keychain. If the user asks about backup
or moving to a new machine, that gap is the answer, not a git operation.

## Commands

    python3 dashboard.py check | boards | fetch | rank | render | run [profile]
    launchctl kickstart -p gui/$(id -u)/com.<you>.flex-dashboard
    launchctl kickstart -p gui/$(id -u)/com.<you>.flex-dashboard-personal
    python3 icsdebug.py /tmp/cal.ics     # structural report on an ICS feed

`render` rebuilds from saved ranked data with no API calls and no tokens — use
it for every layout iteration.
