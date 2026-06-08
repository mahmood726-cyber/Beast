"""Render the tracked evidence as a JSON feed and a static HTML dashboard.

Both are read straight from the store. The HTML is fully self-contained (no CDN,
no external assets) so it can be opened from disk or served by GitHub Pages, and
all data is embedded via ``json.dumps`` -- Python ``None`` becomes JSON ``null``,
never the bare token ``None`` that would break the page.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from beast.store import BeastStore


def build_report_data(store: BeastStore, generated_at: str) -> dict:
    """Assemble the full report payload from the store."""
    topics = []
    for topic in store.list_topics():
        history = store.history(topic.id)
        latest = history[-1].as_dict() if history else None
        topics.append({
            "topic": topic.to_dict(),
            "latest": latest,
            "history": [s.as_dict() for s in history],
            "changes": store.recent_changes(limit=200, topic_id=topic.id),
        })
    return {
        "generated_at": generated_at,
        "topics": topics,
        "recent_changes": store.recent_changes(limit=100),
    }


def write_json(store: BeastStore, path: str, generated_at: str) -> str:
    data = build_report_data(store, generated_at)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return path


def write_html(store: BeastStore, path: str, generated_at: str) -> str:
    data = build_report_data(store, generated_at)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # Embed as JSON (null, not None) and guard the closing tag just in case any
    # string field ever contained one.
    payload = json.dumps(data).replace("</", "<\\/")
    html = _HTML_TEMPLATE.replace("__BEAST_DATA__", payload)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:title" content="Beast - living meta-analysis surveillance">
<meta property="og:description" content="How pooled effects, heterogeneity and conclusions evolve as trials accumulate.">
<title>Beast - living evidence surveillance</title>
<style>
  :root { --bg:#0f1419; --panel:#1a2330; --ink:#e8edf2; --muted:#8b9bb0;
          --accent:#4ea1ff; --major:#ff5d5d; --notable:#ffb23e; --info:#5fd0a0; --line:#2a3645; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
         background:var(--bg); color:var(--ink); }
  header { padding:24px 28px; border-bottom:1px solid var(--line); }
  h1 { margin:0 0 4px; font-size:22px; } h1 span { color:var(--accent); }
  .sub { color:var(--muted); font-size:13px; }
  main { padding:20px 28px 60px; max-width:1100px; margin:0 auto; }
  .topic { background:var(--panel); border:1px solid var(--line); border-radius:10px;
           padding:18px 20px; margin:18px 0; }
  .topic h2 { margin:0 0 2px; font-size:18px; }
  .topic .note { color:var(--muted); font-size:13px; margin:2px 0 12px; }
  .kpis { display:flex; flex-wrap:wrap; gap:18px; margin:8px 0 14px; }
  .kpi { min-width:120px; } .kpi .v { font-size:20px; font-weight:600; }
  .kpi .l { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .sig-yes { color:var(--info); } .sig-no { color:var(--notable); }
  svg { width:100%; height:240px; background:#121a24; border-radius:8px; }
  .axis { stroke:var(--line); stroke-width:1; } .gl { stroke:#1e2937; stroke-width:1; }
  .band { fill:rgba(78,161,255,.16); } .eline { stroke:var(--accent); stroke-width:2; fill:none; }
  .nullline { stroke:#6b7a8d; stroke-dasharray:4 4; stroke-width:1; }
  .dot { fill:var(--accent); } .dot-sig { fill:var(--info); }
  .tick { fill:var(--muted); font-size:11px; } .lbl { fill:var(--muted); font-size:11px; }
  .changes { margin-top:12px; }
  .chg { display:flex; gap:10px; padding:7px 0; border-top:1px solid var(--line); font-size:13px; }
  .badge { flex:0 0 auto; font-size:11px; padding:2px 7px; border-radius:4px; height:fit-content;
           text-transform:uppercase; letter-spacing:.03em; font-weight:600; }
  .b-major { background:rgba(255,93,93,.18); color:var(--major); }
  .b-notable { background:rgba(255,178,62,.18); color:var(--notable); }
  .b-info { background:rgba(95,208,160,.18); color:var(--info); }
  .empty { color:var(--muted); font-style:italic; }
  footer { color:var(--muted); font-size:12px; padding:18px 28px; border-top:1px solid var(--line); }
  code { background:#0b1118; padding:1px 5px; border-radius:4px; }
</style>
</head>
<body>
<header>
  <h1><span>Beast</span> &mdash; living meta-analysis surveillance</h1>
  <div class="sub" id="sub"></div>
</header>
<main id="app"></main>
<footer>
  Self-running surveillance of how pooled effects, heterogeneity and conclusions evolve as
  trials accumulate. Random-effects estimates validated against R&nbsp;metafor.
</footer>
<script>
const DATA = __BEAST_DATA__;
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs, txt){ const e=document.createElementNS(NS,tag);
  for(const k in (attrs||{})) e.setAttribute(k, attrs[k]); if(txt!=null) e.textContent=txt; return e; }
function h(tag, cls, txt){ const e=document.createElement(tag); if(cls) e.className=cls;
  if(txt!=null) e.textContent=txt; return e; }
function fmt(x, n){ return (x==null||!isFinite(x)) ? "n/a" : Number(x).toFixed(n==null?3:n); }

function trendChart(history, measure, logScale){
  const W=900, H=240, padL=54, padR=16, padT=18, padB=28;
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, preserveAspectRatio:"none"});
  const pts = history.map(s => ({
    x: s.as_of_year!=null ? s.as_of_year : s.timestamp,
    est: s.natural.estimate, lo: s.natural.ci_low, hi: s.natural.ci_high,
    sig: s.significant, k: s.k
  }));
  if(!pts.length){ return svg; }
  const xs = pts.map((p,i)=> p.x!=null && typeof p.x==="number" ? p.x : i);
  const xmin=Math.min(...xs), xmax=Math.max(...xs);
  const ys=[]; pts.forEach(p=>{ ys.push(p.lo,p.hi,p.est); });
  const nullv = logScale ? 1 : 0; ys.push(nullv);
  let ymin=Math.min(...ys), ymax=Math.max(...ys);
  if(ymin===ymax){ ymin-=1; ymax+=1; } const yr=(ymax-ymin)*0.08; ymin-=yr; ymax+=yr;
  const sx = x => padL + (xmax===xmin?0.5:(x-xmin)/(xmax-xmin))*(W-padL-padR);
  const sy = y => padT + (1-(y-ymin)/(ymax-ymin))*(H-padT-padB);

  // gridlines + y ticks
  for(let t=0;t<=4;t++){ const yv=ymin+(ymax-ymin)*t/4;
    svg.appendChild(el("line",{class:"gl",x1:padL,y1:sy(yv),x2:W-padR,y2:sy(yv)}));
    svg.appendChild(el("text",{class:"tick",x:padL-6,y:sy(yv)+3,"text-anchor":"end"}, fmt(yv,2))); }
  // null line
  svg.appendChild(el("line",{class:"nullline",x1:padL,y1:sy(nullv),x2:W-padR,y2:sy(nullv)}));
  svg.appendChild(el("text",{class:"lbl",x:W-padR,y:sy(nullv)-4,"text-anchor":"end"}, "null="+nullv));
  // CI band
  let top="", bot="";
  pts.forEach((p,i)=>{ const X=sx(xs[i]); top+=`${i?"L":"M"}${X},${sy(p.hi)} `; });
  for(let i=pts.length-1;i>=0;i--){ const X=sx(xs[i]); bot+=`L${X},${sy(pts[i].lo)} `; }
  svg.appendChild(el("path",{class:"band", d: top+bot+"Z"}));
  // estimate line
  let line=""; pts.forEach((p,i)=>{ line+=`${i?"L":"M"}${sx(xs[i])},${sy(p.est)} `; });
  svg.appendChild(el("path",{class:"eline", d:line}));
  // dots + x labels
  pts.forEach((p,i)=>{ const X=sx(xs[i]);
    svg.appendChild(el("circle",{class:p.sig?"dot-sig":"dot", cx:X, cy:sy(p.est), r:4}));
    svg.appendChild(el("text",{class:"tick",x:X,y:H-8,"text-anchor":"middle"}, String(p.x).slice(0,7)));
  });
  return svg;
}

function badge(sev){ const b=h("span","badge "+(sev==="major"?"b-major":sev==="notable"?"b-notable":"b-info"), sev); return b; }

function render(){
  document.getElementById("sub").textContent =
    `Generated ${DATA.generated_at} · ${DATA.topics.length} topic(s) tracked`;
  const app = document.getElementById("app");
  if(!DATA.topics.length){ app.appendChild(h("p","empty","No topics tracked yet. Run `beast init` then `beast backfill` or `beast run`.")); return; }
  DATA.topics.forEach(t=>{
    const card = h("div","topic");
    card.appendChild(h("h2", null, t.topic.title));
    if(t.topic.notes) card.appendChild(h("div","note", t.topic.notes));
    const L = t.latest;
    const kpis = h("div","kpis");
    function kpi(label, val, cls){ const d=h("div","kpi"); const v=h("div","v "+(cls||""), val); d.appendChild(v); d.appendChild(h("div","l", label)); return d; }
    if(L){
      kpis.appendChild(kpi("Pooled "+L.measure, fmt(L.natural.estimate)));
      kpis.appendChild(kpi("95% CI", fmt(L.natural.ci_low)+" to "+fmt(L.natural.ci_high)));
      kpis.appendChild(kpi("Trials (k)", String(L.k)));
      kpis.appendChild(kpi("I²", fmt(L.i2,1)+"%"));
      kpis.appendChild(kpi("Significant", L.significant?"yes":"no", L.significant?"sig-yes":"sig-no"));
    }
    card.appendChild(kpis);
    card.appendChild(trendChart(t.history, L?L.measure:"", L?L.log_scale:false));
    const ch = h("div","changes");
    if(!t.changes.length){ ch.appendChild(h("div","empty","No changes flagged yet.")); }
    else { t.changes.slice(0,12).forEach(c=>{ const row=h("div","chg");
      row.appendChild(badge(c.severity)); row.appendChild(h("span",null, `${c.timestamp.slice(0,10)} · ${c.message}`)); ch.appendChild(row); }); }
    card.appendChild(ch);
    app.appendChild(card);
  });
}
render();
</script>
</body>
</html>
"""
