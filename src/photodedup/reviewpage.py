"""A local web page for spot-checking edit pairs under edits-review/.

With a `measure-edits` CSV, the page shows only pairs whose edit visibly
differs (a crop, a rotation, or a measurable color change), largest first.

Writes edits-review/index.html next to the side-by-side copies `archive-pairs`
made. The page loads the images by relative path, so it works straight from
the drive with no server. Choices (keep the original, the edit, or both) are kept in the browser's
localStorage and exported as a CSV for `pairs --choices`.
"""

from __future__ import annotations

import json
from pathlib import Path
from sqlite3 import Connection

from photodedup.pairarchive import REVIEW_DIR, review_file, review_folder, split_pair

PAGE = "index.html"


def review_entries(conn: Connection, rows: list[dict], root: Path, diffs: dict[str, dict] | None = None) -> list[dict]:
    out = []
    for row in rows:
        if row.get("edit_pair") != "1":
            continue
        loser = conn.execute("SELECT * FROM asset WHERE uuid = ?", (row["loser"],)).fetchone()
        keeper = conn.execute("SELECT * FROM asset WHERE uuid = ?", (row["keeper"],)).fetchone()
        if loser is None or keeper is None:
            continue
        o, e = split_pair(keeper, loser)
        d = (diffs or {}).get(loser["uuid"])
        if diffs is not None and (d is None or d.get("visible") != "True"):
            continue
        folder = review_folder(o)
        orig, edit = review_file("original", o), review_file("edited", e)
        out.append({
            "loser": loser["uuid"],
            "keeper": keeper["uuid"],
            "kept": "orig" if keeper is o else "edit",
            "name": o["original_filename"] or o["uuid"],
            "date": o["photos_date"] or "",
            "orig": f"{folder}/{orig}",
            "edit": f"{folder}/{edit}",
            "origInfo": f"{o['width']}x{o['height']}, {(o['file_size'] or 0) / 1e6:.1f} MB",
            "editInfo": f"{e['width']}x{e['height']}, {(e['file_size'] or 0) / 1e6:.1f} MB",
            "diff": _describe(d) if d else "",
            "score": float(d["score"]) if d else 0.0,
            "present": (root / REVIEW_DIR / folder / orig).exists() and (root / REVIEW_DIR / folder / edit).exists(),
        })
    if diffs is not None:
        out.sort(key=lambda x: (-x["score"], x["date"], x["name"]))
    else:
        out.sort(key=lambda x: (x["date"], x["name"]))
    return out


def _describe(d: dict) -> str:
    parts = []
    if d.get("rotated") == "True":
        parts.append("rotated")
    if d.get("reshaped") == "True":
        parts.append("cropped")
    parts.append(f"pixel difference {float(d['mad']):.1f}, brightness shift {float(d['shift']):.1f}, "
                 f"contrast x{float(d['contrast']):.2f}")
    if d.get("gps_lost") == "True":
        parts.append("edit lost GPS")
    return "; ".join(parts)


def write_review_page(conn: Connection, rows: list[dict], root: Path,
                      diffs: dict[str, dict] | None = None) -> tuple[Path, int, int]:
    """Write the page; returns (path, pairs, pairs whose copies are missing)."""
    entries = review_entries(conn, rows, root, diffs)
    dest = root / REVIEW_DIR / PAGE
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(entries).replace("</", "<\\/")
    dest.write_text(_HTML.replace("/*DATA*/[]", data))
    return dest, len(entries), sum(not e["present"] for e in entries)


