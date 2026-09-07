# Flex Dashboard

Renders a next-day one-page dashboard matching the Rocketbook Flex dashboard
page - GOALS / TASKS (14 checkboxes) / PRIORITIES 1-3 / EVENTS / NOTES - to be
copied into the paper planner by hand.

## Two pages

    dashboard.py <cmd>            work      -> publish/           Sun-Thu 21:00
    dashboard.py <cmd> personal   personal  -> publish-personal/  daily 21:05

Point a separate Evernote sync folder at each.

## Running

    python3 dashboard.py check     verify every credential, one line each
    python3 dashboard.py boards    list Jira boards (to set board_id)
    python3 dashboard.py fetch     pull all sources -> data/<date>.json
    python3 dashboard.py rank      cut to what fits -> data/<date>.ranked.json
    python3 dashboard.py render    rebuild the page from ranked data (no API calls)
    python3 dashboard.py run       the whole pipeline; this is what launchd calls

Always `/usr/bin/python3`, never pyenv - the Keychain ACL is granted to that
exact binary, and a pyenv upgrade would silently break it.

## Schedule

LaunchAgent `com.<you>.flex-dashboard`, Sun-Thu at 21:00, producing Mon-Fri
pages. Logs to logs/run.log.

    launchctl kickstart -p gui/$(id -u)/com.<you>.flex-dashboard    # run now
    launchctl bootout gui/$(id -u)/com.<you>.flex-dashboard         # disable

launchd has no terminal. If the Keychain ever prompts, the job hangs forever
with no visible error - which is why the ACL binary must not change.

## Output

    out/<date>.html    working copy
    out/index.html     stable local address (deliberately NOT published -
                       a file that changes nightly would create a duplicate
                       Evernote note every day)
    publish/<date>.pdf one file per day; the Evernote sync folder watches here
    publish/<date>.html

PDF comes from headless Chrome, which is the only thing on a stock Mac that
renders CSS grid faithfully. If Chrome is missing the run still produces HTML.

## Sources and what joins them

    fetch/jira.py     Jira Cloud REST v3 + Agile API   (Basic: email + API token)
    fetch/github.py   api.github.com                   (Bearer: fine-grained PAT)
    fetch/google.py   Gmail + Calendar + Tasks REST    (OAuth refresh token)
    correlate.py      joins Jira <-> branches and PRs by ticket key
    rank.py           Anthropic API; cuts to 3 priorities and <=14 tasks
    render/           fills render/template.html

Two decisions worth preserving:

- **Board order, not JQL rank.** The top card of the To Do column comes from the
  Agile API, because a JQL search ordered by Rank ignores the board's own filter
  and sprint scope and picks the wrong issue. `sprint_id` is null and
  `use_active_sprint` is true, so sprint rollover needs no maintenance.
- **The model chooses what, code supplies facts.** Jira status and PR ownership
  shown on the page are looked up from raw source data at render time, never
  carried by the model, so they cannot be hallucinated.

## Secrets

macOS login Keychain, service `flex-dashboard`, never on disk:

    jira_api_token  github_token  anthropic_api_key
    google_client_id  google_client_secret  google_refresh_token

    python3 creds.py set <name>
    python3 fetch/google.py auth      # re-run if Google scopes change

## Two shells

Claude reaches this folder through a sandboxed Linux VM whose network is behind
an egress allowlist: api.github.com and api.anthropic.com reachable,
api.atlassian.com and *.googleapis.com blocked. That VM only writes files. The
job runs under macOS launchd, outside that proxy, where everything is reachable.
So Jira and Google calls cannot be tested from Claude's shell - those get
verified by running in Terminal.

## If the PDFs do not show up in Evernote

The generation side and the delivery side fail differently, and step 1 tells
them apart:

1. `ls -lt publish publish-personal` - if today's PDFs are there with the right
   timestamp, the pipeline worked and the problem is Evernote, not this code.
2. `tail -25 logs/run.log` / `logs/run-personal.log` - what the job actually did.
3. `launchctl print gui/$(id -u)/com.<you>.flex-dashboard | grep -E "state|last exit"`
4. `pmset -g sched` - the mini must be awake at 21:00; `pmset repeat` holds only
   ONE schedule, so anything that sets its own will silently replace ours.

Evernote's sync folders only import while the Evernote app is RUNNING. It does
not sync in the background and it does not catch up on a schedule it missed
while closed - it imports when it next starts. This has already caused one
"missing" morning where the PDFs were on disk the whole time. Evernote is set to
open at login; if that ever gets turned off, the pages stop arriving with no
error anywhere in this project's logs.

## What is NOT in this repository

Deliberately excluded, so the repo can be shared without leaking a household:

    config.<profile>.json   secret calendar URLs, family addresses, account ids
    data/ out/ publish/     real email, tasks and calendar contents
    logs/                   run output
    com.*.plist             machine-specific; hold an absolute home directory
                            (see com.example.flex-dashboard.plist)

The consequence is that **cloning this repo does not restore a working setup.**
The code, the prompts and the layout are versioned; the configuration and the
credentials are not. Rebuilding from scratch means recreating one config file
per profile and every Keychain entry by hand - a fiddly hour, and some of it
(app passwords, OAuth refresh tokens) can only be reissued, not recovered.

So keep a private copy of the config files somewhere outside this repo: an
encrypted disk image, a password manager, an offline drive. `config.example.json`
and `config.example.local.json` show the shape of each, which tells you what to
recreate but not what the values were.

Secrets themselves live only in the macOS login Keychain under service
`flex-dashboard`. They are not in any file, backed up or otherwise, by design -
which also means a Keychain loss is unrecoverable and every token must be
reissued at its source.
