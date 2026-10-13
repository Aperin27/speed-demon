(() => {
  const canvas = document.getElementById("number-field");
  const shell = document.getElementById("app-shell");
  const context = canvas?.getContext("2d");
  if (!context || !shell) return;

  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  const forcedColors = matchMedia("(forced-colors: active)");
  const layers = [
    { size: 18, blur: 1.8, opacity: .24, speed: 6 },
    { size: 34, blur: .65, opacity: .46, speed: 12 },
    { size: 68, blur: .25, opacity: .78, speed: 21 },
    { size: 94, blur: 3.2, opacity: .27, speed: 29 }
  ];
  let width = 0;
  let height = 0;
  let pixelRatio = 1;
  let particles = [];
  let sprites = [];
  let fog;
  let dust;
  let titleBounds;
  let frame = 0;
  let lastTime = 0;
  let elapsed = 0;
  let needsResize = true;
  let seed = 73129;

  function random() {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  }

  function texture(textureWidth, textureHeight) {
    const surface = document.createElement("canvas");
    surface.width = textureWidth;
    surface.height = textureHeight;
    return surface;
  }

  function createSprites() {
    const scale = width < 600 ? .66 : width < 1000 ? .84 : 1;
    sprites = layers.map(layer => Array.from({ length: 10 }, (_, digit) => {
      const size = layer.size * scale;
      const padding = 18;
      const sprite = texture(Math.ceil((size + padding * 2) * pixelRatio), Math.ceil((size * 1.4 + padding * 2) * pixelRatio));
      const painter = sprite.getContext("2d");
      painter.scale(pixelRatio, pixelRatio);
      painter.font = `300 ${size}px "Segoe UI", Arial, sans-serif`;
      painter.textAlign = "center";
      painter.textBaseline = "middle";
      const x = sprite.width / pixelRatio / 2;
      const y = sprite.height / pixelRatio / 2;
      // Diffusion and focus are baked into small reusable glyphs.
      painter.filter = `blur(${layer.blur + 3}px)`;
      painter.fillStyle = "rgba(230,230,230,.25)";
      painter.fillText(String(digit), x, y);
      painter.filter = `blur(${layer.blur}px)`;
      painter.fillStyle = "#ededed";
      painter.fillText(String(digit), x, y);
      return sprite;
    }));
  }

  function createAtmosphere() {
    fog = texture(400, 800);
    const painter = fog.getContext("2d");
    for (let index = 0; index < 95; index++) {
      const y = random() * 800;
      const spine = 175 + Math.sin(y / 145) * 80;
      const x = spine + (random() - .5) * 220;
      const radius = 35 + random() * 105;
      const glow = painter.createRadialGradient(x, y, 0, x, y, radius);
      glow.addColorStop(0, `rgba(170,170,170,${.025 + random() * .065})`);
      glow.addColorStop(.45, "rgba(110,110,110,.025)");
      glow.addColorStop(1, "rgba(80,80,80,0)");
      painter.fillStyle = glow;
      painter.fillRect(x - radius, y - radius, radius * 2, radius * 2);
    }
    // Cut pockets of darkness through the mist instead of applying a screen blur.
    painter.globalCompositeOperation = "destination-out";
    for (let index = 0; index < 18; index++) {
      const x = 60 + random() * 280;
      const y = random() * 800;
      const radius = 40 + random() * 85;
      const shadow = painter.createRadialGradient(x, y, 0, x, y, radius);
      shadow.addColorStop(0, "rgba(0,0,0,.65)");
      shadow.addColorStop(1, "rgba(0,0,0,0)");
      painter.fillStyle = shadow;
      painter.fillRect(x - radius, y - radius, radius * 2, radius * 2);
    }
    // Multiscale grain breaks soft puffs into irregular, smoke-like filaments.
    const clouds = painter.getImageData(0, 0, fog.width, fog.height);
    const noiseLayers = [64, 32, 16, 8].map(cell => {
      const columns = Math.ceil(fog.width / cell) + 1;
      const rows = Math.ceil(fog.height / cell) + 1;
      return { cell, columns, values: Float32Array.from({ length: columns * rows }, random) };
    });
    for (let y = 0; y < fog.height; y++) {
      for (let x = 0; x < fog.width; x++) {
        let noise = 0;
        let weight = .55;
        for (const grid of noiseLayers) {
          const column = Math.floor(x / grid.cell);
          const row = Math.floor(y / grid.cell);
          const offset = row * grid.columns + column;
          const rawX = x / grid.cell - column;
          const rawY = y / grid.cell - row;
          const blendX = rawX * rawX * (3 - 2 * rawX);
          const blendY = rawY * rawY * (3 - 2 * rawY);
          const top = grid.values[offset] * (1 - blendX) + grid.values[offset + 1] * blendX;
          const bottom = grid.values[offset + grid.columns] * (1 - blendX) + grid.values[offset + grid.columns + 1] * blendX;
          noise += (top * (1 - blendY) + bottom * blendY) * weight;
          weight *= .5;
        }
        clouds.data[(y * fog.width + x) * 4 + 3] *= Math.max(.06, Math.min(1, (noise - .23) * 2.6));
      }
    }
    painter.putImageData(clouds, 0, 0);
    dust = texture(400, 800);
    const dustPainter = dust.getContext("2d");
    for (let index = 0; index < 110; index++) {
      const tone = 100 + Math.floor(random() * 100);
      dustPainter.fillStyle = `rgba(${tone},${tone},${tone},${.08 + random() * .18})`;
      const size = random() < .92 ? .65 : 1.2;
      dustPainter.fillRect(random() * 400, random() * 800, size, size);
    }
  }

  function placeParticle(particle, initial) {
    const mobile = width < 600;
    const fieldWidth = mobile ? Math.min(65, width * .2) : Math.min(width * .33, 570);
    const rightSide = random() > (mobile ? .65 : .88);
    // Broad overlapping clusters have independent drift, never aligned columns.
    const cluster = mobile ? .16 : random() < .55 ? .38 : .76;
    const spread = (random() + random() + random() - 1.5) * .43;
    particle.x = rightSide ? width - fieldWidth * (mobile ? .16 + random() * .23 : .08 + random() * .65) : fieldWidth * (cluster + spread);
    particle.y = initial ? random() * height : -120;
    particle.digit = Math.floor(random() * 10);
    particle.speed = layers[particle.layer].speed * (.7 + random() * .65);
    particle.sway = (mobile ? 8 : 18) + random() * (mobile ? 9 : 27);
    particle.phase = random() * Math.PI * 2;
    particle.turn = (random() - .5) * .22;
    particle.brightness = .65 + random() * .55;
  }

  function resize() {
    width = window.innerWidth;
    height = window.innerHeight;
    pixelRatio = Math.min(window.devicePixelRatio || 1, 1.5, Math.sqrt(2800000 / (width * height)));
    canvas.width = Math.round(width * pixelRatio);
    canvas.height = Math.round(height * pixelRatio);
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    seed = 73129;
    createSprites();
    if (!fog) createAtmosphere();
    seed = 73129;
    const title = document.getElementById("home-title").getBoundingClientRect();
    titleBounds = { left: title.left - 16, right: title.right + 16, top: title.top + window.scrollY - 12, bottom: title.bottom + window.scrollY + 12 };
    const count = width < 600 ? 34 : width < 1000 ? 60 : 94;
    particles = [];
    for (let index = 0; index < count; index++) {
      const fraction = index / count;
      const particle = { layer: fraction < .38 ? 0 : fraction < .73 ? 1 : fraction < .93 ? 2 : 3 };
      placeParticle(particle, true);
      if (width >= 600) {
        let attempts = 0;
        while (attempts++ < 10 && particles.some(other => other.layer >= 1 && particle.layer >= 1 && Math.hypot(other.x - particle.x, other.y - particle.y) < (layers[other.layer].size + layers[particle.layer].size) * .4)) {
          placeParticle(particle, true);
        }
      }
      particles.push(particle);
    }
    needsResize = false;
  }

  function drawFog(opacity, offset, time) {
    const fieldWidth = width < 600 ? 155 : Math.min(width * .45, 760);
    context.globalAlpha = opacity;
    context.drawImage(fog, -fieldWidth * .12 + Math.sin(time * .055) * 12, -height * .15 + offset + Math.cos(time * .04) * 14, fieldWidth, height * 1.3);
    context.globalAlpha = opacity * .23;
    context.drawImage(fog, width - fieldWidth * .45, -height * .1 - offset, fieldWidth * .65, height * 1.3);
  }

  function draw(delta) {
    elapsed += delta;
    context.clearRect(0, 0, width, height);
    drawFog(1, 0, elapsed);
    context.globalAlpha = .5;
    context.drawImage(dust, 0, 0, width < 600 ? 130 : width * .36, height);
    for (let layerIndex = 0; layerIndex < layers.length; layerIndex++) {
      if (layerIndex === 1) drawFog(.30, height * .09, elapsed + 30);
      if (layerIndex === 3) drawFog(.22, -height * .13, elapsed + 60);
      for (const particle of particles) {
        if (particle.layer !== layerIndex) continue;
        particle.y += particle.speed * delta;
        if (particle.y > height + 120) placeParticle(particle, false);
        const x = particle.x + Math.sin(elapsed * .13 + particle.phase) * particle.sway;
        const y = particle.y;
        const edge = Math.max(0, Math.min(1, (y + 50) / 130, (height + 40 - y) / 160));
        const mist = .65 + .35 * Math.sin(y / 140 + x / 100 + particle.phase);
        const sprite = sprites[layerIndex][particle.digit];
        const scale = 1 + Math.sin(elapsed * .08 + particle.phase) * .055;
        const spriteWidth = sprite.width / pixelRatio * scale;
        const spriteHeight = sprite.height / pixelRatio * scale;
        const titleDistanceX = Math.max(titleBounds.left - x, x - titleBounds.right, 0);
        const titleDistanceY = Math.max(titleBounds.top - window.scrollY - y, y - titleBounds.bottom + window.scrollY, 0);
        const quiet = .12 + .88 * Math.min(1, Math.max(titleDistanceX / (spriteWidth * .5), titleDistanceY / (spriteHeight * .5)));
        context.globalAlpha = layers[layerIndex].opacity * particle.brightness * edge * mist * quiet;
        context.save();
        context.translate(x, y);
        context.rotate(particle.turn + Math.sin(elapsed * .09 + particle.phase) * .035);
        context.drawImage(sprite, -spriteWidth / 2, -spriteHeight / 2, spriteWidth, spriteHeight);
        context.restore();
      }
    }
    context.globalAlpha = 1;
  }

  function isVisible() {
    return shell.dataset.screen === "home" && !document.hidden && !forcedColors.matches;
  }

  function animate(time) {
    frame = 0;
    if (!isVisible() || reducedMotion.matches) return;
    if (needsResize) resize();
    // A slow cascade is smooth at 30fps; skip drawing on intervening display frames.
    if (!lastTime || time - lastTime >= 32) {
      draw(lastTime ? Math.min((time - lastTime) / 1000, .08) : 0);
      lastTime = time;
    }
    frame = requestAnimationFrame(animate);
  }

  function sync() {
    cancelAnimationFrame(frame);
    frame = 0;
    lastTime = 0;
    if (!isVisible()) return;
    if (needsResize) resize();
    draw(0);
    if (!reducedMotion.matches) frame = requestAnimationFrame(animate);
  }

  new MutationObserver(sync).observe(shell, { attributes: true, attributeFilter: ["data-screen"] });
  document.addEventListener("visibilitychange", sync);
  window.addEventListener("pagehide", () => { cancelAnimationFrame(frame); frame = 0; });
  window.addEventListener("pageshow", sync);
  window.addEventListener("resize", () => {
    needsResize = true;
    if (isVisible() && reducedMotion.matches) sync();
  });
  reducedMotion.addEventListener("change", sync);
  forcedColors.addEventListener("change", sync);
  sync();
})();
