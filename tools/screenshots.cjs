// Screenshots of the Battery Pilot dashboard for the README and DOCS.
// Start the demo first:  python tools/demo_dashboard.py
// Then:                  node tools/screenshots.cjs [url] [output dir]
// Needs Playwright (npm i -g playwright, or NODE_PATH pointing at it).
const { chromium } = require("playwright");
const path = require("path");

const url = process.argv[2] || "http://127.0.0.1:8765/";
const out = process.argv[3] || path.join(__dirname, "..", "docs", "screenshots");

// The page shows one card at a time (tabs); "#all" shows every card, for the card shots.
async function page(browser, opts, hash = "") {
  const p = await browser.newPage(opts);
  const errors = [];
  p.on("pageerror", (e) => errors.push(e.message));
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await p.goto(url + hash);
  await p.waitForSelector("#plan svg");
  if (hash === "#all") await p.waitForSelector("#hday svg");
  await p.waitForTimeout(500);
  const overflow = await p.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  if (errors.length || overflow) throw new Error(`page errors ${JSON.stringify(errors)}, horizontal overflow ${overflow}`);
  return p;
}

const card = (p, inner) => p.locator(".card", { has: p.locator(inner) });

(async () => {
  const browser = await chromium.launch();
  const desk = await page(browser, { viewport: { width: 1100, height: 900 }, deviceScaleFactor: 2, colorScheme: "light" }, "#all");
  await desk.screenshot({ path: path.join(out, "overview.png"), fullPage: true });
  await card(desk, "#plan").screenshot({ path: path.join(out, "plan.png") });
  await card(desk, "#hday").screenshot({ path: path.join(out, "history.png") });
  await card(desk, "#cmp").screenshot({ path: path.join(out, "comparison.png") });
  await desk.locator(".grid", { has: desk.locator("#shade") }).screenshot({ path: path.join(out, "shade-peaks.png") });

  const phone = await page(browser, { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, colorScheme: "light" });
  await phone.screenshot({ path: path.join(out, "phone.png") });

  const dark = await page(browser, { viewport: { width: 1100, height: 760 }, deviceScaleFactor: 2, colorScheme: "dark" });
  await dark.screenshot({ path: path.join(out, "dark.png") });

  await browser.close();
  console.log("screenshots written to", out);
})().catch((e) => { console.error(e); process.exit(1); });
