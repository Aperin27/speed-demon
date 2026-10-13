(() => {
  const dialog = document.getElementById("room-loading");
  const title = document.getElementById("loading-title");
  const status = document.getElementById("loading-status");
  const scene = document.getElementById("loading-scene");
  const hud = document.getElementById("loading-hud");
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
    let depth = .78 + .42 * location.y;
    location.x = Math.max(60 * depth / width, Math.min(1 - 60 * depth / width, location.x));
    location.y = Math.max(80 * depth / height, Math.min((Math.min(height, hudTop) - 80 * depth) / height, location.y));
    depth = .78 + .42 * location.y;
    hunter.style.transition = duration ? `transform ${duration}ms cubic-bezier(.32,.02,.65,1)` : "none";
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
    const y = (number.y * height + 30 * .78) / (height - 30 * .42);
    const depth = .78 + .42 * y;
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
    direction *= -1;
    facing.style.transform = `scaleX(${direction})`;
    // Alternate nearest-prey fixation with abrupt turns toward a different number.
    const prey = [...numbers].sort((a, b) =>
      Math.hypot((a.x - location.x) * width, (a.y - location.y) * height) - Math.hypot((b.x - location.x) * width, (b.y - location.y) * height));
    target = prey[biteCount % 3 === 2 ? prey.length - 1 : 0];
    later(() => {
      const distance = Math.hypot((target.x - location.x) * width, (target.y - location.y) * height);
      const travel = 200 + Math.min(620, distance / 1.8);
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
    }, 110);
  }

  function complete() {
    if (state !== "finishing") return;
    const callback = onFinished;
    cancel(false);
    callback?.();
  }

  function finalFeast() {
    stopTimers();
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
      clearNumbers();
      for (let index = 0; index < 5; index++) spawnNumber();
      setAction("still");
      hunt();
    }
  }

  function cancel(restoreFocus = true) {
    stopTimers();
    setState("idle");
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
    for (let index = 0; index < 5; index++) spawnNumber();
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
