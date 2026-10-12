// Optional real-browser regression: start tests.local_server and the static server first.
// Set PLAYWRIGHT_MODULE_PATH to an existing Playwright package; no installation required.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || "playwright");
const artifacts = "test-artifacts";
fs.mkdirSync(artifacts, { recursive: true });
const fakeAppSdk = "export function initializeApp() { return {}; }";
const fakeAuthSdk = `
  const auth = { currentUser: null }; let notify;
  export function getAuth() { return auth; }
  export class GoogleAuthProvider {}
  export function onIdTokenChanged(value, callback) { notify = callback; callback(null); }
  export async function signInWithPopup() {
    auth.currentUser = { displayName: 'Local Test Account', getIdToken: async () => 'isolated-fake-token' };
    notify(auth.currentUser);
  }
  export async function signOut() { auth.currentUser = null; notify(null); }
`;
async function noOverflow(page) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "No horizontal overflow");
}
async function solve(page) {
  const question = await page.locator("#question").innerText();
  const [, left, operator, right] = question.match(/(\d+)\s*([+−×])\s*(\d+)/);
  const answer = operator === "+" ? +left + +right : operator === "−" ? +left - +right : +left * +right;
  await page.locator("#answer-input").fill(String(answer));
  await page.locator("#answer-input").press("Enter");
}
(async () => {
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || "msedge", headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1672, height: 941 }, reducedMotion: "reduce" });
    // Replace only external Firebase modules. The actual auth lifecycle and UI run unchanged.
    await context.route("https://www.gstatic.com/firebasejs/**", async route => {
      const body = route.request().url().endsWith("firebase-app.js") ? fakeAppSdk : fakeAuthSdk;
      await route.fulfill({ status: 200, contentType: "text/javascript", body, headers: { "Access-Control-Allow-Origin": "*" } });
    });
    const host = await context.newPage();
    const guest = await context.newPage();
    const errors = [];
    for (const page of [host, guest]) page.on("pageerror", error => errors.push(error.message));
    await host.goto("http://localhost:5500");
    await host.waitForFunction(() => document.getElementById("leaderboard-status").textContent.includes("No ranked"));
    assert.equal(await host.locator("#home-title").innerText(), "Speed-Demons");
    await noOverflow(host);
    await host.screenshot({ path: `${artifacts}/homepage-desktop.png`, fullPage: true });
    await host.getByRole("button", { name: "User Guide" }).click();
    assert.equal(await host.locator("#guide-dialog").evaluate(dialog => dialog.open), true);
    await host.keyboard.press("Escape");
    assert.equal(await host.locator("#guide-dialog").evaluate(dialog => dialog.open), false);
    assert.equal(await host.locator("#guide-btn").evaluate(button => button === document.activeElement), true);
    await host.getByRole("button", { name: "Sign In", exact: true }).click();
    await host.getByRole("button", { name: "Local Test Account · Sign Out" }).waitFor();
    await host.screenshot({ path: `${artifacts}/homepage-account.png`, fullPage: true });
    await host.getByRole("button", { name: "Local Test Account · Sign Out" }).click();
    await host.getByRole("button", { name: "Sign In", exact: true }).waitFor();
    for (const width of [320, 390, 768]) {
      await host.setViewportSize({ width, height: 844 });
      await noOverflow(host);
      if (width === 390) await host.screenshot({ path: `${artifacts}/homepage-mobile.png`, fullPage: true });
    }
    await host.setViewportSize({ width: 1672, height: 941 });
    // Real HTTP empty boards, tabs and duration changes.
    await host.getByRole("tab", { name: "All-Time" }).click();
    await host.waitForFunction(() => document.getElementById("leaderboard-status").textContent.includes("No ranked"));
    await host.locator("#leaderboard-duration").selectOption("600");
    await host.getByRole("button", { name: /Refresh/ }).click();
    await host.waitForFunction(() => document.getElementById("leaderboard-status").textContent.includes("No ranked"));
    // Isolated HTTP fixture proves populated, loading, error and hostile-name rendering.
    let boardMode = "rows";
    await host.route("**/leaderboard?**", async route => {
      if (boardMode === "error") return route.fulfill({ status: 503, body: "Unavailable" });
      await new Promise(resolve => setTimeout(resolve, 150));
      return route.fulfill({ json: { entries: Array.from({ length: 10 }, (_, index) => ({ name: index ? `Fixture Player ${index + 1}` : "<img src=x onerror=alert(1)>", score: 20 - index })), week: null } });
    });
    await host.getByRole("button", { name: /Refresh/ }).click();
    assert.match(await host.locator("#leaderboard-status").innerText(), /Loading/);
    await host.locator("#leaderboard-list li").nth(9).waitFor();
    assert.equal(await host.locator("#leaderboard-list img").count(), 0);
    await host.screenshot({ path: `${artifacts}/leaderboard-fixture.png`, fullPage: true });
    boardMode = "error";
    await host.getByRole("button", { name: /Refresh/ }).click();
    await host.waitForFunction(() => document.getElementById("leaderboard-status").textContent.includes("unavailable"));
    await host.unroute("**/leaderboard?**");
    // Guest multiplayer uses the real local WebSocket server and server scoring.
    await host.locator("#nickname").fill("Host");
    await host.getByRole("button", { name: /Create Room/ }).click();
    await host.locator("#lobby-screen").waitFor({ state: "visible" });
    const code = await host.locator("#room-code-display").innerText();
    await guest.goto("http://localhost:5500");
    await guest.locator("#nickname").fill("Guest");
    await guest.locator("#room-code-input").fill(code);
    await guest.getByRole("button", { name: "Join Room", exact: true }).click();
    await guest.locator("#lobby-screen").waitFor({ state: "visible" });
    await host.waitForFunction(() => !document.getElementById("start-btn").disabled);
    await host.screenshot({ path: `${artifacts}/lobby-desktop.png`, fullPage: true });
    await host.getByRole("button", { name: /Start Game/ }).click();
    await host.locator("#game-screen").waitFor({ state: "visible" });
    await guest.locator("#game-screen").waitFor({ state: "visible" });
    for (const page of [host, guest]) {
      assert.equal(await page.locator("#home-screen").isVisible(), false);
      assert.equal(await page.locator(".leaderboard-card").isVisible(), false);
      assert.equal(await page.locator("#sign-in-btn").isVisible(), false);
      assert.equal(await page.locator("#guide-btn").isVisible(), false);
      assert.equal(await page.locator("#answer-input").evaluate(input => input === document.activeElement), true);
      assert.equal(await page.locator("#game-room-code").innerText(), code);
    }
    await solve(host);
    await host.waitForFunction(() => document.getElementById("score").textContent === "1");
    await host.screenshot({ path: `${artifacts}/gameplay-desktop.png`, fullPage: true });
    for (const width of [320, 390, 768]) {
      await guest.setViewportSize({ width, height: 844 });
      await noOverflow(guest);
      assert.ok((await guest.locator("#submit-btn").boundingBox()).y + 64 < 844, "Answer controls visible without scrolling");
      if (width === 390) await guest.screenshot({ path: `${artifacts}/gameplay-mobile.png`, fullPage: true });
    }
    await solve(guest);
    await guest.waitForFunction(() => document.getElementById("score").textContent === "1");
    // Wait only the shortest actual round; longer durations use accelerated backend tests.
    await host.waitForFunction(() => document.getElementById("winner-text").textContent.startsWith("Tie:"), null, { timeout: 65000 });
    await host.screenshot({ path: `${artifacts}/results-desktop.png`, fullPage: true });
    assert.match(await host.locator("#results-list").innerText(), /Host[\s\S]*1 pts/);
    await host.getByRole("button", { name: /Play Again/ }).click();
    await host.locator("#lobby-screen").waitFor({ state: "visible" });
    await guest.locator("#lobby-screen").waitFor({ state: "visible" });
    await host.getByRole("button", { name: /Start Game/ }).click();
    await guest.locator("#game-screen").waitFor({ state: "visible" });
    assert.equal(await guest.locator("#score").innerText(), "0");
    assert.equal(await guest.locator("#question-number").innerText(), "Question 1");
    assert.deepEqual(errors, []);
    console.log("Real Edge browser: guest/account layouts (isolated SDK), guide keyboard focus, 320/390/768/1672 widths, leaderboard HTTP/fixtures, two-player round, server-confirmed tie, replay and second-round reset passed; no page errors.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
