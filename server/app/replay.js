// 3D replay of a party session, drawn with three.js (served from this server, see /assets/).
//
// The session is cut into scenes:
//   field   — a stretch with recorded positions on one map: the map image is the ground (or a
//             made-up meadow when there is no image) and everyone walks where they really walked;
//   virtual — a stretch without positions (inside an instance, the game gives none): a made-up
//             dungeon corridor (or a forest road outside), the party walks it and stops to fight.
// Real time inside a scene maps linearly onto replay time; empty stretches are left out.
// What is on screen at a moment (who stands where, which monsters are up, who is down) follows
// from the replay time alone, so the slider can jump anywhere; bursts and floating text are
// fired only while playing forward.
import * as THREE from "./three.module.min.js";

const GAP = 3 * 60 * 1000;                 // ms between position samples that is a break, not a walk
const MELEE = new Set(["WARRIOR", "ROGUE", "PALADIN", "DRUID", "SHAMAN"]);
const CONTINENT = { 1414: "卡林多", 1415: "東部王國" };
const T = s => new Date(s).getTime();
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const lerp = (a, b, f) => a + (b - a) * f;
const hash = str => { let h = 2166136261; for (const c of String(str)) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619); } return h >>> 0; };
function rng(seed) { let s = seed >>> 0 || 1; return () => { s ^= s << 13; s >>>= 0; s ^= s >> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; }; }
const clock = t => new Date(t).toLocaleTimeString("zh-TW", { hour12: false });

// ------------------------------------------------------------------ the session as scenes and events
function model(d, ctx) {
  const N = ctx.name, me = d.owner.name;
  const S = T(d.started_at), E = d.ended_at ? T(d.ended_at) : Date.now();
  const seen = new Set(), roster = [];
  for (const m of [{ name: me, class: d.owner.class, owner: true }, ...d.members])
    if (m.name && !seen.has(m.name)) { seen.add(m.name); roster.push({ name: m.name, cls: m.class, owner: !!m.owner, color: ctx.color[m.class] || "#cfc6b8" }); }
  // melee in front when the party walks in formation; the recorder first among equals
  roster.sort((a, b) => (MELEE.has(b.cls) - MELEE.has(a.cls)) || (b.owner - a.owner));

  // ---- events, everything with a time
  const queue = list => { const m = new Map(); for (const x of list) { const k = Math.round(x.t / 1000); if (!m.has(k)) m.set(k, []); m.get(k).push(x); } return t => { const q = m.get(Math.round(t / 1000)); return q && q.length ? q.shift() : null; }; };
  const named = queue(d.timeline.filter(e => e.type === "MOB_KILLED").map(e => ({ ...e, t: T(e.timestamp) })));
  const placed = queue(d.marks.filter(k => k.type === "MOB_KILLED" && k.t).map(k => ({ ...k, t: T(k.t) })));
  const encounters = (d.encounters || []).filter(e => e.timestamp).map(e => ({ name: e.name || "首領", end: T(e.timestamp), start: T(e.timestamp) - (e.duration || 60) * 1000, success: e.success }));
  const fight = clamp((d.pace && d.pace.avg_fight_seconds) || 8, 3, 25) * 1000;
  const monsters = [];
  for (const ts of d.kill_times || []) {
    const t = T(ts), n = named(t), at = placed(t);
    if (encounters.some(e => Math.abs(e.end - t) < 4000)) continue;          // the boss itself, drawn below
    monsters.push({ spawn: t - fight, kill: t, name: n ? n.monster_name : null, by: n ? n.killed_by : null, at, boss: false });
  }
  for (const e of encounters) monsters.push({ spawn: e.start, kill: e.end, name: e.name, boss: true, fled: !e.success });
  monsters.sort((a, b) => a.kill - b.kill);

  const events = [];                           // {t, kind, who, text}
  for (const m of monsters) events.push({ t: m.kill, kind: m.boss ? (m.fled ? "wipe" : "boss") : "kill", m,
    text: m.boss ? (m.fled ? `首領戰失敗：${m.name}` : `擊敗首領 ${m.name}！`) : `擊殺 ${m.name || "怪物"}` + (m.by && m.by !== me ? `（${N(m.by)}）` : "") });
  for (const m of monsters) if (m.boss) events.push({ t: m.spawn, kind: "pull", m, text: `首領戰開始：${m.name}` });
  for (const h of d.highlights) {
    if (!h.timestamp) continue;
    const t = T(h.timestamp);
    if (h.type === "LEVEL_UP") events.push({ t, kind: "level", who: me, text: `${N(me)} 升到 ${h.level} 級！` });
    else if (h.type === "GROUP_MEMBER_LEVEL_UP") events.push({ t, kind: "level", who: h.name, text: `${N(h.name)} 升到 ${h.level} 級！` });
    else if (h.type === "PLAYER_DIED") events.push({ t, kind: "death", who: me, text: `${N(me)} 倒下了` });
    else if (h.type === "PARTY_MEMBER_DIED") events.push({ t, kind: "death", who: h.name, text: `${N(h.name)} 倒下了` });
    else if (h.type === "INSTANCE_ENTERED") events.push({ t, kind: "enter", text: `進入 ${h.name || "副本"}`, name: h.name });
    else if (h.type === "ACHIEVEMENT_EARNED") events.push({ t, kind: "achieve", who: me, text: `成就：${h.name || ""}` });
  }
  for (const e of d.timeline) {
    const t = T(e.timestamp);
    if (e.type === "ITEM_LOOTED" && e.item_name) events.push({ t, kind: "loot", who: me, text: `拾取 ${e.item_name}` + (e.count > 1 ? ` ×${e.count}` : "") });
    else if (e.type === "QUEST_COMPLETED") events.push({ t, kind: "quest", who: me, text: `完成任務 ${e.quest_name || ""}` });
    else if (e.type === "GROUP_MEMBER_JOINED" && e.name) events.push({ t, kind: "info", text: `${N(e.name)} 加入隊伍` });
    else if (e.type === "GROUP_MEMBER_LEFT" && e.name) events.push({ t, kind: "info", text: `${N(e.name)} 離開隊伍` });
  }
  events.sort((a, b) => a.t - b.t);
  const deaths = events.filter(e => e.kind === "death");

  // ---- position runs, one map at a time
  const own = [];
  for (const m of d.maps) for (const p of m.points) own.push({ t: T(p[0]), x: p[1], y: p[2], map: m.map_id });
  own.sort((a, b) => a.t - b.t);
  let runs = [];
  for (const p of own) {
    let r = runs[runs.length - 1];
    if (!r || r.map !== p.map || p.t - r.pts[r.pts.length - 1].t > GAP) { r = { map: p.map, pts: [] }; runs.push(r); }
    r.pts.push(p);
  }
  if (runs.some(r => r.pts.length > 1)) runs = runs.filter(r => r.pts.length > 1);

  // ---- scenes: runs, and the stretches between them that had something happen
  const busy = (a, b) => events.some(e => e.t > a && e.t <= b) || monsters.some(m => m.kill > a && m.spawn < b);
  const instanceAt = (a, b) => {
    const e = events.find(x => x.kind === "enter" && x.t >= a - 120000 && x.t <= b);
    if (e) return e.name;
    if (encounters.some(x => x.end > a && x.end <= b) || !runs.length) return d.instances[0] || (encounters.length ? "副本" : null);
    return null;
  };
  const scenes = [];
  const virtual = (a, b) => {
    const inst = instanceAt(a, b);
    const zone = (d.timeline.find(e => e.zone_name && T(e.timestamp) >= a && T(e.timestamp) <= b) || {}).zone_name;
    scenes.push({ kind: "virtual", theme: inst ? "dungeon" : "wild", name: inst || zone || d.zones[0] || "野外", t0: a, t1: b });
  };
  let cursor = Math.min(S, own.length ? own[0].t : S, events.length ? events[0].t : S);
  for (const r of runs) {
    const a = r.pts[0].t, b = r.pts[r.pts.length - 1].t;
    const last = scenes[scenes.length - 1];
    if (a - cursor > 60000 && busy(cursor, a)) virtual(cursor, a);
    else if (last && a - cursor <= 60000) last.t1 = a;           // a short break joins the stretch before
    const prev = scenes[scenes.length - 1];
    if (prev && prev.kind === "field" && prev.map === r.map && !busy(prev.t1, a)) { prev.t1 = b; prev.pts.push(...r.pts); }
    else scenes.push({ kind: "field", map: r.map, t0: scenes.length || a - cursor > 60000 ? a : cursor, t1: b, pts: r.pts.slice() });
    cursor = b;
  }
  const end = Math.max(E, events.length ? events[events.length - 1].t : E);
  if (end - cursor > 60000 && busy(cursor, end)) virtual(cursor, end);
  else if (scenes.length) scenes[scenes.length - 1].t1 = Math.max(scenes[scenes.length - 1].t1, Math.min(end, cursor + 60000));
  for (const s of scenes) if (s.t1 - s.t0 < 1000) s.t1 = s.t0 + 1000;

  // ---- replay time: about two seconds a real minute, every scene at least six seconds
  const real = scenes.reduce((n, s) => n + s.t1 - s.t0, 0) || 1;
  const target = clamp(real / 60000 * 2, 40, 150);
  let off = 0;
  for (const s of scenes) { s.dur = Math.max(6, target * (s.t1 - s.t0) / real); s.off = off; off += s.dur; }
  const sceneOf = t => scenes.find(s => t >= s.t0 - 500 && t <= s.t1 + 500);
  const replayOf = t => { const s = sceneOf(t); return s ? s.off + clamp((t - s.t0) / (s.t1 - s.t0), 0, 1) * s.dur : null; };
  for (const e of events) e.r = replayOf(e.t);
  for (const m of monsters) {
    m.scene = sceneOf(m.kill);
    m.killR = replayOf(m.kill);
    m.spawnR = m.scene ? Math.min(m.killR - 0.8, m.scene.off + clamp((m.spawn - m.scene.t0) / (m.scene.t1 - m.scene.t0), 0, 1) * m.scene.dur) : null;
  }
  const shown = events.filter(e => e.r != null);
  const dead = name => deaths.filter(e => e.who === name && e.r != null).map(e => [e.r, Math.max(e.r + 2.5, replayOf(e.t + 45000) || 0)]);
  for (const p of roster) p.down = dead(p.name);

  // ---- damage each minute, for how hard each member is hitting
  const minutes = (d.damage_minutes || []).map(([ts, m]) => [T(ts), m]);
  let peak = 1;
  for (const [, m] of minutes) for (const v of Object.values(m)) peak = Math.max(peak, v);
  const fury = (name, t) => {
    let lo = 0, hi = minutes.length - 1, best = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (minutes[mid][0] <= t + 30000) { best = mid; lo = mid + 1; } else hi = mid - 1; }
    if (best < 0 || t - minutes[best][0] > 90000) return 0;
    return (minutes[best][1][name] || 0) / peak;
  };
  return { roster, scenes, events: shown, monsters: monsters.filter(m => m.scene), total: off, fury, hasMeter: minutes.length > 0,
    mates: Object.fromEntries(d.maps.map(m => [m.map_id, m.members || {}])),
    mapInfo: Object.fromEntries(d.maps.map(m => [m.map_id, m])) };
}

