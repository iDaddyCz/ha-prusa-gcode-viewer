// Prusa G-code Viewer card: draws the toolpaths of the print the printer is running.
// Printed layers are solid, the rest faint; the split follows the printer's Z height.

import {
  BufferAttribute,
  BufferGeometry,
  Color,
  LineBasicMaterial,
  LineSegments,
  PerspectiveCamera,
  Scene,
  Vector3,
  WebGLRenderer,
} from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

const VERSION = "1.0.2";
const MAGIC = 0x31564750; // "PGV1"
const POLL_MS = 2500;

// PrusaSlicer's feature colours, for color_by: type.
const TYPE_COLORS = {
  "External perimeter": "#FF7D38",
  Perimeter: "#FFE64D",
  "Overhang perimeter": "#0000FF",
  "Internal infill": "#B03029",
  "Solid infill": "#9654CC",
  "Top solid infill": "#F04040",
  Ironing: "#FF8C69",
  "Bridge infill": "#4D80BA",
  "Gap fill": "#FFFFFF",
  "Skirt/Brim": "#00876E",
  "Support material": "#00FF00",
  "Support material interface": "#008000",
  "Wipe tower": "#B3E3AB",
  Custom: "#5ED194",
};
// With color_by: tool, inner features are drawn darker so the outer shell stands out.
const TYPE_SHADE = {
  "External perimeter": 1,
  "Overhang perimeter": 1,
  "Top solid infill": 0.9,
  Perimeter: 0.8,
  "Gap fill": 0.8,
  "Bridge infill": 0.72,
  "Solid infill": 0.68,
  "Internal infill": 0.58,
  "Skirt/Brim": 0.7,
  "Support material": 0.5,
  "Support material interface": 0.55,
  Custom: 0.5,
};
const WALLS = ["External perimeter", "Overhang perimeter"];
// Not part of the object: left out when framing the camera.
const NOT_OBJECT = ["Custom", "Wipe tower"];

// Parsed models by key, kept while the dashboard stays open.
const models = new Map();

function parseModel(buffer) {
  const view = new DataView(buffer);
  if (view.getUint32(0, true) !== MAGIC) throw new Error("Not a viewer model");
  const headerLength = view.getUint32(4, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 8, headerLength)));
  let pos = 8 + headerLength;
  const points = new Uint16Array(buffer, pos, header.points * 3);
  pos += header.points * 6;
  pos += (4 - (pos % 4)) % 4;
  const starts = new Uint32Array(buffer, pos, header.polylines + 1);
  pos += starts.byteLength;
  const types = new Uint8Array(buffer, pos, header.polylines);
  const tools = new Uint8Array(buffer, pos + header.polylines, header.polylines);
  return { header, points, starts, types, tools };
}