_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edit pair review</title>
<style>
:root { --bg:#f6f5f2; --fg:#1d1d1b; --muted:#6b6a65; --card:#fff; --line:#dddbd5; --flag:#c2410c; --flagbg:#fff1e8; --focus:#2563eb; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --fg:#ecebe7; --muted:#9d9b94; --card:#21211f; --line:#353531; --flag:#fb923c; --flagbg:#2c1d12; --focus:#60a5fa; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.4 -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif; }
header { position:sticky; top:0; z-index:5; background:var(--bg); border-bottom:1px solid var(--line); padding:10px 16px; display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; }
header h1 { font-size:15px; margin:0; font-weight:600; }
header .stat { color:var(--muted); }
header button, header select { font:inherit; padding:5px 10px; border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:6px; cursor:pointer; }
header .keys { color:var(--muted); font-size:12px; width:100%; }
main { padding:12px 16px 80px; display:grid; gap:12px; }
.pair { background:var(--card); border:2px solid var(--line); border-radius:10px; padding:10px; }
.pair.cur { border-color:var(--focus); }
.pair.flag { background:var(--flagbg); border-color:var(--flag); }
.pair .top { display:flex; justify-content:space-between; gap:8px; margin-bottom:8px; flex-wrap:wrap; }
.pair .top .n { font-weight:600; word-break:break-all; }
.pair .top .d { color:var(--muted); }
.pair .imgs { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.pair figure { margin:0; }
.pair img { width:100%; height:var(--h, 340px); object-fit:contain; background:#0002; border-radius:6px; cursor:zoom-in; display:block; }
.pair figcaption { color:var(--muted); font-size:12px; margin-top:4px; }
.pair figcaption b { color:var(--fg); font-weight:600; }
.pair .act { margin-top:8px; }
.pair .df { color:var(--muted); font-size:12px; margin-top:6px; }
.pair .act { display:flex; flex-wrap:wrap; gap:6px 16px; align-items:center; }
.pair .act label { display:flex; gap:5px; align-items:center; cursor:pointer; }
.pair.flag .act label.on { color:var(--flag); font-weight:600; }
.missing { color:var(--flag); }
#zoom { position:fixed; inset:0; background:#000e; display:none; z-index:10; }
#zoom.on { display:grid; grid-template-columns:1fr 1fr; gap:4px; }
#zoom.one { grid-template-columns:1fr; }
#zoom img { width:100%; height:100vh; object-fit:contain; }
#zoom .lbl { position:absolute; top:8px; color:#fff; background:#0008; padding:2px 8px; border-radius:4px; font-size:12px; }
@media (max-width:600px) { .pair .imgs { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header>
  <h1>Edit pair review</h1>
  <span class="stat" id="stat"></span>
  <select id="view"><option value="all">All pairs</option><option value="flag">Changed only</option><option value="sample">Random 50</option></select>
  <select id="size"><option value="240px">Small</option><option value="340px" selected>Medium</option><option value="520px">Large</option></select>
  <button id="export">Download choices CSV</button>
  <button id="clear">Reset all choices</button>
  <div class="keys">j / k next and previous. o keeps the original, e keeps the Google edit, b keeps both. Pairs you change are highlighted. z zooms the pair full screen; in zoom, space flips between original and edit. Choices are saved in this browser.</div>
</header>
<main id="list"></main>
<div id="zoom"></div>
<script>
const DATA = /*DATA*/[];
const KEY = "photodedup-edit-review";
// loser uuid -> "orig" | "edit" | "both"; absent means the plan's choice.
let picks = {};
try {
  const raw = JSON.parse(localStorage.getItem(KEY) || "{}");
  picks = Array.isArray(raw) ? Object.fromEntries(raw.map(u => [u, "both"])) : raw;
} catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(picks)); } catch (e) {} };
const pick = e => picks[e.loser] || e.kept;
const changed = e => pick(e) !== e.kept;
// encodeURI leaves # and ? alone, and a filename like "Photo #4.jpg" has them.
const src = p => p.split("/").map(encodeURIComponent).join("/");
const NAMES = { orig: "Keep original", edit: "Keep Google edit", both: "Keep both" };
const list = document.getElementById("list"), stat = document.getElementById("stat"), zoom = document.getElementById("zoom");
let shown = [], cur = 0, sample = null;

function card(e, i) {
  const el = document.createElement("section");
  el.className = "pair" + (changed(e) ? " flag" : "");
  el.dataset.i = i;
  el.innerHTML = `<div class="top"><span class="n"></span><span class="d"></span></div>
    <div class="imgs">
      <figure><img loading="lazy" data-k="orig"><figcaption><b>Original</b> (${e.kept === "orig" ? "kept" : "would be removed"}) <span class="oi"></span></figcaption></figure>
      <figure><img loading="lazy" data-k="edit"><figcaption><b>Google edit</b> (${e.kept === "edit" ? "kept" : "would be removed"}) <span class="ei"></span></figcaption></figure>
    </div>
    <div class="df"></div>
    <div class="act">${["orig", "edit", "both"].map(v => `<label data-v="${v}"><input type="radio" name="p${i}" value="${v}">${NAMES[v]}${v === e.kept ? " (plan)" : ""}</label>`).join("")}${e.present ? "" : ' <span class="missing">copies missing on the drive</span>'}</div>`;
  el.querySelector(".n").textContent = e.name;
  el.querySelector(".d").textContent = e.date.replace("T", " ");
  el.querySelector(".oi").textContent = e.origInfo;
  el.querySelector(".ei").textContent = e.editInfo;
  el.querySelector(".df").textContent = e.diff;
  const [a, b] = el.querySelectorAll("img");
  a.src = src(e.orig); b.src = src(e.edit);
  a.onclick = b.onclick = () => { cur = i; openZoom(); };
  el.querySelectorAll("input").forEach(r => r.onchange = () => { cur = i; choose(r.value); });
  label(el, e);
  return el;
}
function label(el, e) {
  el.querySelectorAll(".act label").forEach(l => {
    const on = l.dataset.v === pick(e);
    l.classList.toggle("on", on);
    l.querySelector("input").checked = on;
  });
}
function render() {
  const v = document.getElementById("view").value;
  if (v === "sample") {
    if (!sample) { sample = DATA.map((_, i) => i).sort(() => Math.random() - 0.5).slice(0, 50).sort((a, b) => a - b); }
    shown = sample.map(i => DATA[i]);
  } else {
    sample = null;
    shown = v === "flag" ? DATA.filter(changed) : DATA;
  }
  list.replaceChildren(...shown.map(card));
  cur = Math.min(cur, Math.max(0, shown.length - 1));
  focus(false);
  count();
}
function count() {
  const missing = DATA.filter(e => !e.present).length;
  stat.textContent = `${shown.length} shown of ${DATA.length} pairs · ${DATA.filter(changed).length} changed from the plan` + (missing ? ` · ${missing} missing` : "");
}
function focus(scroll = true) {
  list.querySelectorAll(".pair.cur").forEach(x => x.classList.remove("cur"));
  const el = list.children[cur];
  if (!el) return;
  el.classList.add("cur");
  if (scroll) el.scrollIntoView({ block: "center", behavior: "smooth" });
}
function choose(v) {
  const e = shown[cur]; if (!e) return;
  if (v === e.kept) delete picks[e.loser]; else picks[e.loser] = v;
  save();
  const el = list.children[cur];
  el.classList.toggle("flag", changed(e));
  label(el, e);
  count();
}
let flip = null;
function openZoom() {
  const e = shown[cur]; if (!e) return;
  flip = null;
  zoom.className = "on";
  zoom.innerHTML = `<div style="position:relative"><img src="${src(e.orig)}"><span class="lbl" style="left:8px">Original</span></div>
                    <div style="position:relative"><img src="${src(e.edit)}"><span class="lbl" style="left:8px">Google edit</span></div>`;
}
function flipZoom() {
  const e = shown[cur]; if (!e) return;
  flip = flip === "orig" ? "edit" : "orig";
  zoom.className = "on one";
  zoom.innerHTML = `<div style="position:relative"><img src="${src(e[flip])}"><span class="lbl" style="left:8px">${flip === "orig" ? "Original" : "Google edit"}</span></div>`;
}
zoom.onclick = () => { zoom.className = ""; };
document.addEventListener("keydown", ev => {
  if (ev.metaKey || ev.ctrlKey || ev.altKey || ev.target.tagName === "SELECT") return;
  const zoomed = zoom.classList.contains("on");
  if (ev.key === "Escape") { zoom.className = ""; return; }
  if (zoomed && ev.key === " ") { ev.preventDefault(); flipZoom(); return; }
  if (ev.key === "j") { cur = Math.min(cur + 1, shown.length - 1); focus(); if (zoomed) openZoom(); }
  else if (ev.key === "k") { cur = Math.max(cur - 1, 0); focus(); if (zoomed) openZoom(); }
  else if (ev.key === "o") choose("orig");
  else if (ev.key === "e") choose("edit");
  else if (ev.key === "b") choose("both");
  else if (ev.key === "z") zoomed ? (zoom.className = "") : openZoom();
});
document.getElementById("view").onchange = () => { cur = 0; render(); };
document.getElementById("size").onchange = ev => document.documentElement.style.setProperty("--h", ev.target.value);
document.getElementById("clear").onclick = () => { if (confirm("Reset every pair to the plan?")) { picks = {}; save(); render(); } };
document.getElementById("export").onclick = () => {
  // choice: "swap" keeps the planned loser instead of the keeper; "both" keeps the two.
  const rows = ["loser,keeper,choice,filename", ...DATA.filter(changed).map(e =>
    `${e.loser},${e.keeper},${pick(e) === "both" ? "both" : "swap"},"${e.name.replace(/"/g, '""')}"`)];
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([rows.join("\n") + "\n"], { type: "text/csv" }));
  a.download = "pair_choices.csv";
  a.click();
};
render();
</script>
</body>
</html>
"""