// ------------------------------------------------------------------ textures drawn on a canvas
function canvasTexture(w, h, draw) {
  const c = document.createElement("canvas"); c.width = w; c.height = h;
  draw(c.getContext("2d"), w, h);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
}
const glowTex = () => canvasTexture(64, 64, (c, w) => { const g = c.createRadialGradient(w / 2, w / 2, 0, w / 2, w / 2, w / 2);
  g.addColorStop(0, "rgba(255,255,255,1)"); g.addColorStop(.35, "rgba(255,255,255,.55)"); g.addColorStop(1, "rgba(255,255,255,0)"); c.fillStyle = g; c.fillRect(0, 0, w, w); });
function textSprite(text, { color = "#fff", size = 30, scale = 0.011, bold = true } = {}) {
  const font = `${bold ? "bold " : ""}${size}px -apple-system, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif`;
  const probe = document.createElement("canvas").getContext("2d"); probe.font = font;
  const w = Math.ceil(probe.measureText(text).width) + 16, h = size + 16;
  const tex = canvasTexture(w, h, c => { c.font = font; c.textAlign = "center"; c.textBaseline = "middle";
    c.lineWidth = 6; c.strokeStyle = "rgba(0,0,0,.85)"; c.strokeText(text, w / 2, h / 2); c.fillStyle = color; c.fillText(text, w / 2, h / 2); });
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
  s.scale.set(w * scale, h * scale, 1); s.renderOrder = 10;
  return s;
}
const stoneTex = seed => canvasTexture(256, 256, (c, w) => {
  const r = rng(seed); c.fillStyle = "#4a4239"; c.fillRect(0, 0, w, w);
  for (let y = 0; y < 4; y++) for (let x = 0; x < 4; x++) {
    const v = 58 + r() * 26 | 0; c.fillStyle = `rgb(${v + 6},${v},${v - 8})`;
    c.fillRect(x * 64 + 2, y * 64 + 2, 60, 60);
    for (let k = 0; k < 30; k++) { c.fillStyle = `rgba(0,0,0,${r() * .12})`; c.fillRect(x * 64 + r() * 60, y * 64 + r() * 60, 3, 3); }
  }
});
const grassTex = seed => canvasTexture(256, 256, (c, w) => {
  const r = rng(seed); c.fillStyle = "#3f5a2c"; c.fillRect(0, 0, w, w);
  for (let k = 0; k < 2600; k++) { const g = 70 + r() * 60 | 0; c.fillStyle = `rgba(${g * .55 | 0},${g},${g * .4 | 0},.5)`; c.fillRect(r() * w, r() * w, 2, 3); }
});

