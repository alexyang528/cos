# cos

A personal chief of staff built on Claude Code. Every working hour it scans Slack, Gmail, Notion, Granola,
Switchboard and GitHub for things that need you. An intern triages them, and a lead per source researches each one
and files it in a local queue. Each item has the context and links you need, and often a ready-to-launch agent
prompt. Nothing is ever sent on your behalf; acting on an item is always your click.

It runs on your Mac, uses your own Claude Code login and claude.ai connectors, and keeps everything it reads in
`data/`, which never leaves your machine.

## Setup

1. Install [uv](https://docs.astral.sh/uv/), [Claude Code](https://claude.com/claude-code) and the
   [GitHub CLI](https://cli.github.com/), sign in to Claude Code, and run `gh auth login`. The GitHub scan uses
   `gh` to find PRs that ask for your review and reviews of your own PRs.
2. In claude.ai connector settings, connect Slack, Gmail, Notion, Granola and GitHub (plus Switchboard and the
   Check data connectors if you use them). The agents call these through `claude -p`.
3. Fork this repo and clone your fork.
4. `cp me.example.toml me.toml` and fill it in: your name, email, what you work on, your Slack and Notion IDs,
   the folders agent sessions start in, and where they open (`launcher`: the Claude desktop app, a terminal, or
   Orca). `me.toml` is gitignored.
5. `uv run python cos.py migrate` to create the database.
6. `uv run python cos.py tick --force` once by hand to check that the scans work, then `uv run python cos.py serve --open`
   to see the queue.
7. `uv run python cos.py schedule install` to run it hourly on work days (launchd). `schedule uninstall` stops it.

## Where things live

- `me.toml`: everything personal. The lead charters in `leads/` use `{me}`, `{about}`, `{github_repo}` and
  similar placeholders, filled in from this file.
- `chief/config.py`: schedule, budgets and limits. The daily spend cap is `DAILY_BUDGET_USD`.
- `chief/policy.py`: which tools each agent may use. Every pass that does research is read-only.
- `data/`: the SQLite queue, run logs and scan snapshots.

`uv run python cos.py --help` lists the rest (status, runs, queue, workstreams, policy).
