"use strict";

(() => {
  const canvas = document.getElementById("live-audio-spectrum");
  const container = document.getElementById("live-audio-visualizer");
  const status = document.getElementById("live-audio-spectrum-status");
  const panel = document.getElementById("panel-live");
  if (!canvas || !container || !status) return;
  let paint;
  try { paint = canvas.getContext("2d"); } catch (_) { /* Playback is independent. */ }
  if (!paint) {
    status.textContent = "Audio visualization is unavailable. Listening still works.";
    return;
  }

  const graphs = new WeakMap();
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  let graph = null;
  let state = "stopped";
  let muted = false;
  let until = 0;
  let failed = false;
  let frame = null;
  let lastDraw = -Infinity;
  let width = 1;
  const height = 96;

  function setStatus(text) {
    if (status.textContent !== text) status.textContent = text;
  }

  function visible() {
    return !document.hidden && !container.hidden && !panel?.hidden
      && !container.closest?.("[hidden]");
  }

  function draw(values = null) {
    paint.clearRect(0, 0, width, height);
    const count = Math.max(16, Math.min(48, Math.floor(width / 13)));
    const gap = 3;
    const barWidth = Math.max(1, (width - gap * (count - 1)) / count);
    const bottom = height - 8;
    const rate = graph?.context.sampleRate || 48000;
    const bins = values?.length || 0;
    const low = Math.max(1, Math.round(60 / (rate / 2) * bins));
    const high = Math.max(low + 1, Math.min(bins, Math.round(12000 / (rate / 2) * bins)));
    paint.fillStyle = values ? "#237c75" : "#a7beb3";
    for (let index = 0; index < count; index += 1) {
      let level = 0;
      if (values && bins) {
        const start = Math.floor(low * (high / low) ** (index / count));
        const end = Math.max(start + 1, Math.ceil(low * (high / low) ** ((index + 1) / count)));
        for (let bin = start; bin < Math.min(end, bins); bin += 1) level = Math.max(level, values[bin] / 255);
      }
      const barHeight = 2 + level * (height - 20);
      paint.fillRect(index * (barWidth + gap), bottom - barHeight, barWidth, barHeight);
    }
  }

  function resize() {
    const nextWidth = Math.max(1, Math.floor(container.getBoundingClientRect().width));
    const ratio = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    width = nextWidth;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    paint.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw();
    lastDraw = -Infinity;
    refresh();
  }

  function cancelFrame() {
    if (frame !== null) window.cancelAnimationFrame(frame);
    frame = null;
  }

  function disconnect() {
    if (!graph) return;
    graph.context.removeEventListener?.("statechange", refresh);
    if (graph.connected) {
      // Disconnect only this passive branch. Never disturb the audible path.
      try { graph.output.disconnect(graph.analyser); } catch (_) { /* A closed context is harmless. */ }
      graph.connected = false;
    }
    graph = null;
  }

  function attach(context, output) {
    if (graph?.context === context && graph.output === output && graph.connected) return true;
    disconnect();
    if (!context || context.state !== "running" || typeof context.createAnalyser !== "function"
        || !output || typeof output.connect !== "function") return false;
    let saved = graphs.get(output);
    try {
      if (!saved || saved.context !== context) {
        const analyser = context.createAnalyser();
        analyser.fftSize = 1024;
        analyser.smoothingTimeConstant = 0.65;
        saved = { context, output, analyser, values: new Uint8Array(analyser.frequencyBinCount), connected: false };
        graphs.set(output, saved);
      }
      // An AnalyserNode works with its output unconnected. The original
      // gain-to-destination connection remains the sole audible route.
      output.connect(saved.analyser);
      context.addEventListener?.("statechange", refresh);
      saved.connected = true;
      graph = saved;
      return true;
    } catch (_) {
      if (saved?.analyser) {
        try { output.disconnect(saved.analyser); } catch (_) { /* Never a broad disconnect. */ }
        saved.connected = false;
      }
      return false;
    }
  }

  function canAnimate() {
    return visible() && state === "playing" && !muted && graph?.connected
      && graph.context.state === "running" && graph.context.currentTime < until;
  }

  function tick(now) {
    frame = null;
    if (!canAnimate()) { refresh(); return; }
    if (now - lastDraw >= (reduced?.matches ? 200 : 1000 / 30)) {
      try {
        graph.analyser.getByteFrequencyData(graph.values);
        draw(graph.values);
        lastDraw = now;
      } catch (_) {
        failed = true;
        disconnect();
        refresh();
        return;
      }
    }
    frame = window.requestAnimationFrame(tick);
  }

  function refresh() {
    if (canAnimate()) {
      setStatus("Live radio audio · local playback");
      if (frame === null) frame = window.requestAnimationFrame(tick);
      return;
    }
    cancelFrame();
    draw();
    if (failed) setStatus("Audio visualization is unavailable. Listening still works.");
    else if (state === "stopped") setStatus("Start listening to view radio audio.");
    else if (muted) setStatus("Muted on this device");
    else if (graph && graph.context.state !== "running") setStatus("Audio paused on this device");
    else if (state === "connecting") setStatus("Waiting for radio audio…");
    else setStatus("Waiting for radio audio");
  }

  window.addEventListener("repeater-live-audio", (event) => {
    const detail = event.detail || {};
    state = ["connecting", "playing"].includes(detail.state) ? detail.state : "stopped";
    muted = detail.muted === true;
    until = Number.isFinite(detail.until) ? detail.until : 0;
    if (state === "stopped") {
      disconnect();
      failed = false;
    } else {
      if (graph && graph.context !== detail.context) disconnect();
      if (detail.context?.state === "running") failed = !attach(detail.context, detail.output);
    }
    refresh();
  });
  document.addEventListener("visibilitychange", refresh);
  window.addEventListener("resize", resize);
  reduced?.addEventListener?.("change", () => { lastDraw = -Infinity; refresh(); });
  if (typeof MutationObserver === "function") {
    const observer = new MutationObserver(resize);
    if (panel) observer.observe(panel, { attributes: true, attributeFilter: ["hidden"] });
    observer.observe(container, { attributes: true, attributeFilter: ["hidden"] });
  }
  if (typeof ResizeObserver === "function") new ResizeObserver(resize).observe(container);
  resize();
})();