// ------------------------------------------------------------------ the replay window
let styled = false;
function style() {
  if (styled) return; styled = true;
  const s = document.createElement("style");
  s.textContent = `
.rp{position:fixed;inset:0;z-index:1000;background:#0d0b09;color:#f3ead9;font:14px/1.45 -apple-system,"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif;user-select:none;overflow:hidden}
.rp canvas{position:absolute;inset:0;width:100%;height:100%;aspect-ratio:auto;border:0;border-radius:0;touch-action:none;cursor:grab}
.rp canvas:active{cursor:grabbing}
.rp-top{position:absolute;left:16px;right:16px;top:12px;display:flex;align-items:flex-start;gap:12px;pointer-events:none;text-shadow:0 1px 3px #000}
.rp-top h3{margin:0;font-size:19px;line-height:1.3}.rp-top small{display:block;color:#cbbfa8;font-size:13px}
.rp-x{margin-left:auto;pointer-events:auto;width:38px;height:38px;border-radius:50%;border:1px solid #ffffff33;background:#00000080;color:#fff;font-size:18px;cursor:pointer}
.rp-x:hover{background:#000000b0}
.rp-side{position:absolute;right:16px;top:64px;width:190px;display:grid;gap:4px;pointer-events:none}
.rp-m{display:grid;grid-template-columns:10px 1fr;gap:2px 7px;align-items:center;background:#0000006b;border-radius:6px;padding:4px 8px;font-size:12px}
.rp-m i{width:10px;height:10px;border-radius:50%}.rp-m span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rp-m b{grid-column:2;height:4px;border-radius:2px;background:#ffffff1f;overflow:hidden}.rp-m b u{display:block;height:100%;width:0;transition:width .25s}
.rp-m.down{opacity:.45}.rp-m.down span::after{content:"　☠"}
.rp-tick{position:absolute;left:16px;bottom:118px;display:grid;gap:4px;max-width:min(420px,calc(100% - 32px));pointer-events:none}
.rp-tick div{background:#0000007a;border-radius:6px;padding:3px 10px;font-size:13px;text-shadow:0 1px 2px #000;animation:rpin .35s ease-out}
.rp-tick time{color:#cbbfa8;font-size:12px;margin-right:8px;font-variant-numeric:tabular-nums}
@keyframes rpin{from{opacity:0;transform:translateX(-12px)}}
.rp-bar{position:absolute;left:0;right:0;bottom:0;padding:28px 16px 14px;background:linear-gradient(transparent,#000000c8)}
.rp-row{display:flex;align-items:center;gap:10px}
.rp-row input[type=range]{flex:1;min-width:100px;accent-color:#e0964a}
.rp-btn{font:inherit;color:#fff;background:#e0964a;border:0;border-radius:99px;padding:6px 16px;cursor:pointer;font-weight:600;white-space:nowrap}
.rp-row select{font:inherit;color:#fff;background:#00000080;border:1px solid #ffffff40;border-radius:6px;padding:4px 6px}
.rp-chips{display:flex;gap:6px;overflow-x:auto;margin-bottom:10px;scrollbar-width:none}
.rp-chips button{font:inherit;font-size:12px;color:#f3ead9;background:#00000080;border:1px solid #ffffff30;border-radius:99px;padding:3px 11px;cursor:pointer;white-space:nowrap}
.rp-chips button.on{background:#e0964a;border-color:#e0964a;color:#1c1710}
.rp-card{position:absolute;left:50%;top:38%;transform:translate(-50%,-50%);text-align:center;pointer-events:none;opacity:0;transition:opacity .6s;text-shadow:0 2px 12px #000}
.rp-card b{display:block;font-size:clamp(24px,5vw,42px);letter-spacing:.06em}.rp-card small{font-size:14px;color:#e8d9bd}
.rp-flash{position:absolute;inset:0;background:#fff3d0;opacity:0;pointer-events:none;transition:opacity .5s}
.rp-wait{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);color:#cbbfa8}
.rp-help{color:#cbbfa8;font-size:12px;margin-left:auto;white-space:nowrap}
@media (max-width:640px){.rp-side{top:auto;bottom:118px;right:16px;width:130px}.rp-tick{max-width:calc(100% - 170px)}.rp-help{display:none}}
@media (prefers-reduced-motion:reduce){.rp-tick div{animation:none}}`;
  document.head.append(s);
}

