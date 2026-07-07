# The Football Report — Daily Briefing Agent

Autonomous agent that generates and publishes one daily football briefing to
[thefootball.report](https://thefootball.report), with no human intervention.

Editorial standard: [`system_prompt.md`](./system_prompt.md) — the single source of truth for
tone, structure, sourcing, and non-negotiable rules. Do not edit without understanding the
editorial implications.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.local.example .env.local   # fill in real values, never commit this file
```

## Running locally

```bash
python agent.py           # connection check against Anthropic, API-Football, WordPress
python agent.py run       # the actual daily job: collect data, generate, publish
```

`PUBLISH_STATUS=draft` (the default) creates a WordPress draft instead of publishing live —
set `PUBLISH_STATUS=publish` to publish for real.

## Secrets

In production these are set as GitHub Actions repository secrets (`ANTHROPIC_API_KEY`,
`API_FOOTBALL_KEY`, `WP_URL`, `WP_USERNAME`, `WP_APP_PASSWORD`) — never committed to the
repo. `WP_APP_PASSWORD` is a WordPress Application Password, not the account login password.
