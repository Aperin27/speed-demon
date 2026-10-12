// Firebase is optional: SDK/config/network failure must not prevent guest games.
(() => {
  const status = document.getElementById("auth-status");
  const signInButton = document.getElementById("sign-in-btn");
  const signOutButton = document.getElementById("sign-out-btn");
  let auth = null;
  let sdk = null;
  function reportStatus(message) {
    status.textContent = message;
    status.classList.remove("visually-hidden");
  }
  let resolveReady;
  const ready = new Promise((resolve) => { resolveReady = resolve; });
  window.speedDemonAuth = {
    setUsername(username) {
      signOutButton.textContent = username ? `Sign Out · ${username}` : "Sign Out";
    },
    async getToken() {
      await ready;
      if (!auth?.currentUser) return null;
      let timer;
      try {
        return await Promise.race([
          auth.currentUser.getIdToken(),
          new Promise((resolve, reject) => { timer = setTimeout(() => reject(new Error("Token refresh timed out")), 5000); })
        ]);
      } finally { clearTimeout(timer); }
    }
  };
  function changed(user) {
    status.textContent = user ? "Signed in · Checking registered username." : "Guest · Sign in and register a username for rankings.";
    status.classList.add("visually-hidden");
    signInButton.classList[user ? "add" : "remove"]("hidden");
    signOutButton.classList[user ? "remove" : "add"]("hidden");
    signOutButton.textContent = "Sign Out";
    resolveReady();
    window.dispatchEvent(new Event("auth-changed"));
  }
  signInButton.addEventListener("click", async () => {
    signInButton.disabled = true;
    try {
      await sdk.signInWithPopup(auth, new sdk.GoogleAuthProvider());
    } catch {
      reportStatus("Sign-in could not complete. Allow popups or continue as a guest.");
    } finally {
      signInButton.disabled = false;
    }
  });
  signOutButton.addEventListener("click", async () => {
    try { await sdk.signOut(auth); }
    catch { reportStatus("Sign-out failed. Try again."); }
  });
  async function initialize() {
    const config = window.SPEED_DEMON_CONFIG.firebase;
    if (!config.apiKey || !config.appId) {
      reportStatus("Guest · Google sign-in awaits Firebase web configuration.");
      signInButton.disabled = true;
      resolveReady();
      return;
    }
    const timeout = setTimeout(() => {
      reportStatus("Google sign-in is taking longer than expected. Guest play is available.");
      resolveReady();
    }, 8000);
    try {
      const [appSdk, authSdk] = await Promise.all([
        import("https://www.gstatic.com/firebasejs/13.0.0/firebase-app.js"),
        import("https://www.gstatic.com/firebasejs/13.0.0/firebase-auth.js")
      ]);
      sdk = authSdk;
      auth = sdk.getAuth(appSdk.initializeApp(config));
      sdk.onIdTokenChanged(auth, (user) => { clearTimeout(timeout); changed(user); }, () => {
        clearTimeout(timeout); changed(null);
      });
      signInButton.disabled = false;
    } catch {
      clearTimeout(timeout);
      reportStatus("Google sign-in unavailable. Guest play is available.");
      signInButton.disabled = true;
      resolveReady();
    }
  }
  initialize();
})();