// Line-segment buffers for the chosen features, each layer's first segment and the
// object's bounding box (without purge lines) to frame the camera on.
function buildSegments(model, { walls, colorBy }) {
  const { header, points, starts, types, tools } = model;
  const names = header.types;
  let wanted = names.map(() => true);
  if (walls) {
    wanted = names.map((n) => WALLS.includes(n));
    if (!wanted.some(Boolean)) wanted = names.map((n) => n === "Perimeter");
  }
  const toolColors = header.tools.map((c) => new Color(c));
  const typeColors = names.map((n) => new Color(TYPE_COLORS[n] || "#C0C0C0"));
  const shade = names.map((n) => TYPE_SHADE[n] ?? 0.7);

  let count = 0;
  for (let i = 0; i < header.polylines; i++) if (wanted[types[i]]) count += starts[i + 1] - starts[i] - 1;

  const positions = new Float32Array(count * 6);
  const colors = new Uint8Array(count * 6);
  const layerStarts = new Uint32Array(header.layers.length + 1);
  const [ox, oy, oz] = header.origin;
  const s = header.scale;
  const c = new Color();
  const object = names.map((n) => !NOT_OBJECT.includes(n));
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  let seg = 0;
  let layer = 0;
  for (let i = 0; i < header.polylines; i++) {
    while (layer < header.layers.length && header.layers[layer].start <= i) layerStarts[layer++] = seg;
    const t = types[i];
    if (object[t]) {
      for (let p = starts[i] * 3; p < starts[i + 1] * 3; p += 3) {
        for (let a = 0; a < 3; a++) {
          const v = points[p + a];
          if (v < lo[a]) lo[a] = v;
          if (v > hi[a]) hi[a] = v;
        }
      }
    }
    if (!wanted[t]) continue;
    if (colorBy === "type") c.copy(typeColors[t]);
    else c.copy(toolColors[tools[i]] || toolColors[0]).multiplyScalar(shade[t]);
    // Every other layer a touch darker, so layer lines read on flat walls.
    if ((layer & 1) === 0) c.multiplyScalar(0.92);
    const r = Math.round(c.r * 255), g = Math.round(c.g * 255), b = Math.round(c.b * 255);
    for (let p = starts[i]; p < starts[i + 1] - 1; p++) {
      const o = seg * 6;
      positions[o] = ox + points[p * 3] * s;
      positions[o + 1] = oy + points[p * 3 + 1] * s;
      positions[o + 2] = oz + points[p * 3 + 2] * s;
      positions[o + 3] = ox + points[p * 3 + 3] * s;
      positions[o + 4] = oy + points[p * 3 + 4] * s;
      positions[o + 5] = oz + points[p * 3 + 5] * s;
      colors.set([r, g, b, r, g, b], o);
      seg++;
    }
  }
  while (layer <= header.layers.length) layerStarts[layer++] = seg;
  const bounds = lo[0] === Infinity ? header.bounds : [lo.map((v, a) => header.origin[a] + v * s), hi.map((v, a) => header.origin[a] + v * s)];
  return { positions, colors, layerStarts, count, bounds };
}

function bedGeometry(header) {
  const [lo, hi] = header.bounds;
  const bed = header.bed || [
    [lo[0] - 10, lo[1] - 10],
    [hi[0] + 10, lo[1] - 10],
    [hi[0] + 10, hi[1] + 10],
    [lo[0] - 10, hi[1] + 10],
  ];
  const xs = bed.map((p) => p[0]), ys = bed.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const v = [];
  for (let i = 0; i < bed.length; i++) {
    const [a, b] = [bed[i], bed[(i + 1) % bed.length]];
    v.push(a[0], a[1], 0, b[0], b[1], 0);
  }
  for (let x = Math.ceil(x0 / 10) * 10; x <= x1; x += 10) v.push(x, y0, 0, x, y1, 0);
  for (let y = Math.ceil(y0 / 10) * 10; y <= y1; y += 10) v.push(x0, y, 0, x1, y, 0);
  const g = new BufferGeometry();
  g.setAttribute("position", new BufferAttribute(new Float32Array(v), 3));
  return g;
}

class PrusaGcodeViewerCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { height: 360, color_by: "tool", walls: false, ...config };
    this._walls = !!this._config.walls;
    if (this._root) this._render();
  }

  getCardSize() {
    return Math.ceil(this._config.height / 50);
  }

  getGridOptions() {
    return { columns: 12, rows: Math.ceil(this._config.height / 56) + 1, min_columns: 6, min_rows: 4 };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (!this._root) this._setup();
    if (first) {
      this._refresh();
      return;
    }
    const e = this._entities || {};
    const watched = [e.state, e.file].map((id) => hass.states[id]?.state).join("|");
    if (this._watched !== undefined && watched !== this._watched) this._refresh();
    this._watched = watched;
    this._applyProgress();
  }

  connectedCallback() {
    this._connected = true;
    if (this._root && this._hass) {
      this._initGl();
      this._refresh();
    }
  }

  disconnectedCallback() {
    this._connected = false;
    clearTimeout(this._poll);
    this._disposeGl();
  }

  // --- DOM -----------------------------------------------------------------------

  _setup() {
    this._root = this.attachShadow({ mode: "open" });
    this._root.innerHTML = `
      <style>
        :host { display: block; height: 100%; }
        ha-card { display: block; position: relative; overflow: hidden; height: 100%; min-height: var(--pgv-height); box-sizing: border-box; }
        .stage { position: absolute; inset: 0; }
        canvas { display: block; width: 100%; height: 100%; touch-action: pan-y; }
        canvas.active { touch-action: none; }
        .info { position: absolute; top: 10px; left: 12px; right: 12px; pointer-events: none;
          font-size: 13px; line-height: 1.35; color: var(--primary-text-color);
          text-shadow: 0 0 4px var(--card-background-color, #000); }
        .name { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .sub { color: var(--secondary-text-color); }
        .status { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
          padding: 24px; text-align: center; color: var(--secondary-text-color); font-size: 14px; }
        .status[hidden] { display: none; }
        .buttons { position: absolute; right: 8px; bottom: 8px; display: flex; gap: 6px; }
        button { font: inherit; font-size: 12px; padding: 5px 10px; border-radius: 14px; cursor: pointer;
          color: var(--primary-text-color); background: var(--secondary-background-color, rgba(127,127,127,.2));
          border: 1px solid var(--divider-color, rgba(127,127,127,.3)); }
        button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
        .hint { position: absolute; left: 50%; bottom: 10px; transform: translateX(-50%); font-size: 12px;
          color: var(--secondary-text-color); pointer-events: none; }
        .hint[hidden] { display: none; }
      </style>
      <ha-card>
        <div class="stage"></div>
        <div class="info"><div class="name"></div><div class="sub"></div></div>
        <div class="status">Loading…</div>
        <div class="hint" hidden>Tap to rotate and zoom</div>
        <div class="buttons">
          <button class="walls" title="Show only the outer walls">Walls</button>
          <button class="reset" title="Reset the view">Reset</button>
          <button class="done" hidden>Done</button>
        </div>
      </ha-card>`;
    const $ = (s) => this._root.querySelector(s);
    this._el = { stage: $(".stage"), name: $(".name"), sub: $(".sub"), status: $(".status"), hint: $(".hint"),
      walls: $(".walls"), reset: $(".reset"), done: $(".done") };
    this._el.walls.addEventListener("click", () => {
      this._walls = !this._walls;
      this._rebuild();
    });
    this._el.reset.addEventListener("click", () => this._fitCamera());
    this._el.done.addEventListener("click", (ev) => {
      ev.stopPropagation();
      this._setInteractive(false);
    });
    this._render();
    this._initGl();
  }

  _render() {
    this.style.setProperty("--pgv-height", `${this._config.height}px`);
    this._el.walls.classList.toggle("on", this._walls);
  }

  _setStatus(text) {
    this._el.status.textContent = text || "";
    this._el.status.hidden = !text;
  }

  // --- data ----------------------------------------------------------------------

  async _refresh() {
    clearTimeout(this._poll);
    if (!this._hass || this._busy) return;
    this._busy = true;
    let status;
    try {
      const msg = { type: "prusa_gcode_viewer/status" };
      if (this._config.entry_id) msg.entry_id = this._config.entry_id;
      status = await this._hass.callWS(msg);
    } catch (err) {
      this._setStatus(err.message || "Prusa G-code Viewer isn't available");
      return;
    } finally {
      this._busy = false;
    }
    this._entities = status.entities || {};
    if (this._live !== status.live) this._splitOf = undefined;
    this._live = status.live;
    if (status.state === "building") {
      const mb = (n) => (n / 1e6).toFixed(1);
      let progress = status.size ? `${mb(status.received)} / ${mb(status.size)} MB` : `${mb(status.received)} MB`;
      // The printer sends slowly while printing: estimate the time left from the last polls.
      const now = Date.now();
      const prev = this._download;
      if (prev && prev.name === status.name && status.received > prev.received && status.size) {
        const rate = (status.received - prev.received) / ((now - prev.at) / 1000);
        const minutes = Math.ceil((status.size - status.received) / rate / 60);
        progress += minutes > 1 ? `, about ${minutes} min left` : ", almost done";
      }
      if (!prev || prev.name !== status.name || now - prev.at > 20000) this._download = { name: status.name, received: status.received, at: now };
      this._showName(status.name, status.stage === "building" ? "Building the 3D view…" : `Downloading G-code… ${progress}`);
      if (!this._model) {
        this._setStatus("Preparing the 3D view of this print. The printer sends the file slowly while it prints, so the first view of a new print can take a while.");
      }
      if (this._connected) this._poll = setTimeout(() => this._refresh(), POLL_MS);
      return;
    }
    if (status.state === "error") {
      this._showName(status.name, "");
      this._setStatus(`Can't show this print: ${status.message}`);
      if (this._connected) this._poll = setTimeout(() => this._refresh(), 30000);
      return;
    }
    if (status.state === "idle") {
      this._setStatus(status.message ? `No model yet: ${status.message}` : "No print to show yet.");
      return;
    }
    if (status.key !== this._key) await this._load(status);
    this._applyProgress();
  }

  async _load(status) {
    let model = models.get(status.key);
    if (!model) {
      this._setStatus("Loading the 3D view…");
      try {
        const response = await this._hass.fetchWithAuth(status.url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        model = parseModel(await response.arrayBuffer());
      } catch (err) {
        this._setStatus(`Couldn't load the model: ${err.message}`);
        return;
      }
      models.clear(); // keep one model in memory
      models.set(status.key, model);
    }
    this._key = status.key;
    this._model = model;
    this._setStatus("");
    this._rebuild();
    this._fitCamera();
  }

  _showName(name, sub) {
    this._el.name.textContent = name || "";
    this._el.sub.textContent = sub || "";
  }

  // --- WebGL ---------------------------------------------------------------------

  _initGl() {
    if (this._renderer || !this._el) return;
    const renderer = new WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this._el.stage.appendChild(renderer.domElement);
    const camera = new PerspectiveCamera(35, 1, 0.5, 5000);
    camera.up.set(0, 0, 1);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.addEventListener("change", () => this._draw());
    this._renderer = renderer;
    this._camera = camera;
    this._controls = controls;
    this._scene = new Scene();
    this._resize = new ResizeObserver(() => this._onResize());
    this._resize.observe(this._el.stage);
    // On touch screens one finger would otherwise rotate instead of scrolling the page.
    this._coarse = window.matchMedia?.("(pointer: coarse)").matches;
    if (this._coarse) {
      this._setInteractive(false);
      renderer.domElement.addEventListener("click", () => this._setInteractive(true));
    }
    if (this._model) {
      this._rebuild();
      this._fitCamera();
    }
  }

  _disposeGl() {
    if (!this._renderer) return;
    this._resize.disconnect();
    this._controls.dispose();
    this._disposeScene();
    this._renderer.dispose();
    this._renderer.forceContextLoss();
    this._renderer.domElement.remove();
    this._renderer = this._camera = this._controls = this._scene = undefined;
  }

  _disposeScene() {
    for (const obj of [...(this._scene?.children || [])]) {
      obj.geometry.dispose();
      obj.material.dispose();
      this._scene.remove(obj);
    }
  }

  _setInteractive(on) {
    if (!this._controls) return;
    this._controls.enabled = !this._coarse || on;
    this._renderer.domElement.classList.toggle("active", this._controls.enabled);
    this._el.hint.hidden = !this._coarse || on || !this._model;
    this._el.done.hidden = !this._coarse || !on;
  }

  _rebuild() {
    this._render();
    if (!this._renderer || !this._model) return;
    this._disposeScene();
    const built = buildSegments(this._model, { walls: this._walls, colorBy: this._config.color_by });
    this._segments = built;
    const position = new BufferAttribute(built.positions, 3);
    const color = new BufferAttribute(built.colors, 3, true);
    const make = (opacity) => {
      const g = new BufferGeometry();
      g.setAttribute("position", position);
      g.setAttribute("color", color);
      const m = new LineBasicMaterial({ vertexColors: true, transparent: opacity < 1, opacity, depthWrite: opacity === 1 });
      return new LineSegments(g, m);
    };
    this._printed = make(1);
    this._pending = make(0.13);
    const bedColor = new Color(getComputedStyle(this).getPropertyValue("--secondary-text-color").trim() || "#888");
    const bed = new LineSegments(
      bedGeometry(this._model.header),
      new LineBasicMaterial({ color: bedColor, transparent: true, opacity: 0.25, depthWrite: false }),
    );
    this._scene.add(bed, this._printed, this._pending);
    this._setInteractive(false);
    this._applyProgress();
  }

  // Splits printed from unprinted at the printer's current Z height.
  _applyProgress() {
    const model = this._model;
    if (!model || !this._segments) return;
    const layers = model.header.layers;
    let done = layers.length;
    const zState = this._hass?.states[this._entities?.z]?.state;
    const z = parseFloat(zState);
    if (this._live && !Number.isNaN(z)) {
      done = layers.findIndex((l) => l.z > z + 0.01);
      if (done < 0) done = layers.length;
    }
    const split = this._segments.layerStarts[done];
    // Called on every Home Assistant state change: only redraw when the split moves.
    if (split === this._split && this._segments === this._splitOf) return;
    this._split = split;
    this._splitOf = this._segments;
    const total = this._segments.count;
    this._printed.geometry.setDrawRange(0, split * 2);
    this._pending.geometry.setDrawRange(split * 2, (total - split) * 2);
    const header = model.header;
    const parts = [`Layer ${done} / ${layers.length}`];
    if (!this._live) parts.push("last print");
    else if (header.estimated_time) parts.push(`${header.estimated_time} total`);
    this._showName(header.name, parts.join(" · "));
    this._draw();
  }

  _fitCamera() {
    if (!this._camera || !this._segments) return;
    const [lo, hi] = this._segments.bounds;
    const center = new Vector3((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2);
    const radius = Math.max(5, new Vector3(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]).length() / 2);
    const distance = (radius / Math.sin(((this._camera.fov / 2) * Math.PI) / 180)) * 1.05;
    const dir = new Vector3(0.55, -1, 0.8).normalize();
    this._camera.position.copy(center).addScaledVector(dir, distance);
    this._camera.near = distance / 100;
    this._camera.far = distance * 10;
    this._camera.updateProjectionMatrix();
    this._controls.target.copy(center);
    this._controls.update();
    this._draw();
  }

  _onResize() {
    const { clientWidth: w, clientHeight: h } = this._el.stage;
    if (!w || !h) return;
    this._renderer.setSize(w, h, false);
    this._camera.aspect = w / h;
    this._camera.updateProjectionMatrix();
    this._draw();
  }

  _draw() {
    if (this._renderer && this._connected !== false) this._renderer.render(this._scene, this._camera);
  }
}

customElements.define("prusa-gcode-viewer-card", PrusaGcodeViewerCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "prusa-gcode-viewer-card",
  name: "Prusa G-code Viewer",
  description: "3D view of the print the printer is running.",
  documentationURL: "https://github.com/iDaddyCz/ha-prusa-gcode-viewer",
});
console.info(`%c PRUSA-GCODE-VIEWER-CARD %c ${VERSION} `, "background:#fa6831;color:#fff", "");
