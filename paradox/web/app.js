// PARADOX investigation UI. Vanilla JS, no build step, no network beyond the local API.
"use strict";

const S = {
  meta: null, geo: null, tl: {}, branch: "main", alt: null, split: false,
  t: 1095, playing: false, hover: null, proj: null, lastWhy: null,
};
const $ = (id) => document.getElementById(id);
const fmt = (x, d = 1) => (x === null || x === undefined || Number.isNaN(x)) ? "–" : Number(x).toFixed(d);
const clock = (t) => { t = Math.round(t); return `${String(Math.floor(t / 60) % 24).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const inIv = (ivs, t) => ivs && ivs.some(([a, b]) => a <= t && t < b);

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  if (!r.ok) { const e = await r.json().catch(() => ({ detail: r.statusText })); throw new Error(e.detail || r.statusText); }
  return r.json();
}
const pql = (q) => api("/api/query", { q });

// ---------------------------------------------------------------- boot
async function boot() {
  const [meta, geo] = await Promise.all([api("/api/meta"), api("/api/geometry")]);
  S.meta = meta; S.geo = geo;
  const sc = meta.scenario;
  $("scenario").innerHTML = `<b>${esc(sc.name)}</b> · ${esc(sc.description)} · world seed ${meta.world_seed} · ` +
    `${meta.city.buildings.toLocaleString()} buildings · ${meta.city.road_segments} road segments · ${meta.city.electrical_components} electrical components`;
  S.tl.main = await api("/api/timeline?branch=main");
  const metrics = Object.keys(meta.kpis);
  for (const id of ["attrMetric", "planMetric"]) $(id).innerHTML = metrics.map((m) => `<option>${m}</option>`).join("");
  $("planMetric").value = "ems.p90_response";
  $("attrMetric").value = "ems.p90_response";
  renderBranches(meta.branches);
  bindUI();
  resize();
  loadLate();
  renderKpis();
  drawTicks();
  requestAnimationFrame(loop);
}

// ---------------------------------------------------------------- branches / split
function renderBranches(list) {
  $("branches").innerHTML = list.map((b) => {
    const on = (b.name === "main" && !S.alt) || b.name === S.alt;
    const label = b.name === "main" ? "ACTUAL · main" : b.interventions.join(" + ");
    return `<span class="chip ${b.name === "main" ? "main" : ""} ${on ? "on" : ""}" data-b="${esc(b.name)}" title="head ${b.head}">` +
      `${esc(label)}<small>${b.head.slice(0, 8)}</small></span>`;
  }).join("");
  document.querySelectorAll("#branches .chip").forEach((el) => el.onclick = () => selectAlt(el.dataset.b === "main" ? null : el.dataset.b));
}
async function selectAlt(name) {
  S.alt = name;
  if (name && !S.tl[name]) S.tl[name] = await api(`/api/timeline?branch=${encodeURIComponent(name)}`);
  S.split = !!name;
  $("splitToggle").checked = S.split;
  $("wrapB").classList.toggle("hidden", !S.split);
  $("labelB").textContent = name ? `ALTERNATE · ${name}` : "ALTERNATE";
  S.meta.branches = await api("/api/branches");
  renderBranches(S.meta.branches);
  resize(); renderKpis(); drawTicks();
}

// ---------------------------------------------------------------- map
function resize() {
  for (const id of ["mapA", "mapB", "ticks"]) {
    const c = $(id); const r = c.getBoundingClientRect();
    c.width = Math.max(10, r.width * devicePixelRatio); c.height = Math.max(10, r.height * devicePixelRatio);
  }
  const c = $("mapA");
  const [[x0, y0], [x1, y1]] = S.geo.bounds;
  const pad = 26 * devicePixelRatio;
  const sc = Math.min((c.width - 2 * pad) / (x1 - x0), (c.height - 2 * pad) / (y1 - y0));
  const ox = (c.width - sc * (x1 - x0)) / 2, oy = (c.height - sc * (y1 - y0)) / 2;
  S.proj = { sc, f: (x, y) => [ox + (x - x0) * sc, oy + (y1 - y) * sc] };
  drawTicks();
}
const P = (xy) => S.proj.f(xy[0], xy[1]);
const NP = (n) => P(S.geo.nodes[n]);

function unitPos(inc, t) {
  const lerp = (a, b, f) => [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
  const st = NP(inc.station), sc = NP(inc.node), h = inc.hospital !== null ? NP(inc.hospital) : null;
  const seg = (t0, t1, a, b) => lerp(a, b, Math.min(1, Math.max(0, (t - t0) / Math.max(0.01, t1 - t0))));
  if (t < inc.t_depart) return st;
  if (t < inc.t_arrive) return seg(inc.t_depart, inc.t_arrive, st, sc);
  if (h && inc.t_leave !== null) {
    if (t < inc.t_leave) return sc;
    if (t < inc.t_hosp) return seg(inc.t_leave, inc.t_hosp, sc, h);
    if (t < inc.t_clear) return h;
    return seg(inc.t_clear, inc.t_free, h, st);
  }
  if (t < inc.t_clear) return sc;
  return seg(inc.t_clear, inc.t_free, sc, st);
}

function draw(canvasId, tl, t, accent) {
  const c = $(canvasId), g = c.getContext("2d"), geo = S.geo, dpr = devicePixelRatio;
  g.clearRect(0, 0, c.width, c.height);
  g.fillStyle = "#05070b"; g.fillRect(0, 0, c.width, c.height);
  const bs = geo.spacing * S.proj.sc * 0.86;
  const fdown = new Set(Object.keys(tl.feeders).filter((f) => inIv(tl.feeders[f], t)));
  const feederIds = geo.feeders.map((f) => f.id);
  // river
  const [ra, rb] = geo.bridges[0];
  const ry = (geo.nodes[ra][1] + geo.nodes[rb][1]) / 2;
  const [, pyr] = S.proj.f(0, ry);
  g.fillStyle = "#0b1a2c"; g.fillRect(0, pyr - bs * 0.32, c.width, bs * 0.64);
  // blocks: lit windows vs blackout
  for (const [x, y, fi, pop] of geo.blocks) {
    const [px, py] = S.proj.f(x, y);
    const dark = fdown.has(feederIds[fi]);
    const k = Math.min(1, pop / 2200);
    g.fillStyle = dark ? "#0c1119" : `rgba(${150 + 70 * k},${100 + 40 * k},${40 + 10 * k},${0.25 + 0.5 * k})`;
    g.fillRect(px - bs / 2, py - bs / 2, bs, bs);
    if (dark) { g.strokeStyle = "rgba(90,110,150,.18)"; g.lineWidth = 1; g.strokeRect(px - bs / 2, py - bs / 2, bs, bs); }
  }
  // roads
  for (const [a, b, art] of geo.segments) {
    const [x1, y1] = NP(a), [x2, y2] = NP(b);
    g.strokeStyle = art ? "rgba(140,155,180,.55)" : "rgba(110,120,140,.25)";
    g.lineWidth = (art ? 1.6 : 0.7) * dpr;
    g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke();
  }
  // signals
  for (const s of geo.signals) {
    const [x, y] = NP(s.node);
    const dark = inIv(tl.signals[s.id], t);
    g.fillStyle = dark ? "#ff5d5d" : s.ups ? "#ffb547" : "rgba(79,214,156,.75)";
    const r = (dark ? 2.6 : 1.5) * dpr;
    g.beginPath(); g.arc(x, y, r, 0, 7); g.fill();
    if (dark) { g.strokeStyle = "rgba(255,93,93,.35)"; g.lineWidth = dpr; g.beginPath(); g.arc(x, y, r + 3 * dpr, 0, 7); g.stroke(); }
  }
  // substations
  for (const s of geo.substations) {
    const [x, y] = P(s.xy), down = inIv(tl.substations[s.id], t), r = 6 * dpr;
    g.fillStyle = down ? "#ff5d5d" : "#6aa9ff"; g.fillRect(x - r, y - r, 2 * r, 2 * r);
    g.fillStyle = "#05070b"; g.font = `${9 * dpr}px ui-monospace`; g.textAlign = "center"; g.textBaseline = "middle";
    g.fillText(s.id.replace("S", ""), x, y + 0.5);
    if (down) { g.strokeStyle = "#ff5d5d"; g.lineWidth = 1.5 * dpr; g.beginPath(); g.arc(x, y, r * 2.2 + Math.sin(t) * 2, 0, 7); g.stroke(); }
  }
  // stations
  for (const s of geo.stations) {
    const [x, y] = NP(s.node), r = 4.5 * dpr;
    g.fillStyle = "#6aa9ff"; g.beginPath(); g.moveTo(x, y - r); g.lineTo(x + r, y + r * 0.8); g.lineTo(x - r, y + r * 0.8); g.closePath(); g.fill();
  }
  // hospitals
  for (const h of geo.hospitals) {
    const [x, y] = NP(h.node), st = tl.hospitals[h.id];
    const unp = inIv(st.unpowered, t), lost = inIv(st.lost, t), div = inIv(st.diversion, t);
    const col = unp ? "#ff5d5d" : lost ? "#ffb547" : "#4fd69c", r = 6 * dpr;
    g.fillStyle = col; g.fillRect(x - r, y - r / 3, 2 * r, (2 * r) / 3); g.fillRect(x - r / 3, y - r, (2 * r) / 3, 2 * r);
    if (div) { g.strokeStyle = "#ff5d5d"; g.setLineDash([3 * dpr, 2 * dpr]); g.lineWidth = 1.5 * dpr; g.beginPath(); g.arc(x, y, r * 1.9, 0, 7); g.stroke(); g.setLineDash([]); }
    g.fillStyle = "rgba(219,226,238,.8)"; g.font = `${9 * dpr}px ui-monospace`; g.textAlign = "left"; g.fillText(h.id, x + r + 3, y - r);
  }
  // incidents and ambulances
  for (const inc of tl.incidents) {
    if (t >= inc.t_call && t < inc.t_arrive) {
      const [x, y] = NP(inc.node), ph = (t * 3) % 1;
      g.strokeStyle = inc.late ? "rgba(255,93,93,.9)" : "rgba(255,181,71,.9)"; g.lineWidth = 2 * dpr;
      g.beginPath(); g.arc(x, y, (4 + 6 * ph) * dpr, 0, 7); g.stroke();
    }
    if (t >= inc.t_depart && t < inc.t_free) {
      const [x, y] = unitPos(inc, t);
      g.fillStyle = "#5fd4e8"; g.beginPath(); g.arc(x, y, 3 * dpr, 0, 7); g.fill();
      g.strokeStyle = "rgba(95,212,232,.35)"; g.lineWidth = dpr; g.beginPath(); g.arc(x, y, 6 * dpr, 0, 7); g.stroke();
    }
  }
  // corner readout
  const k = tl.kpis;
  g.fillStyle = "rgba(219,226,238,.85)"; g.font = `${11 * dpr}px ui-monospace`; g.textAlign = "right"; g.textBaseline = "bottom";
  g.fillText(`${clock(t)} · dark signals ${geo.signals.filter((s) => inIv(tl.signals[s.id], t)).length} · feeders down ${fdown.size}`, c.width - 12 * dpr, c.height - 26 * dpr);
  g.fillStyle = accent;
  g.fillText(`p90 response ${fmt(k["ems.p90_response"])} min · P1 late ${fmt(k["ems.p1_late"], 0)}`, c.width - 12 * dpr, c.height - 10 * dpr);
}

function loop(ts) {
  if (S.playing) {
    S.t += 0.6; if (S.t > 1439) S.t = 960;
    $("time").value = S.t;
  }
  $("clock").textContent = clock(S.t);
  draw("mapA", S.tl.main, S.t, "#ffb547");
  if (S.split && S.alt && S.tl[S.alt]) draw("mapB", S.tl[S.alt], S.t, "#b48cff");
  drawCursor();
  requestAnimationFrame(loop);
}

// ---------------------------------------------------------------- scrubber ticks
const TICK_COL = { fault: "#ff5d5d", feeder: "#ffb547", signal: "#ff8a5d", gen: "#b48cff", hospital: "#ff5d5d",
  tie: "#4fd69c", tower: "#5fd4e8", zone: "#6aa9ff", dispatch: "#ff5d5d", substation: "#4fd69c", power: "#ffb547" };
function drawTicks() {
  const c = $("ticks"); if (!c || !S.tl.main) return;
  const g = c.getContext("2d"), W = c.width, H = c.height, dpr = devicePixelRatio;
  g.clearRect(0, 0, W, H);
  const x = (t) => ((t - 960) / 480) * W;
  for (let h = 16; h <= 24; h++) {
    g.fillStyle = "rgba(125,138,160,.5)"; g.fillRect(x(h * 60), 0, 1, 6 * dpr);
    g.font = `${9 * dpr}px ui-monospace`; g.fillText(`${h}:00`, x(h * 60) + 2, 14 * dpr);
  }
  const lanes = (tl, y, alpha) => {
    for (const e of tl.ticks) {
      const col = TICK_COL[e.kind.split(".")[0]] || "#7d8aa0";
      g.fillStyle = col; g.globalAlpha = alpha; g.fillRect(x(e.t), y, Math.max(1, dpr), 5 * dpr);
    }
    for (const inc of tl.incidents) {
      if (inc.t_call < 960) continue;
      g.fillStyle = inc.late ? "#ff5d5d" : "#3b4a60"; g.globalAlpha = alpha;
      g.fillRect(x(inc.t_call), y + 6 * dpr, Math.max(1, dpr), 3 * dpr);
    }
    g.globalAlpha = 1;
  };
  lanes(S.tl.main, 16 * dpr, 0.9);
  if (S.alt && S.tl[S.alt]) {
    lanes(S.tl[S.alt], 26 * dpr, 0.9);
    const b = S.meta.branches.find((x) => x.name === S.alt) || {};
    if (b.fork_t) { g.fillStyle = "#b48cff"; g.fillRect(x(b.fork_t), 0, 2 * dpr, H); }
  }
}
function drawCursor() { /* the native range thumb is the cursor */ }

// ---------------------------------------------------------------- KPI strip
function renderKpis() {
  const pick = [["ems.p90_response", "p90 EMS response", "min"], ["ems.p1_late", "P1 calls over 8 min", ""],
    ["hospital.unpowered_min", "hospital minutes unpowered", "min"], ["power.customer_hours", "building-hours dark", ""]];
  const a = S.tl.main.kpis, b = S.alt && S.tl[S.alt] ? S.tl[S.alt].kpis : null;
  $("kpis").innerHTML = pick.map(([k, label, u]) => {
    let alt = "", d = "";
    if (b) {
      const delta = b[k] - a[k];
      alt = `<span class="alt">→ ${fmt(b[k], k.includes("hours") ? 0 : 1)}</span>`;
      d = `<div class="d ${delta < 0 ? "good" : delta > 0 ? "bad" : ""}">${delta > 0 ? "+" : ""}${fmt(delta, 1)} ${u} in the alternate</div>`;
    }
    return `<div class="kpi"><div class="k">${label}</div><div class="v">${fmt(a[k], k.includes("hours") ? 0 : 1)}${alt}</div>${d}</div>`;
  }).join("");
}

// ---------------------------------------------------------------- hover / click
function hitTest(mx, my) {
  const dpr = devicePixelRatio, x = mx * dpr, y = my * dpr, near = (p, r) => Math.hypot(p[0] - x, p[1] - y) < r * dpr;
  for (const h of S.geo.hospitals) if (near(NP(h.node), 9)) return { ref: `hospital:${h.id}`, label: `hospital ${h.id} · feeds ${h.feeds.join(", ")}` };
  for (const s of S.geo.substations) if (near(P(s.xy), 9)) return { ref: `substation:${s.id}`, label: `substation ${s.id}` };
  const tl = S.tl.main;
  for (const inc of tl.incidents) if (S.t >= inc.t_call && S.t < inc.t_free && near(NP(inc.node), 8))
    return { ref: `incident:${inc.iid}`, label: `${inc.iid} · P${inc.priority} · ${inc.origin} · response ${fmt(inc.response)} min` };
  for (const s of S.geo.signals) if (near(NP(s.node), 5)) return { ref: `signal:${s.id}`, label: `signal ${s.id}${s.ups ? " · UPS" : ""}` };
  for (const s of S.geo.stations) if (near(NP(s.node), 7)) return { ref: `station:${s.id}`, label: `EMS station ${s.id} · ${s.units} unit(s)` };
  return null;
}

// ---------------------------------------------------------------- Investigate
async function loadLate() {
  const r = await pql("SHOW INCIDENTS LATE LIMIT 12");
  const rows = r.result.incidents.map((i) => `<tr class="click" data-i="${i.iid}"><td>${i.iid}</td><td>P${i.priority}</td><td>${clock(i.t_call)}</td>` +
    `<td>${i.origin}</td><td class="bad">${fmt(i.response)}</td><td>${i.held_by || ""}</td><td>${i.route_dark || ""}</td></tr>`).join("");
  $("lateList").innerHTML = `<h3>Late calls in the record (${r.result.count})</h3><table><tr><th>incident</th><th>pri</th><th>call</th><th>origin</th><th>min</th><th>held by</th><th>dark</th></tr>${rows}</table>`;
  $("lateList").querySelectorAll("tr.click").forEach((tr) => tr.onclick = () => why(`incident:${tr.dataset.i}`));
}

async function why(ref) {
  showTab("investigate");
  $("whyOut").innerHTML = `<div class="spinner">running counterfactual tests for ${esc(ref)}…</div>`;
  try {
    const r = await pql(`WHY ${ref}`);
    const x = r.result;
    if (x.kind === "why_incident") renderWhyIncident(x, r);
    else $("whyOut").innerHTML = `<h3>WHY ${esc(ref)}</h3><div class="card narr">${esc(x.narrative)}</div>` + chainSvg(x.provenance) + receipt(r);
    if (x.kind === "why_incident") S.t = Math.max(960, Math.min(1439, x.provenance.nodes.slice(-1)[0]?.t || S.t));
    $("time").value = S.t;
  } catch (e) { $("whyOut").innerHTML = `<div class="card bad">${esc(e.message)}</div>`; }
}

function renderWhyIncident(x, r) {
  const comps = Object.entries(x.decomposition);
  const maxv = Math.max(1, ...comps.map(([, v]) => Math.max(v.actual, v.nominal)));
  const bars = comps.map(([k, v]) => `<tr><td>${k}</td><td><span class="bar" style="width:${(v.actual / maxv) * 140}px"></span> ${fmt(v.actual)}</td>` +
    `<td><span class="bar g" style="width:${(v.nominal / maxv) * 140}px"></span> ${fmt(v.nominal)}</td><td class="${v.delta > 0.05 ? "bad" : ""}">${v.delta > 0 ? "+" : ""}${fmt(v.delta)}</td></tr>`).join("");
  const cands = x.candidates.map((c) => `<tr><td>${esc(c.label)}<br><span class="tag">${esc(c.neutraliser.join(" & "))}</span></td>` +
    `<td>${c.incident_exists ? fmt(c.response_without) : "no call"}</td><td>${c.delta === null ? "–" : fmt(c.delta)}</td>` +
    `<td>${c.flips ? "<span class='tag ok'>flips</span>" : ""}</td><td><span class="bar v" style="width:${c.responsibility * 60}px"></span> ${fmt(c.responsibility, 2)}</td></tr>`).join("");
  const pn = x.probability_of_necessity.map((p) => `<tr><td>${esc(p.label)}</td><td>${fmt(p.pn_shared, 2)}</td><td>${fmt(p.pn_redrawn, 2)}</td>` +
    `<td>${p.coupling_sensitive ? "<span class='tag warn'>coupling-sensitive</span>" : "<span class='tag ok'>robust</span>"}</td></tr>`).join("");
  $("whyOut").innerHTML = `
    <h3>WHY incident ${esc(x.target)}</h3>
    <div class="cmp"><div><div class="lab">ACTUAL</div><div class="big bad">${fmt(x.response)}<small> min</small></div><div class="sub">target ${x.target_min} · unit ${x.unit} · ${x.hospital || "no transport"}</div></div>
    <div class="alt"><div class="lab">NOMINAL WORLD</div><div class="big good">${x.exists_in_nominal ? fmt(x.nominal_response) : "∅"}<small>${x.exists_in_nominal ? " min" : ""}</small></div><div class="sub">${x.exists_in_nominal ? "same patient, no root faults" : "this call never happens"}</div></div></div>
    <div class="card narr">${esc(x.narrative)}</div>
    <h3>Where the minutes went</h3><table><tr><th>component</th><th>actual</th><th>nominal</th><th>Δ</th></tr>${bars}</table>
    <h3>Causes · but-for · responsibility</h3><table><tr><th>candidate</th><th>without</th><th>Δ min</th><th></th><th>resp.</th></tr>${cands}</table>
    <div class="hint">minimal flipping sets: ${x.minimal_sets.map((s) => `{${s.join(", ")}}`).join(" ") || "none ≤ 3"} · ${x.counterfactual_runs} counterfactual worlds</div>
    <h3>Probability of necessity</h3><table><tr><th>cause</th><th>shared noise</th><th>re-drawn</th><th></th></tr>${pn}</table>
    <h3>Provenance (causal Merkle DAG)</h3>${chainSvg(x.provenance)}
    <div class="disclaimer">${x.caveats.map(esc).join("<br>")}</div>${receipt(r)}`;
}

function chainSvg(prov) {
  if (!prov || !prov.nodes.length) return "";
  const nodes = prov.nodes, W = 430, rowH = 26;
  const depthMax = Math.max(...nodes.map((n) => n.depth));
  const t0 = Math.min(...nodes.map((n) => n.t)), t1 = Math.max(...nodes.map((n) => n.t)) || t0 + 1;
  const pos = {};
  const order = [...nodes].sort((a, b) => a.t - b.t || b.depth - a.depth);
  order.forEach((n, i) => { pos[n.eid] = [12 + ((n.t - t0) / Math.max(1, t1 - t0)) * (W - 150), 16 + i * rowH]; });
  const H = 24 + order.length * rowH;
  const col = { exogenous: "#ff5d5d", noise: "#b48cff", physical: "#ffb547", logical: "#6aa9ff", geographic: "#4fd69c", intervention: "#5fd4e8", cyber: "#5fd4e8" };
  const edges = prov.edges.filter((e) => pos[e.from] && pos[e.to]).map((e) => {
    const [x1, y1] = pos[e.from], [x2, y2] = pos[e.to];
    return `<path d="M${x1},${y1} C${x1 + 30},${y1} ${x2 - 30},${y2} ${x2},${y2}" stroke="#2f3b50" fill="none"/>`;
  }).join("");
  const dots = order.map((n) => {
    const [x, y] = pos[n.eid];
    return `<g><circle cx="${x}" cy="${y}" r="4.5" fill="${col[n.class] || "#7d8aa0"}"><title>${esc(n.rule)} · ${n.eid}</title></circle>` +
      `<text x="${x + 9}" y="${y + 3.5}" font-size="9.5" fill="#dbe2ee">${n.clock} ${esc(n.kind)} <tspan fill="#7d8aa0">${esc(n.entity)}</tspan></text></g>`;
  }).join("");
  const legend = Object.entries(col).map(([k, c], i) => `<g><circle cx="${12 + i * 62}" cy="${H - 6}" r="3.5" fill="${c}"/><text x="${19 + i * 62}" y="${H - 3}" font-size="8.5" fill="#7d8aa0">${k}</text></g>`).join("");
  return `<svg width="100%" viewBox="0 0 ${W} ${H + 8}">${edges}${dots}${legend}</svg>`;
}

