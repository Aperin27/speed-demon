const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("frontend/auth.js", "utf8");
// Substitute only the network module loader; execute the real UI/auth lifecycle.
const isolatedSource = source.replace(/import\("https:\/\/www\.gstatic\.com\/firebasejs\/13\.0\.0\/firebase-(app|auth)\.js"\)/g,
  (_, product) => `loadSdk("${product}")`);
function createSession(configured = true, loadFailure = false) {
  const elements = new Map();
  const timers = new Map();
  const events = [];
  let notify;
  const auth = { currentUser: null };
  const sdk = {
    initializeApp: () => ({}), getAuth: () => auth,
    GoogleAuthProvider: class {},
    onIdTokenChanged: (value, callback) => { notify = callback; callback(null); },
    async signInWithPopup() {
      auth.currentUser = { displayName: "Google Player", getIdToken: async () => "id-token" };
      notify(auth.currentUser);
    },
    async signOut() { auth.currentUser = null; notify(null); }
  };
  const document = { getElementById(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, { disabled: true, textContent: "", listeners: {},
        classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name) },
        addEventListener: function (name, handler) { this.listeners[name] = handler; }
      });
    }
    return elements.get(id);
  } };
  const window = { SPEED_DEMON_CONFIG: { firebase: { apiKey: configured ? "public" : "", appId: "public-app-id" } },
    dispatchEvent: (event) => events.push(event.type) };
  const context = vm.createContext({ document, window, Event,
    setTimeout: (callback) => { timers.set(1, callback); return 1; }, clearTimeout: (id) => timers.delete(id),
    loadSdk: async () => { if (loadFailure) throw Error("offline"); return sdk; }
  });
  vm.runInContext(isolatedSource, context);
  return { window, elements, sdk, auth, events, timers };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  let session = createSession();
  await flush();
  assert.equal(await session.window.speedDemonAuth.getToken(), null);
  assert.equal(session.elements.get("sign-in-btn").disabled, false);
  await session.elements.get("sign-in-btn").listeners.click();
  assert.equal(await session.window.speedDemonAuth.getToken(), "id-token");
  assert.match(session.elements.get("auth-status").textContent, /Checking registered username/);
  assert.equal(session.elements.get("sign-out-btn").textContent, "Sign Out");
  session.window.speedDemonAuth.setUsername("Registered_Player");
  assert.equal(session.elements.get("sign-out-btn").textContent, "Sign Out · Registered_Player");
  assert.equal(session.elements.get("sign-out-btn").classList.contains("hidden"), false);
  await session.elements.get("sign-out-btn").listeners.click();
  assert.equal(await session.window.speedDemonAuth.getToken(), null);
  session.sdk.signInWithPopup = async () => { throw Error("popup cancelled"); };
  await session.elements.get("sign-in-btn").listeners.click();
  assert.match(session.elements.get("auth-status").textContent, /could not complete/);
  assert.equal(session.elements.get("sign-in-btn").disabled, false);
  assert.equal(session.events.filter((event) => event === "auth-changed").length, 3);
  session.auth.currentUser = { getIdToken: () => new Promise(() => {}) };
  const stuckToken = session.window.speedDemonAuth.getToken();
  await flush();
  session.timers.get(1)();
  await assert.rejects(stuckToken, /refresh timed out/);
  assert.equal(session.timers.size, 0);
  session = createSession(false);
  assert.equal(await session.window.speedDemonAuth.getToken(), null);
  assert.match(session.elements.get("auth-status").textContent, /awaits Firebase/);
  session = createSession(true, true);
  await flush();
  assert.equal(await session.window.speedDemonAuth.getToken(), null);
  assert.match(session.elements.get("auth-status").textContent, /unavailable/);
  assert.equal(session.elements.get("sign-in-btn").disabled, true);
  assert.equal(session.timers.size, 0);
  console.log("Firebase frontend lifecycle: guest, sign-in/out, token transport/timeout, cancelled popup, absent config and SDK failure passed (isolated SDK fake)");
})().catch((error) => { console.error(error); process.exitCode = 1; });
