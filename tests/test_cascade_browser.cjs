// Optional real-browser regression. Start tests.local_server and the static server first.
// Set PLAYWRIGHT_MODULE_PATH to an existing Playwright package; no installation required.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || "playwright");
const artifacts = "test-artifacts";
const pageUrl = "http://localhost:5500";
fs.mkdirSync(artifacts, { recursive: true });

const fakeAppSdk = "export function initializeApp() { return {}; }";
const fakeAuthSdk = `
  const auth = { currentUser: null };
  export function getAuth() { return auth; }
  export class GoogleAuthProvider {}
  export function onIdTokenChanged(value, callback) { callback(null); }
  export async function signInWithPopup() { throw new Error('Guest-only cascade regression'); }
  export async function signOut() { auth.currentUser = null; }
`;

async function createContext(browser, reducedMotion) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion });
  await context.route("https://www.gstatic.com/firebasejs/**", route => route.fulfill({
    status: 200,
    contentType: "text/javascript",
    body: route.request().url().endsWith("firebase-app.js") ? fakeAppSdk : fakeAuthSdk,
    headers: { "Access-Control-Allow-Origin": "*" },
  }));
  // Observe the real canvas and animation clock without adding a production debug API.
  await context.addInitScript(() => {
    const metrics = window.__cascadeMetrics = { draws: 0, renders: 0, rafRequests: 0, rafCallbacks: 0, lastPaint: 0, renderSamples: [] };
    for (const method of ["clearRect", "fillRect", "fillText", "strokeText", "drawImage", "fill", "stroke"]) {
      const original = CanvasRenderingContext2D.prototype[method];
      CanvasRenderingContext2D.prototype[method] = function (...args) {
        if (this.canvas.id === "number-field") {
          metrics.draws += 1;
          if (method === "clearRect") {
            metrics.renders += 1;
            metrics.lastPaint = performance.now();
          }
        }
        return original.apply(this, args);
      };
    }
    const originalRaf = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = callback => {
      metrics.rafRequests += 1;
      return originalRaf(timestamp => {
        metrics.rafCallbacks += 1;
        const started = performance.now();
        const previousDraws = metrics.draws;
        try { return callback(timestamp); }
        finally {
          if (metrics.draws > previousDraws) {
            metrics.renderSamples.push({ duration: performance.now() - started, timestamp: metrics.lastPaint });
            if (metrics.renderSamples.length > 120) metrics.renderSamples.shift();
          }
        }
      });
    };
  });
  return context;
}

async function waitForHome(page) {
  await page.goto(pageUrl);
  await page.waitForFunction(() => document.getElementById("leaderboard-status").textContent.includes("No ranked"));
  await page.waitForFunction(() => window.__cascadeMetrics.draws > 0);
}

async function counters(page) {
  return page.evaluate(() => ({
    draws: window.__cascadeMetrics.draws,
    renders: window.__cascadeMetrics.renders,
    rafRequests: window.__cascadeMetrics.rafRequests,
    rafCallbacks: window.__cascadeMetrics.rafCallbacks,
  }));
}

async function assertStopped(page, label) {
  await page.waitForTimeout(100);
  const before = await counters(page);
  await page.waitForTimeout(350);
  assert.deepEqual(await counters(page), before, `${label}: no drawing or animation callbacks`);
}

async function assertMoving(page, label) {
  const before = await page.locator("#number-field").evaluate(canvas => canvas.toDataURL());
  const previousRenders = (await counters(page)).renders;
  await page.waitForFunction(previous => window.__cascadeMetrics.renders >= previous + 2, previousRenders, { timeout: 10000 });
  const after = await page.locator("#number-field").evaluate(canvas => canvas.toDataURL());
  assert.ok(after !== before, `${label}: actual canvas pixels change`);
}

function percentile(values, fraction) {
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * fraction))];
}

