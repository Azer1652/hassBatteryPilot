# Workflow

## Branches

- Work on a feature branch and push it. Home Assistant installs from `main`.
- `main` moves only by fast-forward, and only when the owner asks for it in that conversation.
  An earlier "yes" does not cover a later change. No force pushes, no merge commits.
- No pull request unless the owner asks for one.
- Before moving `main`: the work tree is clean, `python tools/check.py` passed, and `origin/main`
  is an ancestor of the commit.

## Commits

- One commit per finished change, with a subject that says what changed for the user
  ("Battery Pilot 2.11.1: ..." for a release) and a body with the why.
- Attribution lines come from the session's own instructions; do not copy them from old commits
  and do not put model names anywhere in the repository.

## What must never be committed

- Personal data: the owner's entity names, rates, address, phone or car details, exported
  settings files, tariff cards, screenshots of a real installation. Entities and rates come only
  from the app's options; the repository holds public defaults.
- pip dependencies in the image (`battery_pilot/Dockerfile` uses Alpine packages only).

## Telling the owner

- Say what was tested and how (unit tests, the browser check, the simulator), and what was not:
  see `docs/agents/unverified.md`.
- A new default does not reach an existing install; say what the owner has to set.
- When a setting must be changed by hand, name the setting as the Configuration tab shows it and
  its key.

## A session in the cloud

- The container is temporary: commit and push what should survive.
- Outside hosts may be blocked. When a download fails with a proxy or DNS error, do not retry:
  ask for the file or for the host to be allowed.
