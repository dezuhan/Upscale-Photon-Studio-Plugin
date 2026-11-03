"use strict";

// Upscale panel for Photon Studio.
//
// Pipeline:
//   selected layer --(documents.pixels)--> RGBA --(images.encode)--> PNG
//   --(local upscale service)--> PNG bytes --(images.decode)-->
//   new raster layer via documents.writePixels.
//
// Photon plugins run sandboxed, so the model runs in a small local HTTP service
// (service/server.py). Layer insertion uses `layer.new` + `documents.writePixels`,
// which writes real pixels through the imaging worker; `documents.applyImage` is
// kept as a fallback because some builds create the layer but leave it unfilled.

const SDK = globalThis.__photonSdk;
const DEFAULT_ORIGIN = "http://127.0.0.1:8765";
const MAX_EDGE = 8192; // images.encode / images.decode limit
const MAX_PIXELS = 16 * 1024 * 1024; // 16 MP plugin pixel-transfer limit

const el = {
  model: document.getElementById("model"),
  scale: document.getElementById("scale"),
  engine: document.getElementById("engine"),
  hideSource: document.getElementById("hideSource"),
  matchSize: document.getElementById("matchSize"),
  run: document.getElementById("run"),
  cancel: document.getElementById("cancel"),
  refresh: document.getElementById("refresh"),
  service: document.getElementById("service"),
  modelHint: document.getElementById("modelHint"),
  progress: document.getElementById("progress"),
  progressFill: document.getElementById("progressFill"),
  badge: document.getElementById("badge"),
  timing: document.getElementById("timing"),
  reason: document.getElementById("reason"),
  details: document.getElementById("details"),
  detailsOverlay: document.getElementById("detailsOverlay"),
  detailsClose: document.getElementById("detailsClose"),
  tabSystem: document.getElementById("tabSystem"),
  tabLog: document.getElementById("tabLog"),
  systemText: document.getElementById("systemText"),
  logText: document.getElementById("logText"),
};

let api = null;
let cancelRequested = false;
let serviceOrigin = DEFAULT_ORIGIN;
let operationIds = new Set();
let engineInfo = { binary: null, pillow: false };
const logLines = [];
const MAX_LOG_LINES = 300;

/** Custom dropdowns (select.js) need a nudge after values change programmatically. */
const syncSelects = () => window.syncCustomSelects?.();

/* ---------- UI helpers ---------- */

function log(line) {
  const stamp = new Date().toLocaleTimeString("en-GB", { hour12: false });
  logLines.push(`${stamp}  ${line}`);
  if (logLines.length > MAX_LOG_LINES) logLines.shift();
  if (!el.detailsOverlay.hidden && !el.logText.hidden) renderLog();
}

function renderLog() {
  el.logText.textContent = logLines.join("\n");
  el.logText.scrollTop = el.logText.scrollHeight;
}

/** Render GET /system into a plain, aligned report. */
function renderSystem(report) {
  if (!report) {
    el.systemText.textContent = "System information is unavailable.";
    return;
  }
  const os = report.os || {};
  const cpu = report.cpu || {};
  const service = report.service || {};
  const row = (label, value) => `${label.padEnd(9)}${value}`;
  const lines = [];

  lines.push(row("OS", `${os.pretty || os.name || "unknown"} · ${os.arch || "?"}`));
  lines.push(row("CPU", `${cpu.name || "unknown"} · ${cpu.cores || "?"} cores`));
  if (report.memoryGb) lines.push(row("Memory", `${report.memoryGb} GB`));

  const devices = Array.isArray(report.devices) ? report.devices : [];
  if (devices.length) {
    for (const device of devices) {
      lines.push(row("GPU", `[${device.index}] ${device.name} (Vulkan)`));
    }
  } else {
    lines.push(
      row(
        "GPU",
        report.cpuFallback
          ? "no Vulkan device reported — CPU fallback (Pillow)"
          : "no Vulkan device reported",
      ),
    );
  }

  lines.push(row("Backend", report.backend || "unknown"));
  lines.push(
    row(
      "Service",
      `v${service.version || "?"} · engine ${service.engine || "?"} · ` +
        `gpu ${service.gpu || "default"} · tile ${service.tile || 0}`,
    ),
  );
  lines.push(
    row(
      "Models",
      `${service.models ?? 0} · Pillow ${service.pillow ? "ready" : "not installed"}`,
    ),
  );
  lines.push(row("Python", report.python || "unknown"));
  if (service.binary) lines.push(row("Binary", service.binary));

  el.systemText.textContent = lines.join("\n");
}

async function loadSystem(probe) {
  el.systemText.textContent = "Reading system information…";
  try {
    const reply = await request(`${serviceOrigin}/system${probe ? "?probe=1" : ""}`, {
      method: "GET",
    });
    if (reply.status !== 200 || !reply.body) throw new Error(`HTTP ${reply.status}`);
    renderSystem(reply.body);
  } catch (error) {
    el.systemText.textContent = networkHint(error);
  }
}