const receipt = (r) => r && r.receipt ? `<div class="receipt">receipt <b>${r.receipt.id}</b> · code ${r.receipt.code.slice(0, 10)} · params ${r.receipt.params} · heads ${Object.entries(r.receipt.branches).map(([b, v]) => `${b}@${v.head.slice(0, 8)}`).join(", ")} · what-ifs explored ${r.receipt.queries_explored} · ${r.elapsed_ms} ms</div>` : "";

// ---------------------------------------------------------------- What-if
function dotplot(samples, w = 400) {
  if (!samples || !samples.length) return "";
  const s = [...samples].sort((a, b) => a - b), q = 20;
  const qs = Array.from({ length: q }, (_, i) => s[Math.min(s.length - 1, Math.floor(((i + 0.5) / q) * s.length))]);
  let lo = Math.min(...qs, 0), hi = Math.max(...qs, 0);
  if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
  const X = (v) => 16 + ((v - lo) / (hi - lo)) * (w - 32);
  const bins = {}, r = 6;
  const dots = qs.map((v) => { const b = Math.round(X(v) / (2 * r)); bins[b] = (bins[b] || 0) + 1;
    return `<circle cx="${b * 2 * r}" cy="${74 - bins[b] * (2 * r - 1)}" r="${r - 1}" fill="${v < 0 ? "#4fd69c" : v > 0 ? "#ff5d5d" : "#7d8aa0"}"/>`; }).join("");
  return `<svg width="100%" viewBox="0 0 ${w} 96"><line x1="${X(0)}" x2="${X(0)}" y1="4" y2="80" stroke="#dbe2ee" stroke-dasharray="3 3"/>` +
    `<text x="${X(0) + 4}" y="12" font-size="9" fill="#7d8aa0">Δ = 0</text>${dots}<line x1="12" x2="${w - 12}" y1="80" y2="80" stroke="#2f3b50"/>` +
    `<text x="14" y="93" font-size="9" fill="#7d8aa0">${fmt(lo)}</text><text x="${w - 40}" y="93" font-size="9" fill="#7d8aa0">${fmt(hi)}</text></svg>`;
}

