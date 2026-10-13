// Start tests.local_server and the static frontend server first. All traffic is local.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || "playwright");
fs.mkdirSync("test-artifacts", { recursive: true });

async function closed(page) {
  await page.waitForFunction(() => !document.getElementById("room-loading").open);
  assert.equal(await page.locator("#loading-cascade > span").count(), 0, "Closing removes the decorative cascade");
  assert.equal(await page.locator(".hunting-number").count(), 0, "Closing removes all hunting targets");
}

async function layoutFits(page) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  const box = await page.locator("#room-loading").boundingBox();
  const viewport = page.viewportSize();
  assert.ok(box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width && box.y + box.height <= viewport.height);
  assert.equal(Math.round(box.width), viewport.width, "Overlay spans the entire viewport width");
  assert.equal(Math.round(box.height), viewport.height, "Overlay spans the entire viewport height");
  const sceneBox = await page.locator("#loading-scene").boundingBox();
  assert.equal(Math.round(sceneBox.width), viewport.width, "Hunting is not confined to a panel");
  assert.equal(Math.round(sceneBox.height), viewport.height);
  assert.equal(await page.locator("#room-loading").evaluate(dialog => dialog.scrollWidth > dialog.clientWidth), false);
  assert.equal(await page.locator("#room-loading").evaluate(dialog => getComputedStyle(dialog).backgroundColor), "rgba(0, 0, 0, 0)");
  assert.equal(await page.locator("#home-screen").isVisible(), true, "Homepage stays visible beneath the overlay");
  assert.equal(await page.locator("#lobby-screen").isVisible(), false);
  const reduced = await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches);
  assert.equal(await page.locator(".cascade-number").count(), reduced ? 16 : viewport.width <= 640 ? 36 : 64,
    "Cascade density follows viewport size and the motion preference");
  const cascadeMotion = await page.locator(".cascade-number").first().evaluate(element => getComputedStyle(element).animationName);
  assert.equal(cascadeMotion === "none", reduced, "Reduced motion leaves the atmospheric digits static");
  await page.locator("#loading-return").scrollIntoViewIfNeeded();
  const button = await page.locator("#loading-return").boundingBox();
  assert.ok(button.y >= 0 && button.y + button.height <= viewport.height);
  const silhouetteFits = await page.evaluate(async () => {
    const scene = document.getElementById("loading-scene").getBoundingClientRect();
    const limbs = document.querySelectorAll(".demon-head, .demon-cloak, .demon-arm-front, .demon-arm-back, .demon-leg-front, .demon-leg-back");
    const until = performance.now() + 1400;
    while (performance.now() < until) {
      for (const limb of limbs) {
        const box = limb.getBoundingClientRect();
        if (box.x < scene.x || box.y < scene.y || box.right > scene.right || box.bottom > scene.bottom) return false;
      }
      await new Promise(resolve => setTimeout(resolve, 70));
    }
    return true;
  });
  assert.equal(silhouetteFits, true, "The whole animated demon remains inside the scene");
}

async function prepare(page, errors) {
  page.on("pageerror", error => errors.push(error.message));
  // Guest-only check: avoid external SDK/network/credentials entirely.
  await page.route("https://www.gstatic.com/firebasejs/**", route => route.abort());
  await page.goto("http://localhost:5500");
  await page.waitForFunction(() => document.getElementById("leaderboard-status").textContent.includes("No ranked"));
}

