#!/usr/bin/env python3
"""
Visual area editor — inspect and edit step-2 area polygons in the browser.

Usage:
    python validation-flows/area_editor.py <path_to_pdf>
    python validation-flows/area_editor.py <path_to_pdf> --book-name "Title" --port 5050

Navigate : ← → arrow keys or filmstrip clicks
Edit     : drag vertex handles (●) or edge midpoint handles (◦)
Save     : Save button or Ctrl+S
"""

import argparse
import json
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_file

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from utils.config import book_dirs

app = Flask(__name__)
_json_path: Path = None
_pages_dir: Path = None


def _load() -> dict:
    return json.loads(_json_path.read_text())


def _save(data: dict) -> None:
    _json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


@app.route("/")
def index():
    return _HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/book")
def book_info():
    data = _load()
    return jsonify({
        "book": data.get("book", ""),
        "pages": [{"name": p["source_image"], "prohibited": p.get("prohibited", False)} for p in data["pages"]],
    })


@app.route("/api/page/<page_name>")
def get_page(page_name: str):
    data = _load()
    for page in data["pages"]:
        if page["source_image"] == page_name:
            return jsonify(page)
    return jsonify({"error": "not found"}), 404


@app.route("/api/page/<page_name>", methods=["POST"])
def save_page(page_name: str):
    data = _load()
    payload = request.get_json()
    for page in data["pages"]:
        if page["source_image"] == page_name:
            page["areas"] = payload["areas"]
            break
    _save(data)
    return jsonify({"ok": True})


@app.route("/api/page/<page_name>/ignore", methods=["POST"])
def toggle_ignore(page_name: str):
    data = _load()
    for page in data["pages"]:
        if page["source_image"] == page_name:
            page["ignored"] = not page.get("ignored", False)
            _save(data)
            return jsonify({"ok": True, "ignored": page["ignored"]})
    return jsonify({"error": "not found"}), 404


@app.route("/images/<filename>")
def serve_image(filename: str):
    p = _pages_dir / filename
    if not p.exists():
        return "not found", 404
    return send_file(p)


_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Area Editor</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #1a1a1a; color: #ddd;
  font-family: 'SF Mono', Consolas, monospace; font-size: 13px;
  height: 100vh; display: flex; flex-direction: column; overflow: hidden;
}

#toolbar {
  background: #252525; border-bottom: 1px solid #3a3a3a;
  padding: 0 16px; display: flex; align-items: center; gap: 16px;
  height: 46px; flex-shrink: 0;
}
#toolbar-title { color: #666; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
#page-label { color: #fff; font-weight: bold; min-width: 150px; }
#ignore-btn {
  padding: 5px 22px; background: #444; color: #aaa;
  border: none; border-radius: 4px; cursor: pointer;
  font-weight: bold; font-size: 13px; font-family: inherit;
  transition: background 0.15s, color 0.15s;
}
#ignore-btn:hover { background: #555; color: #ddd; }
#ignore-btn.active { background: #c0392b; color: #fff; }
#ignore-btn.active:hover { background: #a93226; }
#status { margin-left: auto; color: #777; font-size: 12px; }

#main {
  flex: 1; display: flex; overflow: hidden;
}
#editor {
  flex: 1; overflow: auto;
  display: flex; justify-content: center; align-items: center;
  padding: 20px; background: #141414;
}
#canvas-wrap { position: relative; display: inline-block; line-height: 0; flex-shrink: 0; }
#page-img { display: block; }
#svg-overlay { position: absolute; top: 0; left: 0; pointer-events: none; overflow: visible; }