async function runWhatif() {
  const iv = $("ivText").value.trim();
  const q = `WHAT IF ${iv} COMPARE ems.p90_response, ems.p1_late, hospital.unpowered_min, power.customer_hours RUNS ${$("runs").value} MODE ${$("mode").value} TIER ${$("tier").value}${$("coupling").checked ? " COUPLING" : ""}`;
  $("runWhatif").disabled = true;
  $("whatifOut").innerHTML = `<div class="spinner">branching reality: ${esc(q)}</div>`;
  try {
    const r = await pql(q); const x = r.result, e = x.ensemble;
    const name = x.branch_created;
    const cards = Object.entries(x.point).map(([m, p]) => {
      const st = e.metrics[m], d = st.delta, unit = (S.meta.kpis[m] || {}).unit || "";
      return `<h3>${m} <span class="tag">${unit}</span></h3>
        <div class="cmp"><div><div class="lab">ACTUAL SIMULATION</div><div class="big">${fmt(p.actual)}</div><div class="sub">the record (seed ${S.meta.world_seed})</div></div>
        <div class="alt"><div class="lab">ALTERNATE · ${esc(name)}</div><div class="big">${fmt(p.alternate)}</div><div class="sub">same luck, one change</div></div></div>
        <div class="delta">Δ mean ${d.mean > 0 ? "+" : ""}${fmt(d.mean, 2)} · 95% CI [${fmt(d.ci95[0], 2)}, ${fmt(d.ci95[1], 2)}] · P(Δ<0) = ${fmt(d.p_negative, 2)} over ${e.n} paired runs${st.variance_reduction ? ` · CRN variance ×${fmt(st.variance_reduction, 1)} smaller` : ""}</div>
        ${dotplot(st.samples)}`;
    }).join("");
    const strata = e.strata ? `<h3>Stratified (Simpson check)</h3><table><tr><th>stratum</th><th>Δ p90</th><th>95% CI</th></tr>${Object.entries(e.strata).map(([k, v]) => `<tr><td>${k}</td><td>${fmt(v.delta_mean, 2)}</td><td>[${fmt(v.ci95[0], 2)}, ${fmt(v.ci95[1], 2)}]</td></tr>`).join("")}</table>${e.reversal && e.reversal.length ? `<div class="tag warn">reversal in ${e.reversal.join(", ")}</div>` : ""}` : "";
    const coup = e.coupling ? `<h3>Coupling band</h3><div class="card">shared noise Δ ${fmt(e.coupling.shared_delta_mean, 2)} · re-drawn service noise Δ ${fmt(e.coupling.redrawn_delta_mean, 2)} [${fmt(e.coupling.redrawn_ci95[0], 2)}, ${fmt(e.coupling.redrawn_ci95[1], 2)}] ${e.coupling.sensitive ? "<span class='tag warn'>coupling-sensitive</span>" : "<span class='tag ok'>coupling-robust</span>"}</div>` : "";
    const card = `<h3>Credibility scorecard</h3><table>${x.scorecard.map((s) => `<tr><td>${esc(s.factor)}</td><td>${s.level === null ? "" : "●".repeat(s.level) + "○".repeat(4 - s.level)}</td><td>${esc(s.evidence)}</td></tr>`).join("")}</table>`;
    const diff = x.branch.diff;
    $("whatifOut").innerHTML = `<div class="card">branch <b>${esc(name)}</b> forked at ${x.branch.fork} · ${diff.shared} events shared with main · ${diff.only_alternate} new · ${diff.only_actual} erased — the symmetric difference is the intervention's causal footprint.</div>${cards}${strata}${coup}${card}<div class="disclaimer">${esc(x.disclaimer)}</div>${receipt(r)}`;
    S.meta.branches = await api("/api/branches");
    await selectAlt(name);
  } catch (err) { $("whatifOut").innerHTML = `<div class="card bad">${esc(err.message)}</div>`; }
  $("runWhatif").disabled = false;
}