async function assertForeground(page) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "No horizontal overflow");
  assert.equal(await page.locator(".number-atmosphere").getAttribute("aria-hidden"), "true", "Numbers are decorative for assistive technology");
  assert.equal(await page.locator("#number-field").evaluate(canvas => canvas.tabIndex), -1, "Canvas is absent from keyboard navigation");
  for (const selector of ["#nickname", "#create-room-btn", "#room-code-input", "#join-room-btn", "#sign-in-btn", "#guide-btn", "#leaderboard-duration", "#leaderboard-refresh", "#weekly-tab", "#all-time-tab"]) {
    const element = page.locator(selector);
    assert.equal(await element.count(), 1, `${selector} exists`);
    if (!await element.isVisible()) continue;
    await element.evaluate(control => control.scrollIntoView({ block: "center", behavior: "instant" }));
    assert.equal(await element.evaluate(control => {
      const box = control.getBoundingClientRect();
      const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
      return control === hit || control.contains(hit);
    }), true, `${selector} receives pointer hits through the decoration`);
  }
  await page.evaluate(() => scrollTo(0, 0));
  // A decoration must not alter card geometry, text color, type size or backgrounds.
  const styles = await page.evaluate(() => {
    const selectors = ["#home-title", ".entry-card", ".leaderboard-card", "#nickname", "#create-room-btn", "#leaderboard-title", "#leaderboard-status"];
    const snapshot = () => selectors.map(selector => {
      const element = document.querySelector(selector);
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return [selector, style.color, style.backgroundColor, style.backgroundImage, style.fontSize, style.lineHeight, box.x, box.y, box.width, box.height];
    });
    const visible = snapshot();
    const decoration = document.querySelector(".number-atmosphere");
    const display = decoration.style.display;
    decoration.style.display = "none";
    const hidden = snapshot();
    decoration.style.display = display;
    return { visible, hidden };
  });
  assert.deepEqual(styles.visible, styles.hidden, "Foreground layout and readable text styles are unchanged by the canvas");
}

