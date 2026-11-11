---
name: verify-page
description: Check the Battery Pilot page in a real browser after changing battery_pilot/pilot/static/index.html, web.py or App.status - every tab at phone and desktop width, with screenshots to look at. Use before saying a page change works.
---

# Verify the page

1. Run `bash tools/page.sh --shots <folder>` with a folder in your scratch space. It starts the
   quick demo (about 20 s), opens every tab at phone and desktop width, and fails on JavaScript
   errors, sideways scrolling, a tab showing another tab's card, or a chart that did not take the
   card's width.
2. Look at the screenshots of the tabs you changed (`phone-<tab>.png`, `desktop-<tab>.png`).
   The check cannot judge layout or wording.
3. For something the check does not click (a button, a form), drive it with a short Playwright
   script against `python tools/demo_dashboard.py --quick` (wait for "Serving on"), as
   `tools/pagecheck.cjs` does.
4. If the look of the page changed, regenerate the docs screenshots: `python
   tools/demo_dashboard.py` (a full year, about 5 minutes) and `node tools/screenshots.cjs`.
5. Say that this was a desktop browser with demo data: the Companion app's web view is not
   covered (`docs/agents/unverified.md`).