#info-panel {
  width: 300px; flex-shrink: 0;
  background: #1e1e1e; border-left: 1px solid #333;
  display: flex; flex-direction: column; overflow: hidden;
}
#info-header {
  padding: 10px 14px; background: #252525; border-bottom: 1px solid #333;
  font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 1px;
  display: flex; align-items: center;
}
#info-id   { color: #fff; font-weight: bold; }
#info-type { color: #aaa; margin-left: 8px; }
#delete-btn {
  display: none; margin-left: auto;
  padding: 3px 10px; background: #4a1010; border: 1px solid #7a2020;
  border-radius: 3px; color: #c87070; cursor: pointer;
  font-size: 11px; font-family: inherit;
  transition: background 0.1s, color 0.1s;
}
#delete-btn:hover { background: #7a1515; color: #f09090; }
#info-text {
  flex: 1; overflow-y: auto; padding: 14px;
  white-space: pre-wrap; font-size: 13px; line-height: 1.6; color: #ccc;
  background: transparent; border: none; outline: none;
  resize: none; width: 100%; font-family: 'SF Mono', Consolas, monospace;
}
#info-text:focus { background: #111; }
#info-text::placeholder { color: #555; font-style: italic; }
#info-text:read-only { color: #555; font-style: italic; cursor: default; }
#add-area-panel {
  display: none; padding: 14px; border-top: 1px solid #2a2a2a;
}
#add-illus-btn {
  width: 100%; padding: 8px 0; background: #0d2b0d; border: 1px solid #1e5c1e;
  border-radius: 4px; color: #6bbf6b; cursor: pointer;
  font-size: 12px; font-family: inherit;
  transition: background 0.1s, color 0.1s;
}
#add-illus-btn:hover { background: #163d16; color: #88d488; }
#add-illus-btn.drawing { background: #1e5c1e; color: #adf0ad; border-color: #4CAF50; }

