(() => {
  const tuning = {
    mobileWidth: 640,
    shortHeight: 480,
    cascadeDesktop: 64,
    cascadeMobile: 36,
    cascadeReduced: 16,
    cascadeDuration: 18,
    cascadeDurationSpread: 14,
    hunterScale: 1.18,
    mobileHunterScale: 1,
    baseDepth: .78,
    depthRange: .42,
    huntTravelMin: 280,
    huntTravelRange: 600,
    huntPixelsPerMs: 1.7,
    huntFocusDelay: 110
  };
  const dialog = document.getElementById("room-loading");
  const title = document.getElementById("loading-title");
  const status = document.getElementById("loading-status");
  const scene = document.getElementById("loading-scene");
  const hud = document.getElementById("loading-hud");
  const cascadeLayer = document.getElementById("loading-cascade");
  const numberLayer = document.getElementById("loading-numbers");
  const hunter = document.getElementById("loading-hunter");
  const facing = hunter.querySelector(".demon-facing");
  const retryButton = document.getElementById("loading-retry");
  const returnButton = document.getElementById("loading-return");
  const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
  const timers = new Set();
  const positions = [
    [.04, .18], [.46, .04], [.94, .24], [.04, .56], [.96, .64],
    [.24, .92], [.76, .96], [.44, .46], [.68, .32], [.32, .28],
    [.07, .94], [.93, .88]
  ];
  let state = "idle";
  let numbers = [];
  let target = null;
  let location = { x: .48, y: .50 };
  let direction = 1;
  let width = 0;
  let height = 0;
  let hudTop = 0;
  let onReturn = null;
  let onRetry = null;
  let onFinished = null;
  let previousFocus = null;
  let biteCount = 0;
  let cascadeMode = null;

  function hunterScale() {
    return width > tuning.mobileWidth && height > tuning.shortHeight ? tuning.hunterScale : tuning.mobileHunterScale;
  }

  function hunterDepth(y) {
    return hunterScale() * (tuning.baseDepth + tuning.depthRange * y);
  }

  function clearCascade() {
    cascadeLayer.replaceChildren();
    cascadeMode = null;
  }

  function populateCascade() {
    if (state !== "waiting" || document.hidden) return;
    const mode = motionPreference.matches ? "reduced" : width <= tuning.mobileWidth ? "mobile" : "desktop";
    if (cascadeMode === mode) return;
    cascadeMode = mode;
    const count = tuning[`cascade${mode[0].toUpperCase()}${mode.slice(1)}`];
    const particles = [];
    for (let index = 0; index < count; index++) {
      const particle = document.createElement("span");
      particle.className = "cascade-number";
      particle.textContent = String(Math.floor(Math.random() * 10));
      const region = index / count;
      const x = region < .7 ? 2 + Math.random() * 27 : region < .9 ? 79 + Math.random() * 19 : 34 + Math.random() * 41;
      const depth = index % 4;
      const duration = tuning.cascadeDuration + Math.random() * tuning.cascadeDurationSpread;
      particle.style.setProperty("--cascade-x", `${x}%`);
      particle.style.setProperty("--cascade-y", mode === "reduced" ? `${4 + Math.random() * 86}vh` : "-12vh");
      particle.style.setProperty("--cascade-drift", `${-10 + Math.random() * 20}px`);
      particle.style.setProperty("--cascade-duration", `${duration}s`);
      particle.style.setProperty("--cascade-delay", `${-Math.random() * duration}s`);
      particle.style.setProperty("--cascade-size", `${18 + depth * 6}px`);
      particle.style.setProperty("--cascade-opacity", String(.10 + depth * .035));
      particle.style.setProperty("--cascade-blur", `${[1.5, .85, .25, 0][depth]}px`);
      particle.style.setProperty("--cascade-scale", String(.68 + depth * .16));
      particles.push(particle);
    }
    cascadeLayer.replaceChildren(...particles);
  }

  function setState(value) {
    state = value;
    dialog.dataset.state = value;
  }

  function stopTimers() {
    timers.forEach(clearTimeout);
    timers.clear();
  }

  function later(callback, delay) {
    const timer = setTimeout(() => {
      timers.delete(timer);
      callback();
    }, delay);
    timers.add(timer);
  }

  function setAction(action) {
    hunter.dataset.action = action;
  }

  function placeHunter(duration = 0) {
    let depth = hunterDepth(location.y);
    location.x = Math.max(60 * depth / width, Math.min(1 - 60 * depth / width, location.x));
    location.y = Math.max(80 * depth / height, Math.min((Math.min(height, hudTop) - 80 * depth) / height, location.y));
    depth = hunterDepth(location.y);
    hunter.style.transition = duration ? `transform ${duration}ms cubic-bezier(.22,.61,.36,1)` : "none";
    hunter.style.transform = `translate3d(${location.x * width}px, ${location.y * height}px, 0) scale(${depth})`;
    hunter.style.setProperty("--depth-shadow", `${12 + (depth - .78) * 24}px`);
    hunter.style.zIndex = String(2 + Math.round(location.y * 4));
    facing.style.transform = `scaleX(${direction})`;
  }

  function placeNumber(number) {
    const depth = .76 + .40 * number.y;
    number.element.style.transform = `translate3d(${number.x * width}px, ${number.y * height}px, 0) scale(${depth})`;
    number.element.style.setProperty("--depth-opacity", String(.54 + .38 * number.y));
    number.element.style.setProperty("--depth-shadow", `${8 + number.y * 12}px`);
    number.element.style.zIndex = String(1 + Math.round(number.y * 3));
  }

  function mouthLocation(number) {
    const scale = hunterScale();
    const y = (number.y * height + 30 * scale * tuning.baseDepth) / (height - 30 * scale * tuning.depthRange);
    const depth = hunterDepth(y);
    return { x: number.x - direction * 19 * depth / width, y };
  }

  function measure() {
    width = scene.clientWidth;
    height = scene.clientHeight;
    const hudBounds = hud.getBoundingClientRect();
    hudTop = hudBounds.top > 0 ? hudBounds.top : height - 100;
    numbers.forEach(number => {
      Object.assign(number, numberCoordinates(number.position, number.final));
      placeNumber(number);
    });
    if (target) location = mouthLocation(target);
    placeHunter();
    populateCascade();
  }

  function numberCoordinates(position, final) {
    const [x, y] = final ? [.73, .43] : positions[position];
    // Hunt across the viewport, with room for the full silhouette and the bottom controls.
    const bottom = Math.max(64, Math.min(height - 122, hudTop - 118));
    return {
      x: (76 + x * Math.max(0, width - 152)) / width,
      y: (58 + y * (bottom - 58)) / height
    };
  }

  function clearNumbers() {
    numberLayer.replaceChildren();
    numbers = [];
    target = null;
  }

  function spawnNumber(final = false, excludedPosition = -1) {
    const freePositions = positions.map((_, index) => index).filter(index =>
      index !== excludedPosition && !numbers.some(number => number.position === index));
    const sector = position => Math.floor(positions[position][0] * 2) + 2 * Math.floor(positions[position][1] * 2);
    const population = position => numbers.filter(number => sector(number.position) === sector(position)).length;
    const lowestPopulation = Math.min(...freePositions.map(population));
    const candidates = freePositions.filter(position => population(position) === lowestPopulation);
    const position = candidates[Math.floor(Math.random() * candidates.length)];
    const { x, y } = numberCoordinates(position, final);
    const element = document.createElement("span");
    element.className = `hunting-number${final ? " final-number" : ""}`;
    const digit = document.createElement("span");
    digit.textContent = final ? "0" : String(1 + Math.floor(Math.random() * 9));
    element.appendChild(digit);
    element.style.setProperty("--float-delay", `${-Math.random() * 3}s`);
    numberLayer.appendChild(element);
    const number = { element, x, y, position, final };
    placeNumber(number);
    numbers.push(number);
    return number;
  }

  function moveTo(number, duration) {
    direction = number.x >= location.x ? 1 : -1;
    // Align the creature's mouth with its prey, leaving both feet inside the scene.
    location = mouthLocation(number);
    setAction("run");
    placeHunter(duration);
  }

  function hunt() {
    if (state !== "waiting" || document.hidden || motionPreference.matches) return;
    setAction("scan");
    // Fixate on nearby prey, then occasionally pursue a distant number across the scene.
    const prey = [...numbers].sort((a, b) =>
      Math.hypot((a.x - location.x) * width, (a.y - location.y) * height) - Math.hypot((b.x - location.x) * width, (b.y - location.y) * height));
    target = prey[biteCount % 4 === 3 ? prey.length - 1 : 0];
    direction = target.x >= location.x ? 1 : -1;
    facing.style.transform = `scaleX(${direction})`;
    later(() => {
      const distance = Math.hypot((target.x - location.x) * width, (target.y - location.y) * height);
      const travel = tuning.huntTravelMin + Math.min(tuning.huntTravelRange, distance / tuning.huntPixelsPerMs);
      moveTo(target, travel);
      later(() => {
        setAction("bite");
        target.element.classList.add("eaten");
        later(() => {
          const consumedPosition = target.position;
          target.element.remove();
          numbers = numbers.filter(number => number !== target);
          biteCount++;
          spawnNumber(false, consumedPosition);
          hunt();
        }, 170);
      }, travel);
    }, tuning.huntFocusDelay);
  }

  function complete() {
    if (state !== "finishing") return;
    const callback = onFinished;
    cancel(false);
    callback?.();
  }

  function finalFeast() {
    stopTimers();
    clearCascade();
    clearNumbers();
    target = spawnNumber(true);
    setAction("alert");
    direction = target.x >= location.x ? 1 : -1;
    facing.style.transform = `scaleX(${direction})`;
    if (motionPreference.matches || document.hidden) {
      setAction("still");
      target.element.classList.add("eaten");
      if (document.hidden) complete();
      else later(complete, 120);
      return;
    }
    later(() => {
      const distance = Math.hypot((target.x - location.x) * width, (target.y - location.y) * height);
      const travel = 180 + Math.min(140, distance / 4);
      moveTo(target, travel);
      later(() => {
        setAction("bite");
        target.element.classList.add("eaten");
        later(() => {
          dialog.classList.add("loading-exit");
          later(complete, 100);
        }, 160);
      }, travel);
    }, 140);
  }

  function visibilityChanged() {
    if (state === "idle") return;
    dialog.dataset.paused = String(document.hidden);
    stopTimers();
    if (state === "finishing") {
      if (document.hidden) complete();
      else finalFeast();
    } else if (state === "waiting") {
      // A hidden tab does no hunting work. Resume from a clean five-number scene.
      hunter.style.transition = "none";
      clearCascade();
      clearNumbers();
      if (!document.hidden) {
        populateCascade();
        for (let index = 0; index < 5; index++) spawnNumber();
      }
      setAction("still");
      hunt();
    }
  }

  function cancel(restoreFocus = true) {
    stopTimers();
    setState("idle");
    clearCascade();
    clearNumbers();
    setAction("still");
    onReturn = onRetry = onFinished = null;
    document.removeEventListener("visibilitychange", visibilityChanged);
    window.removeEventListener("resize", measure);
    motionPreference.removeEventListener("change", visibilityChanged);
    if (dialog.open) dialog.close();
    if (restoreFocus && previousFocus?.isConnected) previousFocus.focus();
    previousFocus = null;
  }

  function start(action, returnHome) {
    cancel(false);
    previousFocus = document.activeElement;
    onReturn = returnHome;
    location = { x: .48, y: .50 };
    direction = 1;
    biteCount = 0;
    setState("waiting");
    dialog.dataset.paused = String(document.hidden);
    dialog.classList.remove("loading-exit");
    title.textContent = "Crunching numbers";
    status.textContent = action === "join_room" ? "Connecting to your room…" : "Connecting to the server…";
    retryButton.classList.add("hidden");
    returnButton.textContent = "Return home";
    dialog.showModal();
    title.focus({ preventScroll: true });
    measure();
    if (!document.hidden) for (let index = 0; index < 5; index++) spawnNumber();
    setAction("still");
    document.addEventListener("visibilitychange", visibilityChanged);
    window.addEventListener("resize", measure);
    motionPreference.addEventListener("change", visibilityChanged);
    hunt();
  }

  function finish(callback) {
    if (state !== "waiting") return;
    onFinished = callback;
    setState("finishing");
    status.textContent = "Room ready…";
    finalFeast();
  }

  function fail(message, retry, returnHome) {
    if (state === "idle" || state === "failed") return;
    stopTimers();
    setState("failed");
    dialog.classList.remove("loading-exit");
    clearCascade();
    clearNumbers();
    setAction("still");
    hunter.style.transition = "none";
    onFinished = null;
    onRetry = retry;
    onReturn = returnHome;
    title.textContent = "Connection interrupted";
    status.textContent = message;
    retryButton.classList.remove("hidden");
    retryButton.focus();
  }

  function returnHome() {
    const callback = onReturn;
    cancel();
    callback?.();
  }

  returnButton.addEventListener("click", returnHome);
  retryButton.addEventListener("click", () => {
    if (state !== "failed") return;
    const callback = onRetry;
    cancel(false);
    callback?.();
  });
  dialog.addEventListener("cancel", event => {
    event.preventDefault();
    returnHome();
  });

  window.roomLoading = {
    start, finish, fail, cancel,
    isActive: () => state !== "idle",
    setStatus(message) { if (state === "waiting") status.textContent = message; }
  };
})();