function showTab(which) {
  const system = which === "system";
  el.tabSystem.classList.toggle("active", system);
  el.tabLog.classList.toggle("active", !system);
  el.systemText.hidden = !system;
  el.logText.hidden = system;
  if (!system) renderLog();
}

function openDetails() {
  el.detailsOverlay.hidden = false;
  showTab("system");
  void loadSystem(true);
}

function closeDetails() {
  el.detailsOverlay.hidden = true;
}

/** The panel body only shows a short badge plus the duration; details go to Details → Log. */
function badge(label, kind) {
  el.badge.textContent = label;
  el.badge.classList.toggle("busy", kind === "busy");
  el.badge.classList.toggle("ok", kind === "ok");
  el.badge.classList.toggle("danger", kind === "error");
}

function formatDuration(ms) {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** One compact line under the badge. `kind` is "error" (danger edge) or "info" (muted). */
function setReason(text, kind) {
  if (!text) {
    el.reason.hidden = true;
    el.reason.textContent = "";
    el.reason.title = "";
    el.reason.classList.remove("info");
    return;
  }
  el.reason.hidden = false;
  el.reason.textContent = text;
  el.reason.title = text;
  el.reason.classList.toggle("info", kind === "info");
}

const ERROR_HINTS = [
  [
    /above the 16 MP|above the 8192px|result would be .*above|melebihi/i,
    "Scale too high — use 2×, a smaller model, or start from a smaller layer.",
  ],
  [
    /ran out of memory|vkallocatememory|out of device memory/i,
    'GPU out of memory — lower the scale, or set "tile": 512 in service/config.json.',
  ],
  [
    /no vulkan|vkcreateinstance|upscaler binary|was not found/i,
    "No usable GPU/Vulkan — set Engine to Pillow, or install Upscayl.",
  ],
  [/active selection/i, "An active selection blocks the write — press Ctrl+D first."],
  [
    /has no pixel data|empty layer|no raster pixels/i,
    "That layer has no pixels — select the original layer instead.",
  ],
  [
    /cannot reach the upscale service/i,
    "Service is offline — start it with tools\\start-service.cmd, then Check service.",
  ],
  [
    /fully transparent|wrong size|wrong position|rejected/i,
    "The result was unusable — try another model or a different source layer.",
  ],
];

function compactError(message) {
  const text = String(message);
  for (const [pattern, hint] of ERROR_HINTS) {
    if (pattern.test(text)) return hint;
  }
  const first = text.split(/\.\s|\n/)[0].trim();
  return first.length > 100 ? `${first.slice(0, 97)}…` : first;
}

const STAGE_LABELS = [
  [/Reading/, "Reading"],
  [/Encoding/, "Encoding"],
  [/Upscaling|upscaling/, "Upscaling"],
  [/Rescal|rescaling/, "Rescaling"],
  [/Saving|saving/, "Saving"],
  [/Decoding/, "Decoding"],
  [/Adding/, "Adding"],
  [/Verifying/, "Verifying"],
  [/Contacting/, "Connecting"],
  [/receiving|preparing/i, "Preparing"],
];

function shortStage(text) {
  for (const [pattern, label] of STAGE_LABELS) {
    if (pattern.test(text)) return label;
  }
  return text.replace(/…$/, "").slice(0, 18);
}

let runStartedAt = 0;
let runTimer = null;

function startRunClock() {
  runStartedAt = performance.now();
  el.badge.title = "";
  el.timing.textContent = "";
  if (runTimer) clearInterval(runTimer);
  runTimer = setInterval(() => {
    el.timing.textContent = formatDuration(Math.round(performance.now() - runStartedAt));
  }, 200);
}

function stopRunClock() {
  if (runTimer) {
    clearInterval(runTimer);
    runTimer = null;
  }
  return runStartedAt ? Math.round(performance.now() - runStartedAt) : 0;
}

function setState(text, kind) {
  if (kind === "error") {
    badge("Failed", "error");
    el.badge.title = text;
    return;
  }
  if (kind === "ok") {
    badge("Done", "ok");
    return;
  }
  badge(shortStage(text), "busy");
}

function errText(error) {
  if (error && typeof error.message === "string") return error.message;
  return String(error);
}

const progress = {
  show() {
    el.progress.hidden = false;
  },
  set(fraction) {
    el.progressFill.style.width = `${Math.min(100, Math.max(0, Math.round(fraction * 100)))}%`;
  },
  hide() {
    el.progress.hidden = true;
    el.progressFill.style.width = "0%";
  },
};

/**
 * Poll the service while the model runs. The bar follows the stages the service reports
 * (preparing → model → rescale → saving) instead of looping an indeterminate animation.
 */
async function trackServiceProgress(pending) {
  let settled = false;
  pending.then(
    () => {
      settled = true;
    },
    () => {
      settled = true;
    },
  );
  while (!settled) {
    await new Promise((resolve) => setTimeout(resolve, 250));
    if (settled) break;
    try {
      const info = await request(`${serviceOrigin}/progress`, { method: "GET" });
      const state = info && info.body;
      if (state && typeof state.value === "number") {
        progress.set(0.2 + Math.min(1, state.value) * 0.65);
        badge(`${shortStage(state.label || "upscaling")} ${Math.round(state.value * 100)}%`, "busy");
      }
    } catch {
      /* a dropped poll must not abort the run */
    }
  }
}

/* ---------- data helpers ---------- */

function readControls() {
  return {
    model: el.model.value || "",
    scale: Number(el.scale.value) || 2,
    engine: el.engine.value || "auto",
    matchSize: el.matchSize.checked,
    hideSource: el.hideSource.checked,
  };
}

/** Upscayl model names usually encode their native scale: 4xLSDIR, remacri-4x, …-x2. */
function detectScale(name) {
  const suffix = /(?:^|[^0-9])x([2-8])(?:[^0-9]|$)/i.exec(name);
  if (suffix) return Number(suffix[1]);
  const prefix = /^([2-8])x/i.exec(name);
  if (prefix) return Number(prefix[1]);
  return 0;
}

/** Layer pixels can be RGB (3 components) or RGBA (4); the SDK needs RGBA. */
function toRgba(pixels, components, width, height) {
  const count = width * height;
  if (components === 4) {
    if (pixels.length !== count * 4) throw new Error("Layer pixel buffer size does not match its dimensions.");
    return pixels instanceof Uint8Array ? pixels : new Uint8Array(pixels);
  }
  if (components === 3) {
    if (pixels.length !== count * 3) throw new Error("Layer pixel buffer size does not match its dimensions.");
    const out = new Uint8Array(count * 4);
    for (let i = 0, j = 0; i < count; i += 1, j += 3) {
      out[i * 4] = pixels[j];
      out[i * 4 + 1] = pixels[j + 1];
      out[i * 4 + 2] = pixels[j + 2];
      out[i * 4 + 3] = 255;
    }
    return out;
  }
  throw new Error(`Unsupported pixel format: ${components} components.`);
}

function toRgb(pixels) {
  const count = pixels.length / 4;
  const out = new Uint8Array(count * 3);
  for (let i = 0; i < count; i += 1) {
    out[i * 3] = pixels[i * 4];
    out[i * 3 + 1] = pixels[i * 4 + 1];
    out[i * 3 + 2] = pixels[i * 4 + 2];
  }
  return out;
}

function decodeBase64Json(value) {
  if (!value) return null;
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return JSON.parse(new TextDecoder().decode(bytes));
}

/**
 * Fallback only: documents.applyImage always places the incoming image at the
 * document origin, so the offset is baked in as transparent top-left padding.
 */
function padTopLeft(image, x, y) {
  if (!x && !y) return image;
  const width = image.width + x;
  const height = image.height + y;
  if (width > MAX_EDGE || height > MAX_EDGE || width * height > MAX_PIXELS) {
    throw new Error(`Placing at ${x},${y} would need a ${width}×${height} layer, above the plugin limits.`);
  }
  const out = new Uint8Array(width * height * 4);
  const rowBytes = image.width * 4;
  for (let row = 0; row < image.height; row += 1) {
    const from = row * rowBytes;
    const to = ((row + y) * width + x) * 4;
    out.set(image.pixels.subarray(from, from + rowBytes), to);
  }
  return { width, height, pixels: out };
}

/* ---------- network ---------- */

function request(url, options) {
  return api.network.request({ url, ...options });
}

/** Photon surfaces connection failures as a bare "fetch failed"; add the fix. */
function networkHint(error) {
  const text = errText(error);
  if (/fetch failed|econnrefused|failed to fetch|network/i.test(text)) {
    return (
      `Cannot reach the upscale service at ${serviceOrigin}. ` +
      "Start it (tools\\start-service.cmd), then click 'Check service'."
    );
  }
  return text;
}

/* ---------- settings and capabilities ---------- */

async function loadSettings() {
  try {
    const stored = await api.settings.get();
    return stored && typeof stored === "object" ? stored : {};
  } catch {
    return {};
  }
}

async function discoverOperations() {
  try {
    const list = await api.documents.operations("");
    operationIds = new Set((Array.isArray(list) ? list : []).map((entry) => entry.id));
  } catch {
    operationIds = new Set();
  }
  return operationIds;
}

/**
 * Read a layer back after insertion to confirm it really holds opaque pixels.
 */
/**
 * Read a layer back after insertion to confirm it really holds opaque pixels, and report
 * its document-space X/Y so the position can be checked too.
 */
async function inspectLayer(layerId) {
  try {
    const read = await api.documents.pixels({ layerId });
    const { width, height, components } = read.options;
    if (!width || !height) return { ok: false, reason: "layer has no allocated pixels" };
    const alphaIndex = components - 1;
    const total = width * height;
    let opaque = 0;
    let sampled = 0;
    for (let pixel = 0; pixel < total; pixel += 4) {
      sampled += 1;
      if (read.pixels[pixel * components + alphaIndex] > 0) opaque += 1;
    }
    return {
      ok: true,
      width,
      height,
      left: read.sourceBounds.left,
      top: read.sourceBounds.top,
      coverage: opaque / Math.max(1, sampled),
    };
  } catch (error) {
    return { ok: false, reason: errText(error) };
  }
}

/**
 * Read the source layer, translating the host's terse imaging errors into advice.
 * An "empty" layer (no allocated surface) is a real state on Photon 0.1.66.
 */
async function readLayerPixels(layerId) {
  try {
    return await api.documents.pixels({ layerId });
  } catch (error) {
    const text = errText(error);
    if (/oversized plugin pixel buffer|no allocated pixels/i.test(text)) {
      throw new Error(
        "This layer has no pixel data (it is empty, e.g. left behind by an earlier import). " +
          "Select the original layer instead and upscale that one.",
      );
    }
    throw error;
  }
}

/* ---------- models ---------- */

function applyModels(models, engineInfo) {
  el.model.textContent = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = models.length ? "— select a model —" : "— no models found —";
  el.model.appendChild(placeholder);

  for (const model of models) {
    const option = document.createElement("option");
    option.value = model.name;
    option.textContent = model.name;
    el.model.appendChild(option);
  }

  if (models.length) {
    const preferred =
      models.find((item) => item.name === el.model.dataset.preferred) ||
      models.find((item) => detectScale(item.name) === Number(el.scale.value)) ||
      models[0];
    el.model.value = preferred.name;
    syncScaleFromModel();
    syncSelects();
  }

  const parts = [
    `ncnn: ${engineInfo.binary ? "ready" : "not found"}`,
    `pillow: ${engineInfo.pillow ? "ready" : "not installed"}`,
    `${models.length} model${models.length === 1 ? "" : "s"}`,
  ];
  if (engineInfo.gpu && engineInfo.gpu !== "default") parts.push(`gpu ${engineInfo.gpu}`);
  if (engineInfo.tile) parts.push(`tile ${engineInfo.tile}`);
  el.modelHint.textContent = parts.join(" · ");
}

function syncScaleFromModel() {
  const scale = detectScale(el.model.value);
  if (scale) el.scale.value = String(scale);
  syncSelects();
}

/** What the service will actually use for the current selection. */
function effectiveEngine() {
  const chosen = el.engine.value;
  if (chosen === "ncnn") return "ncnn";
  if (chosen === "pillow") return "pillow";
  return engineInfo.binary ? "ncnn" : "pillow";
}

/**
 * Pillow only resamples: it takes no model, and combined with "Fit to original layer" the
 * 2x/3x round-trip cancels out (which looks like nothing happened).
 *
 * The model dropdown is intentionally NOT disabled — the native <select> and the custom
 * trigger would then need to stay in sync, which caused a "sometimes clickable" flicker.
 * The choice is simply ignored while Pillow is active and the note explains it.
 */
function updateEngineState(autoAdjust) {
  const engine = effectiveEngine();
  const pillow = engine === "pillow";

  if (!pillow) {
    // Leaving Pillow: clear the explanation we added, keep real failures untouched.
    if (el.reason.classList.contains("info")) setReason("");
    return;
  }

  let note =
    "Pillow resamples without a model — the model choice is ignored while Engine is Pillow.";
  if (autoAdjust && el.matchSize.checked) {
    el.matchSize.checked = false;
    note +=
      " 'Fit to original layer' was turned off too, so the result is genuinely larger " +
      "instead of coming back at the original size.";
    log("Pillow selected: turned off 'fit to original layer'");
  }
  setReason(note, "info");
}

/* ---------- service discovery ---------- */

async function refreshService() {
  badge("Connecting", "busy");
  el.timing.textContent = "";
  progress.show();
  progress.set(0.4);
  try {
    // First call raises a one-time approval dialog in Photon ("Allow this plugin to
    // send images and requests to http://127.0.0.1:8765?"); it appears only once.
    await api.network.allowEndpoint(serviceOrigin);
    const reply = await request(`${serviceOrigin}/health`, { method: "GET" });
    if (reply.status !== 200 || !reply.body) {
      throw new Error(`The service answered with HTTP ${reply.status}.`);
    }
    const health = reply.body;
    if (health.ok !== true) throw new Error(health.error || "The service is not ready.");

    el.service.textContent = `service: ok · v${health.version}`;
    engineInfo = { binary: health.binaryPath, pillow: health.pillow };
    applyModels(health.models || [], {
      binary: health.binaryPath,
      pillow: health.pillow,
      gpu: health.gpu,
      tile: health.tile,
    });
    updateEngineState(true);
    badge("Ready", "ok");
    setReason("");
    log(`service ${health.version} · ${health.models.length} model(s) · output ${health.outDir}`);
    log(`ncnn: ${health.binaryPath || "not found"} · pillow: ${health.pillow ? "ready" : "missing"}`);
    await discoverOperations();
    log(`${operationIds.size} document operation(s) available`);
  } catch (error) {
    el.service.textContent = "service: unreachable";
    badge("Offline", "error");
    el.badge.title = networkHint(error);
    setReason(compactError(networkHint(error)));
    log(`service check failed: ${networkHint(error)}`);
  } finally {
    progress.hide();
  }
}

/* ---------- layer insertion ---------- */

/**
 * Write the upscaled RGBA into an existing layer, trying 4 then 3 components.
 * `at` is a DOCUMENT-space position: Photon converts it to layer-local coordinates with
 * `rect - layer.offset`, so passing the source layer's X/Y keeps the result in place.
 */
async function writePixelsIntoLayer(doc, layerId, image, label, at) {
  const x = at ? at.x : 0;
  const y = at ? at.y : 0;
  const attempts = [
    { components: 4, pixels: image.pixels },
    { components: 3, pixels: toRgb(image.pixels) },
  ];
  let lastError;
  for (const attempt of attempts) {
    try {
      await api.documents.writePixels(
        doc.id,
        layerId,
        {
          pixels: attempt.pixels,
          sourceBounds: { left: x, top: y, right: x + image.width, bottom: y + image.height },
          options: {
            width: image.width,
            height: image.height,
            components: attempt.components,
            componentSize: 8,
            fullRange: true,
            colorSpace: "RGB",
          },
        },
        label,
      );
      return attempt.components;
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError;
}

/** Best path: duplicate the source layer (real surface, matching channels, same
 * offset), then replace its pixels with the upscaled image at the same document X/Y. */
async function insertByDuplicate(doc, image, name, sourceLayerId, origin) {
  const result = await api.documents.execute("layer.duplicate", { layerIds: [sourceLayerId] }, doc.id);
  let layerId =
    (result && (result.layerId || result.id)) ||
    (result && Array.isArray(result.layerIds) ? result.layerIds[0] : null) ||
    (Array.isArray(result) ? result[0] : null);
  if (!layerId) {
    const after = await api.documents.active();
    const selected = after && Array.isArray(after.selectedLayerIds) ? after.selectedLayerIds : [];
    layerId = selected[selected.length - 1];
  }
  if (!layerId || layerId === sourceLayerId) {
    throw new Error("Duplicate did not create a new layer.");
  }
  try {
    await writePixelsIntoLayer(doc, layerId, image, name, origin);
    try {
      // layer.rename takes a single layerId (not layerIds like the other layer ops).
      await api.documents.execute("layer.rename", { layerId, name }, doc.id);
    } catch {
      /* renaming is cosmetic */
    }
    return { layerId };
  } catch (error) {
    try {
      await api.documents.execute("layer.delete", { layerIds: [layerId] }, doc.id);
    } catch {
      /* ignore */
    }
    throw error;
  }
}

/** Primary path: create an empty layer, then write pixels into it. */
async function insertWithPixels(doc, image, name, previousLayerId) {
  await api.documents.execute("layer.new", { name }, doc.id);
  const after = await api.documents.active();
  const selected = after && Array.isArray(after.selectedLayerIds) ? after.selectedLayerIds : [];
  const layerId = selected[selected.length - 1];
  if (!layerId || layerId === previousLayerId) {
    throw new Error("The host did not create a new layer.");
  }
  try {
    // A fresh layer has offset 0,0, so it is written at the document origin and then moved.
    const components = await writePixelsIntoLayer(doc, layerId, image, name, { x: 0, y: 0 });
    return { layerId, components };
  } catch (error) {
    // Do not leave a half-written layer behind before the fallback runs.
    try {
      await api.documents.execute("layer.delete", { layerIds: [layerId] }, doc.id);
    } catch {
      /* ignore */
    }
    throw error;
  }
}

async function translateLayer(doc, layerId, dx, dy) {
  if (!dx && !dy) return true;
  if (!operationIds.has("layer.translate")) return false;
  try {
    await api.documents.execute("layer.translate", { layerIds: [layerId], dx, dy }, doc.id);
    return true;
  } catch {
    return false;
  }
}

/* ---------- main action ---------- */

/**
 * Ask the service to stop the in-flight upscale. The service terminates the upscayl-bin
 * process; the pending /upscale request then answers 409 {"error":"cancelled"}.
 */
async function requestCancel() {
  if (cancelRequested || el.cancel.hidden) return;
  cancelRequested = true;
  el.cancel.disabled = true;
  el.cancel.textContent = "Cancelling…";
  setState("Cancelling…");
  log("cancel requested");
  try {
    await request(`${serviceOrigin}/cancel`, { method: "POST", json: {} });
  } catch (error) {
    // The run may have just finished; its own reply decides the outcome.
    log(`cancel request failed: ${errText(error)}`);
  }
}

async function run() {
  const started = performance.now();
  cancelRequested = false;
  el.cancel.hidden = false;
  el.cancel.disabled = false;
  el.cancel.textContent = "Cancel";
  el.run.disabled = true;
  progress.show();
  progress.set(0.02);
  setReason("");
  startRunClock();

  try {
    const settings = readControls();
    await api.settings.set(settings).catch(() => {});

    const doc = await api.documents.active();
    if (!doc) throw new Error("No active document. Open an image in Photon first.");
    if (doc.colorMode !== "rgb") throw new Error(`The document must be RGB (currently ${doc.colorMode}).`);
    if (doc.depth !== 8) throw new Error(`The document must be 8-bit (currently ${doc.depth}-bit).`);
    if (doc.hasSelection) {
      throw new Error(
        "The document has an active selection. Photon masks plugin pixel writes with the " +
          "selection, so deselect (Ctrl+D) and run again.",
      );
    }

    const layerId = doc.selectedLayerIds[doc.selectedLayerIds.length - 1];
    if (!layerId) throw new Error("Select a single layer in the Layers panel first.");
    const layer = doc.layers.find((item) => item.id === layerId);
    const layerName = layer ? layer.name : layerId;
    log(`document "${doc.title}" ${doc.width}×${doc.height} · layer "${layerName}"`);

    /* 1. read pixels */
    setState("Reading layer pixels…");
    progress.set(0.06);
    const read = await readLayerPixels(layerId);
    const info = read.options;
    if (info.componentSize !== 8) {
      throw new Error(`Layer is ${info.componentSize}-bit; convert it to 8-bit first.`);
    }

    const width = info.width;
    const height = info.height;
    if (!width || !height) {
      throw new Error("This layer has no raster pixels. Rasterize text or vector layers first.");
    }
    if (width * height > MAX_PIXELS) {
      throw new Error(`Layer is ${width}×${height}, above the ${MAX_PIXELS / 1048576} MP limit.`);
    }
    const outWidth = width * settings.scale;
    const outHeight = height * settings.scale;
    if (Math.max(outWidth, outHeight) > MAX_EDGE) {
      throw new Error(`The result would be ${outWidth}×${outHeight}, above the ${MAX_EDGE}px per-side limit.`);
    }
    if (outWidth * outHeight > MAX_PIXELS) {
      throw new Error(
        `A ${settings.scale}× result would be ${outWidth}×${outHeight}, above the ` +
          `${MAX_PIXELS / 1048576} MP layer limit. Use a smaller scale.`,
      );
    }

    const rgba = toRgba(read.pixels, info.components, width, height);
    log(`source ${width}×${height} · ${info.components} components · target ${settings.scale}×`);

    /* 2. encode */
    setState("Encoding PNG…");
    progress.set(0.14);
    // SDK signature: images.encode(image, options) — two arguments, not one blob.
    const encoded = await api.images.encode({ width, height, pixels: rgba }, { maxEdge: MAX_EDGE });
    if (encoded.width !== width || encoded.height !== height) {
      throw new Error("PNG encoding changed the image size; the layer is too large.");
    }
    log(`png ${encoded.bytes.byteLength} bytes`);

    /* 3. upscale in the local service */
    progress.set(0.2);
    await api.network.allowEndpoint(serviceOrigin);
    const multipart = [
      { name: "image", bytes: encoded.bytes, filename: "layer.png", mime: "image/png" },
      { name: "scale", text: String(settings.scale) },
      { name: "model", text: settings.model },
      { name: "engine", text: settings.engine },
      { name: "response", text: "bytes" },
    ];
    // Supersample: upscale for detail, then scale back to the exported layer size so the
    // result lands on exactly the same rectangle as the layer it replaces.
    if (settings.matchSize) {
      multipart.push({ name: "targetWidth", text: String(width) });
      multipart.push({ name: "targetHeight", text: String(height) });
    }

    const pendingReply = request(`${serviceOrigin}/upscale`, {
      method: "POST",
      response: "bytes",
      multipart,
    });
    await trackServiceProgress(pendingReply);

    let reply;
    try {
      reply = await pendingReply;
    } catch (error) {
      throw new Error(networkHint(error));
    }
    progress.set(0.86);
    // Nothing left to cancel once the service has answered; the remaining steps are fast.
    el.cancel.hidden = true;
    if (cancelRequested) throw new Error("cancelled by the user");

    if (reply.status !== 200) {
      let detail = `HTTP ${reply.status}`;
      try {
        const parsed = JSON.parse(new TextDecoder().decode(reply.body));
        if (parsed && parsed.error) detail = parsed.error;
      } catch {
        /* body was not JSON; keep the status */
      }
      throw new Error(`The service failed: ${detail}`);
    }
    progress.set(0.88);

    const meta = decodeBase64Json(reply.headers["x-upscale-meta"]) || {};
    log(
      `result ${meta.width}×${meta.height} · ${meta.engine}${meta.model ? ` / ${meta.model}` : ""} · ` +
        `${Math.round(meta.elapsedMs || 0)} ms`,
    );
    if (meta.note) log(`note: ${meta.note}`);
    if (meta.engine === "pillow" && settings.matchSize) {
      log(
        "note: Pillow + scale-back only resamples the layer back to its original size; " +
          "uncheck 'Scale the result back to the original layer size' to keep a genuinely larger result",
      );
    }
    if (Array.isArray(meta.rescaledFrom) && Array.isArray(meta.rescaledTo)) {
      log(
        `rescaled ${meta.rescaledFrom[0]}×${meta.rescaledFrom[1]} → ` +
          `${meta.rescaledTo[0]}×${meta.rescaledTo[1]} (supersample back to layer size)`,
      );
    }

    /* 4. decode */
    setState("Decoding the result…");
    progress.set(0.9);
    const upscaled = await api.images.decode(
      reply.body instanceof Uint8Array ? reply.body : new Uint8Array(reply.body),
    );
    log(`decoded ${upscaled.width}×${upscaled.height} RGBA`);

    /* 5. insert as a new layer — try the most reliable path first */
    setState("Adding the result as a new layer…");
    progress.set(0.92);
    const resultName = `Upscale ${settings.scale}× — ${layerName}`.slice(0, 256);
    const originX = read.sourceBounds.left;
    const originY = read.sourceBounds.top;
    await discoverOperations();

    const attempts = [
      {
        name: "duplicate + writePixels",
        run: () => insertByDuplicate(doc, upscaled, resultName, layerId, { x: originX, y: originY }),
      },
      {
        name: "layer.new + writePixels",
        run: async () => {
          const inserted = await insertWithPixels(doc, upscaled, resultName, layerId);
          if (originX || originY) {
            const moved = await translateLayer(doc, inserted.layerId, originX, originY);
            if (!moved) throw new Error("layer.translate is unavailable for an offset layer");
          }
          return inserted;
        },
      },
      {
        name: "applyImage",
        run: async () => {
          const image = originX || originY ? padTopLeft(upscaled, originX, originY) : upscaled;
          const placed = await api.documents.applyImage({ documentId: doc.id, image, name: resultName });
          return { layerId: placed.layerId };
        },
      },
    ];

    let newLayerId = null;
    let method = "";
    const failures = [];
    for (const attempt of attempts) {
      try {
        const outcome = await attempt.run();
        newLayerId = outcome.layerId;
        method = attempt.name;
        break;
      } catch (error) {
        failures.push(`${attempt.name}: ${errText(error)}`);
      }
    }
    if (!newLayerId) {
      throw new Error(`Could not add the result layer. ${failures.join(" | ")}`);
    }
    for (const failure of failures) log(`fallback note: ${failure}`);
    log(`added layer ${newLayerId} via ${method}`);

    /* 6. verify the layer really holds pixels before touching the source layer */
    setState("Verifying the new layer…");
    progress.set(0.97);
    const check = await inspectLayer(newLayerId);
    const elapsed = Math.round(performance.now() - started);
    // A duplicate that kept its original pixels would still pass a coverage test,
    // so compare the size too: an upscale is never smaller than its result.
    const wrongSize = check.ok && (check.width < upscaled.width || check.height < upscaled.height);
    // Photon writes plugin pixels in document space, so the result must land on the
    // exact X/Y the source layer had.
    const wrongPosition = check.ok && (check.left !== originX || check.top !== originY);

    if (!check.ok || check.coverage === 0 || wrongSize || wrongPosition) {
      // Never hide the original when the result is empty; drop the empty layer instead
      // so the document is left exactly as it was.
      let removed = false;
      try {
        await api.documents.execute("layer.delete", { layerIds: [newLayerId] }, doc.id);
        removed = true;
      } catch {
        /* the layer stays, but the original stays visible too */
      }
      const reason = !check.ok
        ? check.reason
        : wrongSize
          ? `layer is ${check.width}×${check.height}, expected at least ${upscaled.width}×${upscaled.height}`
          : wrongPosition
            ? `layer sits at ${check.left},${check.top}, expected ${originX},${originY}`
            : "it is fully transparent";
      log(`rejected ${newLayerId} (${method}): ${reason}`);
      const rejection =
        `Result rejected (${method}): ${reason}. ` +
        `${removed ? "The layer was removed" : `Layer ${newLayerId} was left in place`}, ` +
        "the original layer is untouched. Try another model or a different source layer.";
      badge("Failed", "error");
      el.badge.title = rejection;
      setReason(compactError(rejection));
      log(rejection);
      return;
    }

    log(
      `verified ${newLayerId}: ${check.width}×${check.height} @ ${check.left},${check.top} · ` +
        `${(check.coverage * 100).toFixed(0)}% sampled pixels with alpha`,
    );

    if (settings.hideSource) {
      try {
        await api.documents.execute("layer.visibility", { layerIds: [layerId], visible: false }, doc.id);
        log(`original layer "${layerName}" hidden`);
      } catch {
        /* optional */
      }
    }

    progress.set(1);
    stopRunClock();
    badge("Done", "ok");
    setReason(
      meta.engine === "pillow" && settings.matchSize
        ? "Pillow + 'scale back' returns the layer at its original size — uncheck it for a genuinely larger result."
        : "",
      "info",
    );
    el.timing.textContent = `${formatDuration(elapsed)} · ${elapsed} ms`;
    log(
      `done in ${elapsed} ms · ${check.width}×${check.height} @ X${check.left} Y${check.top} · ` +
        `${(check.coverage * 100).toFixed(0)}% alpha · ${method}`,
    );
  } catch (error) {
    stopRunClock();
    if (cancelRequested || /cancel/i.test(errText(error))) {
      el.run.disabled = false;
      badge("Cancelled", "busy");
      el.badge.title = "Cancelled — no layer was added.";
      setReason("Cancelled — the original layer is untouched.", "info");
      log("cancelled by the user");
      return;
    }
    badge("Failed", "error");
    el.badge.title = errText(error);
    setReason(compactError(errText(error)));
    log(`ERROR: ${errText(error)}`);
  } finally {
    stopRunClock();
    el.run.disabled = false;
    el.cancel.hidden = true;
    el.cancel.disabled = false;
    el.cancel.textContent = "Cancel";
    setTimeout(() => progress.hide(), 700);
  }
}

/* ---------- wiring ---------- */

function wire() {
  el.run.addEventListener("click", () => void run());
  el.cancel.addEventListener("click", () => void requestCancel());
  el.refresh.addEventListener("click", () => void refreshService());
  el.model.addEventListener("change", syncScaleFromModel);
  el.engine.addEventListener("change", () => {
    setReason("");
    updateEngineState(true);
  });
  el.details.addEventListener("click", () =>
    el.detailsOverlay.hidden ? openDetails() : closeDetails(),
  );
  el.detailsClose.addEventListener("click", closeDetails);
  el.tabSystem.addEventListener("click", () => showTab("system"));
  el.tabLog.addEventListener("click", () => showTab("log"));
  el.detailsOverlay.addEventListener("click", (event) => {
    if (event.target === el.detailsOverlay) closeDetails();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el.detailsOverlay.hidden) closeDetails();
  });
}

/** Manifest command (Plugins menu + Ctrl+Shift+U) triggers the same run. */
function wireCommand() {
  return api.commands.on("upscale.run", () => {
    if (el.run.disabled) return;
    void run();
  });
}

async function init() {
  log("panel ready");
  const settings = await loadSettings();
  if (typeof settings.origin === "string" && /^http:\/\/127\.0\.0\.1:\d{2,5}$/.test(settings.origin)) {
    serviceOrigin = settings.origin;
  }
  if (settings.model) el.model.dataset.preferred = settings.model;
  if (settings.scale) el.scale.value = String(settings.scale);
  if (settings.engine) el.engine.value = settings.engine;
  syncSelects();
  if (typeof settings.matchSize === "boolean") el.matchSize.checked = settings.matchSize;
  if (typeof settings.hideSource === "boolean") el.hideSource.checked = settings.hideSource;
  badge("Ready", "ok");
  el.timing.textContent = "";
  void refreshService();
}

if (!SDK) {
  badge("Failed", "error");
  el.badge.title = "Photon Plugin SDK is unavailable — open this panel from Photon Studio.";
} else {
  SDK.activate((sdk) => {
    api = sdk;
    wire();
    const command = wireCommand();
    void init().catch((error) => {
      badge("Failed", "error");
      el.badge.title = errText(error);
      log(`init ERROR: ${errText(error)}`);
    });
    return {
      dispose() {
        command.dispose();
        api = null;
      },
    };
  });
}