#filmstrip {
  height: 50px; background: #0f0f0f; border-top: 1px solid #2a2a2a;
  display: flex; align-items: center; overflow-x: auto;
  padding: 0 12px; gap: 3px; flex-shrink: 0;
}
#filmstrip::-webkit-scrollbar { height: 4px; }
#filmstrip::-webkit-scrollbar-thumb { background: #333; }
.pg {
  padding: 5px 11px; background: #222; border: 1px solid #3a3a3a;
  color: #999; cursor: pointer; border-radius: 3px;
  font-size: 12px; font-family: monospace; flex-shrink: 0;
  transition: background 0.1s, color 0.1s;
}
.pg:hover { background: #333; color: #eee; }
.pg.active { background: #0d6efd; border-color: #0d6efd; color: #fff; }
.pg.prohibited { background: #3a0e0e; border-color: #7a2020; color: #c87070; }
.pg.prohibited:hover { background: #4d1414; color: #e08080; }
.pg.prohibited.active { background: #c0392b; border-color: #e74c3c; color: #fff; }

#type-switcher {
  display: none; padding: 7px 14px; gap: 5px;
  border-bottom: 1px solid #2a2a2a; flex-wrap: wrap;
}
#type-switcher.visible { display: flex; }
.type-btn {
  padding: 3px 9px; border: 1px solid #3a3a3a; border-radius: 3px;
  background: #252525; color: #666; cursor: pointer;
  font-size: 11px; font-family: inherit;
  transition: background 0.1s, color 0.1s, border-color 0.1s;
}
.type-btn:hover { background: #333; color: #ccc; }
.type-btn.active { border-color: var(--type-color, #888); color: var(--type-color, #ccc); background: #1a1a1a; }
</style>
</head>
<body>

<div id="toolbar">
  <span id="toolbar-title">Area Editor</span>
  <span id="page-label">—</span>
  <button id="ignore-btn" onclick="toggleIgnore()">Ignore Page</button>
  <span id="status">Select a page from the filmstrip</span>
</div>

<div id="main">
  <div id="editor">
    <div id="canvas-wrap">
      <img id="page-img" src="" alt="">
      <svg id="svg-overlay"></svg>
    </div>
  </div>
  <div id="info-panel">
    <div id="info-header">
      <span id="info-id">—</span><span id="info-type"></span>
      <button id="delete-btn" onclick="deleteArea()">Delete</button>
    </div>
    <div id="type-switcher">
      <button class="type-btn" data-type="main_text"     onclick="setAreaType('main_text')">main_text</button>
      <button class="type-btn" data-type="chapter_title" onclick="setAreaType('chapter_title')">chapter_title</button>
      <button class="type-btn" data-type="subtitle"      onclick="setAreaType('subtitle')">subtitle</button>
    </div>
    <textarea id="info-text" placeholder="Click an area to see its text" readonly></textarea>
    <div id="add-area-panel">
      <button id="add-illus-btn" onclick="toggleDrawMode()">+ Add Illustration</button>
    </div>
  </div>
</div>

<div id="filmstrip"></div>

<script>
const COLORS = {
  main_text:            '#4CAF50',
  illustration:         '#2196F3',
  illustration_caption: '#00BCD4',
  footnote:             '#FF9800',
  header:               '#F44336',
  footer:               '#EF6C00',
  page_number:          '#AB47BC',
  chapter_title:        '#E91E63',
  subtitle:             '#FF5722',
  decoration:           '#757575',
};
const DEFAULT_COLOR = '#FFEB3B';

const img = document.getElementById('page-img');
const svg = document.getElementById('svg-overlay');

let pages = [], currentIdx = -1, pageData = null, scale = 1, selectedAreaIdx = -1;

const SWITCHABLE_TYPES = new Set(['main_text', 'chapter_title', 'subtitle']);

function showTypeSwitcher(type) {
  const sw = document.getElementById('type-switcher');
  if (SWITCHABLE_TYPES.has(type)) {
    sw.classList.add('visible');
    sw.querySelectorAll('.type-btn').forEach(btn => {
      const active = btn.dataset.type === type;
      btn.classList.toggle('active', active);
      btn.style.setProperty('--type-color', active ? (COLORS[type] || '#888') : '');
    });
  } else {
    sw.classList.remove('visible');
  }
}

function setAreaType(type) {
  if (selectedAreaIdx < 0 || !pageData) return;
  pageData.areas[selectedAreaIdx].type = type;
  renderSVG();
  selectArea(selectedAreaIdx);
  savePage();
}

// ── Fit image to editor ───────────────────────────────────────────────────

function fitImage() {
  if (!pageData) return;
  // Use window dimensions minus known fixed chrome heights so the result is
  // stable regardless of when this is called during layout.
  const TOOLBAR    = 46;
  const FILMSTRIP  = 50;
  const PAD        = 24; // padding on each side inside #editor
  const HANDLE_R   = 10; // extra room so vertex handles aren't clipped
  const INFO_W     = 300;
  const availW = window.innerWidth  - INFO_W - PAD * 2 - HANDLE_R * 2;
  const availH = window.innerHeight - TOOLBAR - FILMSTRIP - PAD * 2 - HANDLE_R * 2;
  const nw = pageData.page_dimensions.width;
  const nh = pageData.page_dimensions.height;
  scale = Math.min(availW / nw, availH / nh);
  const dispW = Math.round(nw * scale);
  const dispH = Math.round(nh * scale);
  img.style.width  = dispW + 'px';
  img.style.height = dispH + 'px';
  svg.style.width  = dispW + 'px';
  svg.style.height = dispH + 'px';
}

window.addEventListener('resize', () => { fitImage(); renderSVG(); });

// ── Init ──────────────────────────────────────────────────────────────────

async function init() {
  const r = await fetch('/api/book');
  const b = await r.json();
  pages = b.pages;

  const strip = document.getElementById('filmstrip');
  pages.forEach((p, i) => {
    const num = p.name.replace(/\\D/g, '').replace(/^0+/, '') || String(i + 1);
    const btn = document.createElement('button');
    btn.className = 'pg';
    btn.id = 'pg' + i;
    btn.textContent = num;
    btn.onclick = () => loadPage(i);
    if (p.prohibited) btn.classList.add('prohibited');
    strip.appendChild(btn);
  });
}

// ── Load page ─────────────────────────────────────────────────────────────

async function loadPage(idx) {
  if (idx < 0 || idx >= pages.length) return;
  currentIdx = idx;

  document.querySelectorAll('.pg').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('pg' + idx);
  btn.classList.add('active');
  btn.scrollIntoView({ inline: 'nearest', block: 'nearest' });

  document.getElementById('page-label').textContent = pages[idx].name;
  setStatus('Loading…');

  const r = await fetch('/api/page/' + encodeURIComponent(pages[idx].name));
  pageData = await r.json();

  selectedAreaIdx = -1;
  document.getElementById('ignore-btn').className  = '';
  document.getElementById('info-id').textContent   = '—';
  document.getElementById('info-id').style.color   = '';
  document.getElementById('info-type').textContent = '';
  document.getElementById('type-switcher').classList.remove('visible');
  const ib = document.getElementById('info-text');
  ib.value = ''; ib.placeholder = 'Click an area to see its text'; ib.readOnly = true;
  document.getElementById('delete-btn').style.display = 'none';
  document.getElementById('add-area-panel').style.display = 'none';
  exitDrawMode();
  svg.innerHTML = '';
  img.onload = () => {
    fitImage();
    renderSVG();
    document.getElementById('ignore-btn').className = pageData.ignored ? 'active' : '';
    document.getElementById('add-area-panel').style.display = 'block';
    setStatus(pageData.areas.length + ' areas  ·  click area to inspect  ·  drag handles to edit');
  };
  img.src = '/images/' + pages[idx].name;
}

// ── Render SVG ────────────────────────────────────────────────────────────

function renderSVG() {
  svg.innerHTML = '';

  pageData.areas.forEach((area, ai) => {
    const c   = COLORS[area.type] || DEFAULT_COLOR;
    const pts = area.polygon;

    // Polygon fill + border
    const isSelected = ai === selectedAreaIdx;
    const poly = el('polygon');
    poly.setAttribute('points', pts.map(([x, y]) => x * scale + ',' + y * scale).join(' '));
    poly.setAttribute('fill', c);
    poly.setAttribute('fill-opacity', isSelected ? '0.28' : '0.12');
    poly.setAttribute('stroke', c);
    poly.setAttribute('stroke-width', isSelected ? '3' : '2');
    poly.setAttribute('stroke-opacity', '0.9');
    poly.style.cssText = 'cursor:pointer;pointer-events:all';
    poly.addEventListener('click', e => { e.stopPropagation(); if (!drawMode) selectArea(ai); });
    svg.appendChild(poly);

    // Label at centroid
    const [lx, ly] = centroid(pts);
    const lbl = el('text');
    lbl.setAttribute('x', lx * scale);
    lbl.setAttribute('y', ly * scale);
    lbl.setAttribute('text-anchor', 'middle');
    lbl.setAttribute('dominant-baseline', 'middle');
    lbl.setAttribute('fill', c);
    lbl.setAttribute('font-size', '11');
    lbl.setAttribute('font-family', 'monospace');
    lbl.setAttribute('pointer-events', 'none');
    lbl.textContent = area.id + '  ' + area.type;
    svg.appendChild(lbl);

    // Rectangle handles — polygon is [TL, TR, BR, BL]
    // Edge midpoints: constrained to one axis
    const edges = [
      { mx: (pts[0][0]+pts[1][0])/2, my: pts[0][1],               axis: 1, vis: [0,1], cursor: 'ns-resize' }, // top
      { mx: pts[1][0],               my: (pts[1][1]+pts[2][1])/2,  axis: 0, vis: [1,2], cursor: 'ew-resize' }, // right
      { mx: (pts[2][0]+pts[3][0])/2, my: pts[2][1],               axis: 1, vis: [2,3], cursor: 'ns-resize' }, // bottom
      { mx: pts[0][0],               my: (pts[0][1]+pts[3][1])/2,  axis: 0, vis: [0,3], cursor: 'ew-resize' }, // left
    ];
    edges.forEach(({ mx, my, axis, vis, cursor }) => {
      const h = el('circle');
      h.setAttribute('cx', mx * scale);
      h.setAttribute('cy', my * scale);
      h.setAttribute('r', 5);
      h.setAttribute('fill', 'rgba(255,255,255,0.5)');
      h.setAttribute('stroke', c);
      h.setAttribute('stroke-width', '1.5');
      h.style.cssText = `cursor:${cursor};pointer-events:all`;
      makeEdgeDraggable(h, ai, axis, vis);
      svg.appendChild(h);
    });

    // Corner handles: move two edges at once
    const corners = [
      { vi: 0, xv: [0,3], yv: [0,1], cursor: 'nwse-resize' }, // TL
      { vi: 1, xv: [1,2], yv: [0,1], cursor: 'nesw-resize' }, // TR
      { vi: 2, xv: [1,2], yv: [2,3], cursor: 'nwse-resize' }, // BR
      { vi: 3, xv: [0,3], yv: [2,3], cursor: 'nesw-resize' }, // BL
    ];
    corners.forEach(({ vi, xv, yv, cursor }) => {
      const [vx, vy] = pts[vi];
      const h = el('circle');
      h.setAttribute('cx', vx * scale);
      h.setAttribute('cy', vy * scale);
      h.setAttribute('r', 7);
      h.setAttribute('fill', '#ffffff');
      h.setAttribute('stroke', c);
      h.setAttribute('stroke-width', '2.5');
      h.style.cssText = `cursor:${cursor};pointer-events:all`;
      makeCornerDraggable(h, ai, xv, yv);
      svg.appendChild(h);
    });
  });
}

// ── Drag (rectangle-constrained) ──────────────────────────────────────────

function makeEdgeDraggable(handle, areaIdx, axis, vertexIndices) {
  // axis 0 = X only (left/right edge), axis 1 = Y only (top/bottom edge)
  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    e.stopPropagation();
    const poly  = pageData.areas[areaIdx].polygon;
    const start = axis === 0 ? e.clientX : e.clientY;
    const orig  = vertexIndices.map(vi => poly[vi][axis]);
    function onMove(e) {
      const delta = ((axis === 0 ? e.clientX : e.clientY) - start) / scale;
      vertexIndices.forEach((vi, k) => { poly[vi][axis] = Math.round(orig[k] + delta); });
      renderSVG();
    }
    function onUp() {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      savePage();
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  });
}

function makeCornerDraggable(handle, areaIdx, xVertices, yVertices) {
  // xVertices: vertex indices whose X changes; yVertices: whose Y changes
  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    e.stopPropagation();
    const poly   = pageData.areas[areaIdx].polygon;
    const startX = e.clientX, startY = e.clientY;
    const origX  = xVertices.map(vi => poly[vi][0]);
    const origY  = yVertices.map(vi => poly[vi][1]);
    function onMove(e) {
      const dx = (e.clientX - startX) / scale;
      const dy = (e.clientY - startY) / scale;
      xVertices.forEach((vi, k) => { poly[vi][0] = Math.round(origX[k] + dx); });
      yVertices.forEach((vi, k) => { poly[vi][1] = Math.round(origY[k] + dy); });
      renderSVG();
    }
    function onUp() {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      savePage();
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  });
}

// ── Ignore page ───────────────────────────────────────────────────────────

async function toggleIgnore() {
  if (!pageData) return;
  const r = await fetch('/api/page/' + encodeURIComponent(pageData.source_image) + '/ignore', {
    method: 'POST',
  });
  const j = await r.json();
  if (j.ok) {
    pageData.ignored = j.ignored;
    document.getElementById('ignore-btn').className = j.ignored ? 'active' : '';
    setStatus(j.ignored ? 'Page marked as ignored' : 'Page unmarked');
  }
}

// ── Area selection / info panel ───────────────────────────────────────────

function selectArea(ai) {
  selectedAreaIdx = ai;
  renderSVG();
  const area = pageData.areas[ai];
  const c = COLORS[area.type] || DEFAULT_COLOR;
  document.getElementById('info-id').textContent   = area.id;
  document.getElementById('info-id').style.color   = c;
  document.getElementById('info-type').textContent = '  ' + area.type;
  document.getElementById('delete-btn').style.display = 'inline-block';
  document.getElementById('add-area-panel').style.display = 'none';
  showTypeSwitcher(area.type);
  exitDrawMode();
  const box = document.getElementById('info-text');
  if (area.type === 'illustration' || area.type === 'decoration') {
    box.value       = '';
    box.placeholder = '(no text for this area type)';
    box.readOnly    = true;
  } else {
    box.value       = area.text || '';
    box.placeholder = '';
    box.readOnly    = false;
  }
}

// ── Delete area ───────────────────────────────────────────────────────────

function deleteArea() {
  if (selectedAreaIdx < 0 || !pageData) return;
  const area = pageData.areas[selectedAreaIdx];
  if (!confirm(`Delete ${area.id} (${area.type})?`)) return;
  pageData.areas.splice(selectedAreaIdx, 1);
  selectedAreaIdx = -1;
  document.getElementById('info-id').textContent   = '—';
  document.getElementById('info-id').style.color   = '';
  document.getElementById('info-type').textContent = '';
  document.getElementById('delete-btn').style.display = 'none';
  document.getElementById('add-area-panel').style.display = 'block';
  const box = document.getElementById('info-text');
  box.value       = '';
  box.placeholder = 'Click an area to see its text';
  box.readOnly    = true;
  renderSVG();
  savePage();
}

// ── Save ──────────────────────────────────────────────────────────────────

async function savePage() {
  if (!pageData) return;
  setStatus('Saving…');
  const r = await fetch('/api/page/' + encodeURIComponent(pageData.source_image), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ areas: pageData.areas }),
  });
  const j = await r.json();
  setStatus(j.ok ? 'Saved ✓' : 'Save failed!');
}

// ── Draw mode (add illustration) ─────────────────────────────────────────

let drawMode = false, drawStart = null;
const canvasWrap = document.getElementById('canvas-wrap');

function toggleDrawMode() {
  drawMode ? exitDrawMode() : enterDrawMode();
}
function enterDrawMode() {
  drawMode = true;
  canvasWrap.style.cursor = 'crosshair';
  document.getElementById('add-illus-btn').classList.add('drawing');
  setStatus('Click and drag to place illustration area  ·  Esc to cancel');
}
function exitDrawMode() {
  drawMode = false;
  drawStart = null;
  canvasWrap.style.cursor = '';
  const btn = document.getElementById('add-illus-btn');
  if (btn) btn.classList.remove('drawing');
  const preview = document.getElementById('draw-preview');
  if (preview) preview.remove();
}

canvasWrap.addEventListener('mousedown', e => {
  if (!drawMode) return;
  e.preventDefault(); e.stopPropagation();
  const r = canvasWrap.getBoundingClientRect();
  drawStart = {
    x: Math.round((e.clientX - r.left) / scale),
    y: Math.round((e.clientY - r.top)  / scale),
  };
});

canvasWrap.addEventListener('mousemove', e => {
  if (!drawMode || !drawStart) return;
  const r  = canvasWrap.getBoundingClientRect();
  const cx = Math.round((e.clientX - r.left) / scale);
  const cy = Math.round((e.clientY - r.top)  / scale);
  let preview = document.getElementById('draw-preview');
  if (!preview) {
    preview = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    preview.id = 'draw-preview';
    preview.setAttribute('fill', COLORS.illustration);
    preview.setAttribute('fill-opacity', '0.18');
    preview.setAttribute('stroke', COLORS.illustration);
    preview.setAttribute('stroke-width', '2');
    preview.setAttribute('stroke-dasharray', '6,3');
    preview.setAttribute('pointer-events', 'none');
    svg.appendChild(preview);
  }
  const x1 = Math.min(drawStart.x, cx) * scale;
  const y1 = Math.min(drawStart.y, cy) * scale;
  preview.setAttribute('x', x1);
  preview.setAttribute('y', y1);
  preview.setAttribute('width',  Math.abs(cx - drawStart.x) * scale);
  preview.setAttribute('height', Math.abs(cy - drawStart.y) * scale);
});

window.addEventListener('mouseup', e => {
  if (!drawMode || !drawStart) return;
  const r  = canvasWrap.getBoundingClientRect();
  const cx = Math.round((e.clientX - r.left) / scale);
  const cy = Math.round((e.clientY - r.top)  / scale);
  const x1 = Math.min(drawStart.x, cx), y1 = Math.min(drawStart.y, cy);
  const x2 = Math.max(drawStart.x, cx), y2 = Math.max(drawStart.y, cy);
  exitDrawMode();
  if (x2 - x1 > 5 && y2 - y1 > 5) {
    const maxN = pageData.areas.reduce((m, a) => {
      const n = parseInt((a.id || '').replace('area_', ''), 10);
      return isNaN(n) ? m : Math.max(m, n);
    }, 0);
    const maxIllus = pageData.areas.reduce((m, a) => {
      const n = parseInt((a.illustration_id || '').replace('illus_', ''), 10);
      return isNaN(n) ? m : Math.max(m, n);
    }, 0);
    pageData.areas.push({
      id: 'area_' + String(maxN + 1).padStart(3, '0'),
      type: 'illustration',
      polygon: [[x1,y1],[x2,y1],[x2,y2],[x1,y2]],
      text: null, illustration_id: 'illus_' + String(maxIllus + 1).padStart(3, '0'), linked_illustration_id: null,
    });
    renderSVG();
    savePage();
    setStatus(pageData.areas.length + ' areas  ·  click area to inspect  ·  drag handles to edit');
  }
});

// ── Keyboard ──────────────────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  if (e.key === 'Escape')     { exitDrawMode(); return; }
  if (e.key === 'ArrowRight') loadPage(currentIdx + 1);
  if (e.key === 'ArrowLeft')  loadPage(currentIdx - 1);
});