export function openReplay(d, ctx) {
  style();
  const M = model(d, ctx), N = ctx.name;
  if (!M.scenes.length) return false;
  const h = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const root = h("div", "rp"); root.setAttribute("role", "dialog"); root.setAttribute("aria-label", "3D 回放");
  const top = h("div", "rp-top"), titleBox = h("div"), title = h("h3"), sub = h("small"), close = h("button", "rp-x", "✕");
  close.type = "button"; close.setAttribute("aria-label", "關閉回放");
  titleBox.append(title, sub); top.append(titleBox, close);
  const side = h("div", "rp-side"), tick = h("div", "rp-tick"), card = h("div", "rp-card"), flash = h("div", "rp-flash"), wait = h("div", "rp-wait", "載入場景…");
  const bar = h("div", "rp-bar"), chips = h("div", "rp-chips"), row = h("div", "rp-row");
  const play = h("button", "rp-btn", "▶ 播放"); play.type = "button";
  const slider = h("input"); Object.assign(slider, { type: "range", min: 0, max: 1000, value: 0 }); slider.setAttribute("aria-label", "回放時間");
  const speed = h("select"); for (const v of [0.5, 1, 2, 4, 8]) { const o = h("option", null, v + "×"); o.value = v; if (v === 1) o.selected = true; speed.append(o); }
  speed.setAttribute("aria-label", "播放速度");
  row.append(play, slider, speed, h("span", "rp-help", "拖曳旋轉・滾輪縮放・空白鍵播放"));
  if (M.scenes.length > 1) bar.append(chips);
  bar.append(row);
  root.append(top, side, tick, card, flash, wait, bar);
  document.body.append(root);
  const prevOverflow = document.body.style.overflow; document.body.style.overflow = "hidden";

  // ---- three.js basics
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  root.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(48, 1, 0.1, 2000);
  const hemi = new THREE.HemisphereLight(0xfff2dd, 0x2a2018, 1.1), sun = new THREE.DirectionalLight(0xfff0d6, 1.6);
  sun.position.set(30, 60, 20);
  const torch = new THREE.PointLight(0xffa04a, 0, 34, 1.6);
  scene.add(hemi, sun, torch);
  const GLOW = glowTex();

  // shared parts for people and monsters
  const G = { body: new THREE.CapsuleGeometry(0.34, 0.7, 4, 12), head: new THREE.SphereGeometry(0.27, 18, 14), ring: new THREE.RingGeometry(0.55, 0.72, 40),
    disc: new THREE.CircleGeometry(0.7, 24), monster: new THREE.DodecahedronGeometry(0.62, 0), horn: new THREE.ConeGeometry(0.12, 0.45, 8), eye: new THREE.SphereGeometry(0.08, 8, 6),
    bolt: new THREE.SphereGeometry(0.13, 10, 8), beam: new THREE.CylinderGeometry(0.9, 0.9, 14, 24, 1, true) };
  const shadowMat = new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.38, depthWrite: false });

  const heroes = M.roster.map((p, i) => {
    const g = new THREE.Group(), col = new THREE.Color(p.color);
    const mat = new THREE.MeshStandardMaterial({ color: col, roughness: 0.45, metalness: 0.15, emissive: col, emissiveIntensity: 0.18 });
    const body = new THREE.Mesh(G.body, mat); body.position.y = 0.72;
    const head = new THREE.Mesh(G.head, mat); head.position.y = 1.43;
    const pose = new THREE.Group(); pose.add(body, head);
    const shadow = new THREE.Mesh(G.disc, shadowMat); shadow.rotation.x = -Math.PI / 2; shadow.position.y = 0.02;
    const ring = new THREE.Mesh(G.ring, new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: p.owner ? 0.95 : 0.6, side: THREE.DoubleSide, depthWrite: false }));
    ring.rotation.x = -Math.PI / 2; ring.position.y = 0.04; if (p.owner) ring.scale.setScalar(1.25);
    const label = textSprite(N(p.name) + (p.owner ? " ★" : ""), { color: p.color, size: 26, scale: 0.012 }); label.position.y = 2.15;
    g.add(shadow, ring, pose, label);
    scene.add(g);
    return { ...p, i, g, pose, mat, base: col.clone(), ring, label, dir: new THREE.Vector3(0, 0, -1), last: new THREE.Vector3(), charge: Math.random(), swing: 0, ui: null };
  });

  // ---- the member list on the right
  for (const hr of heroes) {
    const m = h("div", "rp-m"), dot = h("i"), name = h("span", null, N(hr.name)), b = h("b"), u = h("u");
    dot.style.background = hr.color; u.style.background = hr.color; b.append(u);
    m.append(dot, name); if (M.hasMeter) m.append(h("i"), b);
    side.append(m); hr.ui = { m, u };
  }
  if (heroes.length > 12) side.style.display = "none";            // a raid list would cover the scene

  // ---- per-scene world, rebuilt when the scene changes
  let world = null, building = null, ticket = 0;
  const textures = new Map(), loaded = new Set();
  const loadImage = id => {
    const info = M.mapInfo[id];
    if (!info || !info.has_image) return Promise.resolve(null);
    if (!textures.has(id)) textures.set(id, new Promise(res => new THREE.TextureLoader().load(`${ctx.api}/map/${id}`, t => { t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = renderer.capabilities.getMaxAnisotropy(); loaded.add(t); res(t); }, undefined, () => res(null))));
    return textures.get(id);
  };
  const disposeWorld = w => { if (!w) return; scene.remove(w.group); w.group.traverse(o => { if (o.geometry && !Object.values(G).includes(o.geometry)) o.geometry.dispose(); if (o.material && o.material !== shadowMat) { const ms = [].concat(o.material); for (const m of ms) { if (m.map && m.map !== GLOW && !loaded.has(m.map)) m.map.dispose(); m.dispose(); } } }); for (const mo of w.mobs.values()) releaseMob(mo); };

  async function build(sc) {
    const my = ++ticket;
    const group = new THREE.Group(), w = { sc, group, mobs: new Map(), crosses: [] };
    const seed = hash(sc.kind === "field" ? sc.map : sc.name + sc.t0);
    const r = rng(seed);
    if (sc.kind === "field") {
      const tex = await loadImage(sc.map);
      if (my !== ticket) return null;
      const aspect = tex && tex.image ? tex.image.height / tex.image.width : 2 / 3;
      // who else walked here in this stretch, and how much of the map they covered
      const mates = {};
      for (const [name, list] of Object.entries(M.mates[sc.map] || {})) {
        const pts = list.map(p => ({ t: T(p[0]), x: p[1], y: p[2] })).filter(p => p.t >= sc.t0 - 60000 && p.t <= sc.t1 + 60000 && typeof p.y === "number");
        if (pts.length) mates[name] = pts;
      }
      const all = sc.pts.concat(...Object.values(mates));
      const xs = all.map(p => p.x), ys = all.map(p => p.y);
      const span = clamp(Math.max(Math.max(...xs) - Math.min(...xs), (Math.max(...ys) - Math.min(...ys)) * aspect) * 1.3, 8, 100);
      const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2;
      const A = clamp(5000 / span, 50, 600);
      const X = x => (x - cx) / 100 * A, Z = y => (y - cy) / 100 * A * aspect;
      w.track = sc.pts.map(p => ({ t: p.t, X: X(p.x), Z: Z(p.y) }));
      w.mates = Object.fromEntries(Object.entries(mates).map(([n, pts]) => [n, pts.map(p => ({ t: p.t, X: X(p.x), Z: Z(p.y) }))]));
      w.place = k => k ? [X(k.x), Z(k.y)] : null;
      if (tex) {
        const plane = new THREE.Mesh(new THREE.PlaneGeometry(A, A * aspect), new THREE.MeshStandardMaterial({ map: tex, roughness: 1 }));
        plane.rotation.x = -Math.PI / 2; plane.position.set(X(50), 0, Z(50));
        group.add(plane);
        const under = new THREE.Mesh(new THREE.PlaneGeometry(A * 6, A * 6), new THREE.MeshBasicMaterial({ color: 0x15120e }));
        under.rotation.x = -Math.PI / 2; under.position.y = -0.05; group.add(under);
      } else meadow(group, r, seed, w.track);
      ribbon(group, w.track, 0.32, null, w);
      for (const [n, pts] of Object.entries(w.mates)) { const hr = heroes.find(x => x.name === n); if (hr) ribbon(group, pts, 0.14, hr.color, null); }
      scene.fog = new THREE.Fog(0x15120e, 60, 220);
      scene.background = new THREE.Color(0x15120e);
      hemi.intensity = 1.1; sun.intensity = 1.6; torch.intensity = 0;
    } else {
      // a made-up walk: the party advances while nothing is up, and stops to fight
      const mobs = M.monsters.filter(m => m.scene === sc);
      const N_ = 900, cum = new Float32Array(N_ + 1);
      for (let k = 1; k <= N_; k++) {
        const t = sc.t0 + (sc.t1 - sc.t0) * k / N_;
        cum[k] = cum[k - 1] + (mobs.some(m => t >= m.spawn && t <= m.kill) ? 0 : 1);
      }
      const L = clamp(24 + mobs.length * 5, 36, 700);
      const path = [{ x: 0, z: 0 }]; let ang = -Math.PI / 2, len = 0, turn = r() < 0.5 ? 1 : -1;
      while (len < L + 30) { const seg = 16 + r() * 14; const p = path[path.length - 1]; path.push({ x: p.x + Math.cos(ang) * seg, z: p.z + Math.sin(ang) * seg }); len += seg; ang += turn * (Math.PI / 2) * (0.5 + r() * 0.5); turn = -turn; }
      const segs = []; let acc = 0;
      for (let k = 1; k < path.length; k++) { const a = path[k - 1], b = path[k], l = Math.hypot(b.x - a.x, b.z - a.z); segs.push({ a, b, l, s: acc }); acc += l; }
      w.pathAt = s => {
        s = clamp(s, 0, acc - 0.01);
        const g = segs.find(x => s >= x.s && s < x.s + x.l) || segs[segs.length - 1];
        const f = (s - g.s) / g.l, dx = (g.b.x - g.a.x) / g.l, dz = (g.b.z - g.a.z) / g.l;
        return { x: lerp(g.a.x, g.b.x, f), z: lerp(g.a.z, g.b.z, f), dx, dz };
      };
      w.progress = t => { const k = clamp((t - sc.t0) / (sc.t1 - sc.t0), 0, 1) * N_; const i = Math.floor(k); const c = lerp(cum[i], cum[Math.min(N_, i + 1)], k - i); return 6 + (cum[N_] ? c / cum[N_] : k / N_) * L; };
      const jit = rng(seed ^ 0x9e37);
      w.spot = m => {                                           // where a monster stands: ahead of where the party stopped
        if (!m._spot) { const p = w.pathAt(w.progress(m.spawn) + (m.boss ? 8 : 4.2)); const side = (jit() - 0.5) * 3; m._spot = [p.x - p.dz * side, p.z + p.dx * side]; }
        return m._spot;
      };
      const rooms = mobs.filter(m => m.boss).map(m => w.pathAt(w.progress(m.spawn) + 8));
      if (sc.theme === "dungeon") dungeon(group, segs, rooms, r, seed); else forestRoad(group, segs, r, seed);
    }
    w.ghosts = [];
    return w;
  }

  function ribbon(group, pts, width, color, w) {
    // a flat band along the path; segments over a long break are left out
    const n = pts.length; if (n < 2) return;
    const pos = new Float32Array((n - 1) * 18), col = new Float32Array((n - 1) * 18);
    const c0 = new THREE.Color(0x39d0ff), c1 = new THREE.Color(0xff4d8d), fixed = color ? new THREE.Color(color) : null, c = new THREE.Color();
    for (let k = 1; k < n; k++) {
      const a = pts[k - 1], b = pts[k], dx = b.X - a.X, dz = b.Z - a.Z, l = Math.hypot(dx, dz) || 1;
      const nx = -dz / l * width, nz = dx / l * width, skip = b.t - a.t > GAP, y = color ? 0.05 : 0.07;
      const quad = [[a.X + nx, a.Z + nz], [a.X - nx, a.Z - nz], [b.X + nx, b.Z + nz], [b.X - nx, b.Z - nz], [b.X + nx, b.Z + nz], [a.X - nx, a.Z - nz]];
      c.copy(fixed || c0.clone().lerp(c1, k / n));
      quad.forEach(([x, z], v) => { const o = (k - 1) * 18 + v * 3; pos[o] = skip ? a.X : x; pos[o + 1] = y; pos[o + 2] = skip ? a.Z : z; col[o] = c.r; col[o + 1] = c.g; col[o + 2] = c.b; });
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3)); geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
    const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, opacity: color ? 0.55 : 0.9, side: THREE.DoubleSide, depthWrite: false }));
    mesh.renderOrder = 2; group.add(mesh);
    // drawn up to the moment shown
    if (w) w.trail = { geo, pts }; else (group.userData.mates = group.userData.mates || []).push({ geo, pts });
  }

  function grass(group, seed, cx = 0, cz = 0) {
    const tex = grassTex(seed); tex.wrapS = tex.wrapT = THREE.RepeatWrapping; tex.repeat.set(40, 40);
    const g = new THREE.Mesh(new THREE.PlaneGeometry(800, 800), new THREE.MeshStandardMaterial({ map: tex, roughness: 1 }));
    g.rotation.x = -Math.PI / 2; g.position.set(cx, 0, cz); group.add(g);
  }
  function meadow(group, r, seed, track) {
    grass(group, seed);
    trees(group, r, 260, 140, (x, z) => !track.some(p => Math.hypot(p.X - x, p.Z - z) < 4));
  }
  function trees(group, r, count, radius, ok) {
    const trunkG = new THREE.CylinderGeometry(0.18, 0.26, 1.4, 6), leafG = new THREE.ConeGeometry(1.3, 3.2, 7);
    const trunk = new THREE.InstancedMesh(trunkG, new THREE.MeshStandardMaterial({ color: 0x5a3d26, roughness: 1 }), count);
    const leaf = new THREE.InstancedMesh(leafG, new THREE.MeshStandardMaterial({ color: 0x2f5a2a, roughness: 0.9, flatShading: true }), count);
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), s = new THREE.Vector3(), p = new THREE.Vector3(), c = new THREE.Color();
    let n = 0;
    for (let k = 0; k < count * 4 && n < count; k++) {
      const x = (r() - 0.5) * radius * 2, z = (r() - 0.5) * radius * 2;
      if (!ok(x, z)) continue;
      const sc = 0.7 + r() * 0.9;
      m.compose(p.set(x, 0.7 * sc, z), q, s.set(sc, sc, sc)); trunk.setMatrixAt(n, m);
      m.compose(p.set(x, (1.4 + 1.5) * sc, z), q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), r() * 6), s.set(sc, sc, sc)); leaf.setMatrixAt(n, m);
      leaf.setColorAt(n, c.setHSL(0.27 + r() * 0.08, 0.45, 0.22 + r() * 0.1)); q.identity();
      n++;
    }
    trunk.count = leaf.count = n; group.add(trunk, leaf);
  }
  function forestRoad(group, segs, r, seed) {
    const cx = segs.reduce((n, g) => n + g.a.x, 0) / segs.length, cz = segs.reduce((n, g) => n + g.a.z, 0) / segs.length;
    grass(group, seed, cx, cz);
    const road = new THREE.MeshStandardMaterial({ color: 0x8a6c47, roughness: 1 });
    for (const g of segs) {
      const m = new THREE.Mesh(new THREE.PlaneGeometry(g.l + 5, 5), road);
      m.rotation.x = -Math.PI / 2; m.rotation.z = -Math.atan2(g.b.z - g.a.z, g.b.x - g.a.x);
      m.position.set((g.a.x + g.b.x) / 2, 0.02, (g.a.z + g.b.z) / 2); group.add(m);
    }
    const near = (x, z) => segs.some(g => { const t = clamp(((x - g.a.x) * (g.b.x - g.a.x) + (z - g.a.z) * (g.b.z - g.a.z)) / (g.l * g.l), 0, 1); return Math.hypot(x - lerp(g.a.x, g.b.x, t), z - lerp(g.a.z, g.b.z, t)) < 5; });
    const sub = new THREE.Group(); trees(sub, r, 420, 160, (x, z) => !near(x + cx, z + cz)); sub.position.set(cx, 0, cz); group.add(sub);
    scene.fog = new THREE.Fog(0x1d2418, 40, 150); scene.background = new THREE.Color(0x1d2418);
    hemi.intensity = 1.0; sun.intensity = 1.3; torch.intensity = 0;
  }
  function dungeon(group, segs, rooms, r, seed) {
    const W = 8, tex = stoneTex(seed); tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
    const wallMat = new THREE.MeshStandardMaterial({ color: 0x3b332b, roughness: 1, flatShading: true });
    const pillarMat = new THREE.MeshStandardMaterial({ color: 0x5c5247, roughness: 0.9, flatShading: true });
    const flameMat = new THREE.SpriteMaterial({ map: GLOW, color: 0xff9a3c, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false });
    const pillarG = new THREE.BoxGeometry(0.9, 3.2, 0.9), wallG = new THREE.BoxGeometry(1, 1.6, 0.7);
    const inRoom = (x, z) => rooms.some(c => Math.hypot(x - c.x, z - c.z) < 12.5);
    const flames = [];
    for (const g of segs) {
      const ang = -Math.atan2(g.b.z - g.a.z, g.b.x - g.a.x), mx = (g.a.x + g.b.x) / 2, mz = (g.a.z + g.b.z) / 2;
      const ft = tex.clone(); ft.needsUpdate = true; ft.repeat.set((g.l + W) / 4, W / 4);
      const fl = new THREE.Mesh(new THREE.BoxGeometry(g.l + W, 0.3, W), new THREE.MeshStandardMaterial({ map: ft, roughness: 0.95 }));
      fl.position.set(mx, -0.15, mz); fl.rotation.y = ang; group.add(fl);
      const dx = (g.b.x - g.a.x) / g.l, dz = (g.b.z - g.a.z) / g.l;
      for (const side of [-1, 1]) {
        // low wall in pieces, left out where a boss hall opens up
        for (let s = W * 0.1; s < g.l - W * 0.1 - 0.5; s += 4) {
          const l = Math.min(4, g.l - W * 0.1 - s), c = s + l / 2;
          const wx = g.a.x + dx * c - dz * side * (W / 2 + 0.3), wz = g.a.z + dz * c + dx * side * (W / 2 + 0.3);
          if (inRoom(wx, wz)) continue;
          const wall = new THREE.Mesh(wallG, wallMat); wall.scale.set(l, 1 + r() * 0.25, 1);
          wall.position.set(wx, 0.8, wz); wall.rotation.y = ang; group.add(wall);
        }
        for (let s = 3; s < g.l - 2; s += 6) {
          const px = g.a.x + dx * s - dz * side * (W / 2), pz = g.a.z + dz * s + dx * side * (W / 2);
          if (inRoom(px, pz)) continue;
          const p = new THREE.Mesh(pillarG, pillarMat); p.position.set(px, 1.6, pz); group.add(p);
          if (((s / 6) | 0) % 2 === 0) { const f = new THREE.Sprite(flameMat.clone()); f.position.set(px + dz * side * 0.7, 2.6, pz - dx * side * 0.7); f.scale.setScalar(1.3); f.userData.ph = r() * 6; group.add(f); flames.push(f); }
        }
      }
    }
    for (const c of rooms) {                                     // a round hall where a boss waits
      const ft = tex.clone(); ft.needsUpdate = true; ft.repeat.set(5, 5);
      const fl = new THREE.Mesh(new THREE.CylinderGeometry(11, 11, 0.3, 40), new THREE.MeshStandardMaterial({ map: ft, roughness: 0.95 }));
      fl.position.set(c.x, -0.13, c.z); group.add(fl);
      const rune = new THREE.Mesh(new THREE.RingGeometry(5.2, 5.6, 48), new THREE.MeshBasicMaterial({ color: 0xb23cff, transparent: true, opacity: 0.55, side: THREE.DoubleSide }));
      rune.rotation.x = -Math.PI / 2; rune.position.set(c.x, 0.04, c.z); group.add(rune);
      for (let k = 0; k < 10; k++) { const a = k / 10 * Math.PI * 2; const p = new THREE.Mesh(pillarG, pillarMat); p.position.set(c.x + Math.cos(a) * 11.5, 2.2, c.z + Math.sin(a) * 11.5); p.scale.y = 1.4; group.add(p); }
    }
    group.userData.flames = flames;
    scene.fog = new THREE.Fog(0x0b0807, 14, 60); scene.background = new THREE.Color(0x0b0807);
    hemi.intensity = 0.35; sun.intensity = 0.25; torch.intensity = 60;
  }

  // ---- monsters: a small pool of meshes
  const pool = [];
  function takeMob(m) {
    let o = pool.pop();
    if (!o) {
      const g = new THREE.Group();
      const mat = new THREE.MeshStandardMaterial({ color: 0x7a2420, roughness: 0.6, flatShading: true, emissive: 0x300806, transparent: true });
      const body = new THREE.Mesh(G.monster, mat); body.position.y = 0.7;
      const hornMat = new THREE.MeshStandardMaterial({ color: 0xd8cbb0, roughness: 0.5, transparent: true });
      const h1 = new THREE.Mesh(G.horn, hornMat), h2 = new THREE.Mesh(G.horn, hornMat);
      h1.position.set(-0.3, 1.25, 0.1); h1.rotation.z = 0.5; h2.position.set(0.3, 1.25, 0.1); h2.rotation.z = -0.5;
      const eyeMat = new THREE.MeshBasicMaterial({ color: 0xffd23c, transparent: true });
      const e1 = new THREE.Mesh(G.eye, eyeMat), e2 = new THREE.Mesh(G.eye, eyeMat); e1.position.set(-0.2, 0.85, 0.5); e2.position.set(0.2, 0.85, 0.5);
      const body3 = new THREE.Group(); body3.add(body, h1, h2, e1, e2);
      const shadow = new THREE.Mesh(G.disc, shadowMat); shadow.rotation.x = -Math.PI / 2; shadow.position.y = 0.02;
      const ring = new THREE.Mesh(G.ring, new THREE.MeshBasicMaterial({ color: 0xff3b2f, transparent: true, opacity: 0.7, side: THREE.DoubleSide, depthWrite: false }));
      ring.rotation.x = -Math.PI / 2; ring.position.y = 0.04;
      g.add(shadow, ring, body3);
      o = { g, body3, mats: [mat, hornMat, eyeMat], mat, ring, label: null };
    }
    const boss = m.boss;
    o.g.scale.setScalar(boss ? 2.3 : 0.85 + (hash(m.name || "") % 40) / 100);
    o.mat.color.set(boss ? 0x5a2a7a : [0x7a2420, 0x6b4a22, 0x3f5a2e, 0x4a3a5c][hash(m.name || "") % 4]);
    o.ring.material.color.set(boss ? 0xc040ff : 0xff3b2f);
    if (boss || heroes.length <= 5) { o.label = textSprite(m.name || "怪物", { color: boss ? "#e6a8ff" : "#ffb3a8", size: boss ? 30 : 22, scale: boss ? 0.008 : 0.011 }); o.label.position.y = 2.0; o.g.add(o.label); }
    scene.add(o.g);
    return o;
  }
  function releaseMob(o) {
    scene.remove(o.g);
    if (o.label) { o.g.remove(o.label); o.label.material.map.dispose(); o.label.material.dispose(); o.label = null; }
    pool.push(o);
  }

  // ---- effects that live on the wall clock
  const fx = [];
  function burst(pos, color, n = 36, power = 5, life = 0.9) {
    const geo = new THREE.BufferGeometry(), p = new Float32Array(n * 3), v = [];
    for (let k = 0; k < n; k++) { p[k * 3] = pos.x; p[k * 3 + 1] = pos.y; p[k * 3 + 2] = pos.z; const a = Math.random() * 6.28, u = Math.random() * 2 - 1, s = power * (0.4 + Math.random() * 0.6); v.push([Math.cos(a) * Math.sqrt(1 - u * u) * s, Math.abs(u) * s + 1.5, Math.sin(a) * Math.sqrt(1 - u * u) * s]); }
    geo.setAttribute("position", new THREE.BufferAttribute(p, 3));
    const pts = new THREE.Points(geo, new THREE.PointsMaterial({ map: GLOW, color, size: 0.55, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
    scene.add(pts);
    fx.push({ o: pts, life, age: 0, step(dt, f) { const a = geo.attributes.position.array; for (let k = 0; k < n; k++) { v[k][1] -= 9 * dt; a[k * 3] += v[k][0] * dt; a[k * 3 + 1] = Math.max(0.05, a[k * 3 + 1] + v[k][1] * dt); a[k * 3 + 2] += v[k][2] * dt; } geo.attributes.position.needsUpdate = true; pts.material.opacity = 1 - f; } });
  }
  function floatText(text, pos, color, big) {
    const s = textSprite(text, { color, size: big ? 34 : 24, scale: big ? 0.014 : 0.012 });
    s.position.copy(pos); s.position.y += 2.4; scene.add(s);
    fx.push({ o: s, life: big ? 2.4 : 1.7, age: 0, step(dt, f) { s.position.y += dt * (big ? 0.9 : 1.3); s.material.opacity = f < 0.7 ? 1 : 1 - (f - 0.7) / 0.3; } });
  }
  function beam(pos, color) {
    const m = new THREE.Mesh(G.beam, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.6, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
    m.position.set(pos.x, 7, pos.z); scene.add(m);
    fx.push({ o: m, life: 2.2, age: 0, step(dt, f) { m.scale.set(1 - f * 0.6, 1, 1 - f * 0.6); m.material.opacity = 0.6 * (1 - f); m.rotation.y += dt * 2; } });
    burst(pos.clone().setY(0.4), color, 50, 4, 1.4);
  }
  function bolt(from, to, color) {
    const m = new THREE.Mesh(G.bolt, new THREE.MeshBasicMaterial({ color, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
    const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false })); glow.scale.setScalar(0.9); m.add(glow);
    const a = from.clone(), b = to.clone(); scene.add(m);
    fx.push({ o: m, life: 0.38, age: 0, step(dt, f) { m.position.lerpVectors(a, b, f); m.position.y += Math.sin(f * Math.PI) * 0.8; }, done() { spark(b, color); } });
  }
  function spark(pos, color) {
    const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false }));
    s.position.copy(pos); scene.add(s);
    fx.push({ o: s, life: 0.25, age: 0, step(dt, f) { s.scale.setScalar(0.6 + f * 1.4); s.material.opacity = 1 - f; } });
  }
  const dropFx = () => { for (const e of fx.splice(0)) { scene.remove(e.o); e.o.traverse(o => { if (o.geometry && !Object.values(G).includes(o.geometry)) o.geometry.dispose(); if (o.material) { if (o.material.map && o.material.map !== GLOW) o.material.map.dispose(); o.material.dispose(); } }); } };

  // ---- camera: orbit around the party, drag to turn, wheel or pinch to zoom
  const cam = { theta: 0.35, phi: 0.92, r: 24, target: new THREE.Vector3(), want: new THREE.Vector3() };
  const pointers = new Map(); let pinch = 0;
  const cv = renderer.domElement;
  cv.addEventListener("pointerdown", e => { cv.setPointerCapture(e.pointerId); pointers.set(e.pointerId, [e.clientX, e.clientY]); });
  cv.addEventListener("pointermove", e => {
    if (!pointers.has(e.pointerId)) return;
    const [px, py] = pointers.get(e.pointerId); pointers.set(e.pointerId, [e.clientX, e.clientY]);
    if (pointers.size === 1) { cam.theta -= (e.clientX - px) * 0.006; cam.phi = clamp(cam.phi - (e.clientY - py) * 0.005, 0.25, 1.45); }
    else if (pointers.size === 2) { const [a, b] = [...pointers.values()]; const dd = Math.hypot(a[0] - b[0], a[1] - b[1]); if (pinch) cam.r = clamp(cam.r * pinch / dd, 6, 160); pinch = dd; }
  });
  const up = e => { pointers.delete(e.pointerId); if (pointers.size < 2) pinch = 0; };
  cv.addEventListener("pointerup", up); cv.addEventListener("pointercancel", up);
  cv.addEventListener("wheel", e => { e.preventDefault(); cam.r = clamp(cam.r * Math.exp(e.deltaY * 0.0012), 6, 160); }, { passive: false });

  // ---- state
  let g = 0, playing = false, lastNow = performance.now(), lastG = 0, curScene = null, tickShown = -1, uiAt = 0;
  const fmtScene = sc => (sc.kind === "field" ? (CONTINENT[sc.map] ? CONTINENT[sc.map] + "（移動中）" : (M.mapInfo[sc.map] && M.mapInfo[sc.map].name) || "地圖 " + sc.map) : sc.name);
  const sceneAt = v => { let k = M.scenes.length - 1; while (k > 0 && M.scenes[k].off > v) k--; return M.scenes[k]; };
  M.scenes.forEach(sc => {
    const b = h("button", null, `${clock(sc.t0).slice(0, 5)} ${fmtScene(sc)}${sc.kind === "virtual" && sc.theme === "dungeon" ? " ⚔" : ""}`); b.type = "button";
    b.addEventListener("click", () => seek(sc.off + 0.01)); sc.chip = b; chips.append(b);
  });
  const showCard = (big, small) => { card.replaceChildren(h("b", null, big), h("small", null, small)); card.style.opacity = 1; clearTimeout(showCard.t); showCard.t = setTimeout(() => card.style.opacity = 0, 2200); };

  function seek(v) { g = clamp(v, 0, M.total); lastG = g; slider.value = Math.round(g / M.total * 1000); }
  const setPlaying = on => {
    playing = on; play.textContent = on ? "❚❚ 暫停" : "▶ 播放";
    if (on && g >= M.total - 0.01) seek(0);
    lastNow = performance.now();
  };
  play.addEventListener("click", () => setPlaying(!playing));
  slider.addEventListener("input", () => { seek(slider.value / 1000 * M.total); });
  const onKey = e => {
    if (e.key === "Escape") { e.preventDefault(); shut(); }
    else if (e.key === " " && e.target.tagName !== "SELECT") { e.preventDefault(); setPlaying(!playing); }
    else if (e.key === "ArrowRight") seek(g + 3); else if (e.key === "ArrowLeft") seek(g - 3);
  };
  document.addEventListener("keydown", onKey);
  close.addEventListener("click", () => shut());

  const at = (pts, t) => {
    let lo = 0, hi = pts.length - 1;
    if (t <= pts[0].t) return [pts[0].X, pts[0].Z, false];
    if (t >= pts[hi].t) return [pts[hi].X, pts[hi].Z, false];
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (pts[m].t <= t) lo = m; else hi = m; }
    const a = pts[lo], b = pts[hi];
    if (b.t - a.t > GAP) return [a.X, a.Z, false];
    const f = (t - a.t) / (b.t - a.t); return [lerp(a.X, b.X, f), lerp(a.Z, b.Z, f), true];
  };
  const drawTo = (geo, pts, t) => { let lo = 0, hi = pts.length; while (lo < hi) { const m = (lo + hi) >> 1; if (pts[m].t <= t) lo = m + 1; else hi = m; } geo.setDrawRange(0, Math.max(0, lo - 1) * 6); };
  const slot = k => k === 0 ? [0, 0] : [((k - 1) % 3 - 1) * 1.6, -(1 + Math.floor((k - 1) / 3)) * 1.5];
  const V = new THREE.Vector3(), V2 = new THREE.Vector3(), GREY = new THREE.Color(0x555555);

  function update(dt) {
    const sc = sceneAt(g);
    if (!world || world.sc !== sc) {
      if (building !== sc) {
        building = sc; wait.style.display = "";
        build(sc).then(w => {
          if (!w || building !== sc) return;
          disposeWorld(world); dropFx(); world = w; scene.add(w.group); building = null; wait.style.display = "none";
          for (const s of M.scenes) s.chip && s.chip.classList.toggle("on", s === sc);
          if (sc !== curScene) { curScene = sc; showCard(fmtScene(sc), sc.kind === "virtual" ? (sc.theme === "dungeon" ? "副本內沒有座標，這段以示意場景呈現" : "這段沒有位置紀錄，以示意場景呈現") : `${clock(sc.t0)} – ${clock(sc.t1)}`); }
          title.textContent = fmtScene(sc);
          if (sc !== shownScene) { shownScene = sc; cam.phi = sc.kind === "virtual" && sc.theme === "dungeon" ? 0.72 : 0.92; }
          lastNow = performance.now(); first = true;
        });
      }
      return false;
    }
    const t = sc.t0 + clamp((g - sc.off) / sc.dur, 0, 1) * (sc.t1 - sc.t0);
    const isDown = hr => hr.down.some(([a, b]) => g >= a && g < b);
    // monsters up at this moment
    const up = M.monsters.filter(m => m.scene === sc && g >= m.spawnR && g < m.killR + 1.2);
    const live = new Set(up);
    for (const [m, o] of world.mobs) if (!live.has(m)) { releaseMob(o); world.mobs.delete(m); }
    // where the party is
    let lead, dir;
    if (sc.kind === "field") {
      const [x, z] = at(world.track, t); lead = V.set(x, 0, z);
      const [x2, z2] = at(world.track, t + 4000); dir = V2.set(x2 - x, 0, z2 - z);
    } else {
      const s = world.progress(t), p = world.pathAt(s); lead = V.set(p.x, 0, p.z); dir = V2.set(p.dx, 0, p.dz);
    }
    if (dir.lengthSq() > 1e-4) dir.normalize(); else dir.set(0, 0, -1);
    const spot = m => {
      if (sc.kind !== "field") return world.spot(m);
      if (!m._spot) { const k = world.place(m.at); if (k) m._spot = k; else { const [x, z] = at(world.track, m.kill); const j = (hash(m.kill) % 100) / 100 - 0.5; m._spot = [x + dir.x * 2.2 + j * 2, z + dir.z * 2.2 + j]; } }
      return m._spot;
    };
    for (const m of up) {
      let o = world.mobs.get(m); if (!o) { o = takeMob(m); world.mobs.set(m, o); }
      const [x, z] = spot(m), k = g - m.killR;
      o.g.position.set(x, k > 0 ? -k * 0.8 : 0, z);
      const born = clamp((g - m.spawnR) / 0.35, 0, 1);
      o.body3.scale.setScalar(k > 0 ? Math.max(0.01, 1 - k / 1.2) : born);
      o.body3.position.y = Math.abs(Math.sin(performance.now() / 260 + m.kill)) * 0.12;
      o.body3.rotation.y = Math.atan2(lead.x - x, lead.z - z);
      for (const mt of o.mats) mt.opacity = k > 0 ? Math.max(0, 1 - k / 1.2) : 1;
      o.ring.material.opacity = k > 0 ? 0 : 0.7;
    }
    const target = up.filter(m => g < m.killR)
      .map(m => ({ m, p: spot(m) })).sort((a, b) => Math.hypot(a.p[0] - lead.x, a.p[1] - lead.z) - Math.hypot(b.p[0] - lead.x, b.p[1] - lead.z))[0];
    // everyone's spot
    const right = { x: -dir.z, z: dir.x };
    let order = 0;
    for (const hr of heroes) {
      let x, z, real = false;
      if (sc.kind === "field" && hr.owner) { x = lead.x; z = lead.z; real = true; }
      else if (sc.kind === "field" && world.mates[hr.name]) { const pts = world.mates[hr.name]; if (t >= pts[0].t - 30000 && t <= pts[pts.length - 1].t + 30000) { [x, z] = at(pts, t); real = true; } }
      if (!real) {
        const k = sc.kind === "field" ? ++order : order++;
        const [sx, sf] = slot(k); x = lead.x + right.x * sx + dir.x * sf; z = lead.z + right.z * sx + dir.z * sf;
        if (target && MELEE.has(hr.cls) && !isDown(hr)) {                // melee step up to whatever they are fighting
          const [mx, mz] = target.p, a = Math.atan2(x - mx, z - mz);
          x = lerp(x, mx + Math.sin(a) * 1.5, 0.8); z = lerp(z, mz + Math.cos(a) * 1.5, 0.8);
        }
      }
      const moved = Math.hypot(x - hr.g.position.x, z - hr.g.position.z);
      if (first || moved > 6) hr.g.position.set(x, 0, z);
      else hr.g.position.set(lerp(hr.g.position.x, x, Math.min(1, dt * 10)), 0, lerp(hr.g.position.z, z, Math.min(1, dt * 10)));
      const down = isDown(hr);
      const face = target && !down ? Math.atan2(target.p[0] - x, target.p[1] - z) : moved > 0.02 ? Math.atan2(x - hr.last.x, z - hr.last.z) : hr.pose.rotation.y;
      hr.pose.rotation.y = face;
      hr.last.set(x, 0, z);
      const walking = moved / Math.max(dt, 0.001) > 0.5 && !down;
      hr.pose.position.y = walking ? Math.abs(Math.sin(performance.now() / 110 + hr.i)) * 0.14 : 0;
      hr.swing = Math.max(0, hr.swing - dt * 4);
      hr.pose.rotation.x = down ? -Math.PI / 2 * 0.95 : -hr.swing * 0.5;
      hr.pose.position.y += down ? 0.25 : 0;
      hr.mat.color.copy(hr.base).lerp(GREY, down ? 0.75 : 0); hr.mat.emissiveIntensity = down ? 0 : 0.18;
      hr.ring.material.opacity = down ? 0.15 : hr.owner ? 0.95 : 0.6;
      hr.label.material.opacity = down ? 0.5 : 1;
      // attacks, only while playing
      if (playing && target && !down && dt > 0) {
        const f = M.hasMeter ? M.fury(hr.name, t) : 0.5;
        hr.charge += dt * (0.7 + f * 3.2) * Math.min(2, +speed.value);
        if (hr.charge >= 1) {
          hr.charge = 0;
          const tp = new THREE.Vector3(target.p[0], target.m.boss ? 1.6 : 0.8, target.p[1]);
          if (MELEE.has(hr.cls)) { hr.swing = 1; spark(tp.clone().add(new THREE.Vector3((Math.random() - .5) * .6, Math.random() * .5, (Math.random() - .5) * .6)), hr.base); }
          else bolt(hr.g.position.clone().setY(1.2), tp, hr.base);
        }
      }
    }
    // trails up to now
    if (world.trail) drawTo(world.trail.geo, world.trail.pts, t);
    for (const mt of world.group.userData.mates || []) drawTo(mt.geo, mt.pts, t);
    for (const f of world.group.userData.flames || []) { f.scale.setScalar(1.15 + Math.sin(performance.now() / 90 + f.userData.ph) * 0.15 + Math.random() * 0.08); }
    // camera follows the party; torch light with it
    cam.want.copy(lead).setY(0.8);
    cam.target.lerp(cam.want, first ? 1 : Math.min(1, dt * 4));
    torch.position.set(cam.target.x, 4.5, cam.target.z);
    // events passed since the last frame
    if (playing && g > lastG && g - lastG < 3) for (const e of M.events) if (e.r > lastG && e.r <= g) fire(e, sc);
    lastG = g;
    // the lists, a few times a second
    const now = performance.now();
    if (now - uiAt > 150) {
      uiAt = now;
      sub.textContent = `${clock(t)}${M.scenes.length > 1 ? `　第 ${M.scenes.indexOf(sc) + 1} / ${M.scenes.length} 段` : ""}`;
      let k = -1; for (let i = 0; i < M.events.length && M.events[i].r <= g; i++) k = i;
      if (k !== tickShown) {
        tickShown = k;
        tick.replaceChildren(...M.events.slice(Math.max(0, k - 4), k + 1).map(e => { const n = h("div"); const tm = h("time", null, clock(e.t).slice(0, 5)); n.append(tm, e.text); return n; }));
      }
      for (const hr of heroes) { hr.ui.m.classList.toggle("down", isDown(hr)); if (M.hasMeter) hr.ui.u.style.width = Math.round(M.fury(hr.name, t) * 100) + "%"; }
    }
    first = false;
    return true;
  }
  let first = true, shownScene = null;

  function fire(e, sc) {
    const hr = e.who ? heroes.find(x => x.name === e.who) : null;
    const pos = hr ? hr.g.position.clone() : cam.target.clone().setY(0);
    if (e.kind === "kill" || e.kind === "boss") {
      const o = world.mobs.get(e.m), p = o ? o.g.position.clone().setY(0.8) : pos.setY(0.8);
      burst(p, e.kind === "boss" ? 0xd36bff : 0xff7a3c, e.kind === "boss" ? 120 : 34, e.kind === "boss" ? 9 : 5, e.kind === "boss" ? 1.6 : 0.9);
      if (e.kind === "boss") { floatText(`擊敗 ${e.m.name}！`, p, "#f0c6ff", true); flash.style.transition = "none"; flash.style.opacity = 0.55; requestAnimationFrame(() => { flash.style.transition = "opacity .9s"; flash.style.opacity = 0; }); showCard(`擊敗 ${e.m.name}`, clock(e.t)); }
      else if (heroes.length > 5) floatText("✕ " + (e.m.name || ""), p, "#ffb08a");
    } else if (e.kind === "wipe") { showCard(`${e.m.name}　首領戰失敗`, clock(e.t)); }
    else if (e.kind === "pull") { showCard(`首領戰：${e.m.name}`, clock(e.t)); }
    else if (e.kind === "level") { beam(pos, 0xffd76a); floatText("升級！", pos, "#ffe08a", true); }
    else if (e.kind === "death") { burst(pos.clone().setY(1), 0x9aa0a6, 30, 3, 1.2); floatText("☠", pos, "#e8e8e8", true); }
    else if (e.kind === "loot") { if (!fire.lootAt || performance.now() - fire.lootAt > 350) { fire.lootAt = performance.now(); spark(pos.clone().setY(1.2), 0xffd24a); floatText(e.text.replace(/^拾取 /, ""), pos, "#ffd86b"); } }
    else if (e.kind === "quest") { burst(pos.clone().setY(1.2), 0xffe066, 40, 4, 1.1); floatText("！任務完成", pos, "#ffe066", true); }
    else if (e.kind === "achieve") { beam(pos, 0x8ad8ff); floatText("成就！", pos, "#bfe9ff", true); }
    else if (e.kind === "enter") { showCard(e.text, clock(e.t)); }
  }

  // ---- the loop
  let raf = 0, alive = true;
  const size = () => { const w = root.clientWidth, hh = root.clientHeight; renderer.setSize(w, hh, false); camera.aspect = w / hh; camera.updateProjectionMatrix(); };
  const ro = new ResizeObserver(size); ro.observe(root); size();
  function frame(now) {
    if (!alive) return;
    const dt = Math.min(0.1, (now - lastNow) / 1000); lastNow = now;
    if (playing && !building && world && world.sc === sceneAt(g)) {
      g = Math.min(M.total, g + dt * +speed.value);
      slider.value = Math.round(g / M.total * 1000);
      if (g >= M.total) setPlaying(false);
    }
    update(dt);
    for (let k = fx.length - 1; k >= 0; k--) {
      const e = fx[k]; e.age += dt; const f = Math.min(1, e.age / e.life); e.step(dt, f);
      if (f >= 1) { if (e.done) e.done(); scene.remove(e.o); e.o.traverse(o => { if (o.geometry && !Object.values(G).includes(o.geometry)) o.geometry.dispose(); if (o.material) { if (o.material.map && o.material.map !== GLOW) o.material.map.dispose(); o.material.dispose(); } }); fx.splice(k, 1); }
    }
    const s = Math.sin(cam.phi);
    camera.position.set(cam.target.x + cam.r * s * Math.sin(cam.theta), cam.target.y + cam.r * Math.cos(cam.phi), cam.target.z + cam.r * s * Math.cos(cam.theta));
    camera.lookAt(cam.target);
    renderer.render(scene, camera);
    raf = requestAnimationFrame(frame);
  }
  raf = requestAnimationFrame(frame);
  close.focus();
  setTimeout(() => setPlaying(true), 600);

  function shut() {
    alive = false; cancelAnimationFrame(raf); ro.disconnect();
    document.removeEventListener("keydown", onKey);
    dropFx(); disposeWorld(world);
    for (const o of pool) o.g.traverse(x => { if (x.material) x.material.dispose(); });
    for (const hr of heroes) { hr.mat.dispose(); hr.ring.material.dispose(); hr.label.material.map.dispose(); hr.label.material.dispose(); }
    for (const geo of Object.values(G)) geo.dispose();
    textures.forEach(p => p.then(t => t && t.dispose()));
    GLOW.dispose(); renderer.dispose(); renderer.forceContextLoss();
    root.remove(); document.body.style.overflow = prevOverflow;
    if (ctx.onClose) ctx.onClose();
  }
  return true;
}
