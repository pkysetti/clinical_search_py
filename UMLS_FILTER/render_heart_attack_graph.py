#!/usr/bin/env python3
"""
render_heart_attack_graph.py
============================

Render data/filter/heart_attack_graph.json as a self-contained interactive HTML page
(inline SVG + vanilla JS, no external assets):

  * center: myocardial infarction (C0027051)
  * inner ring: MI's UMLS ancestry spine + direct subtypes (hierarchy edges)
  * outer sectors: remaining concepts grouped by category
    (medication / diagnosis / finding / procedure / allergy / other)
  * edges: hierarchy (solid) + MRREL relationships (opacity by score)
  * hover a node -> tooltip (CUI, types, synonyms, ICD-10) + edge highlight
  * legend click -> isolate a category; wheel zoom; drag pan

Outputs:
  data/filter/heart_attack_graph.html   — standalone document (open in any browser)
  data/filter/heart_attack_graph_body.html — body-only fragment (for publishing)
"""

from __future__ import annotations

import html
import json
import math
from collections import Counter
from pathlib import Path


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)

DATA = Path(r"C:\github\clinical_search_py\data\filter")
SRC = DATA / "heart_attack_graph.json"
OUT_FULL = DATA / "heart_attack_graph.html"
OUT_BODY = DATA / "heart_attack_graph_body.html"

CORE = "C0027051"

CAT_COLORS = {
    "medication": "#4dabf7",
    "diagnosis": "#ff6b6b",
    "finding": "#69db7c",
    "procedure": "#ffd43b",
    "allergy": "#da77f2",
    "other": "#868e96",
}
CAT_ORDER = ["medication", "diagnosis", "finding", "procedure", "allergy", "other"]


