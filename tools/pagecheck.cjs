// Check the Battery Pilot page in a real browser: every tab, at phone and desktop width.
// Start the demo first:  python tools/demo_dashboard.py --quick      (or use: bash tools/page.sh)
// Then:                  node tools/pagecheck.cjs [url] [--shots <dir>]
//
// Fails (exit 1) on: a JavaScript error, horizontal overflow, a tab that shows another tab's
// card, or a chart narrower than its card. With --shots it saves one screenshot per tab and
// width, to look at. Needs Playwright (see tools/screenshots.cjs).
const { chromium } = require("playwright");
const path = require("path");
const fs = require("fs");

const args = process.argv.slice(2);
const at = args.indexOf("--shots");
const shots = at >= 0 ? args.splice(at, 2)[1] : null;
const url = args[0] || "http://127.0.0.1:8765/";
const SIZES = [["phone", 390, 844], ["desktop", 1100, 800]];

(async () => {
  if (shots) fs.mkdirSync(shots, { recursive: true });
  const browser = await chromium.launch();
  const problems = [];
  for (const [name, width, height] of SIZES) {
    const p = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
    p.on("pageerror", (e) => problems.push(`${name}: page error: ${e.message}`));
    p.on("console", (m) => { if (m.type() === "error") problems.push(`${name}: console error: ${m.text()}`); });
    await p.goto(url);
    await p.waitForSelector("#tabs button");
    await p.waitForSelector("#plan svg");
    const tabs = await p.$$eval("#tabs button", (bs) => bs.map((b) => b.dataset.show));
    for (const tab of tabs) {
      await p.click(`#tabs [data-show="${tab}"]`);
      await p.waitForTimeout(400);
      const r = await p.evaluate(() => {
        const visible = (e) => e.offsetParent !== null;
        const cards = [...document.querySelectorAll("[data-tab]")].filter(visible);
        return {
          shown: [...new Set(cards.map((e) => e.dataset.tab))],
          overflow: document.documentElement.scrollWidth > window.innerWidth,
          narrow: [...document.querySelectorAll("[data-tab] .hchart")].filter(visible).filter((el) => {
            const svg = el.querySelector("svg");
            return svg && svg.getBoundingClientRect().width < el.clientWidth * 0.6;
          }).map((el) => el.id),
        };
      });
      if (r.shown.length !== 1 || r.shown[0] !== tab) problems.push(`${name}/${tab}: shows ${JSON.stringify(r.shown)}`);
      if (r.overflow) problems.push(`${name}/${tab}: the page scrolls sideways`);
      if (r.narrow.length) problems.push(`${name}/${tab}: chart narrower than its card: ${r.narrow.join(", ")}`);
      if (shots) await p.screenshot({ path: path.join(shots, `${name}-${tab}.png`) });
      console.log(`${name}/${tab}: ok`);
    }
    await p.close();
  }
  await browser.close();
  if (problems.length) { console.error("PROBLEMS:\n  " + problems.join("\n  ")); process.exit(1); }
  console.log("page check passed" + (shots ? `; screenshots in ${shots}` : ""));
})().catch((e) => { console.error(e); process.exit(1); });
