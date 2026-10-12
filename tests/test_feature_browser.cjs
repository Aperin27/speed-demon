// Requires the isolated tests.local_server and static frontend server on localhost.
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || "playwright");
const channel = process.env.BROWSER_CHANNEL || "msedge";
const sdkApp = "export function initializeApp(){return {}}";
function sdkAuth(token) {
  return `const auth={currentUser:null};let notify;export function getAuth(){return auth}
    export class GoogleAuthProvider{};export function onIdTokenChanged(a,cb){notify=cb;cb(null)}
    export async function signInWithPopup(){auth.currentUser={displayName:'Private Google Name',getIdToken:async()=>${JSON.stringify(token)}};notify(auth.currentUser)}
    export async function signOut(){auth.currentUser=null;notify(null)}`;
}
async function context(browser, token) {
  const value = await browser.newContext({ viewport: { width: 1366, height: 900 } });
  await value.grantPermissions(["clipboard-read", "clipboard-write"], { origin: "http://localhost:5500" });
  await value.route("https://www.gstatic.com/firebasejs/**", route => route.fulfill({ status: 200, contentType: "text/javascript", headers: { "Access-Control-Allow-Origin": "*" }, body: route.request().url().endsWith("firebase-app.js") ? sdkApp : sdkAuth(token) }));
  return value;
}
async function answer(page, correct) {
  const text = await page.locator("#question").innerText();
  const [, left, operator, right] = text.match(/(\d+)\s*([+−×])\s*(\d+)/);
  const expected = operator === "+" ? +left + +right : operator === "−" ? +left - +right : +left * +right;
  await page.locator("#answer-input").fill(String(correct ? expected : 999));
  await page.locator("#answer-input").press("Enter");
}
(async () => {
  const browser = await chromium.launch({ channel, headless: true });
  try {
    const hostContext = await context(browser, "local-smoke-account-a");
    const otherContext = await context(browser, "local-smoke-account-b");
    const host = await hostContext.newPage();
    const other = await otherContext.newPage();
    const errors = [];
    for (const page of [host, other]) page.on("pageerror", error => errors.push(error.message));
    await host.goto("http://localhost:5500");
    await host.getByRole("button", { name: "Sign In", exact: true }).click();
    // Edge creates the account; Chrome verifies the same permanent profile on a second run.
    const registered = await hostContext.request.get("http://localhost:8000/profile", { headers: { Authorization: "Bearer local-smoke-account-a" } });
    const profile = await registered.json();
    if (!profile.username) {
      await host.getByRole("button", { name: "Choose username", exact: true }).click();
      assert.match(await host.locator("#username-dialog .username-warning").innerText(), /Once registered, your username cannot be changed/i);
      await host.locator("#username-input").fill("Feature_Player");
      assert.equal(await host.locator("#username-register").isDisabled(), true);
      await host.locator("#username-confirm").check();
      await host.screenshot({ path: `test-artifacts/username-registration-${channel}.png`, fullPage: true });
      await host.getByRole("button", { name: "Register permanently", exact: true }).click();
      await host.getByRole("heading", { name: "Register Feature_Player permanently?", exact: true }).waitFor();
      const unconfirmed = await hostContext.request.get("http://localhost:8000/profile", { headers: { Authorization: "Bearer local-smoke-account-a" } });
      assert.equal((await unconfirmed.json()).username, null, "No registration before final confirmation");
      await host.screenshot({ path: `test-artifacts/username-final-confirmation-${channel}.png`, fullPage: true });
      await host.getByRole("button", { name: "Go back", exact: true }).click();
      await host.getByRole("button", { name: "Register permanently", exact: true }).click();
      await host.getByRole("button", { name: "Confirm registration", exact: true }).click();
      await host.locator("#username-dialog").waitFor({ state: "hidden" });
    }
    await host.waitForFunction(() => document.getElementById("nickname").value === "Feature_Player" && document.getElementById("nickname").readOnly);
    assert.equal(await host.locator("#username-prompt").isVisible(), false);
    const rename = await hostContext.request.post("http://localhost:8000/username", { headers: { Authorization: "Bearer local-smoke-account-a" }, data: { username: "Different_Name", uid: "forged" } });
    assert.equal(rename.status(), 409);
    await other.goto("http://localhost:5500");
    await other.getByRole("button", { name: "Sign In", exact: true }).click();
    await other.getByRole("button", { name: "Choose username", exact: true }).click();
    await other.locator("#username-input").fill("FEATURE_PLAYER");
    await other.locator("#username-confirm").check();
    await other.getByRole("button", { name: "Register permanently", exact: true }).click();
    await other.getByRole("button", { name: "Confirm registration", exact: true }).click();
    await other.waitForFunction(() => document.getElementById("username-message").textContent.includes("already registered"));
    await other.getByRole("button", { name: "Close username registration", exact: true }).click();
    for (const width of [320, 390, 1366]) {
      await host.setViewportSize({ width, height: 900 });
      assert.equal(await host.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    }
    await host.getByRole("button", { name: /Create Room/ }).click();
    await host.locator("#lobby-screen").waitFor({ state: "visible" });
    assert.equal(await host.locator("#lobby-eligibility").innerText(), "Ranked");
    assert.equal(await host.locator("#app-notice").isVisible(), false, "No Google identity banner");
    const code = await host.locator("#room-code-display").innerText();
    assert.match(code, /^[A-Z0-9]{6}$/);
    const guestContext = await context(browser, "unused");
    const guest = await guestContext.newPage();
    await guest.goto("http://localhost:5500");
    for (const page of [guest, other]) {
      await page.locator("#nickname").fill("fEaTuRe_PlAyEr");
      await page.getByRole("button", { name: /Create Room/ }).click();
      await page.waitForFunction(() => document.getElementById("app-notice").textContent.includes("belongs to a registered player"));
      await page.locator("#room-code-input").fill(code);
      await page.getByRole("button", { name: "Join Room", exact: true }).click();
      await page.waitForFunction(() => document.getElementById("app-notice").textContent.includes("belongs to a registered player"));
      assert.equal(await page.locator("#home-screen").isVisible(), true);
    }
    await guest.locator("#nickname").fill("BrowserGuest");
    await guest.getByRole("button", { name: "Join Room", exact: true }).click();
    await guest.locator("#lobby-screen").waitFor({ state: "visible" });
    assert.match(await host.locator("#players-list").innerText(), /Guest · BrowserGuest/);
    await guest.getByRole("button", { name: /Back/ }).click();
    await guestContext.close();
    await host.getByRole("button", { name: "Copy", exact: true }).click();
    await host.waitForFunction(() => document.getElementById("copy-status").textContent.includes("copied"));
    assert.equal(await host.evaluate(() => navigator.clipboard.readText()), code);
    await host.locator("#duration").click();
    await host.screenshot({ path: `test-artifacts/duration-dropdown-${channel}.png`, fullPage: true });
    await host.keyboard.press("Escape");
    await host.screenshot({ path: `test-artifacts/ranked-lobby-${channel}.png`, fullPage: true });
    await other.locator("#nickname").fill("Challenger");
    await other.locator("#room-code-input").fill(code);
    await other.getByRole("button", { name: "Join Room", exact: true }).click();
    await other.locator("#lobby-screen").waitFor({ state: "visible" });
    assert.match(await host.locator("#players-list").innerText(), /Guest · Challenger/);
    assert.equal(await other.locator("#lobby-eligibility").innerText(), "Username required");
    for (const width of [320, 390]) {
      await host.setViewportSize({ width, height: 844 });
      assert.equal(await host.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "New lobby controls fit mobile");
    }
    await host.screenshot({ path: `test-artifacts/ranked-lobby-mobile-${channel}.png`, fullPage: true });
    await host.setViewportSize({ width: 1366, height: 900 });
    if (process.env.UI_ONLY === "1") {
      console.log(`${channel}: targeted final profile/auth, username conflict, clipboard, native dropdown and mobile lobby checks passed.`);
      return;
    }
    await host.getByRole("button", { name: /Start Game/ }).click();
    await host.locator("#game-screen").waitFor({ state: "visible" });
    await other.locator("#game-screen").waitFor({ state: "visible" });
    await answer(host, true);
    await host.waitForFunction(() => document.getElementById("score").textContent === "1");
    await answer(host, false);
    await host.waitForFunction(() => document.getElementById("score").textContent === "0");
    await answer(host, false);
    await host.waitForFunction(() => document.getElementById("score").textContent === "-1");
    assert.match(await host.locator("#answer-feedback").innerText(), /−1 point/);
    await answer(other, false);
    await other.waitForFunction(() => document.getElementById("score").textContent === "-1");
    await host.waitForFunction(() => document.getElementById("winner-text").textContent.startsWith("Tie:"), null, { timeout: 65000 });
    assert.match(await host.locator("#results-list").innerText(), /Feature_Player[\s\S]*-1 pts/);
    await host.screenshot({ path: `test-artifacts/negative-results-${channel}.png`, fullPage: true });
    // Persistence is asynchronous; query the real isolated API after its cache expires.
    let board;
    for (let attempt = 0; attempt < 5; attempt++) {
      const response = await hostContext.request.get("http://localhost:8000/leaderboard?duration=60&period=all_time");
      board = await response.json();
      if (board.entries.some(entry => entry.name === "Feature_Player")) break;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    assert.equal(board.scoring_version, "arithmetic-v3");
    assert.ok(board.entries.some(entry => entry.name === "Feature_Player" && entry.score === -1));
    assert.equal(board.entries.some(entry => entry.name === "Challenger"), false);
    await host.getByRole("button", { name: /Play Again/ }).click();
    await host.locator("#lobby-screen").waitFor({ state: "visible" });
    await other.locator("#lobby-screen").waitFor({ state: "visible" });
    await host.getByRole("button", { name: /Start Game/ }).click();
    await host.locator("#game-screen").waitFor({ state: "visible" });
    assert.equal(await host.locator("#score").innerText(), "0");
    assert.deepEqual(errors, []);
    console.log(`${channel}: real local profile/registration, permanence, conflict, ranked/username-required states, copy, dropdown, +1/-1/negative scores, registered-name results, negative leaderboard persistence and replay passed (isolated SDK/Firebase).`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