// ---------------------------------------------------------------- Attribute / Plan / Evidence
async function runAttr() {
  $("attrOut").innerHTML = `<div class="spinner">computing exact Shapley values over root events…</div>`;
  const r = await pql(`ATTRIBUTE ${$("attrMetric").value}`); const x = r.result;
  const maxv = Math.max(1e-9, ...x.attribution.map((a) => Math.abs(a.phi)));
  $("attrOut").innerHTML = `<div class="cmp"><div><div class="lab">ACTUAL</div><div class="big">${fmt(x.actual)}</div></div><div class="alt"><div class="lab">ROOT-FREE WORLD</div><div class="big">${fmt(x.root_free)}</div></div></div>
    <h3>Who owns the excess of ${fmt(x.excess, 2)}</h3><table>${x.attribution.map((a) => `<tr><td>${esc(a.label)}</td><td><span class="bar ${a.phi < 0 ? "g" : "r"}" style="width:${(Math.abs(a.phi) / maxv) * 160}px"></span></td><td>${fmt(a.phi, 2)}</td><td>${a.share === null ? "" : fmt(a.share * 100, 0) + "%"}</td></tr>`).join("")}</table>
    <div class="hint">${x.evaluations} counterfactual worlds · efficiency gap ${x.efficiency_gap}</div><div class="disclaimer">${esc(x.note)}</div>${receipt(r)}`;
}

