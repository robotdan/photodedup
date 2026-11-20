"""A local web page for spot-checking edit pairs under edits-review/.

With a `measure-edits` CSV, the page shows only pairs whose edit visibly
differs (a crop, a rotation, or a measurable color change), largest first.

Writes edits-review/index.html next to the side-by-side copies `archive-pairs`
made. The page loads the images by relative path, so it works straight from
the drive with no server. Marks are kept in the browser's localStorage and
exported as a CSV of loser uuids whose original should stay in the library.
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
.pair .act button { font:inherit; padding:5px 10px; border-radius:6px; border:1px solid var(--line); background:var(--bg); color:var(--fg); cursor:pointer; }
.pair.flag .act button { border-color:var(--flag); color:var(--flag); }
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
  <select id="view"><option value="all">All pairs</option><option value="flag">Marked only</option><option value="sample">Random 50</option></select>
  <select id="size"><option value="240px">Small</option><option value="340px" selected>Medium</option><option value="520px">Large</option></select>
  <button id="export">Download marked CSV</button>
  <button id="clear">Clear marks</button>
  <div class="keys">j / k next and previous. x marks a pair "keep both", so neither copy is removed. z zooms the pair full screen; in zoom, space flips between original and edit. Marks are saved in this browser.</div>
</header>
<main id="list"></main>
<div id="zoom"></div>
<script>
const DATA = /*DATA*/[];
const KEY = "photodedup-edit-review";
let marks = new Set();
try { marks = new Set(JSON.parse(localStorage.getItem(KEY) || "[]")); } catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify([...marks])); } catch (e) {} };
const list = document.getElementById("list"), stat = document.getElementById("stat"), zoom = document.getElementById("zoom");
let shown = [], cur = 0, sample = null;

function card(e, i) {
  const el = document.createElement("section");
  el.className = "pair" + (marks.has(e.loser) ? " flag" : "");
  el.dataset.i = i;
  el.innerHTML = `<div class="top"><span class="n"></span><span class="d"></span></div>
    <div class="imgs">
      <figure><img loading="lazy" data-k="orig"><figcaption><b>Original</b> (${e.kept === "orig" ? "kept" : "would be removed"}) <span class="oi"></span></figcaption></figure>
      <figure><img loading="lazy" data-k="edit"><figcaption><b>Google edit</b> (${e.kept === "edit" ? "kept" : "would be removed"}) <span class="ei"></span></figcaption></figure>
    </div>
    <div class="df"></div>
    <div class="act"><button></button>${e.present ? "" : ' <span class="missing">copies missing on the drive</span>'}</div>`;
  el.querySelector(".n").textContent = e.name;
  el.querySelector(".d").textContent = e.date.replace("T", " ");
  el.querySelector(".oi").textContent = e.origInfo;
  el.querySelector(".ei").textContent = e.editInfo;
  el.querySelector(".df").textContent = e.diff;
  const [a, b] = el.querySelectorAll("img");
  a.src = encodeURI(e.orig); b.src = encodeURI(e.edit);
  a.onclick = b.onclick = () => { cur = i; openZoom(); };
  el.querySelector("button").onclick = () => { cur = i; toggle(); };
  label(el, e);
  return el;
}
function label(el, e) {
  el.querySelector(".act button").textContent = marks.has(e.loser) ? "Marked: keep both (x to undo)" : "Keep both (x)";
}
function render() {
  const v = document.getElementById("view").value;
  if (v === "sample") {
    if (!sample) { sample = DATA.map((_, i) => i).sort(() => Math.random() - 0.5).slice(0, 50).sort((a, b) => a - b); }
    shown = sample.map(i => DATA[i]);
  } else {
    sample = null;
    shown = v === "flag" ? DATA.filter(e => marks.has(e.loser)) : DATA;
  }
  list.replaceChildren(...shown.map(card));
  cur = Math.min(cur, Math.max(0, shown.length - 1));
  focus(false);
  count();
}
function count() {
  const missing = DATA.filter(e => !e.present).length;
  stat.textContent = `${shown.length} shown of ${DATA.length} pairs · ${marks.size} marked` + (missing ? ` · ${missing} missing` : "");
}
function focus(scroll = true) {
  list.querySelectorAll(".pair.cur").forEach(x => x.classList.remove("cur"));
  const el = list.children[cur];
  if (!el) return;
  el.classList.add("cur");
  if (scroll) el.scrollIntoView({ block: "center", behavior: "smooth" });
}
function toggle() {
  const e = shown[cur]; if (!e) return;
  marks.has(e.loser) ? marks.delete(e.loser) : marks.add(e.loser);
  save();
  const el = list.children[cur];
  el.classList.toggle("flag", marks.has(e.loser));
  label(el, e);
  count();
}
let flip = null;
function openZoom() {
  const e = shown[cur]; if (!e) return;
  flip = null;
  zoom.className = "on";
  zoom.innerHTML = `<div style="position:relative"><img src="${encodeURI(e.orig)}"><span class="lbl" style="left:8px">Original</span></div>
                    <div style="position:relative"><img src="${encodeURI(e.edit)}"><span class="lbl" style="left:8px">Google edit</span></div>`;
}
function flipZoom() {
  const e = shown[cur]; if (!e) return;
  flip = flip === "orig" ? "edit" : "orig";
  zoom.className = "on one";
  zoom.innerHTML = `<div style="position:relative"><img src="${encodeURI(e[flip])}"><span class="lbl" style="left:8px">${flip === "orig" ? "Original" : "Google edit"}</span></div>`;
}
zoom.onclick = () => { zoom.className = ""; };
document.addEventListener("keydown", ev => {
  if (ev.metaKey || ev.ctrlKey || ev.altKey || ev.target.tagName === "SELECT") return;
  const zoomed = zoom.classList.contains("on");
  if (ev.key === "Escape") { zoom.className = ""; return; }
  if (zoomed && ev.key === " ") { ev.preventDefault(); flipZoom(); return; }
  if (ev.key === "j") { cur = Math.min(cur + 1, shown.length - 1); focus(); if (zoomed) openZoom(); }
  else if (ev.key === "k") { cur = Math.max(cur - 1, 0); focus(); if (zoomed) openZoom(); }
  else if (ev.key === "x") toggle();
  else if (ev.key === "z") zoomed ? (zoom.className = "") : openZoom();
});
document.getElementById("view").onchange = () => { cur = 0; render(); };
document.getElementById("size").onchange = ev => document.documentElement.style.setProperty("--h", ev.target.value);
document.getElementById("clear").onclick = () => { if (confirm("Clear all marks?")) { marks.clear(); save(); render(); } };
document.getElementById("export").onclick = () => {
  const rows = ["loser,keeper,filename", ...DATA.filter(e => marks.has(e.loser)).map(e => `${e.loser},${e.keeper},"${e.name.replace(/"/g, '""')}"`)];
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([rows.join("\n") + "\n"], { type: "text/csv" }));
  a.download = "keep_originals.csv";
  a.click();
};
render();
</script>
</body>
</html>
"""