(async () => {
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || "msedge", headless: true });
  const errors = [];
  try {
    const context = await createContext(browser, "no-preference");
    const host = await context.newPage();
    const requests = [];
    function observePage(page) {
      page.on("pageerror", error => errors.push(error.message));
      page.on("request", request => requests.push(request.url()));
    }
    observePage(host);
    await waitForHome(host);
    assert.equal(await host.locator(".atmosphere").isVisible(), true, "Existing atmospheric forms remain on the homepage");
    await assertMoving(host, "Normal motion");
    for (const [width, height] of [[320, 568], [390, 844], [768, 1024], [1440, 900], [1920, 1080]]) {
      await host.setViewportSize({ width, height });
      await assertForeground(host);
      await host.waitForTimeout(150);
      await host.screenshot({ path: `${artifacts}/cascade-home-${width}.png`, fullPage: true });
    }
    await host.setViewportSize({ width: 1440, height: 900 });
    await host.bringToFront();
    const resizedRenders = (await counters(host)).renders;
    await host.waitForFunction(previous => window.__cascadeMetrics.renders >= previous + 2, resizedRenders, { timeout: 10000 });
    const initialRequests = requests.length;
    const sampleStart = await host.evaluate(() => performance.now());
    await host.waitForTimeout(1100);
    const renderSamples = await host.evaluate(start => window.__cascadeMetrics.renderSamples.filter(sample => sample.timestamp >= start), sampleStart);
    assert.equal(requests.length, initialRequests, "The animation makes no idle HTTP requests or leaderboard polls");
    assert.ok(requests.every(url => /^(?:http:\/\/(?:localhost|127\.0\.0\.1):(?:5500|8000)\/|https:\/\/www\.gstatic\.com\/firebasejs\/)/.test(url)), "Only local application requests and intercepted Firebase SDK modules are requested");

    // Headless Edge keeps background tabs visible. Exercise the browser visibility event
    // with a controlled hidden-state getter, and keep this distinction in test output.
    await host.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await assertStopped(host, "Hidden visibility event");
    await host.evaluate(() => {
      delete document.hidden;
      delete document.visibilityState;
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await assertMoving(host, "Visible visibility event resumes");

    await host.emulateMedia({ reducedMotion: "reduce" });
    await assertStopped(host, "Live reduced-motion preference");
    const staticBefore = await host.locator("#number-field").evaluate(canvas => canvas.toDataURL());
    await host.waitForTimeout(350);
    assert.ok(await host.locator("#number-field").evaluate(canvas => canvas.toDataURL()) === staticBefore, "Reduced-motion pixels stay static");
    await host.screenshot({ path: `${artifacts}/cascade-home-reduced-motion.png`, fullPage: true });
    await host.emulateMedia({ reducedMotion: "no-preference" });
    await assertMoving(host, "Live normal-motion preference resumes");

    await host.locator("#nickname").fill("Cascade Host");
    await host.getByRole("button", { name: /Create Room/ }).click();
    await host.locator("#lobby-screen").waitFor({ state: "visible" });
    assert.equal(await host.locator(".number-atmosphere").isVisible(), false, "Numbers are hidden in the real lobby");
    await assertStopped(host, "Lobby");
    await host.getByRole("button", { name: /Back/ }).click();
    await host.locator("#home-screen").waitFor({ state: "visible" });
    await assertMoving(host, "Returning home resumes");
    await host.getByRole("button", { name: /Create Room/ }).click();
    await host.locator("#lobby-screen").waitFor({ state: "visible" });
    const roomCode = await host.locator("#room-code-display").innerText();
    const guest = await context.newPage();
    observePage(guest);
    await waitForHome(guest);
    await guest.locator("#nickname").fill("Cascade Guest");
    await guest.locator("#room-code-input").fill(roomCode);
    await guest.getByRole("button", { name: "Join Room", exact: true }).click();
    await guest.locator("#lobby-screen").waitFor({ state: "visible" });
    await host.waitForFunction(() => !document.getElementById("start-btn").disabled);
    await host.getByRole("button", { name: /Start Game/ }).click();
    await Promise.all([host, guest].map(page => page.locator("#countdown-screen").waitFor({ state: "visible" })));
    for (const page of [host, guest]) {
      assert.equal(await page.locator(".number-atmosphere").isVisible(), false, "Numbers are hidden during real countdown");
      await assertStopped(page, "Countdown");
    }
    await host.locator("#game-screen").waitFor({ state: "visible" });
    await guest.locator("#game-screen").waitFor({ state: "visible" });
    for (const page of [host, guest]) {
      assert.equal(await page.locator(".number-atmosphere").isVisible(), false, "Numbers are hidden during real gameplay");
      await assertStopped(page, "Gameplay");
      assert.equal(await page.locator("#answer-input").evaluate(input => input === document.activeElement), true, "Gameplay answer focus remains intact");
    }
    await host.screenshot({ path: `${artifacts}/cascade-game-1440.png`, fullPage: true });

    const reducedContext = await createContext(browser, "reduce");
    const reducedPage = await reducedContext.newPage();
    reducedPage.on("pageerror", error => errors.push(error.message));
    await waitForHome(reducedPage);
    await assertStopped(reducedPage, "Initial reduced-motion preference");
    assert.equal((await counters(reducedPage)).rafRequests, 0, "Initial reduced motion never starts an animation frame loop");
    assert.deepEqual(errors, [], "No browser JavaScript errors");
    const durations = renderSamples.map(sample => sample.duration);
    const intervals = renderSamples.slice(1).map((sample, index) => sample.timestamp - renderSamples[index].timestamp);
    const performanceResult = durations.length >= 5
      ? `Drawing callbacks: median ${percentile(durations, .5).toFixed(2)} ms, p95 ${percentile(durations, .95).toFixed(2)} ms; median paint interval ${percentile(intervals, .5).toFixed(2)} ms (${durations.length} samples during a 1.1-second home idle window).`
      : "The idle sampling window produced too few drawing callbacks for performance statistics.";
    console.log(`Cascade ${process.env.BROWSER_CHANNEL || "msedge"} regression passed: 320/390/768/1440/1920 screenshots, foreground layout and pointer access, changing normal-motion pixels, static initial/live reduced motion, controlled hidden/visible events, idle network, real lobby/countdown/game stop and Back resume; no page errors. Headless tab visibility was event-controlled. ${performanceResult}`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