async function runPlan() {
  $("runPlan").disabled = true;
  $("planOut").innerHTML = `<div class="spinner">CELF search over paired training evenings…</div>`;
  const using = $("planUsing").value ? ` USING ${$("planUsing").value}` : "";
  try {
    const r = await pql(`PLAN REDUCE ${$("planMetric").value} BELOW ${$("planTarget").value} BUDGET ${$("planBudget").value}${using} RUNS 8`);
    const x = r.result, te = x.test;
    const pts = x.pareto, maxc = Math.max(1, ...pts.map((p) => p.cost)), vals = pts.map((p) => p.test_mean);
    const lo = Math.min(...vals, x.target) * 0.95, hi = Math.max(...vals, x.target) * 1.05, W = 420, H = 140;
    const X = (c) => 30 + (c / maxc) * (W - 50), Y = (v) => 10 + ((hi - v) / (hi - lo)) * (H - 30);
    const svg = `<svg width="100%" viewBox="0 0 ${W} ${H}"><line x1="30" x2="${W - 10}" y1="${Y(x.target)}" y2="${Y(x.target)}" stroke="#4fd69c" stroke-dasharray="4 3"/><text x="${W - 70}" y="${Y(x.target) - 4}" font-size="9" fill="#4fd69c">target ${x.target}</text>` +
      `<polyline fill="none" stroke="#ffb547" stroke-width="2" points="${pts.map((p) => `${X(p.cost)},${Y(p.test_mean)}`).join(" ")}"/>` +
      pts.map((p) => `<circle cx="${X(p.cost)}" cy="${Y(p.test_mean)}" r="4" fill="#ffb547"><title>${p.actions.join(", ")}</title></circle>`).join("") +
      `<text x="30" y="${H - 4}" font-size="9" fill="#7d8aa0">cost (CU) →  held-out mean ${x.metric}</text></svg>`;
    $("planOut").innerHTML = `<div class="card">${esc(x.search)} · ${x.simulations} simulations · ${x.candidates.length} candidates from the causal cone</div>
      <h3>Irreducible plan · ${fmt(x.plan_cost, 1)} CU</h3><table>${x.plan.map((a) => `<tr><td>${esc(a.label)}<br><span class="tag">${a.kind}</span> <span class="tag">${esc(a.basis)}</span></td><td>${a.cost}</td></tr>`).join("") || "<tr><td>no plan within budget meets the target</td></tr>"}</table>
      <div class="cmp"><div><div class="lab">HELD-OUT BASELINE</div><div class="big">${fmt(te.baseline)}</div><div class="sub">P(meet target) ${fmt(te.p_meet_target_baseline, 2)}</div></div><div class="alt"><div class="lab">WITH PLAN</div><div class="big">${fmt(te.with_plan)}</div><div class="sub">P(meet target) ${fmt(te.p_meet_target, 2)}</div></div></div>
      <div class="delta">Δ ${fmt(te.delta_mean, 2)} · 95% CI [${fmt(te.delta_ci95[0], 2)}, ${fmt(te.delta_ci95[1], 2)}] on ${te.n} unseen evenings · optimism gap ${fmt(x.optimism_gap, 2)}</div>
      <h3>Pareto: cost vs impact</h3>${svg}<div class="disclaimer">${esc(x.watermark)}</div>${receipt(r)}`;
  } catch (e) { $("planOut").innerHTML = `<div class="card bad">${esc(e.message)}</div>`; }
  $("runPlan").disabled = false;
}

