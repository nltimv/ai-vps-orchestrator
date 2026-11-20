// Dashboard end-to-end check against a running stack (fake cloud provider).
//   BASE_URL=http://localhost:5173 node e2e/walkthrough.cjs [screenshotDir]
const { chromium } = require("playwright");
const assert = require("node:assert/strict");

const BASE = process.env.BASE_URL ?? "http://localhost:5173";
const SHOTS = process.argv[2];
const JARGON = /\b(CFG|samplers?|schedulers?|denois\w*|LoRA|checkpoints?|latents?|VAE|fp8|bf16)\b/i;

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const shot = async (name, full = false) => SHOTS && page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: full });

  await page.goto(BASE);
  await page.getByText(/Welcome back|Set up aivo/).waitFor();
  if (await page.getByText("Set up aivo").isVisible()) {
    await page.getByLabel("Your name").fill("Admin");
  }
  await page.getByLabel("Email", { exact: true }).fill("e2e-admin@example.com");
  await page.getByLabel("Password").fill("correct horse battery");
  await page.getByRole("button", { name: /Create account|Sign in/ }).click();
  await page.getByText("What do you want to create?").waitFor();

  // image
  await page.getByLabel("Description").fill("A cozy cabin in a snowy forest at dusk");
  await page.getByRole("radio", { name: "Photo" }).click();
  await page.getByRole("radio", { name: /^Draft/ }).click();
  await page.getByText(/ready in/).waitFor({ timeout: 20000 });
  await shot("create");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await page.getByText(/Starting a machine|Loading the AI model|Creating/).first().waitFor({ timeout: 20000 });
  await shot("progress");

  // video + music queue onto the same machine
  await page.getByRole("tab", { name: "Video" }).click();
  await page.getByLabel("Description").fill("Waves crashing on black volcanic rocks, drone shot");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await page.getByRole("tab", { name: "Music" }).click();
  await page.getByLabel("Description").fill("mellow beat with soft piano");
  await page.getByRole("button", { name: "Lo-fi" }).click();
  await page.getByRole("button", { name: "Create", exact: true }).click();

  for (let i = 0; i < 120 && (await page.getByText(/Done.*view/).count()) < 3; i++) await page.waitForTimeout(1000);
  assert.ok((await page.getByText(/Done.*view/).count()) >= 3, "three creations finished");

  await page.goto(`${BASE}/gallery`);
  await page.locator('a[href^="/item/"]').first().waitFor();
  assert.ok((await page.locator('a[href^="/item/"]').count()) >= 3, "gallery shows the creations");
  await shot("gallery");

  // video seeking works through the encrypted store (HTTP Range)
  await page.getByRole("radio", { name: "Videos" }).click();
  await page.waitForTimeout(500);
  await page.locator('a[href^="/item/"]').first().click();
  await page.locator("video").waitFor();
  const seek = await page.evaluate(async () => {
    const v = document.querySelector("video");
    await new Promise((r) => (v.readyState >= 1 ? r() : v.addEventListener("loadedmetadata", r, { once: true })));
    v.currentTime = v.duration / 2;
    await new Promise((r) => v.addEventListener("seeked", r, { once: true }));
    return { duration: v.duration, t: v.currentTime };
  });
  assert.ok(seek.duration > 1 && seek.t > 0.4 * seek.duration, `video seek ${JSON.stringify(seek)}`);
  await shot("item");

  for (const p of ["/", "/gallery", "/machines", "/spending", "/settings"]) {
    await page.goto(BASE + p);
    await page.waitForTimeout(1000);
    const text = await page.evaluate(() => document.body.innerText);
    const m = text.match(JARGON);
    assert.equal(m, null, `jargon "${m?.[0]}" on ${p}`);
    await shot(p === "/" ? "home" : p.slice(1), p !== "/");
  }
  assert.deepEqual(errors, [], "no page errors");
  await browser.close();
  console.log("e2e walkthrough: OK");
})().catch((e) => {
  console.error("e2e walkthrough FAILED:", e);
  process.exit(1);
});
