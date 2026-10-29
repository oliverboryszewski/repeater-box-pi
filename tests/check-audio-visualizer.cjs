"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../dashboard/static/audio-visualizer.js"), "utf8");
let checks = 0;
const check = (value, message) => { assert.ok(value, message); checks += 1; };

function harness(options = {}) {
  const windowEvents = new Map();
  const documentEvents = new Map();
  const frames = new Map();
  const mutations = [];
  let frameID = 0;
  const rectangles = [];
  let clears = 0;
  const paint = {
    clearRect() { rectangles.length = 0; clears += 1; },
    fillRect(x, y, width, height) { rectangles.push({ x, y, width, height }); },
    setTransform() {}
  };
  const panel = { hidden: false };
  const container = { hidden: false, width: 480, getBoundingClientRect() { return { width: this.width }; }, closest() { return this.ancestorHidden ? {} : null; } };
  const status = { textContent: "" };
  const canvas = { getContext() { return options.noCanvas ? null : paint; } };
  const elements = { "live-audio-spectrum": canvas, "live-audio-visualizer": container, "live-audio-spectrum-status": status, "panel-live": panel };
  const media = { matches: !!options.reduced, addEventListener(name, callback) { this.changed = callback; } };
  const window = {
    devicePixelRatio: 2,
    matchMedia() { return media; },
    addEventListener(name, callback) { windowEvents.set(name, callback); },
    requestAnimationFrame(callback) { const id = ++frameID; frames.set(id, callback); return id; },
    cancelAnimationFrame(id) { frames.delete(id); }
  };
  const document = {
    hidden: false,
    getElementById(id) { return elements[id]; },
    addEventListener(name, callback) { documentEvents.set(name, callback); }
  };
  class MutationObserver {
    constructor(callback) { this.callback = callback; mutations.push(this); }
    observe() {}
  }
  class ResizeObserver {
    constructor(callback) { this.callback = callback; }
    observe() {}
  }
  vm.runInNewContext(source, { document, window, MutationObserver, ResizeObserver, Uint8Array, WeakMap, Number, Math });
  return {
    window, document, panel, container, canvas, status, rectangles, frames, media,
    get clears() { return clears; },
    emit(detail) { windowEvents.get("repeater-live-audio")?.({ detail }); },
    step(now = 0) { const pending = [...frames.values()]; frames.clear(); pending.forEach(callback => callback(now)); },
    visibility(hidden) { document.hidden = hidden; documentEvents.get("visibilitychange")?.(); },
    panelHidden(hidden) { panel.hidden = hidden; mutations.forEach(observer => observer.callback()); },
    resize(width) { container.width = width; windowEvents.get("resize")?.(); },
  };
}

function audio(options = {}) {
  const analysers = [];
  const stateEvents = new Map();
  const destination = {};
  const context = {
    state: "running", currentTime: 0, sampleRate: 48000, destination,
    addEventListener(name, callback) { stateEvents.set(name, callback); },
    removeEventListener(name) { stateEvents.delete(name); },
    createAnalyser() {
      if (options.createFailure) throw new Error("not supported");
      const analyser = {
        frequencyBinCount: 512, calls: 0, level: 0,
        getByteFrequencyData(array) {
          if (this.fail) throw new Error("analysis failed");
          this.calls += 1;
          array.fill(this.level);
          if (this.bin !== undefined) array[this.bin] = 255;
        }
      };
      analysers.push(analyser);
      return analyser;
    }
  };
  const connections = new Set([destination]);
  const disconnects = [];
  const output = {
    connect(node) { if (options.connectFailure) throw new Error("connect failed"); connections.add(node); },
    disconnect(node) { assert.notEqual(node, undefined, "Never disconnect the audible route broadly"); disconnects.push(node); connections.delete(node); }
  };
  return {
    context, output, analysers, connections, disconnects,
    detail(extra = {}) { return { context, output, state: "playing", muted: false, until: 10, ...extra }; },
    state(value) { context.state = value; stateEvents.get("statechange")?.(); }
  };
}