(async () => {
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || "msedge", headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: "no-preference" });
    const errors = [];
    const host = await context.newPage();
    const outbound = [];
    let releaseRoom;
    let roomHeld;
    await host.routeWebSocket("**/ws", ws => {
      const server = ws.connectToServer();
      ws.onMessage(message => { outbound.push(JSON.parse(message)); server.send(message); });
      server.onMessage(message => {
        if (JSON.parse(message).type === "room_created") {
          roomHeld = true;
          releaseRoom = () => ws.send(message);
        } else ws.send(message);
      });
    });
    await prepare(host, errors);
    const homeLayout = await host.locator("#home-screen").boundingBox();
    const requests = [];
    host.on("request", request => requests.push(request.url()));
    await host.locator("#nickname").fill("LoaderHost");
    await host.locator("#create-room-btn").click();
    await host.locator('#room-loading[data-state="waiting"]').waitFor();
    assert.deepEqual(await host.locator("#home-screen").boundingBox(), homeLayout, "Overlay does not change homepage layout");
    assert.equal(await host.locator("#loading-title").evaluate(element => element === document.activeElement), true);
    assert.equal(await host.locator(".hunting-number").count(), 5);
    assert.equal(await host.locator(".cascade-number").count(), 64);
    const cascadeTransform = await host.locator(".cascade-number").first().evaluate(element => getComputedStyle(element).transform);
    const initialTransform = await host.locator("#loading-hunter").evaluate(element => element.style.transform);
    await host.waitForFunction(initial => document.getElementById("loading-hunter").style.transform !== initial, initialTransform);
    await layoutFits(host);
    assert.notEqual(await host.locator(".cascade-number").first().evaluate(element => getComputedStyle(element).transform), cascadeTransform,
      "The decorative cascade drifts through CSS transforms");
    await host.screenshot({ path: "test-artifacts/loading-hunt-desktop.png" });
    await host.locator("#loading-scene").screenshot({ path: "test-artifacts/loading-creature.png" });
    for (const [width, height] of [[320, 568], [390, 844], [844, 390]]) {
      await host.setViewportSize({ width, height });
      await layoutFits(host);
      await host.screenshot({ path: `test-artifacts/loading-hunt-${width}x${height}.png` });
    }
    assert.equal(outbound.filter(message => message.type === "create_room").length, 1);
    assert.deepEqual(requests, [], "Hunting issues no extra HTTP requests");
    assert.ok(roomHeld, "Actual room confirmation was deliberately held by the browser fixture");
    await host.setViewportSize({ width: 1440, height: 900 });
    await host.emulateMedia({ reducedMotion: "reduce" });
    await host.waitForFunction(() => document.querySelectorAll(".cascade-number").length === 16);
    assert.equal(await host.locator("#loading-hunter").getAttribute("data-action"), "still");
    await host.emulateMedia({ reducedMotion: "no-preference" });
    await host.waitForFunction(() => document.querySelectorAll(".cascade-number").length === 64);
    await host.evaluate(() => {
      window.finalWasEaten = false;
      const layer = document.getElementById("loading-numbers");
      const observer = new MutationObserver(records => {
        if (records.some(record => record.target.classList?.contains("final-number") && record.target.classList.contains("eaten"))) {
          window.finalWasEaten = true;
          observer.disconnect();
        }
      });
      observer.observe(layer, { attributes: true, subtree: true });
    });
    releaseRoom();
    await host.locator('#room-loading[data-state="finishing"]').waitFor();
    assert.equal(await host.locator("#home-screen").isVisible(), true, "Home remains visible through the red bite");
    assert.equal(await host.locator("#lobby-screen").isVisible(), false);
    assert.equal(await host.locator(".hunting-number").count(), 1);
    assert.equal(await host.locator(".final-number").count(), 1);
    assert.equal(await host.locator(".cascade-number").count(), 0, "Room confirmation clears the ambient cascade for the final bite");
    await host.waitForFunction(() => {
      const digit = document.querySelector(".final-number > span");
      return digit && Number(getComputedStyle(digit).opacity) > .95;
    });
    await host.screenshot({ path: "test-artifacts/loading-final-number.png" });
    await closed(host);
    assert.equal(await host.evaluate(() => window.finalWasEaten), true);
    assert.equal(await host.locator("#lobby-screen").evaluate(element => element === document.activeElement), true);
    const code = await host.locator("#room-code-display").innerText();

    const guest = await context.newPage();
    const guestOutbound = [];
    let releaseGuest;
    await guest.routeWebSocket("**/ws", ws => {
      const server = ws.connectToServer();
      ws.onMessage(message => { guestOutbound.push(JSON.parse(message)); server.send(message); });
      server.onMessage(message => {
        if (JSON.parse(message).type === "player_joined") releaseGuest = () => ws.send(message);
        else ws.send(message);
      });
    });
    await prepare(guest, errors);
    await guest.locator("#nickname").fill("LoaderGuest");
    await guest.locator("#room-code-input").fill(code);
    await guest.locator("#join-room-btn").click();
    await guest.locator('#room-loading[data-state="waiting"]').waitFor();
    await host.waitForFunction(() => !document.getElementById("start-btn").disabled);
    assert.ok(releaseGuest);
    releaseGuest();
    await guest.locator('#room-loading[data-state="finishing"]').waitFor();
    assert.equal(await guest.locator("#home-screen").isVisible(), true);
    assert.equal(await guest.locator("#lobby-screen").isVisible(), false);
    // Actual host-start arriving during the final bite must retain the latest phase.
    await host.locator("#start-btn").click();
    await closed(guest);
    await guest.locator("#countdown-screen").waitFor({ state: "visible" });
    await guest.locator("#game-screen").waitFor({ state: "visible" });
    assert.equal(await guest.locator("#answer-input").evaluate(element => element === document.activeElement), true);
    assert.equal(guestOutbound.filter(message => message.type === "join_room").length, 1);

    const failure = await context.newPage();
    let forceFailure = true;
    let attempts = 0;
    await failure.routeWebSocket("**/ws", ws => {
      attempts++;
      if (forceFailure) { ws.close({ code: 1013, reason: "Local failure fixture" }); return; }
      ws.connectToServer();
    });
    await prepare(failure, errors);
    await failure.locator("#nickname").fill("RetryHost");
    await failure.locator("#create-room-btn").click();
    await failure.locator('#room-loading[data-state="failed"]').waitFor();
    assert.equal(await failure.locator("#loading-hunter").getAttribute("data-action"), "still");
    assert.equal(await failure.locator(".cascade-number").count(), 0, "Failure stops ambient animation");
    assert.equal(await failure.locator("#loading-retry").evaluate(element => element === document.activeElement), true);
    await failure.waitForFunction(() => Number(getComputedStyle(document.getElementById("loading-hud")).opacity) >= .99);
    await failure.screenshot({ path: "test-artifacts/loading-failure.png" });
    forceFailure = false;
    await failure.locator("#loading-retry").click();
    await closed(failure);
    await failure.locator("#lobby-screen").waitFor({ state: "visible" });
    assert.equal(attempts, 2, "One failed attempt and one manually requested retry");
    await failure.locator("#back-btn").click();
    await failure.locator("#room-code-input").fill("ZZZZZZ");
    await failure.locator("#join-room-btn").click();
    await failure.locator('#room-loading[data-state="failed"]').waitFor();
    assert.match(await failure.locator("#loading-status").innerText(), /Room not found/);
    await failure.locator("#loading-return").click();
    await closed(failure);
    assert.equal(await failure.locator("#create-room-btn").isEnabled(), true);

    const cancelled = await context.newPage();
    const cancelledOutbound = [];
    await cancelled.routeWebSocket("**/ws", ws => ws.onMessage(message => cancelledOutbound.push(JSON.parse(message))));
    await prepare(cancelled, errors);
    await cancelled.locator("#nickname").fill("CancelledHost");
    await cancelled.locator("#create-room-btn").click();
    await cancelled.locator('#room-loading[data-state="waiting"]').waitFor();
    await cancelled.keyboard.press("Escape");
    await closed(cancelled);
    assert.equal(await cancelled.locator("#create-room-btn").isEnabled(), true);

    const reduced = await context.newPage();
    await reduced.emulateMedia({ reducedMotion: "reduce" });
    await reduced.setViewportSize({ width: 844, height: 320 });
    let releaseReduced;
    await reduced.routeWebSocket("**/ws", ws => {
      const server = ws.connectToServer();
      server.onMessage(message => {
        if (JSON.parse(message).type === "room_created") releaseReduced = () => ws.send(message);
        else ws.send(message);
      });
    });
    await prepare(reduced, errors);
    await reduced.locator("#nickname").fill("ReducedHost");
    await reduced.locator("#create-room-btn").click();
    await reduced.locator('#room-loading[data-state="waiting"]').waitFor();
    await layoutFits(reduced);
    const creature = await reduced.locator(".demon-facing").boundingBox();
    const hud = await reduced.locator("#loading-hud").boundingBox();
    assert.ok(creature.y + creature.height < hud.y, "Static reduced-motion creature clears the short-screen controls");
    assert.ok(releaseReduced);
    releaseReduced();
    await closed(reduced);
    await reduced.locator("#lobby-screen").waitFor({ state: "visible" });
    assert.equal(await reduced.locator("#loading-numbers").evaluate(element => element.children.length), 0);
    assert.deepEqual(errors, []);
    console.log("Real local Edge browser: transparent fullviewport overlay, responsive CSS cascade, live reduced-motion changes and particle cleanup, unchanged visible homepage through final red bite, delayed create/join, depth hunting, focus, host-start during finale, failure/retry/missing room/return/Escape, reduced motion, desktop/320px/390px/short-landscape layout, no extra HTTP or room requests, and no page errors passed. External Firebase SDK blocked; no production traffic.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