// ── Helpers ───────────────────────────────────────────────────────────────

function el(tag) { return document.createElementNS('http://www.w3.org/2000/svg', tag); }
function centroid(pts) {
  return [
    pts.reduce((s, p) => s + p[0], 0) / pts.length,
    pts.reduce((s, p) => s + p[1], 0) / pts.length,
  ];
}
function setStatus(m) { document.getElementById('status').textContent = m; }

// ── Info-text editing ─────────────────────────────────────────────────────

const infoTextEl = document.getElementById('info-text');
infoTextEl.addEventListener('input', () => {
  if (selectedAreaIdx >= 0 && pageData)
    pageData.areas[selectedAreaIdx].text = infoTextEl.value || null;
});
infoTextEl.addEventListener('blur', () => {
  if (selectedAreaIdx >= 0 && pageData) savePage();
});
infoTextEl.addEventListener('keydown', e => e.stopPropagation());

init();
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Visual area editor.")
    parser.add_argument("pdf",         help="Path to the source PDF file")
    parser.add_argument("--book-name", default=None, help="JSON filename stem (default: PDF filename stem)")
    parser.add_argument("--port",      type=int, default=5050, help="HTTP port (default: 5050)")
    args = parser.parse_args()

    global _json_path, _pages_dir
    pdf_path = Path(args.pdf)
    book_dir = pdf_path.parent
    dirs = book_dirs(book_dir)
    _pages_dir = dirs["pages"]

    if args.book_name:
        _json_path = dirs["json"] / f"{args.book_name}.json"
        if not _json_path.exists():
            print(f"Error: JSON not found: {_json_path}", file=sys.stderr)
            sys.exit(1)
    else:
        candidates = [f for f in dirs["json"].glob("*.json") if f.name != "book.json"]
        if not candidates:
            print(f"Error: no JSON file found in {dirs['json']}", file=sys.stderr)
            sys.exit(1)
        if len(candidates) > 1:
            print(f"Multiple JSON files found, use --book-name to select one:")
            for c in candidates:
                print(f"  {c.name}")
            sys.exit(1)
        _json_path = candidates[0]

    print(f"  Book : {_json_path.stem}")
    print(f"  JSON : {_json_path}")
    print(f"  Open : http://127.0.0.1:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