async function runEvidence(kind) {
  const q = kind === "audit" ? "AUDIT" : kind === "validate" ? "VALIDATE RUNS 3" : `CORRELATE ${$("corrA").value} WITH ${$("corrB").value} RUNS 40`;
  $("evOut").innerHTML = `<div class="spinner">${q}…</div>`;
  try {
    const r = await pql(q), x = r.result;
    let html = "";
    if (kind === "audit") {
      html = `<div class="card">${esc(x.method)} · ${x.scenarios} stress evenings</div><h3>Residual screen</h3><table><tr><th>entity</th><th>episodes</th><th>excess</th><th>p</th><th>grade</th></tr>${x.residual_findings.map((f) => `<tr><td>${f.entity}</td><td>${f.excess_episodes}/${f.episodes}</td><td>+${f.mean_excess_min} min</td><td>${f.sign_test_p}</td><td><span class="tag warn">${esc(f.grade)}</span></td></tr>`).join("")}</table>
      <h3>Localised mechanism</h3>${x.localised_mechanisms.map((m) => `<div class="card"><b>${esc(m.event)}</b> at ${esc(m.entity)} — not predicted by the twin.<br>observed causes: ${esc(m.observed_causes.join(", "))}<br>suspected hidden dependency: <b>${esc(m.suspected_dependency || "?")}</b>${m.knockout ? `<br>knockout: ${esc(m.knockout.intervention)} → unpowered ${m.knockout.unpowered_before} → ${m.knockout.unpowered_after} min` : ""}<br><span class="tag warn">${esc(m.grade || "")}</span></div>`).join("")}
      <div class="disclaimer">hidden truth used only for scoring: ${esc(JSON.stringify(x.hidden_truth_for_scoring))}</div>`;
    } else if (kind === "validate") {
      html = `<div class="card">${esc(x.verdict)}</div><table><tr><th>metric</th><th>cases</th><th>90% coverage</th><th>mean |error|</th></tr>${Object.entries(x.by_metric).map(([k, v]) => `<tr><td>${k}</td><td>${v.cases}</td><td class="${v.coverage_90 < 0.85 ? "bad" : "good"}">${fmt(v.coverage_90 * 100, 0)}%</td><td>${v.mean_abs_error}</td></tr>`).join("")}</table>
      <h3>Cases</h3><table><tr><th>scenario</th><th>intervention</th><th>true Δ</th><th>twin interval</th></tr>${x.rows.map((c) => `<tr><td>${esc(c.scenario)}<br><span class="tag">${c.metric}</span></td><td>${esc(c.intervention)}</td><td>${fmt(c.true_delta, 2)}</td><td class="${c.covered ? "good" : "bad"}">[${fmt(c.twin_interval[0], 2)}, ${fmt(c.twin_interval[1], 2)}]</td></tr>`).join("")}</table>`;
    } else {
      html = `<div class="card"><b>${esc(x.verdict)}</b></div><table><tr><td>phi</td><td>${x.phi}</td></tr><tr><td>lift</td><td>${x.lift}</td></tr><tr><td>phi within strata</td><td>${x.phi_within_strata ?? "–"}</td></tr><tr><td>modelled path</td><td>${esc((x.modelled_path || []).join(" → ") || "none")}</td></tr><tr><td>common ancestors</td><td>${esc((x.common_ancestors || []).join(", "))}</td></tr><tr><td>minimal d-separator</td><td>${esc((x.minimal_d_separator || []).join(", "))}</td></tr><tr><td>runs</td><td>${x.runs}</td></tr></table>`;
    }
    $("evOut").innerHTML = html + receipt(r);
  } catch (e) { $("evOut").innerHTML = `<div class="card bad">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- Console / Ledger
const EXAMPLES = ["AS OF 18:40 SHOW DEPENDENTS OF substation:S3 DEPTH 2", "WHY hospital:H04", "SHOW INCIDENTS LATE LIMIT 5",
  "WHAT IF hospital:H04 GENERATOR STARTS AFTER 0 COMPARE hospital.unpowered_min RUNS 12", "ATTRIBUTE ems.p1_late",
  "CORRECT substation:S3 FAULT TIME TO 18:12", "AS OF 18:20 KNOWN AT 1 SHOW EVENTS OF substation:S3", "SHOW TELEMETRY OF feeder:F3-6",
  "SENSITIVITY ems.p90_response RUNS 3", "DIFF substations3_never_fails", "VERIFY main", "SHOW BRANCHES", "SHOW PARAMS"];
async function runPql() {
  const q = $("pql").value.trim(); if (!q) return;
  $("pqlOut").textContent = "…";
  try { const r = await pql(q); $("pqlOut").textContent = JSON.stringify(r, null, 2).slice(0, 60000); if (q.toUpperCase().includes("WHAT IF")) { S.meta.branches = await api("/api/branches"); renderBranches(S.meta.branches); } }
  catch (e) { $("pqlOut").textContent = "error: " + e.message; }
}
async function loadLedger() {
  const [b, r] = await Promise.all([pql("SHOW BRANCHES"), pql("SHOW RECEIPTS")]);
  const st = b.result.store;
  $("ledgerOut").innerHTML = `<h3>ChronoStore</h3><div class="card">${st.objects} event objects · ${st.references} references · dedup ×${st.dedup_ratio} · tx ${st.tx}</div>
    <h3>Branches (hash-chained overlays)</h3><table><tr><th>branch</th><th>fork</th><th>own</th><th>head</th><th></th></tr>${b.result.branches.map((x) => `<tr><td>${esc(x.name)}</td><td>${x.fork || "—"}</td><td>${x.own_events}</td><td>${x.head.slice(0, 12)}</td><td><button class="go ghost" data-v="${esc(x.name)}">verify</button></td></tr>`).join("")}</table>
    <h3>Receipts</h3><table><tr><th>id</th><th>query</th><th>kind</th><th>explored</th></tr>${r.result.receipts.slice().reverse().map((x) => `<tr><td>${x.id}</td><td>${esc(x.query)}</td><td>${x.kind}</td><td>${x.queries_explored}</td></tr>`).join("")}</table>
    <div id="verifyOut"></div>`;
  $("ledgerOut").querySelectorAll("button[data-v]").forEach((btn) => btn.onclick = async () => {
    const v = await pql(`VERIFY ${btn.dataset.v}`);
    $("verifyOut").innerHTML = `<div class="card">${v.result.ok ? "<span class='tag ok'>chain verified</span>" : "<span class='tag warn'>TAMPERED</span>"} ${esc(btn.dataset.v)} · ${v.result.events_checked} events re-hashed · head ${v.result.head}</div>`;
  });
}

// ---------------------------------------------------------------- wiring
function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.id === `tab-${name}`));
  if (name === "ledger") loadLedger();
}
function bindUI() {
  document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => showTab(b.dataset.tab));
  $("time").oninput = (e) => { S.t = +e.target.value; };
  $("play").onclick = () => { S.playing = !S.playing; $("play").textContent = S.playing ? "❚❚" : "▶"; };
  $("splitToggle").onchange = (e) => { S.split = e.target.checked && !!S.alt; $("wrapB").classList.toggle("hidden", !S.split); resize(); };
  $("preset").onchange = (e) => { $("ivText").value = e.target.value; };
  $("runWhatif").onclick = runWhatif;
  $("runAttr").onclick = runAttr;
  $("runPlan").onclick = runPlan;
  $("runAudit").onclick = () => runEvidence("audit");
  $("runValidate").onclick = () => runEvidence("validate");
  $("runCorr").onclick = () => runEvidence("correlate");
  $("runPql").onclick = runPql;
  $("pql").onkeydown = (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) runPql(); };
  $("examples").innerHTML = EXAMPLES.map((q) => `<span class="chip">${esc(q)}</span>`).join("");
  $("examples").querySelectorAll(".chip").forEach((c) => c.onclick = () => { $("pql").value = c.textContent; runPql(); });
  for (const id of ["mapA", "mapB"]) {
    const c = $(id);
    c.onmousemove = (e) => {
      const rect = c.getBoundingClientRect(), hit = hitTest(e.clientX - rect.left, e.clientY - rect.top), tip = $("tooltip");
      c.style.cursor = hit ? "pointer" : "default";
      if (hit) { tip.textContent = hit.label + " — click: WHY"; tip.style.left = (e.clientX - $("maps").getBoundingClientRect().left + 12) + "px"; tip.style.top = (e.clientY - $("maps").getBoundingClientRect().top + 12) + "px"; tip.classList.remove("hidden"); }
      else tip.classList.add("hidden");
    };
    c.onmouseleave = () => $("tooltip").classList.add("hidden");
    c.onclick = (e) => { const rect = c.getBoundingClientRect(), hit = hitTest(e.clientX - rect.left, e.clientY - rect.top); if (hit) why(hit.ref); };
  }
  window.onresize = resize;
}

boot().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<pre style="color:#ff5d5d;padding:20px">boot failed: ${esc(e.message)}</pre>`); });