{
  const view = harness();
  check(view.frames.size === 0, "Idle view schedules no animation");
  check(view.rectangles.every(bar => bar.height === 2), "Idle frequency bars are flat");
  check(view.status.textContent.includes("Start listening"), "Idle state explains how to begin");
  const stream = audio();
  view.emit(stream.detail());
  check(stream.analysers.length === 1, "One analyser is attached to the real playback graph");
  check(stream.connections.has(stream.context.destination), "The existing direct audible route is preserved");
  stream.analysers[0].level = 128;
  view.step(0);
  check(view.rectangles.every(bar => bar.height > 30 && bar.height < 50), "Real analyser amplitudes determine all bar heights");
  stream.analysers[0].level = 0;
  stream.analysers[0].bin = 30;
  view.step(40);
  check(view.rectangles.some(bar => bar.height > 70) && view.rectangles.some(bar => bar.height === 2), "A real frequency-bin peak affects only its spectrum bands");
  stream.analysers[0].bin = undefined;
  view.step(80);
  check(view.rectangles.every(bar => bar.height === 2), "Silent PCM remains flat with no fabricated movement");
  check(view.status.textContent.includes("local playback"), "The display describes audio rather than RF strength");
  view.emit(stream.detail({ muted: true }));
  check(view.frames.size === 0 && view.status.textContent.includes("Muted"), "Muted playback is labeled and stops animation");
  check(view.rectangles.every(bar => bar.height === 2), "Muted display is flat");
  view.emit(stream.detail());
  check(view.frames.size === 1, "Unmuting resumes analysis of queued playback");
  view.visibility(true);
  check(view.frames.size === 0, "A hidden document stops RAF");
  check(stream.connections.has(stream.context.destination), "Hiding the document does not stop listening");
  view.visibility(false);
  check(view.frames.size === 1, "Returning to the page resumes visualization");
  view.panelHidden(true);
  check(view.frames.size === 0, "Changing away from Live audio stops RAF");
  view.panelHidden(false);
  check(view.frames.size === 1, "Returning to Live audio resumes RAF");
  stream.state("suspended");
  check(view.frames.size === 0 && view.status.textContent.includes("paused"), "Browser-suspended audio is flat and paused");
  stream.state("running");
  check(view.frames.size === 1, "Browser resume reuses the same analyser");
  stream.context.currentTime = 11;
  view.step(120);
  check(view.frames.size === 0 && view.rectangles.every(bar => bar.height === 2), "Exhausted playback queues stop RAF and go flat");
  view.emit(stream.detail({ until: 12 }));
  check(view.frames.size === 1, "A fresh real PCM packet wakes an idle visualizer");
  view.emit(stream.detail({ state: "stopped" }));
  check(view.frames.size === 0 && stream.connections.size === 1, "Stopping removes only the passive analyser branch");
  check(stream.connections.has(stream.context.destination), "The direct destination connection is never removed");
  view.emit(stream.detail({ until: 12 }));
  check(stream.analysers.length === 1 && stream.connections.size === 2, "A restart on the same graph reuses one analyser and one branch");
  const next = audio();
  view.emit(next.detail());
  check(stream.connections.size === 1 && next.analysers.length === 1, "A new playback context detaches the old branch and binds its own analyser");
  view.resize(240);
  check(view.canvas.width === 480 && view.canvas.height === 192, "Canvas sizing responds to width and device pixel ratio");
  check(view.rectangles.length >= 16 && view.rectangles.length <= 48, "Responsive bar count stays bounded");
}

{
  const view = harness({ reduced: true });
  const stream = audio();
  view.emit(stream.detail());
  view.step(0); view.step(50); view.step(100); view.step(150);
  check(stream.analysers[0].calls === 1, "Reduced motion draws at most five times per second");
  view.step(200);
  check(stream.analysers[0].calls === 2, "Reduced-motion display still reflects subsequent real audio");
}

for (const option of ["createFailure", "connectFailure"]) {
  const view = harness();
  const stream = audio({ [option]: true });
  view.emit(stream.detail());
  check(view.frames.size === 0 && view.status.textContent.includes("unavailable"), `${option} uses a static fallback`);
  check(stream.connections.size === 1 && stream.connections.has(stream.context.destination), `${option} cannot silence existing playback`);
  check(stream.disconnects.every(node => node !== stream.context.destination), `${option} never disconnects the audible destination`);
}

{
  const view = harness();
  const stream = audio();
  stream.context.state = "suspended";
  view.emit(stream.detail());
  check(stream.analysers.length === 0 && stream.connections.size === 1, "No analyser attaches before the existing context is running");
  stream.context.state = "running";
  view.emit(stream.detail());
  stream.analysers[0].fail = true;
  view.step(0);
  check(view.frames.size === 0 && stream.connections.size === 1, "An analyser read failure cleanly drops its own branch");
  check(view.status.textContent.includes("Listening still works"), "Visual failure explicitly leaves listening available");
}

{
  const view = harness({ noCanvas: true });
  const stream = audio();
  view.emit(stream.detail());
  check(stream.analysers.length === 0 && view.frames.size === 0, "Missing canvas support leaves ordinary audio untouched");
  check(view.status.textContent.includes("Listening still works"), "Unsupported visualization has an accessible fallback");
}

check(!/getUserMedia|AudioContext\s*\(|createMediaElementSource|Math\.random|fetch\s*\(|WebSocket/.test(source), "Visualizer never requests a device, creates another player, fabricates data or opens a stream");
console.log(`Audio visualizer: ${checks} checks passed.`);