def main() -> None:
    g = json.loads(SRC.read_text(encoding="utf-8"))
    concepts = {c["cui"]: c for c in g["concepts"]}
    hier = [e for e in g["edges"] if e["type"] == "hierarchy"]
    rel = [e for e in g["edges"] if e["type"] == "rel"]

    deg: Counter[str] = Counter()
    for e in g["edges"]:
        deg[e["from"]] += 1
        deg[e["to"]] += 1

    # ── inner ring: hierarchy neighbours of the core, ordered ───────────────
    adj: dict[str, list[str]] = {}
    for e in hier:
        adj.setdefault(e["from"], []).append(e["to"])
        adj.setdefault(e["to"], []).append(e["from"])
    inner_neigh = set(adj.get(CORE, []))

    # walk the ancestry chain: start at the leaf-most ancestor (degree 1 in
    # hier graph) and follow towards the core
    chain = []
    if inner_neigh:
        start = min(inner_neigh, key=lambda c: len(adj.get(c, [])))
        prev, cur = None, start
        while cur is not None and cur != CORE:
            chain.append(cur)
            nxts = [n for n in adj.get(cur, []) if n != prev]
            prev, cur = cur, (nxts[0] if nxts else None)
    subtypes = sorted(inner_neigh - set(chain),
                      key=lambda c: concepts[c]["canonical"].lower())

    # ── positions ────────────────────────────────────────────────────────────
    W, H = 1600, 1200
    cx, cy = W / 2, H / 2
    pos: dict[str, tuple[float, float]] = {CORE: (cx, cy)}

    inner_all = chain + subtypes
    n_in = len(inner_all)
    R_IN = 190
    for i, c in enumerate(inner_all):
        # ancestors counter-clockwise from top, subtypes clockwise from top
        if c in chain:
            j = chain.index(c)
            ang = -math.pi / 2 - (j + 1) * (2.4 / max(1, len(chain)))
        else:
            j = subtypes.index(c)
            ang = -math.pi / 2 + (j + 1) * (2.4 / max(1, len(subtypes)))
        pos[c] = (cx + R_IN * math.cos(ang), cy + R_IN * math.sin(ang))

    # outer sectors by category
    outer = [c for c in concepts if c not in pos and c != CORE]
    by_cat: dict[str, list[str]] = {k: [] for k in CAT_ORDER}
    for c in outer:
        cat = concepts[c]["category"]
        by_cat.setdefault(cat, []).append(c)
    for cat in by_cat:
        by_cat[cat].sort(key=lambda c: (-deg.get(c, 0), concepts[c]["canonical"].lower()))

    total_outer = len(outer) or 1
    a0 = -math.pi / 2
    R_OUT0, DR = 330, 62
    ROW = 14
    for cat in CAT_ORDER:
        items = by_cat.get(cat, [])
        if not items:
            continue
        span = max(0.55, 2 * math.pi * len(items) / total_outer)
        a_start = a0 + span / 2
        a_end = a0 - span / 2
        # walk sectors clockwise from top
        for i, c in enumerate(items):
            row = i // ROW
            in_row = i % ROW
            frac = (in_row + 0.5) / ROW
            ang = a_start + (a_end - a_start) * frac
            r = R_OUT0 + row * DR
            pos[c] = (cx + r * math.cos(ang), cy + r * math.sin(ang))
        a0 = a_end

    # ── SVG pieces ───────────────────────────────────────────────────────────
    def node_r(c: str) -> float:
        if c == CORE:
            return 26
        return 7 + min(deg.get(c, 0), 12) * 0.9

    edge_svg = []
    for e in hier:
        x1, y1 = pos[e["from"]]
        x2, y2 = pos[e["to"]]
        edge_svg.append(
            f'<line class="edge hier" data-a="{e["from"]}" data-b="{e["to"]}" '
            f'x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>'
        )
    for e in rel:
        if e["from"] not in pos or e["to"] not in pos:
            continue
        x1, y1 = pos[e["from"]]
        x2, y2 = pos[e["to"]]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        # bow the curve slightly towards center for readability
        dx, dy = cx - mx, cy - my
        d = math.hypot(dx, dy) or 1
        k = min(60, d * 0.18)
        qx, qy = mx + dx / d * k, my + dy / d * k
        sc = e.get("score", 1)
        cls = f"edge rel s{max(1, min(3, sc))}"
        label = esc((e.get("rela") or e.get("rel") or "")[:40])
        edge_svg.append(
            f'<path class="{cls}" data-a="{e["from"]}" data-b="{e["to"]}" '
            f'd="M{x1:.1f},{y1:.1f} Q{qx:.1f},{qy:.1f} {x2:.1f},{y2:.1f}" '
            f'data-label="{e["rel"]} {label}"/>'
        )

    node_svg = []
    for c, meta in concepts.items():
        if c not in pos:
            continue
        x, y = pos[c]
        r = node_r(c)
        color = "#ffffff" if c == CORE else CAT_COLORS.get(meta["category"], "#868e96")
        label = esc(meta["canonical"])
        show_label = c == CORE or deg.get(c, 0) >= 4 or len(label) <= 18
        node_svg.append(
            f'<g class="node" data-cui="{c}" data-cat="{meta["category"]}" '
            f'transform="translate({x:.1f},{y:.1f})">'
            f'<circle r="{r:.1f}" fill="{color}" stroke="#0b1220" stroke-width="1.5"/>'
            + (f'<text class="lbl" y="{r + 13:.0f}">{label}</text>' if show_label else "")
            + "</g>"
        )

    legend_items = []
    for cat in CAT_ORDER:
        n = sum(1 for c in concepts.values() if c["category"] == cat)
        if not n and cat != "other":
            continue
        legend_items.append(
            f'<span class="lg" data-cat="{cat}"><i style="background:{CAT_COLORS[cat]}"></i>'
            f'{cat} <b>{n}</b></span>'
        )

    body = f"""
<div id="app">
  <header>
    <h1>Heart Attack — UMLS 2026AA Concept Graph</h1>
    <div class="sub">
      {len(concepts)} concepts · {len(hier)} hierarchy + {len(rel)} relationship edges ·
      source: UMLS 2026AA Full Release (MRCONSO/MRSTY/MRHIER/MRREL) · core: C0027051 Myocardial Infarction
    </div>
    <div class="legend">{''.join(legend_items)}
      <span class="lg key"><i style="background:#fff"></i>hierarchy</span>
      <span class="lg key"><i style="background:#4dabf7"></i>MRREL relationship (hover edge for label)</span>
    </div>
  </header>
  <div id="stage">
    <svg id="svg" viewBox="0 0 {W} {H}" width="100%" height="100%">
      <g id="viewport">
        <g id="edges">{''.join(edge_svg)}</g>
        <g id="nodes">{''.join(node_svg)}</g>
      </g>
    </svg>
  </div>
  <div id="tip" hidden></div>
</div>
<style>
  #app {{ font-family: "Segoe UI", system-ui, sans-serif; color:#e9eef7; background:#0b1220; height:100vh; display:flex; flex-direction:column; }}
  header {{ padding:14px 20px 8px; }}
  h1 {{ font-size:19px; margin:0 0 4px; font-weight:600; }}
  .sub {{ font-size:12px; color:#8fa3c0; margin-bottom:8px; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:10px; font-size:12px; }}
  .lg {{ display:inline-flex; align-items:center; gap:6px; cursor:pointer; user-select:none; opacity:.95; }}
  .lg i {{ width:10px; height:10px; border-radius:50%; display:inline-block; }}
  .lg b {{ color:#8fa3c0; font-weight:600; }}
  .lg.key {{ cursor:default; }}
  #stage {{ flex:1; position:relative; overflow:hidden; }}
  svg {{ display:block; cursor:grab; }}
  svg:active {{ cursor:grabbing; }}
  .edge {{ fill:none; stroke:#9fb4d8; stroke-opacity:.16; transition:stroke-opacity .12s; }}
  .edge.hier {{ stroke:#ffffff; stroke-opacity:.5; stroke-width:1.6; }}
  .edge.s3 {{ stroke:#4dabf7; stroke-opacity:.55; stroke-width:2; }}
  .edge.s2 {{ stroke:#4dabf7; stroke-opacity:.3; stroke-width:1.3; }}
  .edge.s1 {{ stroke:#8fa3c0; stroke-opacity:.14; stroke-width:1; }}
  .edge.hot {{ stroke-opacity:.95 !important; stroke-width:2.6 !important; }}
  .node circle {{ cursor:pointer; transition:stroke-width .1s; }}
  .node:hover circle {{ stroke:#fff; stroke-width:3; }}
  .lbl {{ font-size:10.5px; fill:#c7d4ea; text-anchor:middle; pointer-events:none; }}
  #tip {{ position:absolute; z-index:9; max-width:340px; background:#101a2e; border:1px solid #2b3c5c;
          border-radius:8px; padding:10px 12px; font-size:12px; line-height:1.45; pointer-events:none;
          box-shadow:0 8px 24px rgba(0,0,0,.5); }}
  #tip .t {{ font-size:13.5px; font-weight:600; margin-bottom:2px; }}
  #tip .m {{ color:#8fa3c0; }}
  #tip .syn {{ margin-top:5px; color:#aebfda; }}
</style>
<script>
(function(){{
  const svg = document.getElementById('svg');
  const vp = document.getElementById('viewport');
  const tip = document.getElementById('tip');
  const stage = document.getElementById('stage');
  let tx=0, ty=0, k=1;
  function apply(){{ vp.setAttribute('transform', `translate(${{tx}},${{ty}}) scale(${{k}})`); }}
  svg.addEventListener('wheel', ev=>{{
    ev.preventDefault();
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) * (1600 / r.width);
    const py = (ev.clientY - r.top) * (1200 / r.height);
    const nk = Math.min(8, Math.max(.4, k * (ev.deltaY < 0 ? 1.15 : 1/1.15)));
    tx = px - (px - tx) * (nk / k);
    ty = py - (py - ty) * (nk / k);
    k = nk; apply();
  }}, {{passive:false}});
  let drag=null;
  svg.addEventListener('mousedown', ev=>{{ drag={{x:ev.clientX,y:ev.clientY,tx,ty}}; }});
  window.addEventListener('mousemove', ev=>{{
    if(!drag) return;
    const r = svg.getBoundingClientRect();
    tx = drag.tx + (ev.clientX - drag.x) * (1600 / r.width);
    ty = drag.ty + (ev.clientY - drag.y) * (1200 / r.height);
    apply();
  }});
  window.addEventListener('mouseup', ()=>{{ drag=null; }});

  const DATA = __DATA_JSON__;
  const byCui = {{}}; DATA.concepts.forEach(c=>byCui[c.cui]=c);
  const edges = [...svg.querySelectorAll('.edge')];
  function hot(cui, on){{
    edges.forEach(e=>{{
      if(e.dataset.a===cui || e.dataset.b===cui) e.classList.toggle('hot', on);
    }});
  }}
  document.querySelectorAll('.node').forEach(n=>{{
    n.addEventListener('mouseenter', ev=>{{
      const c = byCui[n.dataset.cui]; if(!c) return;
      hot(n.dataset.cui, true);
      const syn = (c.synonyms||[]).slice(0,6).join(', ');
      tip.innerHTML = `<div class="t">${{c.canonical}}</div>` +
        `<div class="m">${{c.cui}} · ${{c.category}} · deg ${{DATA.deg[n.dataset.cui]||0}}</div>` +
        `<div class="m">${{(c.semantic_types||[]).join(', ')||'—'}}</div>` +
        (c.icd10?`<div class="m">ICD-10: ${{c.icd10}}</div>`:'') +
        (syn?`<div class="syn">${{syn}}</div>`:'');
      tip.hidden = false;
    }});
    n.addEventListener('mousemove', ev=>{{
      const r = stage.getBoundingClientRect();
      let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 10;
      if(x > r.width - 360) x = ev.clientX - r.left - 350;
      tip.style.left = x+'px'; tip.style.top = y+'px';
    }});
    n.addEventListener('mouseleave', ()=>{{ hot(n.dataset.cui, false); tip.hidden = true; }});
  }});
  edges.forEach(e=>{{
    e.addEventListener('mouseenter', ev=>{{
      e.classList.add('hot');
      const a = byCui[e.dataset.a], b = byCui[e.dataset.b];
      tip.innerHTML = `<div class="t">${{a?a.canonical:'?'}} ↔ ${{b?b.canonical:'?'}}</div>` +
        `<div class="m">${{e.dataset.label||'relationship'}}</div>`;
      tip.hidden = false;
    }});
    e.addEventListener('mouseleave', ()=>{{ e.classList.remove('hot'); tip.hidden = true; }});
  }});
  let isolated = null;
  document.querySelectorAll('.lg[data-cat]').forEach(lg=>{{
    lg.addEventListener('click', ()=>{{
      const cat = lg.dataset.cat;
      isolated = (isolated === cat) ? null : cat;
      document.querySelectorAll('.node').forEach(n=>{{
        n.style.opacity = (!isolated || n.dataset.cat===isolated || n.dataset.cui==='C0027051') ? 1 : .12;
      }});
      edges.forEach(e=>{{
        const a = byCui[e.dataset.a], b = byCui[e.dataset.b];
        const ok = !isolated || (a&&a.category===isolated) || (b&&b.category===isolated)
                   || e.dataset.a==='C0027051' || e.dataset.b==='C0027051';
        e.style.opacity = ok ? '' : .05;
      }});
    }});
  }});
}})();
</script>
"""

    # degree map for tooltips (guard against </script> breaking out of the tag)
    data_js = json.dumps({
        "concepts": list(concepts.values()),
        "deg": dict(deg),
    }, ensure_ascii=False).replace("</", "<\\/")
    body = body.replace("__DATA_JSON__", data_js)

    full = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Heart Attack — UMLS 2026AA Concept Graph</title>"
        "<style>html,body{margin:0;height:100%}</style></head><body>"
        + body
        + "</body></html>"
    )
    OUT_FULL.write_text(full, encoding="utf-8")
    OUT_BODY.write_text(body, encoding="utf-8")
    print(f"wrote {OUT_FULL}")
    print(f"wrote {OUT_BODY}")


if __name__ == "__main__":
    main()
