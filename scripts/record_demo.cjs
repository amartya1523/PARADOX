// Records the README demo: a scripted investigation of the Heat Dome evening.
// usage: node scripts/record_demo.cjs [http://127.0.0.1:8642] [out-dir]
// Needs Playwright (PLAYWRIGHT_PATH=/path/to/node_modules/playwright) and a Chrome/Chromium
// (CHROME=/path/to/chrome). The server must be running: python -m paradox.cli serve
const path = require("path");
const { chromium } = require(process.env.PLAYWRIGHT_PATH || "playwright");

const URL = process.argv[2] || "http://127.0.0.1:8642";
const OUT = process.argv[3] || path.join(__dirname, "..", "out", "video");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function scrub(page, from, to, ms) {
  await page.evaluate(async ([a, b, d]) => {
    const t0 = performance.now();
    await new Promise((res) => {
      const step = () => {
        const f = Math.min(1, (performance.now() - t0) / d);
        S.t = a + (b - a) * f; document.getElementById("time").value = S.t;
        f < 1 ? requestAnimationFrame(step) : res();
      };
      step();
    });
  }, [from, to, ms]);
}

async function smoothScroll(page, selector, px, ms) {
  await page.evaluate(async ([sel, dy, d]) => {
    const el = document.querySelector(sel), y0 = el.scrollTop, t0 = performance.now();
    await new Promise((res) => {
      const step = () => { const f = Math.min(1, (performance.now() - t0) / d); el.scrollTop = y0 + dy * f; f < 1 ? requestAnimationFrame(step) : res(); };
      step();
    });
  }, [selector, px, ms]);
}

async function caption(page, text) {
  await page.evaluate((t) => {
    let c = document.getElementById("demoCaption");
    if (!c) {
      c = document.createElement("div"); c.id = "demoCaption";
      Object.assign(c.style, { position: "fixed", left: "50%", top: "62px", transform: "translateX(-50%)", zIndex: 99,
        background: "rgba(9,12,18,.92)", border: "1px solid rgba(255,181,71,.55)", color: "#ffb547", padding: "8px 16px",
        font: "600 14px ui-monospace, Menlo, monospace", borderRadius: "8px", letterSpacing: ".02em", maxWidth: "80%", textAlign: "center" });
      document.body.appendChild(c);
    }
    c.textContent = t; c.style.opacity = t ? "1" : "0";
  }, text);
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1,
    recordVideo: { dir: OUT, size: { width: 1440, height: 900 } } });
  const page = await ctx.newPage();
  await page.goto(URL);
  await page.waitForSelector("#lateList table");
  await caption(page, "PARADOX · a synthetic city on a heat-dome evening · 50,000 buildings · 500 electrical components");
  await scrub(page, 17 * 60 + 40, 18 * 60 + 14, 2500);
  await caption(page, "18:15 — substation S3 fails. Blocks go dark, signals fail, hospital H04's generator does not start.");
  await scrub(page, 18 * 60 + 14, 19 * 60 + 40, 6000);
  await sleep(800);

  await caption(page, "WHY was this patient reached in 19.7 minutes? — counterfactual tests, not correlation");
  await page.click("#lateList tr.click");
  await page.waitForSelector("#whyOut .cmp", { timeout: 60000 });
  await sleep(1800);
  await smoothScroll(page, "#tab-investigate", 420, 2500);
  await sleep(1500);
  await smoothScroll(page, "#tab-investigate", 520, 3000);
  await sleep(1500);
  await smoothScroll(page, "#tab-investigate", 700, 3000);
  await sleep(1500);

  await caption(page, "WHAT IF substation S3 had not failed? — branch reality with the same luck, compare with intervals");
  await page.click('#tabs button[data-tab="whatif"]');
  await page.selectOption("#runs", "12");
  await sleep(800);
  await page.click("#runWhatif");
  await page.waitForSelector("#whatifOut .cmp", { timeout: 120000 });
  await sleep(600);
  await scrub(page, 18 * 60, 20 * 60 + 30, 6000);
  await smoothScroll(page, "#tab-whatif", 380, 2500);
  await sleep(1200);
  await smoothScroll(page, "#tab-whatif", 900, 4000);
  await sleep(1200);
  await smoothScroll(page, "#tab-whatif", 1600, 4000);
  await sleep(1500);

  await caption(page, "AUDIT — the twin finds its own blind spot: a fuel pump wired to the wrong bus (hidden reality)");
  await page.click('#tabs button[data-tab="evidence"]');
  await page.click("#runAudit");
  await page.waitForSelector("#evOut table", { timeout: 120000 });
  await sleep(4500);

  await caption(page, "PLAN — the cheapest irreducible fix, scored on evenings the optimiser never saw");
  await page.click('#tabs button[data-tab="plan"]');
  await page.click("#runPlan");
  await page.waitForSelector("#planOut svg", { timeout: 180000 });
  await sleep(1500);
  await smoothScroll(page, "#tab-plan", 500, 3000);
  await sleep(2000);

  await caption(page, "PQL — a temporal query language: AS OF, KNOWN AT, WHAT IF, WHY, |> pipelines");
  await page.click('#tabs button[data-tab="console"]');
  await page.fill("#pql", "");
  await page.type("#pql", "AS OF 18:40 SHOW DEPENDENTS OF substation:S3 DEPTH 2", { delay: 35 });
  await page.click("#runPql");
  await sleep(2500);
  await caption(page, "Every answer is pinned to hash-chained branch heads and issued a verifiable receipt");
  await page.click('#tabs button[data-tab="ledger"]');
  await page.waitForSelector("#ledgerOut button[data-v]");
  await sleep(1200);
  await page.click("#ledgerOut button[data-v]");
  await sleep(3500);
  await caption(page, "");
  await sleep(500);
  const video = page.video();
  await ctx.close();
  console.log(await video.path());
  await browser.close();
})();
