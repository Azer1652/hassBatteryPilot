---
name: release
description: Prepare a Battery Pilot release - bump the version, write the CHANGELOG entry, check everything, commit and push the work branch. Use when a change is finished and should reach users, or when asked to "release", "bump the version" or "cut X.Y.Z".
---

# Release Battery Pilot

A release is a new `version` in `battery_pilot/config.yaml` plus an entry in
`battery_pilot/CHANGELOG.md`. Home Assistant offers it once `main` has it.

1. Pick the version: patch for a fix, minor for a feature. Run `python tools/release.py X.Y.Z`.
2. Write the entry under the new heading in `battery_pilot/CHANGELOG.md`: what changes for the
   user, in plain words, most important first. Say when existing installs must change a setting
   themselves (an update never changes saved options).
3. Update `battery_pilot/DOCS.md` if users see the change, and `docs/agents/` if the code's shape,
   a recipe or the list of unverified behaviour changed.
4. Run `python tools/check.py` (the full run, not `--fast`). If the page changed, also
   `bash tools/page.sh --shots <folder>` and look at the screenshots.
5. Commit and push the work branch.
6. Do **not** move `main` unless the owner asked for it in this conversation. When they did:
   check the tree is clean and `origin/main` is an ancestor, then `git push origin HEAD:main`
   (a plain push, never forced) and confirm both refs point at the same commit.
7. Tell the owner: the version, what it does, what was tested and what is unverified on the real
   inverter (`docs/agents/unverified.md`), and anything they must set by hand.
