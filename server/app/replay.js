// 3D replay of a party session, drawn with three.js (served from this server, see /assets/).
//
// The session is cut into scenes:
//   field   — a stretch with recorded positions on one map: the map image is the ground (or a
//             made-up meadow when there is no image) and everyone walks where they really walked;
//   virtual — a stretch without positions (inside an instance, the game gives none): a made-up
//             dungeon corridor (or a forest road outside), the party walks it and stops to fight.
// Time is warped: walking goes by fast, fighting slower, a boss fight slowest, so the fights can
// be followed. When the combat log was recorded, every hit, miss and heal in it is played as it
// happened (who, on whom, with what, for how much); otherwise the party just fights each kill.
// What stands where at a moment follows from the replay time alone, so the slider can jump
// anywhere; attacks, bursts and floating numbers are fired only while playing forward.
import * as THREE from "./three.module.min.js";
import * as S from "./scenery.js";

const GAP = 3 * 60 * 1000;                 // ms between position samples that is a break, not a walk
const MELEE = new Set(["WARRIOR", "ROGUE", "PALADIN", "DRUID", "SHAMAN"]);
const CONTINENT = { 1414: "卡林多", 1415: "東部王國" };
const SCHOOL = { 1: "#f4efe6", 2: "#ffe680", 4: "#ff8a3c", 8: "#7dff6a", 16: "#8fd8ff", 32: "#b98cff", 64: "#ff8ff0" };
const MISS = { MISS: "未擊中", DODGE: "閃躲", PARRY: "招架", BLOCK: "格擋", RESIST: "抵抗", IMMUNE: "免疫", ABSORB: "吸收", EVADE: "閃避", DEFLECT: "偏斜", REFLECT: "反射" };
const WEAPON = { WARRIOR: "sword", PALADIN: "mace", ROGUE: "dagger", SHAMAN: "mace", DRUID: "staff", HUNTER: "bow", MAGE: "staff", WARLOCK: "staff", PRIEST: "staff" };
const ORB = { DRUID: 0x7dff6a, MAGE: 0x8fd8ff, WARLOCK: 0xb98cff, PRIEST: 0xffe680 };
const T = s => new Date(s).getTime();
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const lerp = (a, b, f) => a + (b - a) * f;
const hash = str => { let h = 2166136261; for (const c of String(str)) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619); } return h >>> 0; };
function rng(seed) { let s = seed >>> 0 || 1; return () => { s ^= s << 13; s >>>= 0; s ^= s >> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; }; }
const clock = t => new Date(t).toLocaleTimeString("zh-TW", { hour12: false });
const schoolColor = s => { if (!s) return SCHOOL[1]; for (const b of [4, 16, 32, 8, 2, 64, 1]) if (s & b) return SCHOOL[b]; return SCHOOL[1]; };
const fmt = n => n >= 10000 ? (n / 10000).toFixed(1) + " 萬" : Math.round(n).toLocaleString();
const lowerBound = (list, v, key) => { let lo = 0, hi = list.length; while (lo < hi) { const m = (lo + hi) >> 1; if (key(list[m]) <= v) lo = m + 1; else hi = m; } return lo; };

// ------------------------------------------------------------------ the combat log rows
function parseHits(json, me) {
  if (!json || !Array.isArray(json.columns) || !Array.isArray(json.rows)) return [];
  const ix = Object.fromEntries(json.columns.map((c, i) => [c, i]));
  const out = [];
  for (const r of json.rows) {
    const way = r[ix.way];
    if (way !== "out" && way !== "in" && way !== "heal" && way !== "death") continue;
    out.push({ t: r[ix.ts], way, who: r[ix.who] || me, pet: !!r[ix.pet], unit: r[ix.unit], enemy: r[ix.enemy], guid: r[ix.guid],
      spell: r[ix.spell], school: r[ix.school], kind: r[ix.kind], amount: +r[ix.amount] || 0, over: r[ix.overkill], crit: !!r[ix.crit], miss: r[ix.miss] });
  }
  out.sort((a, b) => a.t - b.t);
  return out;
}

// ------------------------------------------------------------------ the session as scenes and events
function model(d, ctx, hitsJson) {
  const N = ctx.name, me = d.owner.name;
  const S = T(d.started_at), E = d.ended_at ? T(d.ended_at) : Date.now();
  const short = u => typeof u === "string" ? u.split("-")[0] : u;
  // who was in the group when: [from, to] in ms; the recorder throughout
  const seen = new Set(), roster = [];
  for (const m of [{ name: me, class: d.owner.class, owner: true }, ...d.members]) {
    if (!m.name || seen.has(m.name)) continue;
    seen.add(m.name);
    const spans = m.owner || !Array.isArray(m.spans) || !m.spans.length ? [[-Infinity, Infinity]]
      : m.spans.map(([a, b]) => [a ? T(a) : -Infinity, b ? T(b) : Infinity]);
    roster.push({ name: m.name, cls: m.class, owner: !!m.owner, color: ctx.color[m.class] || "#cfc6b8", spans });
  }
  roster.sort((a, b) => (MELEE.has(b.cls) - MELEE.has(a.cls)) || (b.owner - a.owner));
  const inParty = new Set(roster.map(p => p.name));

  const hits = parseHits(hitsJson, me).filter(h => h.t >= S - 60000 && h.t <= E + 60000);
  const hasHits = hits.some(h => h.way === "out" || h.way === "in");

  // ---- kills recorded by the addon, with the name and the place where it happened
  const queue = list => { const m = new Map(); for (const x of list) { const k = Math.round(x.t / 1000); if (!m.has(k)) m.set(k, []); m.get(k).push(x); }
    return t => { for (const k of [0, -1, 1, -2, 2]) { const q = m.get(Math.round(t / 1000) + k); if (q && q.length) return q.shift(); } return null; }; };
  const named = queue(d.timeline.filter(e => e.type === "MOB_KILLED").map(e => ({ ...e, t: T(e.timestamp) })));
  const placed = queue(d.marks.filter(k => k.type === "MOB_KILLED" && k.t).map(k => ({ ...k, t: T(k.t) })));
  const encounters = (d.encounters || []).filter(e => e.timestamp).map(e => ({ name: e.name || "首領", end: T(e.timestamp), start: T(e.timestamp) - (e.duration || 60) * 1000, success: e.success }));

  // ---- monsters: one per enemy in the combat log, then one per kill the log did not cover
  const byGuid = new Map();
  for (const h of hits) {
    if ((h.way !== "out" && h.way !== "in") || !h.guid || inParty.has(short(h.enemy))) continue;
    let m = byGuid.get(h.guid);
    if (!m) byGuid.set(h.guid, m = { guid: h.guid, name: h.enemy, first: h.t, last: h.t, kill: null, by: null, total: 0, dmg: [], logged: true });
    m.last = h.t; h.mob = m;
    if (h.way === "out") { m.total += h.amount; m.dmg.push([h.t, m.total]); if (h.over != null && h.over >= 0 && m.kill == null) { m.kill = h.t; m.by = h.who; } }
  }
  const monsters = [...byGuid.values()];
  const kills = (d.kill_times || []).map(T);
  const covered = new Set();
  for (const t of kills) {
    let best = null;
    for (const m of monsters) if (Math.abs((m.kill ?? m.last) - t) < 3500 && (!best || Math.abs((m.kill ?? m.last) - t) < Math.abs((best.kill ?? best.last) - t))) best = m;
    if (best) { covered.add(t); if (best.kill == null) best.kill = t; }
  }
  const fight = clamp((d.pace && d.pace.avg_fight_seconds) || 8, 3, 25) * 1000;
  for (const t of kills) {
    if (covered.has(t) || encounters.some(e => Math.abs(e.end - t) < 4000)) continue;
    monsters.push({ guid: null, name: null, first: t - fight, last: t, kill: t, by: null, total: 0, dmg: [], logged: false });
  }
  for (const m of monsters) {
    if (m.kill != null) { const n = named(m.kill); if (n) { m.name = m.name || n.monster_name; m.by = m.by || n.killed_by; } m.at = placed(m.kill); }
    m.spawn = m.first - 800; m.end = m.kill ?? m.last + 2500;
  }
  for (const e of encounters) {                                   // the boss: the enemy hit hardest in the fight
    const inside = monsters.filter(m => m.logged && m.first < e.end && m.last > e.start);
    let boss = inside.find(m => m.name === e.name) || inside.sort((a, b) => b.total - a.total)[0];
    if (!boss) { boss = { guid: null, name: e.name, first: e.start, last: e.end, kill: e.success ? e.end : null, total: 0, dmg: [], logged: false, spawn: e.start, end: e.end + (e.success ? 0 : 2500) }; monsters.push(boss); }
    boss.boss = true; boss.enc = e; boss.title = e.name;
    if (e.success && boss.kill == null) { boss.kill = e.end; boss.end = e.end; }
    if (!e.success) boss.fled = true;
  }
  monsters.sort((a, b) => a.spawn - b.spawn);

  // ---- events for the ticker and the bursts
  const events = [];
  for (const m of monsters) {
    if (m.kill != null) events.push({ t: m.kill, kind: m.boss ? "boss" : "kill", m,
      text: m.boss ? `擊敗首領 ${m.title}！` : `擊殺 ${m.name || "怪物"}` + (m.by && m.by !== me ? `（${N(m.by)}）` : "") });
    else if (m.boss) events.push({ t: m.end, kind: "wipe", m, text: `首領戰失敗：${m.title}` });
    if (m.boss) events.push({ t: m.enc.start, kind: "pull", m, text: `首領戰開始：${m.title}` });
  }
  for (const h of hits) if (h.way === "death" && !h.pet && inParty.has(h.who)) events.push({ t: h.t, kind: "death", who: h.who, text: `${N(h.who)} 倒下了` });
  for (const h of d.highlights) {
    if (!h.timestamp) continue;
    const t = T(h.timestamp);
    if (h.type === "LEVEL_UP") events.push({ t, kind: "level", who: me, text: `${N(me)} 升到 ${h.level} 級！` });
    else if (h.type === "GROUP_MEMBER_LEVEL_UP") events.push({ t, kind: "level", who: h.name, text: `${N(h.name)} 升到 ${h.level} 級！` });
    else if (h.type === "PLAYER_DIED" || h.type === "PARTY_MEMBER_DIED") {
      const who = h.type === "PLAYER_DIED" ? me : h.name;
      if (!events.some(e => e.kind === "death" && e.who === who && Math.abs(e.t - t) < 10000)) events.push({ t, kind: "death", who, text: `${N(who)} 倒下了` });
    }
    else if (h.type === "INSTANCE_ENTERED") events.push({ t, kind: "enter", text: `進入 ${h.name || "副本"}`, name: h.name });
    else if (h.type === "ACHIEVEMENT_EARNED") events.push({ t, kind: "achieve", who: me, text: `成就：${h.name || ""}` });
  }
  for (const e of d.timeline) {
    const t = T(e.timestamp);
    if (e.type === "ITEM_LOOTED" && e.item_name) events.push({ t, kind: "loot", who: me, text: `拾取 ${e.item_name}` + (e.count > 1 ? ` ×${e.count}` : "") });
    else if (e.type === "QUEST_COMPLETED") events.push({ t, kind: "quest", who: me, text: `完成任務 ${e.quest_name || ""}` });
  }
  for (const p of roster) {                                        // comings and goings, from the spans
    for (const [a, b] of p.spans) {
      if (isFinite(a) && a > S + 1000) events.push({ t: a, kind: "join", who: p.name, text: `${N(p.name)} 加入隊伍` });
      if (isFinite(b)) events.push({ t: b, kind: "leave", who: p.name, text: `${N(p.name)} 離開隊伍` });
    }
  }
  events.sort((a, b) => a.t - b.t);

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
  const busy = (a, b) => events.some(e => e.t > a && e.t <= b && e.kind !== "join" && e.kind !== "leave") || monsters.some(m => m.end > a && m.spawn < b);
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
  let cursor = Math.min(S, own.length ? own[0].t : S, events.length ? events[0].t : S, monsters.length ? monsters[0].spawn : S);
  for (const r of runs) {
    const a = r.pts[0].t, b = r.pts[r.pts.length - 1].t;
    const last = scenes[scenes.length - 1];
    if (a - cursor > 60000 && busy(cursor, a)) virtual(cursor, a);
    else if (last && a - cursor <= 60000) last.t1 = a;
    const prev = scenes[scenes.length - 1];
    if (prev && prev.kind === "field" && prev.map === r.map && !busy(prev.t1, a)) { prev.t1 = b; prev.pts.push(...r.pts); }
    else scenes.push({ kind: "field", map: r.map, t0: scenes.length || a - cursor > 60000 ? a : cursor, t1: b, pts: r.pts.slice() });
    cursor = b;
  }
  const end = Math.max(E, events.length ? events[events.length - 1].t : E);
  if (end - cursor > 60000 && busy(cursor, end)) virtual(cursor, end);
  else if (scenes.length) scenes[scenes.length - 1].t1 = Math.max(scenes[scenes.length - 1].t1, Math.min(end, cursor + 60000));
  for (const s of scenes) if (s.t1 - s.t0 < 1000) s.t1 = s.t0 + 1000;

  // ---- replay time, warped: walking fast, fighting slower, a boss slowest
  const real = scenes.reduce((n, s) => n + s.t1 - s.t0, 0) || 1;
  const target = clamp(real / 60000 * 2, 40, 150);
  const fights = monsters.filter(m => !m.boss).map(m => [m.spawn, m.end]);
  const bosses = monsters.filter(m => m.boss).map(m => [m.enc ? m.enc.start : m.spawn, m.end]);
  for (const s of scenes) {
    s.base = Math.max(6, target * (s.t1 - s.t0) / real) / ((s.t1 - s.t0) / 1000);     // replay s per real s
    s.step = Math.max(250, (s.t1 - s.t0) / 6000); s.n = Math.ceil((s.t1 - s.t0) / s.step);
    s.cat = new Uint8Array(s.n);
    const mark = ([a, b], c) => { for (let i = Math.max(0, Math.floor((a - s.t0) / s.step)); i < Math.min(s.n, Math.ceil((b - s.t0) / s.step)); i++) s.cat[i] = Math.max(s.cat[i], c); };
    for (const w of fights) mark(w, 1);
    for (const w of bosses) mark(w, 2);
  }
  let fightRate = 1 / 6;                                           // six real seconds a replay second
  const bossRate = ([a, b]) => clamp(15 / ((b - a) / 1000), 1 / 4, 1);
  const tally = () => { let walk = 0, fightReal = 0, boss = 0;
    for (const s of scenes) for (let i = 0; i < s.n; i++) { const sec = s.step / 1000; if (s.cat[i] === 0) walk += sec * s.base; else if (s.cat[i] === 1) fightReal += sec; }
    for (const w of bosses) boss += (w[1] - w[0]) / 1000 * bossRate(w);
    return { walk, fightReal, boss }; };
  const tl = tally();
  if (tl.walk + tl.fightReal * fightRate + tl.boss > 420) fightRate = Math.max(1 / 40, (420 - tl.walk - tl.boss) / Math.max(1, tl.fightReal));
  let off = 0;
  for (const s of scenes) {
    s.rate = new Float32Array(s.n); s.cum = new Float32Array(s.n + 1);
    for (let i = 0; i < s.n; i++) {
      const t = s.t0 + (i + 0.5) * s.step;
      const bw = s.cat[i] === 2 ? bosses.find(w => t >= w[0] && t <= w[1]) : null;
      s.rate[i] = s.cat[i] === 2 && bw ? bossRate(bw) : s.cat[i] === 1 ? Math.max(s.base, fightRate) : s.base;
      s.cum[i + 1] = s.cum[i] + s.rate[i] * s.step / 1000;
    }
    if (s.cum[s.n] < 5) { const k = 5 / s.cum[s.n]; for (let i = 0; i < s.n; i++) { s.rate[i] *= k; s.cum[i + 1] = s.cum[i] + s.rate[i] * s.step / 1000; } }
    s.dur = s.cum[s.n]; s.off = off; off += s.dur;
  }
  const sceneOf = t => scenes.find(s => t >= s.t0 - 500 && t <= s.t1 + 500);
  const toReplay = (s, t) => { const i = clamp(Math.floor((t - s.t0) / s.step), 0, s.n - 1); return s.off + s.cum[i] + clamp((t - s.t0 - i * s.step) / 1000, 0, s.step / 1000) * s.rate[i]; };
  const replayOf = t => { const s = sceneOf(t); return s ? toReplay(s, t) : null; };
  const realOf = (s, g) => { const v = clamp(g - s.off, 0, s.dur); let lo = 0, hi = s.n - 1; while (lo < hi) { const m = (lo + hi + 1) >> 1; if (s.cum[m] <= v) lo = m; else hi = m - 1; } return s.t0 + lo * s.step + (v - s.cum[lo]) / s.rate[lo] * 1000; };
  const catOf = (s, g) => { const t = realOf(s, g); return s.cat[clamp(Math.floor((t - s.t0) / s.step), 0, s.n - 1)]; };

  for (const e of events) e.r = replayOf(e.t);
  for (const m of monsters) {
    m.scene = sceneOf(m.kill ?? m.end) || sceneOf(m.spawn);
    if (!m.scene) continue;
    m.endR = toReplay(m.scene, m.end); m.killR = m.kill != null ? toReplay(m.scene, m.kill) : null;
    m.spawnR = Math.min(m.endR - 0.6, toReplay(m.scene, Math.max(m.spawn, m.scene.t0)));
    m.peak = m.kill != null ? Math.max(1, m.total) : Math.max(1, m.total * 1.3);
  }
  const H = hits.filter(h => h.way !== "death").map(h => { h.r = replayOf(h.t); return h; }).filter(h => h.r != null);
  for (const p of roster) {
    p.down = events.filter(e => e.kind === "death" && e.who === p.name && e.r != null).map(e => [e.r, Math.max(e.r + 2.5, replayOf(e.t + 30000) || 0)]);
    p.pet = hits.some(h => h.pet && h.who === p.name);
  }

  // damage each minute, for the bars and when there is no combat log
  const minutes = (d.damage_minutes || []).map(([ts, m]) => [T(ts), m]);
  let peak = 1;
  for (const [, m] of minutes) for (const v of Object.values(m)) peak = Math.max(peak, v);
  const fury = (name, t) => {
    const i = lowerBound(minutes, t + 30000, x => x[0]) - 1;
    if (i < 0 || t - minutes[i][0] > 90000) return 0;
    return (minutes[i][1][name] || 0) / peak;
  };
  return { roster, scenes, events: events.filter(e => e.r != null), monsters: monsters.filter(m => m.scene), hits: H, hasHits,
    total: off, fury, hasMeter: minutes.length > 0, realOf, catOf, fightRate,
    mates: Object.fromEntries(d.maps.map(m => [m.map_id, m.members || {}])),
    mapInfo: Object.fromEntries(d.maps.map(m => [m.map_id, m])) };
}

// ------------------------------------------------------------------ textures drawn on a canvas
function canvasTexture(w, h, draw) {
  const c = document.createElement("canvas"); c.width = w; c.height = h;
  draw(c.getContext("2d"), w, h);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
}
const FONT = '-apple-system, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif';
function textSprite(text, { color = "#fff", size = 30, scale = 0.011, bold = true } = {}) {
  const font = `${bold ? "bold " : ""}${size}px ${FONT}`;
  const probe = document.createElement("canvas").getContext("2d"); probe.font = font;
  const w = Math.ceil(probe.measureText(text).width) + 16, h = size + 16;
  const tex = canvasTexture(w, h, c => { c.font = font; c.textAlign = "center"; c.textBaseline = "middle";
    c.lineWidth = 6; c.strokeStyle = "rgba(0,0,0,.85)"; c.strokeText(text, w / 2, h / 2); c.fillStyle = color; c.fillText(text, w / 2, h / 2); });
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false, depthTest: false }));
  s.scale.set(w * scale, h * scale, 1); s.renderOrder = 10; s.userData.base = s.scale.clone();
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
.rp{position:fixed;inset:0;z-index:1000;background:#0d0b09;color:#f3ead9;font:14px/1.45 ${FONT};user-select:none;overflow:hidden}
.rp canvas{position:absolute;inset:0;width:100%;height:100%;aspect-ratio:auto;border:0;border-radius:0;touch-action:none;cursor:grab}
.rp canvas:active{cursor:grabbing}
.rp-top{position:absolute;left:16px;right:16px;top:12px;display:flex;align-items:flex-start;gap:12px;pointer-events:none;text-shadow:0 1px 3px #000}
.rp-top h3{margin:0;font-size:19px;line-height:1.3}.rp-top small{display:block;color:#cbbfa8;font-size:13px}
.rp-x,.rp-view{pointer-events:auto;height:38px;border-radius:99px;border:1px solid #ffffff33;background:#00000080;color:#fff;font:inherit;cursor:pointer}
.rp-x{margin-left:auto;width:38px;font-size:18px}.rp-view{padding:0 14px;font-size:13px;font-weight:600;margin-left:auto}
.rp-view+.rp-x{margin-left:0}
.rp-x:hover,.rp-view:hover{background:#000000b0}
.rp-slow{position:absolute;left:50%;top:14px;transform:translateX(-50%);padding:4px 14px;border-radius:99px;background:#00000090;border:1px solid #ffffff30;font-size:13px;font-weight:600;letter-spacing:.04em;opacity:0;transition:opacity .4s;pointer-events:none;white-space:nowrap}
.rp-slow.on{opacity:1}.rp-slow.boss{background:#5a2a7acc;border-color:#c040ff}
.rp-boss{position:absolute;left:50%;top:46px;transform:translateX(-50%);width:min(420px,calc(100% - 140px));opacity:0;transition:opacity .4s;pointer-events:none;text-shadow:0 1px 3px #000}
.rp-boss.on{opacity:1}.rp-boss>div:first-child{display:flex;justify-content:space-between;font-size:13px;margin-bottom:3px}.rp-boss b{color:#e6a8ff}
.rp-boss-bar{height:10px;border-radius:5px;background:#1a1612e6;border:1px solid #c040ff99;overflow:hidden}.rp-boss-bar i{display:block;height:100%;background:linear-gradient(90deg,#8a2be2,#d36bff);transition:width .2s}
.rp-side{position:absolute;right:16px;top:64px;width:190px;display:grid;gap:4px;pointer-events:none}
.rp-m{display:grid;grid-template-columns:10px 1fr;gap:2px 7px;align-items:center;background:#0000006b;border-radius:6px;padding:4px 8px;font-size:12px;transition:opacity .4s}
.rp-m i{width:10px;height:10px;border-radius:50%}.rp-m span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rp-m b{grid-column:2;height:4px;border-radius:2px;background:#ffffff1f;overflow:hidden}.rp-m b u{display:block;height:100%;width:0;transition:width .25s}
.rp-m.down{opacity:.5}.rp-m.down span::after{content:"　☠"}
.rp-m.away{opacity:.22}.rp-m.away span::after{content:"　不在隊伍"}
.rp-tick{position:absolute;left:16px;bottom:124px;display:grid;gap:4px;max-width:min(420px,calc(100% - 32px));pointer-events:none}
.rp-tick div{background:#0000007a;border-radius:6px;padding:3px 10px;font-size:13px;text-shadow:0 1px 2px #000;animation:rpin .35s ease-out}
.rp-tick time{color:#cbbfa8;font-size:12px;margin-right:8px;font-variant-numeric:tabular-nums}
@keyframes rpin{from{opacity:0;transform:translateX(-12px)}}
.rp-bar{position:absolute;left:0;right:0;bottom:0;padding:28px 16px 14px;background:linear-gradient(transparent,#000000c8)}
.rp-row{display:flex;align-items:center;gap:10px}
.rp-track{position:relative;flex:1;min-width:100px;display:flex;align-items:center}
.rp-track input[type=range]{width:100%;margin:0;accent-color:#e0964a;position:relative;z-index:1}
.rp-marks{position:absolute;left:8px;right:8px;top:-9px;height:7px;pointer-events:none}
.rp-marks i{position:absolute;top:0;width:3px;height:7px;border-radius:2px;transform:translateX(-50%)}
.rp-marks i.scene{background:#ffffff55;width:1px;height:20px;top:2px}
.rp-btn{font:inherit;color:#fff;background:#e0964a;border:0;border-radius:99px;padding:6px 16px;cursor:pointer;font-weight:600;white-space:nowrap}
.rp-row select{font:inherit;color:#fff;background:#00000080;border:1px solid #ffffff40;border-radius:6px;padding:4px 6px}
.rp-chips{display:flex;gap:6px;overflow-x:auto;margin-bottom:12px;scrollbar-width:none}
.rp-chips button{font:inherit;font-size:12px;color:#f3ead9;background:#00000080;border:1px solid #ffffff30;border-radius:99px;padding:3px 11px;cursor:pointer;white-space:nowrap}
.rp-chips button.on{background:#e0964a;border-color:#e0964a;color:#1c1710}
.rp-card{position:absolute;left:50%;top:36%;transform:translate(-50%,-50%);text-align:center;pointer-events:none;opacity:0;transition:opacity .6s;text-shadow:0 2px 12px #000}
.rp-card b{display:block;font-size:clamp(24px,5vw,42px);letter-spacing:.06em}.rp-card small{font-size:14px;color:#e8d9bd}
.rp-flash{position:absolute;inset:0;background:#fff3d0;opacity:0;pointer-events:none;transition:opacity .5s}
.rp-wait{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);color:#cbbfa8}
.rp-help{color:#cbbfa8;font-size:12px;white-space:nowrap}
.rp-end{position:absolute;left:50%;top:46%;transform:translate(-50%,-50%) scale(.96);width:min(440px,calc(100% - 32px));background:#15110dee;border:1px solid #e0964a66;border-radius:14px;padding:20px 22px;opacity:0;pointer-events:none;transition:opacity .5s,transform .5s;box-shadow:0 20px 60px #000a}
.rp-end.on{opacity:1;pointer-events:auto;transform:translate(-50%,-50%)}
.rp-end h4{margin:0 0 2px;font-size:20px}.rp-end .sub{color:#cbbfa8;font-size:13px;margin-bottom:14px}
.rp-end .tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.rp-end .tiles div{background:#ffffff0d;border-radius:8px;padding:8px 10px}.rp-end .tiles b{display:block;font-size:20px}.rp-end .tiles span{font-size:12px;color:#cbbfa8}
.rp-end ol{list-style:none;margin:0 0 12px;padding:0;display:grid;gap:5px}
.rp-end li{display:grid;grid-template-columns:76px 1fr auto;gap:8px;align-items:center;font-size:13px}
.rp-end li span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rp-end li u{display:block;height:8px;border-radius:4px}
.rp-end .best{font-size:13px;color:#e8d9bd;margin-bottom:14px}
.rp-end .acts{display:flex;gap:8px;justify-content:flex-end}
.rp-end .ghost{background:transparent;border:1px solid #ffffff40}
@media (max-width:640px){.rp-side{top:auto;bottom:124px;right:16px;width:130px}.rp-tick{max-width:calc(100% - 170px)}.rp-help{display:none}.rp-view{padding:0 10px}}
@media (prefers-reduced-motion:reduce){.rp-tick div{animation:none}}`;
  document.head.append(s);
}

const SKIN = [0xe8c19c, 0xd9a37a, 0xb98159, 0x8d5a3b, 0xf0d0b0];
const HAIR = [0x2a2018, 0x5a3a20, 0xc9a050, 0x8a2a1a, 0xdcdcdc, 0x1a1a24];
const FOE_ARMOR = [0x9a3a30, 0x7a5a34, 0x5a7a3a, 0x5b5080, 0x8a6a4a, 0x40628a];
const FOE_SKIN = [0xc79a74, 0x7a9a5a, 0x8a8a8a, 0x9a7a5a, 0x6a8aa0];
const BEAST_FUR = [0x6b5034, 0x4a4440, 0x2e2a28, 0x8a6a44, 0x9a9590];
const BLOB_COLOR = [0x3fb8a0, 0x6abf4a, 0x8a5ad8, 0x4a8ad8];
const VIEW_KEY = "rp-view";

export async function openReplay(d, ctx) {
  style();
  const h = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  // every hit of the session, when the combat log was recorded
  let hitsJson = ctx.hits || null;
  const logged = (d.skills || []).length || (d.meter && (d.meter.members || []).length);
  if (!hitsJson && logged && ctx.api) {
    try {
      const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 15000);
      const r = await fetch(`${ctx.api}/hits.json`, { signal: ctl.signal });
      clearTimeout(timer);
      if (r.ok) hitsJson = await r.json();
    } catch (e) { /* replay without it */ }
  }
  const M = model(d, ctx, hitsJson), N = ctx.name;
  if (!M.scenes.length) return false;
  let view = "chase";
  try { view = localStorage.getItem(VIEW_KEY) === "map" ? "map" : "chase"; } catch (e) { /* no storage: the default */ }

  const root = h("div", "rp"); root.setAttribute("role", "dialog"); root.setAttribute("aria-label", "3D 回放");
  const top = h("div", "rp-top"), titleBox = h("div"), title = h("h3"), sub = h("small"), close = h("button", "rp-x", "✕");
  const viewBtn = h("button", "rp-view"); viewBtn.type = "button";
  close.type = "button"; close.setAttribute("aria-label", "關閉回放");
  titleBox.append(title, sub); top.append(titleBox, viewBtn, close);
  const slow = h("div", "rp-slow");
  const bossBox = h("div", "rp-boss"), bossName = h("b"), bossPct = h("span"), bossTrack = h("div", "rp-boss-bar"), bossFill = h("i");
  bossTrack.append(bossFill); const bossHead = h("div"); bossHead.append(bossName, bossPct); bossBox.append(bossHead, bossTrack);
  const side = h("div", "rp-side"), tick = h("div", "rp-tick"), card = h("div", "rp-card"), flash = h("div", "rp-flash"), wait = h("div", "rp-wait", "載入場景…");
  const bar = h("div", "rp-bar"), chips = h("div", "rp-chips"), row = h("div", "rp-row");
  const play = h("button", "rp-btn", "▶ 播放"); play.type = "button";
  const track = h("div", "rp-track"), marks = h("div", "rp-marks");
  const slider = h("input"); Object.assign(slider, { type: "range", min: 0, max: 1000, value: 0 }); slider.setAttribute("aria-label", "回放時間");
  track.append(marks, slider);
  const speed = h("select"); for (const v of [0.25, 0.5, 1, 2, 4, 8]) { const o = h("option", null, v + "×"); o.value = v; if (v === 1) o.selected = true; speed.append(o); }
  speed.setAttribute("aria-label", "播放速度");
  const help = h("span", "rp-help");
  row.append(play, track, speed, help);
  if (M.scenes.length > 1) bar.append(chips);
  bar.append(row);
  const endCard = h("div", "rp-end");
  root.append(top, slow, bossBox, side, tick, card, flash, wait, endCard, bar);
  document.body.append(root);
  const prevOverflow = document.body.style.overflow; document.body.style.overflow = "hidden";

  const markAt = (r, cls, color, tip) => { const i = h("i", cls); i.style.left = (r / M.total * 100) + "%"; if (color) i.style.background = color; if (tip) i.title = tip; marks.append(i); };
  M.scenes.forEach((s, k) => { if (k) markAt(s.off, "scene"); });
  for (const e of M.events) {
    if (e.kind === "boss" || e.kind === "wipe") markAt(e.r, null, "#c040ff", e.text);
    else if (e.kind === "death") markAt(e.r, null, "#c9ced6", e.text);
    else if (e.kind === "level") markAt(e.r, null, "#ffd76a", e.text);
  }

  // ---- three.js basics
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  root.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(48, 1, 0.1, 2400);
  const hemi = new THREE.HemisphereLight(0xfff2dd, 0x2a2018, 1.1), sun = new THREE.DirectionalLight(0xfff0d6, 1.6);
  sun.position.set(30, 60, 20);
  sun.shadow.mapSize.set(2048, 2048); Object.assign(sun.shadow.camera, { left: -32, right: 32, top: 32, bottom: -32, near: 1, far: 260 }); sun.shadow.bias = -0.0006; sun.shadow.normalBias = 0.03;
  const torch = new THREE.PointLight(0xffa04a, 0, 34, 1.6);
  const lamps = Array.from({ length: 6 }, () => { const l = new THREE.PointLight(0xff9a40, 0, 16, 1.4); scene.add(l); return l; });
  scene.add(hemi, sun, sun.target, torch);
  const GLOW = S.glowTex();
  const sky = S.makeSky(); const windTime = { value: 0 };

  // shared parts
  const G = { ring: new THREE.RingGeometry(0.55, 0.72, 40), disc: new THREE.CircleGeometry(0.7, 24), bolt: new THREE.SphereGeometry(0.13, 10, 8),
    beam: new THREE.CylinderGeometry(0.9, 0.9, 14, 24, 1, true), arrow: new THREE.CylinderGeometry(0.025, 0.025, 0.8, 5).rotateX(Math.PI / 2),
    slash: new THREE.TorusGeometry(0.8, 0.045, 4, 24, Math.PI * 0.85), pet: new THREE.CapsuleGeometry(0.2, 0.45, 3, 8).rotateX(Math.PI / 2),
    petHead: new THREE.SphereGeometry(0.16, 12, 10), crown: new THREE.ConeGeometry(0.07, 0.28, 5) };
  const shared = new Set(Object.values(G));
  const shadowMat = new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.32, depthWrite: false });
  const GREY = new THREE.Color(0x555555);

  const heroes = M.roster.map((p, i) => {
    const col = new THREE.Color(p.color), hs = hash(p.name);
    const rig = S.humanoid({ armor: col.clone(), trim: col.clone().multiplyScalar(0.5), skin: SKIN[hs % SKIN.length], hair: HAIR[(hs >> 4) % HAIR.length],
      weapon: WEAPON[p.cls] || "sword", orb: ORB[p.cls], shield: p.cls === "WARRIOR" || p.cls === "PALADIN", cape: col.clone().multiplyScalar(0.38) });
    const g = new THREE.Group();
    const shadow = new THREE.Mesh(G.disc, shadowMat); shadow.rotation.x = -Math.PI / 2; shadow.position.y = 0.03;
    const ring = new THREE.Mesh(G.ring, new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: p.owner ? 0.95 : 0.6, side: THREE.DoubleSide, depthWrite: false }));
    ring.rotation.x = -Math.PI / 2; ring.position.y = 0.05; if (p.owner) ring.scale.setScalar(1.25);
    const label = textSprite(N(p.name) + (p.owner ? " ★" : ""), { color: p.color, size: 26, scale: 0.011 }); label.position.y = 2.4;
    g.add(rig.root, shadow, ring, label);
    let pet = null;
    if (p.pet) {
      pet = new THREE.Group();
      const pm = new THREE.Mesh(G.pet, new THREE.MeshStandardMaterial({ color: col.clone().multiplyScalar(0.6), roughness: 0.7, transparent: true }));
      pm.position.y = 0.35; pm.castShadow = true; const ph = new THREE.Mesh(G.petHead, pm.material); ph.position.set(0, 0.55, 0.32);
      const ps = new THREE.Mesh(G.disc, shadowMat); ps.scale.setScalar(0.6); ps.rotation.x = -Math.PI / 2; ps.position.y = 0.03;
      pet.add(ps, pm, ph); scene.add(pet);
    }
    scene.add(g);
    return { ...p, i, g, rig, base: col.clone(), ring, label, pet, last: new THREE.Vector3(), charge: Math.random(), act: null,
      cool: 0, pend: null, foe: null, ui: null, shown: 0, walk: 0, phase: 0, face: 0 };
  });
  const heroOf = new Map(heroes.map(x => [x.name, x]));

  for (const hr of heroes) {
    const m = h("div", "rp-m"), dot = h("i"), name = h("span", null, N(hr.name)), b = h("b"), u = h("u");
    dot.style.background = hr.color; u.style.background = hr.color; b.append(u);
    m.append(dot, name); if (M.hasMeter) m.append(h("i"), b);
    side.append(m); hr.ui = { m, u };
  }
  if (heroes.length > 12) side.style.display = "none";

  // ---- per-scene world, rebuilt when the scene or the view changes
  let world = null, building = null, ticket = 0;
  const textures = new Map(), loaded = new Set();
  const loadImage = id => {
    const info = M.mapInfo[id];
    if (!info || !info.has_image) return Promise.resolve(null);
    if (!textures.has(id)) textures.set(id, new Promise(res => new THREE.TextureLoader().load(`${ctx.api}/map/${id}`, t => { t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = renderer.capabilities.getMaxAnisotropy(); loaded.add(t); res(t); }, undefined, () => res(null))));
    return textures.get(id);
  };
  const dispose = o => o.traverse(x => {
    if (x.geometry && !shared.has(x.geometry)) x.geometry.dispose();
    if (x.material && x.material !== shadowMat) for (const m of [].concat(x.material)) { if (m.map && m.map !== GLOW && !loaded.has(m.map)) m.map.dispose(); m.dispose(); }
  });
  const disposeWorld = w => { if (!w) return; scene.remove(w.group); dispose(w.group); if (w.grass) { scene.remove(w.grass.group); w.grass.dispose(); } for (const mo of w.mobs.values()) releaseMob(mo); };

  const MOTES = 260, motePos = new Float32Array(MOTES * 3), moteR = rng(7);
  for (let k = 0; k < MOTES * 3; k++) motePos[k] = (moteR() - 0.5) * (k % 3 === 1 ? 8 : 44);
  const moteGeo = new THREE.BufferGeometry(); moteGeo.setAttribute("position", new THREE.BufferAttribute(motePos, 3));
  const motes = new THREE.Points(moteGeo, new THREE.PointsMaterial({ map: GLOW, color: 0xffd9a0, size: 0.16, transparent: true, opacity: 0.6, depthWrite: false, blending: THREE.AdditiveBlending }));
  scene.add(motes);

  // a made-up walk for a stretch without positions: the party advances while nothing is up, and stops to fight
  function walkway(sc, r) {
    const w = {};
    const mobs = M.monsters.filter(m => m.scene === sc);
    const N_ = 900, cum = new Float32Array(N_ + 1);
    for (let k = 1; k <= N_; k++) { const t = sc.t0 + (sc.t1 - sc.t0) * k / N_; cum[k] = cum[k - 1] + (mobs.some(m => t >= m.spawn && t <= m.end) ? 0 : 1); }
    const L = clamp(24 + mobs.length * 5, 36, 700);
    const path = [{ x: 0, z: 0 }]; let ang = -Math.PI / 2, len = 0, turn = r() < 0.5 ? 1 : -1;
    while (len < L + 30) { const seg = 16 + r() * 14; const p = path[path.length - 1]; path.push({ x: p.x + Math.cos(ang) * seg, z: p.z + Math.sin(ang) * seg }); len += seg; ang += turn * (Math.PI / 2) * (0.5 + r() * 0.5); turn = -turn; }
    const segs = []; let acc = 0;
    for (let k = 1; k < path.length; k++) { const a = path[k - 1], b = path[k], l = Math.hypot(b.x - a.x, b.z - a.z); segs.push({ a, b, l, s: acc }); acc += l; }
    w.segs = segs;
    w.pathAt = s => {
      s = clamp(s, 0, acc - 0.01);
      const g = segs.find(x => s >= x.s && s < x.s + x.l) || segs[segs.length - 1];
      const f = (s - g.s) / g.l, dx = (g.b.x - g.a.x) / g.l, dz = (g.b.z - g.a.z) / g.l;
      return { x: lerp(g.a.x, g.b.x, f), z: lerp(g.a.z, g.b.z, f), dx, dz };
    };
    w.progress = t => { const k = clamp((t - sc.t0) / (sc.t1 - sc.t0), 0, 1) * N_; const i = Math.floor(k); const c = lerp(cum[i], cum[Math.min(N_, i + 1)], k - i); return 6 + (cum[N_] ? c / cum[N_] : k / N_) * L; };
    w.spot = m => {
      if (!m._spot || m._spotOf !== w) {
        const j = rng(hash(m.guid || m.kill || m.spawn));
        const p = w.pathAt(w.progress(m.spawn) + (m.boss ? 8 : 3.8 + j() * 1.6)); const side = m.boss ? 0 : (j() - 0.5) * 4.5;
        m._spot = [p.x - p.dz * side, p.z + p.dx * side]; m._spotOf = w;
      }
      return m._spot;
    };
    w.rooms = mobs.filter(m => m.boss).map(m => w.pathAt(w.progress(m.spawn) + 8));
    return w;
  }

  async function build(sc) {
    const my = ++ticket;
    const group = new THREE.Group();
    const seed = hash(sc.kind === "field" ? sc.map : sc.name + sc.t0), r = rng(seed);
    let w = { sc, group, mobs: new Map(), view, ground: () => 0 };
    const chase = view === "chase";
    if (sc.kind === "field") {
      const tex = await loadImage(sc.map);
      if (my !== ticket) return null;
      const aspect = tex && tex.image ? tex.image.height / tex.image.width : 2 / 3;
      const mates = {};
      for (const [name, list] of Object.entries(M.mates[sc.map] || {})) {
        const pts = list.map(p => ({ t: T(p[0]), x: p[1], y: p[2] })).filter(p => p.t >= sc.t0 - 60000 && p.t <= sc.t1 + 60000 && typeof p.y === "number");
        if (pts.length) mates[name] = pts;
      }
      const all = sc.pts.concat(...Object.values(mates));
      const xs = all.map(p => p.x), ys = all.map(p => p.y);
      const ext = Math.max(Math.max(...xs) - Math.min(...xs), (Math.max(...ys) - Math.min(...ys)) * aspect);
      const span = clamp(ext * 1.3, 8, 100);
      const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2;
      // the map view fits the walk on screen; the third-person view keeps the world about life size
      const A = chase ? Math.min(1000, 1500 / Math.max(0.01, ext / 100)) : clamp(5000 / span, 50, 600);
      const X = x => (x - cx) / 100 * A, Z = y => (y - cy) / 100 * A * aspect;
      w.track = sc.pts.map(p => ({ t: p.t, X: X(p.x), Z: Z(p.y) }));
      w.mates = Object.fromEntries(Object.entries(mates).map(([n, pts]) => [n, pts.map(p => ({ t: p.t, X: X(p.x), Z: Z(p.y) }))]));
      w.place = k => k ? [X(k.x), Z(k.y)] : null;
      if (chase) {
        const sample = S.imageSampler(tex && tex.image);
        const xs2 = w.track.map(p => p.X).concat(...Object.values(w.mates).map(l => l.map(p => p.X))), zs2 = w.track.map(p => p.Z).concat(...Object.values(w.mates).map(l => l.map(p => p.Z)));
        const size = clamp(Math.max(Math.max(...xs2) - Math.min(...xs2), Math.max(...zs2) - Math.min(...zs2)) + 260, 260, 1800);
        const tc = { x: (Math.max(...xs2) + Math.min(...xs2)) / 2, z: (Math.max(...zs2) + Math.min(...zs2)) / 2 };
        outdoors(w, { cx: tc.x, cz: tc.z, size, seed, avoid: w.track.map(p => [p.X, p.Z]),
          sample: sample ? (x, z) => sample((cx + x / A * 100) / 100, (cy + z / (A * aspect) * 100) / 100) : null });
      } else {
        if (tex) {
          const plane = new THREE.Mesh(new THREE.PlaneGeometry(A, A * aspect), new THREE.MeshStandardMaterial({ map: tex, roughness: 1 }));
          plane.rotation.x = -Math.PI / 2; plane.position.set(X(50), 0, Z(50));
          group.add(plane);
          const under = new THREE.Mesh(new THREE.PlaneGeometry(A * 6, A * 6), new THREE.MeshBasicMaterial({ color: 0x15120e }));
          under.rotation.x = -Math.PI / 2; under.position.y = -0.05; group.add(under);
        } else meadow(group, r, seed, w.track);
        ribbon(group, w.track, 0.32, null, w);
        for (const [n, pts] of Object.entries(w.mates)) { const hr = heroOf.get(n); if (hr) ribbon(group, pts, 0.14, hr.color, null); }
        flatLight(0x15120e, 60, 220, 1.1, 1.6, 0, 0xfff3b0);
      }
    } else {
      Object.assign(w, walkway(sc, r));
      const mine = /礦|坑/.test(sc.name);
      if (chase && sc.theme === "dungeon") {
        const tn = S.tunnel(w.segs, w.rooms, seed, mine);
        group.add(tn.group); w.tunnel = tn;
        scene.fog = new THREE.Fog(0x0a0706, 10, 48); scene.background = new THREE.Color(0x0a0706);
        hemi.color.set(0xffd6a8); hemi.groundColor.set(0x1a120c); hemi.intensity = 0.32; sun.intensity = 0; sun.castShadow = false; torch.intensity = 26; torch.distance = 18;
        motes.material.color.set(0xffc27a);
      } else if (chase) {
        const pts = []; for (const g of w.segs) pts.push([g.a.x, g.a.z]); pts.push([w.segs[w.segs.length - 1].b.x, w.segs[w.segs.length - 1].b.z]);
        const xs = pts.map(p => p[0]), zs = pts.map(p => p[1]);
        outdoors(w, { cx: (Math.max(...xs) + Math.min(...xs)) / 2, cz: (Math.max(...zs) + Math.min(...zs)) / 2, size: clamp(Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...zs) - Math.min(...zs)) + 260, 260, 1400),
          seed, avoid: pts, road: true, lush: 0.35, sample: null });
      } else if (sc.theme === "dungeon") dungeon(group, w.segs, w.rooms, r, seed); else forestRoad(group, w.segs, r, seed);
    }
    for (const l of lamps) l.intensity = 0;
    return w;

    function outdoors(w, o) {
      const tr = S.terrain(o);
      const trees = S.scatter(tr, o);
      group.add(tr.group, trees);
      w.ground = tr.ground; w.terrain = tr; w.trees = trees;
      w.grass = S.grass(tr, windTime); scene.add(w.grass.group);
      w.outdoors = true;
      scene.background = null; sun.castShadow = true; torch.intensity = 0;
    }
  }
  function flatLight(fog, near, far, hemiI, sunI, torchI, mote) {
    scene.fog = new THREE.Fog(fog, near, far); scene.background = new THREE.Color(fog);
    hemi.color.set(0xfff2dd); hemi.groundColor.set(0x2a2018); hemi.intensity = hemiI; sun.intensity = sunI; sun.color.set(0xfff0d6); sun.castShadow = false;
    sun.position.set(30, 60, 20); sun.target.position.set(0, 0, 0); torch.intensity = torchI; torch.distance = 34; motes.material.color.set(mote);
  }

  function ribbon(group, pts, width, color, w) {
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
    if (w) w.trail = { geo, pts }; else (group.userData.mates = group.userData.mates || []).push({ geo, pts });
  }
  function grassPlane(group, seed, cx = 0, cz = 0) {
    const tex = grassTex(seed); tex.wrapS = tex.wrapT = THREE.RepeatWrapping; tex.repeat.set(40, 40);
    const g = new THREE.Mesh(new THREE.PlaneGeometry(800, 800), new THREE.MeshStandardMaterial({ map: tex, roughness: 1 }));
    g.rotation.x = -Math.PI / 2; g.position.set(cx, 0, cz); group.add(g);
  }
  function meadow(group, r, seed, track) { grassPlane(group, seed); trees(group, r, 260, 140, (x, z) => !track.some(p => Math.hypot(p.X - x, p.Z - z) < 4)); }
  function trees(group, r, count, radius, ok) {
    const trunkG = new THREE.CylinderGeometry(0.18, 0.26, 1.4, 6), leafG = new THREE.ConeGeometry(1.3, 3.2, 7);
    const trunk = new THREE.InstancedMesh(trunkG, new THREE.MeshStandardMaterial({ color: 0x5a3d26, roughness: 1 }), count);
    const leaf = new THREE.InstancedMesh(leafG, new THREE.MeshStandardMaterial({ color: 0x2f5a2a, roughness: 0.9, flatShading: true }), count);
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), s = new THREE.Vector3(), p = new THREE.Vector3(), c = new THREE.Color(), up = new THREE.Vector3(0, 1, 0);
    let n = 0;
    for (let k = 0; k < count * 4 && n < count; k++) {
      const x = (r() - 0.5) * radius * 2, z = (r() - 0.5) * radius * 2;
      if (!ok(x, z)) continue;
      const sc = 0.7 + r() * 0.9;
      q.identity(); m.compose(p.set(x, 0.7 * sc, z), q, s.set(sc, sc, sc)); trunk.setMatrixAt(n, m);
      q.setFromAxisAngle(up, r() * 6); m.compose(p.set(x, 2.9 * sc, z), q, s.set(sc, sc, sc)); leaf.setMatrixAt(n, m);
      leaf.setColorAt(n, c.setHSL(0.27 + r() * 0.08, 0.45, 0.22 + r() * 0.1));
      n++;
    }
    trunk.count = leaf.count = n; group.add(trunk, leaf);
  }
  function forestRoad(group, segs, r, seed) {
    const cx = segs.reduce((n, g) => n + g.a.x, 0) / segs.length, cz = segs.reduce((n, g) => n + g.a.z, 0) / segs.length;
    grassPlane(group, seed, cx, cz);
    const road = new THREE.MeshStandardMaterial({ color: 0x8a6c47, roughness: 1 });
    for (const g of segs) {
      const m = new THREE.Mesh(new THREE.PlaneGeometry(g.l + 5, 5), road);
      m.rotation.x = -Math.PI / 2; m.rotation.z = -Math.atan2(g.b.z - g.a.z, g.b.x - g.a.x);
      m.position.set((g.a.x + g.b.x) / 2, 0.02, (g.a.z + g.b.z) / 2); group.add(m);
    }
    const near = (x, z) => segs.some(g => { const t = clamp(((x - g.a.x) * (g.b.x - g.a.x) + (z - g.a.z) * (g.b.z - g.a.z)) / (g.l * g.l), 0, 1); return Math.hypot(x - lerp(g.a.x, g.b.x, t), z - lerp(g.a.z, g.b.z, t)) < 5; });
    const sub = new THREE.Group(); trees(sub, r, 420, 160, (x, z) => !near(x + cx, z + cz)); sub.position.set(cx, 0, cz); group.add(sub);
    flatLight(0x1d2418, 40, 150, 1.0, 1.3, 0, 0xd8ff9a);
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
    for (const c of rooms) {
      const ft = tex.clone(); ft.needsUpdate = true; ft.repeat.set(5, 5);
      const fl = new THREE.Mesh(new THREE.CylinderGeometry(11, 11, 0.3, 40), new THREE.MeshStandardMaterial({ map: ft, roughness: 0.95 }));
      fl.position.set(c.x, -0.13, c.z); group.add(fl);
      const rune = new THREE.Mesh(new THREE.RingGeometry(5.2, 5.6, 48), new THREE.MeshBasicMaterial({ color: 0xb23cff, transparent: true, opacity: 0.55, side: THREE.DoubleSide }));
      rune.rotation.x = -Math.PI / 2; rune.position.set(c.x, 0.04, c.z); group.add(rune); (group.userData.runes = group.userData.runes || []).push(rune);
      for (let k = 0; k < 10; k++) { const a = k / 10 * Math.PI * 2; const p = new THREE.Mesh(pillarG, pillarMat); p.position.set(c.x + Math.cos(a) * 11.5, 2.2, c.z + Math.sin(a) * 11.5); p.scale.y = 1.4; group.add(p);
        const f = new THREE.Sprite(flameMat.clone()); f.material.color.set(0xc06bff); f.position.set(c.x + Math.cos(a) * 10.8, 4.6, c.z + Math.sin(a) * 10.8); f.scale.setScalar(1.4); f.userData.ph = r() * 6; group.add(f); flames.push(f); }
    }
    group.userData.flames = flames;
    flatLight(0x0b0807, 14, 60, 0.35, 0.25, 60, 0xffc27a);
  }

  // ---- monsters: jointed figures by kind (people, beasts, oozes), kept in pools
  const pools = { humanoid: [], beast: [], blob: [] };
  const HEIGHT = { humanoid: 2.05, beast: 1.15, blob: 1.35 };
  function takeMob(m) {
    const arch = S.archetype(m.name), kind = m.boss && arch !== "beast" ? "humanoid" : arch;
    let o = pools[kind].pop();
    const hs = hash(m.name || "");
    if (!o) {
      const rig = kind === "beast" ? S.beast({ color: 0x6b5034 }) : kind === "blob" ? S.blob({ color: 0x3fb8a0 })
        : S.humanoid({ armor: 0x6a2a24, trim: 0x3a1a14, skin: 0xc79a74, hair: 0x1a1410, weapon: ["sword", "axe", "mace", "dagger"][pools.humanoid.length % 4] });
      const g = new THREE.Group();
      const shadow = new THREE.Mesh(G.disc, shadowMat); shadow.rotation.x = -Math.PI / 2; shadow.position.y = 0.03;
      const ring = new THREE.Mesh(G.ring, new THREE.MeshBasicMaterial({ color: 0xff3b2f, transparent: true, opacity: 0.7, side: THREE.DoubleSide, depthWrite: false }));
      ring.rotation.x = -Math.PI / 2; ring.position.y = 0.05;
      const barRim = new THREE.Sprite(new THREE.SpriteMaterial({ color: 0xf3ead9, transparent: true, opacity: 0.55, depthTest: false }));
      const barBg = new THREE.Sprite(new THREE.SpriteMaterial({ color: 0x1a1612, transparent: true, opacity: 0.92, depthTest: false }));
      const barFg = new THREE.Sprite(new THREE.SpriteMaterial({ color: 0x5bd85b, transparent: true, depthTest: false }));
      barRim.center.set(0, 0.5); barBg.center.set(0, 0.5); barFg.center.set(0, 0.5);
      barRim.renderOrder = 7; barBg.renderOrder = 8; barFg.renderOrder = 9;        // drawn in this order: rim, backing, bar
      let crown = null;
      if (kind === "humanoid") { crown = new THREE.Group(); for (let k = 0; k < 5; k++) { const c = new THREE.Mesh(G.crown, rig.mats[5]); const a = k / 5 * Math.PI * 2; c.position.set(Math.cos(a) * 0.17, 2.08, Math.sin(a) * 0.17); crown.add(c); } rig.body.add(crown); }
      g.add(rig.root, shadow, ring, barRim, barBg, barFg);
      o = { kind, g, rig, crown, ring, barRim, barBg, barFg, label: null, act: null, flinch: 0, lungeDir: new THREE.Vector3(), phase: 0 };
    }
    const boss = !!m.boss, sc = boss ? 2.2 : 1.0 + (hs % 30) / 100;
    o.rig.root.scale.setScalar(sc * (kind === "beast" ? 1.2 : 1));
    const mats = o.rig.mats;
    if (kind === "humanoid") {
      const armor = boss ? 0x4a2266 : FOE_ARMOR[hs % FOE_ARMOR.length];
      mats[0].color.set(armor); mats[1].color.set(armor).multiplyScalar(0.5); mats[2].color.set(FOE_SKIN[(hs >> 3) % FOE_SKIN.length]); mats[3].color.set(0x1a1410);
      mats[4].color.set(boss ? 0xff3030 : 0x1a1410);
      o.crown.visible = boss;
    } else if (kind === "beast") { const c = new THREE.Color(BEAST_FUR[hs % BEAST_FUR.length]); mats[0].color.copy(c); mats[1].color.copy(c).multiplyScalar(0.55); }
    else { const c = new THREE.Color(BLOB_COLOR[hs % BLOB_COLOR.length]); mats[0].color.copy(c); mats[0].emissive.copy(c).multiplyScalar(0.25); }
    o.rig.base = mats.map(x => x.color.clone());
    o.H = HEIGHT[kind] * sc * (kind === "beast" ? 1.2 : 1);
    o.ring.scale.setScalar(boss ? 2.4 : kind === "beast" ? 1.3 : 1);
    o.ring.material.color.set(boss ? 0xc040ff : 0xff3b2f);
    const bw = boss ? 2.2 : 1.3, by = o.H + 0.3;
    o.barBg.scale.set(bw, 0.15, 1); o.barBg.position.set(-bw / 2, by, 0);
    o.barRim.scale.set(bw + 0.08, 0.23, 1); o.barRim.position.set(-bw / 2 - 0.04, by, 0);
    o.barFg.position.copy(o.barBg.position); o.barFg.scale.copy(o.barBg.scale); o.barW = bw;
    if (boss || heroes.length <= 5) { o.label = textSprite((boss ? m.title : m.name) || "怪物", { color: boss ? "#e6a8ff" : "#ffb3a8", size: boss ? 30 : 22, scale: boss ? 0.016 : 0.011 }); o.label.position.y = by + (boss ? 0.65 : 0.4); o.g.add(o.label); }
    o.act = null; o.flinch = 0;
    scene.add(o.g);
    return o;
  }
  function releaseMob(o) {
    scene.remove(o.g);
    if (o.label) { o.g.remove(o.label); o.label.material.map.dispose(); o.label.material.dispose(); o.label = null; }
    pools[o.kind].push(o);
  }

  // ---- effects that live on the wall clock
  const fx = [];
  let numbers = 0;
  const lift = (v, dy) => new THREE.Vector3(v.x, v.y + dy, v.z);
  const add = (o, life, step, done) => { scene.add(o); fx.push({ o, life, age: 0, step, done }); };
  function burst(pos, color, n = 36, power = 5, life = 0.9) {
    const geo = new THREE.BufferGeometry(), p = new Float32Array(n * 3), v = [], floor = pos.y - 1.2;
    for (let k = 0; k < n; k++) { p[k * 3] = pos.x; p[k * 3 + 1] = pos.y; p[k * 3 + 2] = pos.z; const a = Math.random() * 6.28, u = Math.random() * 2 - 1, s = power * (0.4 + Math.random() * 0.6); v.push([Math.cos(a) * Math.sqrt(1 - u * u) * s, Math.abs(u) * s + 1.5, Math.sin(a) * Math.sqrt(1 - u * u) * s]); }
    geo.setAttribute("position", new THREE.BufferAttribute(p, 3));
    const pts = new THREE.Points(geo, new THREE.PointsMaterial({ map: GLOW, color, size: 0.55, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
    add(pts, life, (dt, f) => { const a = geo.attributes.position.array; for (let k = 0; k < n; k++) { v[k][1] -= 9 * dt; a[k * 3] += v[k][0] * dt; a[k * 3 + 1] = Math.max(floor, a[k * 3 + 1] + v[k][1] * dt); a[k * 3 + 2] += v[k][2] * dt; } geo.attributes.position.needsUpdate = true; pts.material.opacity = 1 - f; });
  }
  function rise(pos, color, n = 18) {
    const geo = new THREE.BufferGeometry(), p = new Float32Array(n * 3), v = [];
    for (let k = 0; k < n; k++) { const a = Math.random() * 6.28, r = Math.random() * 0.6; p[k * 3] = pos.x + Math.cos(a) * r; p[k * 3 + 1] = pos.y + Math.random() * 0.8; p[k * 3 + 2] = pos.z + Math.sin(a) * r; v.push(1.2 + Math.random() * 1.5); }
    geo.setAttribute("position", new THREE.BufferAttribute(p, 3));
    const pts = new THREE.Points(geo, new THREE.PointsMaterial({ map: GLOW, color, size: 0.35, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
    add(pts, 1.1, (dt, f) => { const a = geo.attributes.position.array; for (let k = 0; k < n; k++) a[k * 3 + 1] += v[k] * dt; geo.attributes.position.needsUpdate = true; pts.material.opacity = 1 - f; });
  }
  // a floating number or word, starting at `at` (already above whoever it is about)
  function number(text, at, color, { big = false, crit = false, small = false } = {}) {
    if (numbers > 34 && !crit && !big) return;
    const k = view === "chase" ? 0.8 : 1;
    const s = textSprite(text, { color, size: crit ? 40 : big ? 34 : small ? 20 : 26, scale: 0.012 * k });
    s.position.copy(at); s.position.x += (Math.random() - 0.5) * 1.6; s.position.z += (Math.random() - 0.5) * 0.8; s.position.y += Math.random() * 0.5;
    const drift = (Math.random() - 0.5) * 1.4;
    numbers++;
    add(s, crit ? 1.5 : big ? 2.2 : 1.15, (dt, f) => {
      s.position.y += dt * (big ? 0.8 : 1.9); s.position.x += dt * drift;
      const pop = crit ? (f < 0.12 ? 1 + (1 - f / 0.12) * 0.9 : 1) : 1; s.scale.copy(s.userData.base).multiplyScalar(pop);
      s.material.opacity = f < 0.65 ? 1 : 1 - (f - 0.65) / 0.35;
    }, () => { numbers--; });
  }
  const above = hr => lift(hr.g.position, 2.6);
  const overMob = o => lift(o.g.position, o.H + 0.9);
  function beam(pos, color) {
    const m = new THREE.Mesh(G.beam, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.6, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
    m.position.set(pos.x, pos.y + 7, pos.z);
    add(m, 2.2, (dt, f) => { m.scale.set(1 - f * 0.6, 1, 1 - f * 0.6); m.material.opacity = 0.6 * (1 - f); m.rotation.y += dt * 2; });
    burst(lift(pos, 0.4), color, 50, 4, 1.4);
  }
  function spark(pos, color, size = 1) {
    const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false }));
    s.position.copy(pos);
    add(s, 0.25, (dt, f) => { s.scale.setScalar((0.6 + f * 1.4) * size); s.material.opacity = 1 - f; });
  }
  function slash(at, from, color) {
    const m = new THREE.Mesh(G.slash, new THREE.MeshBasicMaterial({ color, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
    m.position.copy(at); m.lookAt(from); const spin = (Math.random() < 0.5 ? 1 : -1);
    m.rotateZ(Math.random() * Math.PI);
    add(m, 0.22, (dt, f) => { m.rotateZ(spin * dt * 9); m.scale.setScalar(0.8 + f * 0.5); m.material.opacity = 1 - f; });
  }
  function projectile(from, to, color, kind, onHit) {
    const life = kind === "range" ? 0.22 : 0.34;
    let m;
    if (kind === "range") m = new THREE.Mesh(G.arrow, new THREE.MeshBasicMaterial({ color: 0xf2e6c8 }));
    else {
      m = new THREE.Mesh(G.bolt, new THREE.MeshBasicMaterial({ color, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
      const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false })); glow.scale.setScalar(1); m.add(glow);
    }
    const a = from.clone(), b = to.clone(); let trail = 0;
    m.position.copy(a);
    add(m, life, (dt, f) => {
      const prev = m.position.clone();
      m.position.lerpVectors(a, b, f); m.position.y += Math.sin(f * Math.PI) * (kind === "range" ? 0.5 : 0.9);
      if (kind === "range") m.lookAt(m.position.clone().add(m.position.clone().sub(prev)));
      else if ((trail += dt) > 0.03) { trail = 0; spark(m.position, color, 0.55); }
    }, onHit);
  }
  function ring(pos, color) {
    const m = new THREE.Mesh(G.ring, new THREE.MeshBasicMaterial({ color, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
    m.rotation.x = -Math.PI / 2; m.position.set(pos.x, pos.y + 0.07, pos.z);
    add(m, 0.45, (dt, f) => { m.scale.setScalar(1 + f * 1.4); m.material.opacity = 1 - f; });
  }
  const dropFx = () => { for (const e of fx.splice(0)) { scene.remove(e.o); dispose(e.o); } numbers = 0; };

  // ---- camera. Map view: orbit around the party. Third person: behind the recorder, turning with them.
  const cam = { theta: 0.35, phi: 0.92, r: 24, want: 24, target: new THREE.Vector3(), aim: new THREE.Vector3(), shake: 0, touched: -1e9,
    yaw: 0, heading: 0, yawOff: 0, pitch: 0.3, dist: 9, pos: new THREE.Vector3(), look: new THREE.Vector3() };
  const pointers = new Map(); let pinch = 0;
  const cv = renderer.domElement;
  const touch = () => { cam.touched = performance.now(); };
  cv.addEventListener("pointerdown", e => { cv.setPointerCapture(e.pointerId); pointers.set(e.pointerId, [e.clientX, e.clientY]); touch(); });
  cv.addEventListener("pointermove", e => {
    if (!pointers.has(e.pointerId)) return;
    const [px, py] = pointers.get(e.pointerId); pointers.set(e.pointerId, [e.clientX, e.clientY]); touch();
    if (pointers.size === 1) {
      if (view === "chase") { cam.yawOff -= (e.clientX - px) * 0.006; cam.pitch = clamp(cam.pitch + (e.clientY - py) * 0.004, 0.02, 1.25); }
      else { cam.theta -= (e.clientX - px) * 0.006; cam.phi = clamp(cam.phi - (e.clientY - py) * 0.005, 0.25, 1.45); }
    } else if (pointers.size === 2) { const [a, b] = [...pointers.values()]; const dd = Math.hypot(a[0] - b[0], a[1] - b[1]); if (pinch) zoom(pinch / dd); pinch = dd; }
  });
  const up = e => { pointers.delete(e.pointerId); if (pointers.size < 2) pinch = 0; };
  const zoom = k => { if (view === "chase") cam.dist = clamp(cam.dist * k, 3.5, 40); else cam.r = clamp(cam.r * k, 6, 160); };
  cv.addEventListener("pointerup", up); cv.addEventListener("pointercancel", up);
  cv.addEventListener("wheel", e => { e.preventDefault(); touch(); zoom(Math.exp(e.deltaY * 0.0012)); }, { passive: false });
  const angLerp = (a, b, f) => { let d = (b - a) % (Math.PI * 2); if (d > Math.PI) d -= Math.PI * 2; if (d < -Math.PI) d += Math.PI * 2; return a + d * f; };

  // ---- state
  let g = 0, playing = false, lastNow = performance.now(), lastG = 0, curScene = null, tickShown = -1, uiAt = 0, first = true, shownScene = null;
  let evAt = 0, hitAt = 0, now = 0, lampAt = 0;
  const fmtScene = sc => (sc.kind === "field" ? (CONTINENT[sc.map] ? CONTINENT[sc.map] + "（移動中）" : (M.mapInfo[sc.map] && M.mapInfo[sc.map].name) || "地圖 " + sc.map) : sc.name);
  const sceneAt = v => { let k = M.scenes.length - 1; while (k > 0 && M.scenes[k].off > v) k--; return M.scenes[k]; };
  M.scenes.forEach(sc => {
    const b = h("button", null, `${clock(sc.t0).slice(0, 5)} ${fmtScene(sc)}${sc.kind === "virtual" && sc.theme === "dungeon" ? " ⚔" : ""}`); b.type = "button";
    b.addEventListener("click", () => seek(sc.off + 0.01)); sc.chip = b; chips.append(b);
  });
  const showCard = (big, small) => { card.replaceChildren(h("b", null, big), h("small", null, small)); card.style.opacity = 1; clearTimeout(showCard.t); showCard.t = setTimeout(() => card.style.opacity = 0, 2200); };
  const setView = v => {
    view = v; try { localStorage.setItem(VIEW_KEY, v); } catch (e) { /* not kept: fine */ }
    viewBtn.textContent = v === "chase" ? "🗺 地圖視角" : "🎥 第三人稱";
    help.textContent = v === "chase" ? "拖曳轉鏡頭・滾輪拉遠近・空白鍵播放" : "拖曳旋轉・滾輪縮放・空白鍵播放";
    camera.fov = v === "chase" ? 58 : 48; camera.updateProjectionMatrix();
    scene.remove(sky.group); if (v === "chase") scene.add(sky.group);
    if (world) { disposeWorld(world); dropFx(); world = null; } building = null; ticket++; first = true; shownScene = null;
  };
  viewBtn.addEventListener("click", () => setView(view === "chase" ? "map" : "chase"));
  setView(view);

  function seek(v) {
    g = clamp(v, 0, M.total); lastG = g; slider.value = Math.round(g / M.total * 1000);
    evAt = lowerBound(M.events, g, e => e.r); hitAt = lowerBound(M.hits, g, x => x.r);
    for (const hr of heroes) { hr.pend = null; hr.act = null; }
    for (const m of M.monsters) m.pend = null;
    endCard.classList.remove("on");
  }
  const setPlaying = on => {
    playing = on; play.textContent = on ? "❚❚ 暫停" : "▶ 播放";
    if (on && g >= M.total - 0.01) seek(0);
    if (on) endCard.classList.remove("on");
    lastNow = performance.now();
  };
  play.addEventListener("click", () => setPlaying(!playing));
  slider.addEventListener("input", () => { seek(slider.value / 1000 * M.total); });
  const onKey = e => {
    if (e.key === "Escape") { e.preventDefault(); shut(); }
    else if (e.key === " " && e.target.tagName !== "SELECT") { e.preventDefault(); setPlaying(!playing); }
    else if (e.key === "ArrowRight") seek(g + 3); else if (e.key === "ArrowLeft") seek(g - 3);
    else if (e.key === "v" || e.key === "V") setView(view === "chase" ? "map" : "chase");
  };
  document.addEventListener("keydown", onKey);
  close.addEventListener("click", () => shut());

  function credits() {
    const rows = (d.meter && d.meter.members && d.meter.members.length ? d.meter.members.map(m => ({ name: m.name, amount: m.damage })) : d.damage || []).filter(r => r.amount > 0).slice(0, 5);
    const topAmount = rows.length ? rows[0].amount : 1;
    const best = (d.meter && d.meter.members || []).filter(m => m.best).sort((a, b) => b.best.amount - a.best.amount)[0];
    const kills = (d.kill_times || []).length, deaths = (d.deaths_by || []).reduce((n, x) => n + x.deaths, 0) || d.deaths || 0;
    const mins = Math.round((d.duration || (M.scenes[M.scenes.length - 1].t1 - M.scenes[0].t0) / 1000) / 60);
    const tile = (n, l) => { const x = h("div"); x.append(h("b", null, n), h("span", null, l)); return x; };
    const tiles = h("div", "tiles"); tiles.append(tile(mins >= 60 ? `${Math.floor(mins / 60)}h${mins % 60}m` : `${mins} 分`, "時間"), tile(kills.toLocaleString(), "擊殺"), tile(String(deaths), "死亡"));
    const list = h("ol");
    for (const r of rows) {
      const hr = heroOf.get(r.name), li = h("li"), bar = h("u");
      bar.style.width = Math.max(4, r.amount / topAmount * 100) + "%"; bar.style.background = hr ? hr.color : "#e0964a";
      const wrap = h("div"); wrap.append(bar);
      li.append(h("span", null, N(r.name)), wrap, h("span", null, fmt(r.amount)));
      list.append(li);
    }
    const again = h("button", "rp-btn", "↺ 重播"), shutB = h("button", "rp-btn ghost", "關閉");
    again.type = shutB.type = "button";
    again.addEventListener("click", () => { seek(0); setPlaying(true); });
    shutB.addEventListener("click", () => shut());
    const acts = h("div", "acts"); acts.append(shutB, again);
    endCard.replaceChildren(h("h4", null, "這趟冒險"), h("div", "sub", `${clock(M.scenes[0].t0)} – ${clock(M.scenes[M.scenes.length - 1].t1)}　${M.scenes.map(fmtScene).filter((x, i, a) => a.indexOf(x) === i).join("、")}`),
      tiles, rows.length ? h("div", "sub", "傷害輸出") : "", rows.length ? list : "",
      best ? h("div", "best", `最強一擊：${N(best.name)} 的 ${best.best.spell || "攻擊"} ${fmt(best.best.amount)}${best.best.crit ? "（暴擊）" : ""}` + (best.best.target ? ` → ${best.best.target}` : "")) : "", acts);
    endCard.classList.add("on");
  }

  const at = (pts, t) => {
    let lo = 0, hi = pts.length - 1;
    if (t <= pts[0].t) return [pts[0].X, pts[0].Z];
    if (t >= pts[hi].t) return [pts[hi].X, pts[hi].Z];
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (pts[m].t <= t) lo = m; else hi = m; }
    const a = pts[lo], b = pts[hi];
    if (b.t - a.t > GAP) return [a.X, a.Z];
    const f = (t - a.t) / (b.t - a.t); return [lerp(a.X, b.X, f), lerp(a.Z, b.Z, f)];
  };
  const drawTo = (geo, pts, t) => geo.setDrawRange(0, Math.max(0, lowerBound(pts, t, p => p.t) - 1) * 6);
  const slot = k => k === 0 ? [0, 0] : [((k - 1) % 3 - 1) * 1.6, -(1 + Math.floor((k - 1) / 3)) * 1.5];
  const V = new THREE.Vector3(), V2 = new THREE.Vector3();
  const present = (hr, t) => hr.spans.some(([a, b]) => t >= a && t <= b);
  const isDown = hr => hr.down.some(([a, b]) => g >= a && g < b);
  const hpOf = (m, t) => { if (m.kill != null && t >= m.kill) return 0; if (!m.dmg.length) return m.kill != null ? clamp(1 - (t - m.spawn) / (m.kill - m.spawn), 0.05, 1) : 1;
    const i = lowerBound(m.dmg, t, x => x[0]) - 1; return clamp(1 - (i >= 0 ? m.dmg[i][1] : 0) / m.peak, 0.02, 1); };
  const tint = (rig, grey, opacity) => rig.mats.forEach((m, k) => { m.color.copy(rig.base[k]).lerp(GREY, grey); m.opacity = opacity; });

  function update(dt) {
    const sc = sceneAt(g);
    if (!world || world.sc !== sc || world.view !== view) {
      if (building !== sc) {
        building = sc; wait.style.display = "";
        const v = view;
        build(sc).then(w => {
          if (!w || building !== sc || view !== v) return;
          disposeWorld(world); dropFx(); world = w; scene.add(w.group); building = null; wait.style.display = "none";
          for (const s of M.scenes) s.chip && s.chip.classList.toggle("on", s === sc);
          if (sc !== curScene) { curScene = sc; showCard(fmtScene(sc), sc.kind === "virtual" ? (sc.theme === "dungeon" ? "副本內沒有座標，這段以示意場景呈現" : "這段沒有位置紀錄，以示意場景呈現") : `${clock(sc.t0)} – ${clock(sc.t1)}`); }
          title.textContent = fmtScene(sc);
          if (sc !== shownScene) { shownScene = sc; cam.phi = sc.kind === "virtual" && sc.theme === "dungeon" ? 0.72 : 0.92; }
          lastNow = performance.now(); first = true;
        });
      }
      return;
    }
    const t = M.realOf(sc, g), cat = M.catOf(sc, g), chase = view === "chase", ground = world.ground;
    sky.group.visible = !!world.outdoors;
    // sky and light by the hour of the moment shown, outdoors in the third-person view
    if (world.outdoors) {
      const d0 = new Date(t), hour = d0.getHours() + d0.getMinutes() / 60, skyNow = S.skyAt(hour);
      sky.set(skyNow); sky.follow(camera.position, dt);
      const fogC = skyNow.horizon.clone().lerp(new THREE.Color(0x000000), skyNow.night * 0.3);
      if (!scene.fog || !scene.fog.isFog) scene.fog = new THREE.Fog(fogC, 60, 330); else { scene.fog.color.copy(fogC); scene.fog.near = 60; scene.fog.far = 330; }
      hemi.color.copy(skyNow.horizon).lerp(new THREE.Color(0xffffff), 0.4); hemi.groundColor.copy(world.terrain.avgColor).multiplyScalar(0.6); hemi.intensity = 0.35 + skyNow.light * 0.55;
      sun.color.copy(skyNow.sun); sun.intensity = skyNow.light * 1.9;
      sun.position.copy(cam.target).addScaledVector(skyNow.dir, 120); sun.target.position.copy(cam.target);
      if (world.trees && world.trees.userData.tint) world.trees.userData.tint(skyNow);
      motes.material.color.set(skyNow.night > 0.5 ? 0xd8ff7a : 0xfff3c0); motes.material.opacity = skyNow.night > 0.5 ? 0.85 : 0.35;
      windTime.value = now / 1000;
      world.grass.update(cam.target.x, cam.target.z);
    }
    // monsters up at this moment
    const up = M.monsters.filter(m => m.scene === sc && g >= m.spawnR && g < m.endR + 1.4);
    const live = new Set(up);
    for (const [m, o] of world.mobs) if (!live.has(m)) { releaseMob(o); world.mobs.delete(m); }
    // where the party is
    let lead, dir;
    if (sc.kind === "field") {
      const [x, z] = at(world.track, t); lead = V.set(x, 0, z);
      const [x2, z2] = at(world.track, t + 4000); dir = V2.set(x2 - x, 0, z2 - z);
    } else {
      const p = world.pathAt(world.progress(t)); lead = V.set(p.x, 0, p.z); dir = V2.set(p.dx, 0, p.dz);
    }
    if (dir.lengthSq() > 1e-4) dir.normalize(); else dir.set(Math.sin(cam.heading), 0, Math.cos(cam.heading));
    lead.y = ground(lead.x, lead.z);
    const spot = m => {
      if (sc.kind !== "field") return world.spot(m);
      if (!m._spot || m._spotOf !== world) {
        const k = world.place(m.at), j = rng(hash(m.guid || m.kill || m.spawn));
        if (k && m.kill != null) m._spot = k;
        else { const [x, z] = at(world.track, m.first), [x2, z2] = at(world.track, m.first + 4000); let dx = x2 - x, dz = z2 - z; const l = Math.hypot(dx, dz) || 1; dx /= l; dz /= l; if (l < 0.01) { dx = 0; dz = -1; }
          const side = (j() - 0.5) * 4, ahead = 2.6 + j() * 1.8; m._spot = [x + dx * ahead - dz * side, z + dz * ahead + dx * side]; }
        m._spotOf = world;
      }
      return m._spot;
    };
    const alive = []; let bossNow = null;
    for (const m of up) {
      let o = world.mobs.get(m); if (!o) { o = takeMob(m); world.mobs.set(m, o); }
      let [x, z] = spot(m);
      const dying = m.killR != null && g >= m.killR, fled = m.kill == null && g >= m.endR, k = dying ? g - m.killR : fled ? g - m.endR : 0;
      if (!dying && !fled) alive.push({ m, o, p: [x, z] });
      // coming in: they run up from further out
      const comeIn = clamp((g - m.spawnR) / 0.7, 0, 1);
      if (comeIn < 1) { const ax = x - lead.x, az = z - lead.z, l = Math.hypot(ax, az) || 1; x += ax / l * (1 - comeIn) * 9; z += az / l * (1 - comeIn) * 9; }
      o.flinch = Math.max(0, o.flinch - dt * 6);
      let act = null, f = 0;
      if (o.act) { o.act.age += dt; f = o.act.age / o.act.dur; if (f >= 1) o.act = null; else act = o.act.kind; }
      const lg = act === "swing" ? Math.sin(f * Math.PI) * 0.6 : 0;
      const gy = ground(x, z);
      o.g.position.set(x + o.lungeDir.x * lg, gy - (k > 0.7 ? (k - 0.7) * 1.2 : 0), z + o.lungeDir.z * lg);
      const foe = m.foe && present(m.foe, t) ? m.foe.g.position : lead;
      o.rig.root.rotation.y = comeIn < 1 ? Math.atan2(lead.x - x, lead.z - z) : Math.atan2(foe.x - x, foe.z - z);
      o.phase += dt * (comeIn < 1 ? 14 : 0);
      o.rig.pose({ walk: comeIn < 1 ? 1 : 0, phase: o.phase, act, f, down: k > 0, t: now / 1000 + (m.spawn % 997) });
      tint(o.rig, 0, k > 0.7 ? Math.max(0, 1 - (k - 0.7) / 0.7) : 1);
      for (const mt of o.rig.mats) if (mt.emissive && o.kind !== "blob") mt.emissive.setRGB(o.flinch * 0.45, o.flinch * 0.12, o.flinch * 0.06);
      o.ring.material.opacity = k > 0 ? 0 : 0.7;
      const hp = hpOf(m, t);
      o.barFg.scale.x = o.barW * hp; o.barFg.material.color.setHSL(0.33 * hp, 0.75, 0.5);
      o.barRim.visible = o.barBg.visible = o.barFg.visible = k === 0;
      if (o.label) o.label.visible = k === 0;
      if (m.boss && k === 0) bossNow = { m, hp };
    }
    const nearest = (x, z) => { let best = null, bd = 1e9; for (const a of alive) { const dd = Math.hypot(a.p[0] - x, a.p[1] - z); if (dd < bd) { bd = dd; best = a; } } return best; };
    const right = { x: -dir.z, z: dir.x };
    let order = 0;
    for (const hr of heroes) {
      const here = present(hr, t);
      hr.shown = lerp(hr.shown, here ? 1 : 0, first ? 1 : Math.min(1, dt * 4));
      hr.g.visible = hr.shown > 0.02; if (hr.pet) hr.pet.visible = hr.g.visible;
      if (!here && hr.shown < 0.02) continue;
      let x, z, real = false;
      if (sc.kind === "field" && hr.owner) { x = lead.x; z = lead.z; real = true; }
      else if (sc.kind === "field" && world.mates[hr.name]) { const pts = world.mates[hr.name]; if (t >= pts[0].t - 30000 && t <= pts[pts.length - 1].t + 30000) { [x, z] = at(pts, t); real = true; } }
      const down = isDown(hr);
      const fight = hr.foe && alive.find(a => a.m === hr.foe) || (alive.length ? nearest(lead.x, lead.z) : null);
      if (!real) {
        const k = sc.kind === "field" ? ++order : order++;
        const [sx, sf] = slot(k); x = lead.x + right.x * sx + dir.x * sf; z = lead.z + right.z * sx + dir.z * sf;
        if (fight && !down) {
          const [mx, mz] = fight.p, a = Math.atan2(x - mx, z - mz), reach = MELEE.has(hr.cls) ? 1.5 + (fight.m.boss ? 1.6 : 0) : 6.5;
          const dd = Math.hypot(x - mx, z - mz);
          if (MELEE.has(hr.cls) || dd > reach) { x = lerp(x, mx + Math.sin(a) * reach, 0.85); z = lerp(z, mz + Math.cos(a) * reach, 0.85); }
        }
      }
      const moved = Math.hypot(x - hr.g.position.x, z - hr.g.position.z);
      if (first || moved > 6) hr.g.position.set(x, 0, z);
      else hr.g.position.set(lerp(hr.g.position.x, x, Math.min(1, dt * 8)), 0, lerp(hr.g.position.z, z, Math.min(1, dt * 8)));
      hr.g.position.y = ground(hr.g.position.x, hr.g.position.z);
      const step = Math.hypot(hr.g.position.x - hr.last.x, hr.g.position.z - hr.last.z);
      const want = fight && !down ? Math.atan2(fight.p[0] - hr.g.position.x, fight.p[1] - hr.g.position.z) : step > 0.01 ? Math.atan2(hr.g.position.x - hr.last.x, hr.g.position.z - hr.last.z) : hr.face;
      hr.face = first ? want : angLerp(hr.face, want, Math.min(1, dt * 10));
      hr.last.copy(hr.g.position);
      const walking = step / Math.max(dt, 0.001) > 0.6 && !down;
      hr.walk = lerp(hr.walk, walking ? 1 : 0, Math.min(1, dt * 8)); hr.phase += step * 3.4;
      let act = null, f = 0;
      if (hr.act) { hr.act.age += dt; f = hr.act.age / hr.act.dur; if (f >= 1) hr.act = null; else act = hr.act.kind; }
      const lunge = act === "swing" ? Math.sin(Math.min(1, f * 1.4) * Math.PI) * 0.5 : 0;
      hr.rig.root.rotation.y = hr.face;
      hr.rig.root.position.set(Math.sin(hr.face) * lunge, 0, Math.cos(hr.face) * lunge);
      hr.rig.pose({ walk: hr.walk, phase: hr.phase, act, f, down, t: now / 1000 + hr.i });
      if (hr.rig.glow) hr.rig.glow.scale.setScalar(0.6 + (act === "cast" ? Math.sin(f * Math.PI) * 2.2 : 0));
      tint(hr.rig, down ? 0.7 : 0, hr.shown);
      hr.ring.material.opacity = (down ? 0.15 : hr.owner ? 0.95 : 0.6) * hr.shown * (chase ? 0.6 : 1);
      hr.label.material.opacity = (down ? 0.5 : 1) * hr.shown;
      if (hr.pet) {
        const pa = fight && !down ? [fight.p[0] + Math.sin(hr.face + 2.2) * 1.4, fight.p[1] + Math.cos(hr.face + 2.2) * 1.4] : [hr.g.position.x + right.x * 1.1 - dir.x * 0.4, hr.g.position.z + right.z * 1.1 - dir.z * 0.4];
        hr.pet.position.set(lerp(hr.pet.position.x, pa[0], first ? 1 : Math.min(1, dt * 6)), 0, lerp(hr.pet.position.z, pa[1], first ? 1 : Math.min(1, dt * 6)));
        hr.pet.position.y = ground(hr.pet.position.x, hr.pet.position.z);
        hr.pet.rotation.y = fight ? Math.atan2(fight.p[0] - hr.pet.position.x, fight.p[1] - hr.pet.position.z) : hr.face;
      }
      if (playing && fight && !fight.m.logged && !down && here && !hr.act) {
        hr.charge += dt * (0.6 + (M.hasMeter ? M.fury(hr.name, t) : 0.5) * 2.2) * Math.min(2, +speed.value);
        if (hr.charge >= 1) { hr.charge = 0; attack(hr, fight.m, { kind: MELEE.has(hr.cls) ? "swing" : WEAPON[hr.cls] === "bow" ? "range" : "spell", amount: 0, school: hr.cls === "MAGE" ? 16 : hr.cls === "WARLOCK" ? 32 : hr.cls === "PRIEST" ? 2 : hr.cls === "DRUID" ? 8 : 1 }); }
      }
    }
    if (playing && g > lastG && g - lastG < 3) {
      while (hitAt < M.hits.length && M.hits[hitAt].r <= g) take(M.hits[hitAt++], t);
      while (evAt < M.events.length && M.events[evAt].r <= g) fire(M.events[evAt++]);
    } else { evAt = lowerBound(M.events, g, e => e.r); hitAt = lowerBound(M.hits, g, x => x.r); }
    lastG = g;
    for (const hr of heroes) { hr.cool -= dt; if (hr.pend && hr.cool <= 0) flush(hr); }
    for (const m of M.monsters) if (m.pend && (m.cool -= dt) <= 0) flushMob(m);
    if (world.trail) drawTo(world.trail.geo, world.trail.pts, t);
    for (const mt of world.group.userData.mates || []) drawTo(mt.geo, mt.pts, t);
    const flames = (world.tunnel ? world.tunnel.group.userData.flames : world.group.userData.flames) || [];
    for (const f of flames) f.scale.setScalar((f.userData.s || (f.userData.s = f.scale.x)) * (1 + Math.sin(now / 90 + f.userData.ph) * 0.12 + Math.random() * 0.06));
    for (const r of (world.tunnel ? world.tunnel.group.userData.runes : world.group.userData.runes) || []) { r.rotation.z += dt * 0.3; r.material.opacity = 0.35 + Math.sin(now / 400) * 0.2 + (cat === 2 ? 0.3 : 0); }
    // in the tunnel, the torches nearest the party light up
    if (world.tunnel && now - lampAt > 250) {
      lampAt = now;
      const near = world.tunnel.torches.map(p => [p, p.distanceToSquared(cam.target)]).sort((a, b) => a[1] - b[1]).slice(0, lamps.length);
      lamps.forEach((l, i) => { const p = near[i]; if (p) { l.position.copy(p[0]); l.userData.base = p[0].y < 2.5 ? 30 : 18; l.color.set(p[0].y < 2.5 ? 0xc06bff : 0xff9a40); } else l.userData.base = 0; });
    }
    for (const l of lamps) l.intensity = world.tunnel ? (l.userData.base || 0) * (0.85 + Math.sin(now / 70 + l.id) * 0.08 + Math.random() * 0.07) : 0;
    const mp = moteGeo.attributes.position.array;
    for (let k = 0; k < MOTES; k++) { mp[k * 3 + 1] += dt * (0.15 + (k % 7) * 0.03); mp[k * 3] += Math.sin(now / 2000 + k) * dt * 0.2; if (mp[k * 3 + 1] > 4) mp[k * 3 + 1] = -4; }
    moteGeo.attributes.position.needsUpdate = true; motes.position.set(cam.target.x, cam.target.y + 2.5, cam.target.z);
    // camera
    const owner = heroes.find(x => x.owner);
    const focus = sc.kind === "field" && owner && owner.g.visible ? owner.g.position : lead;
    cam.aim.set(focus.x, focus.y + (chase ? 1.4 : 0.8), focus.z);
    const fightNow = alive.length && cat ? (alive.find(a => a.m.boss) || nearest(focus.x, focus.z)) : null;
    if (!chase && fightNow) { cam.aim.x = lerp(cam.aim.x, fightNow.p[0], 0.4); cam.aim.z = lerp(cam.aim.z, fightNow.p[1], 0.4); }
    cam.target.lerp(cam.aim, first ? 1 : Math.min(1, dt * (chase ? 6 : 3)));
    const idle = now - cam.touched > (chase ? 3500 : 6000);
    if (chase) {
      // facing: where they walk, or the fight when there is one
      const hw = fightNow ? Math.atan2(fightNow.p[0] - focus.x, fightNow.p[1] - focus.z) : Math.atan2(dir.x, dir.z);
      cam.heading = first ? hw : angLerp(cam.heading, hw, Math.min(1, dt * (fightNow ? 1.5 : 2.2)));
      if (idle) { cam.yawOff = angLerp(cam.yawOff, 0, Math.min(1, dt * 0.8)); cam.dist = lerp(cam.dist, cat === 2 ? (world.tunnel ? 8 : 15) : cat === 1 ? 11 : 9.5, Math.min(1, dt * 0.6)); cam.pitch = lerp(cam.pitch, world.tunnel ? (cat === 2 ? 0.62 : 0.5) : cat === 2 ? 0.45 : 0.3, Math.min(1, dt * 0.6)); }
      const yaw = cam.heading + cam.yawOff + Math.PI;
      let dist = cam.dist;
      const place = d0 => cam.pos.set(cam.target.x + Math.sin(yaw) * Math.cos(cam.pitch) * d0, cam.target.y + Math.sin(cam.pitch) * d0 + 0.4, cam.target.z + Math.cos(yaw) * Math.cos(cam.pitch) * d0);
      place(dist);
      if (world.tunnel) {                                          // keep inside the walls and under the roof
        while (dist > 2.5 && world.tunnel.inside(cam.pos.x, cam.pos.z) > world.tunnel.W / 2 - 0.8) { dist *= 0.88; place(dist); }
        if (!world.tunnel.inRoom(cam.pos.x, cam.pos.z, -2)) cam.pos.y = Math.min(cam.pos.y, world.tunnel.H - 0.7);
      }
      cam.pos.y = Math.max(cam.pos.y, ground(cam.pos.x, cam.pos.z) + 1.0);
      cam.look.set(cam.target.x + Math.sin(cam.heading + cam.yawOff) * 2.5, cam.target.y + 0.4, cam.target.z + Math.cos(cam.heading + cam.yawOff) * 2.5);
    } else if (idle && playing) { cam.theta += dt * (cat === 2 ? 0.12 : 0.04); cam.want = cat === 2 ? 17 : cat === 1 ? 20 : 25; cam.r = lerp(cam.r, cam.want, Math.min(1, dt * 0.8)); }
    torch.position.set(cam.target.x, cam.target.y + 3.5, cam.target.z);
    bossBox.classList.toggle("on", !!bossNow);
    if (bossNow) { bossName.textContent = bossNow.m.title; bossFill.style.width = (bossNow.hp * 100).toFixed(1) + "%"; bossPct.textContent = Math.round(bossNow.hp * 100) + "%"; }
    slow.className = "rp-slow" + (cat ? " on" : "") + (cat === 2 ? " boss" : "");
    slow.textContent = cat === 2 ? "⏱ 首領戰・慢動作" : "⏱ 戰鬥中・放慢";
    if (now - uiAt > 150) {
      uiAt = now;
      sub.textContent = `${clock(t)}${M.scenes.length > 1 ? `　第 ${M.scenes.indexOf(sc) + 1} / ${M.scenes.length} 段` : ""}`;
      const k = lowerBound(M.events, g, e => e.r) - 1;
      if (k !== tickShown) {
        tickShown = k;
        tick.replaceChildren(...M.events.slice(Math.max(0, k - 4), k + 1).map(e => { const n = h("div"); n.append(h("time", null, clock(e.t).slice(0, 5)), e.text); return n; }));
      }
      for (const hr of heroes) {
        const here = present(hr, t);
        hr.ui.m.classList.toggle("away", !here); hr.ui.m.classList.toggle("down", here && isDown(hr));
        if (M.hasMeter) hr.ui.u.style.width = (here ? Math.round(M.fury(hr.name, t) * 100) : 0) + "%";
      }
    }
    first = false;
  }

  function take(x, t) {
    if (x.way === "out" || x.way === "heal") {
      const hr = heroOf.get(x.who); if (!hr || !hr.g.visible || isDown(hr)) return;
      if (x.way === "out" && x.mob) { hr.foe = x.mob; }
      const p = hr.pend || (hr.pend = { way: x.way, amount: 0, crit: false, n: 0, best: null, miss: null, mob: null, to: null, pet: false });
      if (p.way !== x.way) { flush(hr); return take(x, t); }
      p.n++; p.amount += x.amount; p.crit = p.crit || x.crit; p.pet = p.pet || x.pet;
      if (x.miss && !x.amount) p.miss = p.miss || x.miss;
      if (!p.best || x.amount > p.best.amount) p.best = x;
      if (x.way === "out") p.mob = x.mob || p.mob; else p.to = heroOf.get(typeof x.enemy === "string" ? x.enemy.split("-")[0] : x.who) || hr;
      if (hr.cool <= 0) flush(hr);
    } else if (x.way === "in" && x.mob) {
      const m = x.mob, hr = heroOf.get(x.who); if (!hr || !hr.g.visible) return;
      m.foe = hr;
      const p = m.pend || (m.pend = { amount: 0, crit: false, miss: null, hr });
      p.amount += x.amount; p.crit = p.crit || x.crit; if (x.miss && !x.amount) p.miss = x.miss; p.hr = hr; p.spell = x.spell;
      if (!(m.cool > 0)) flushMob(m);
    }
  }
  function flush(hr) {
    const p = hr.pend; hr.pend = null; if (!p) return;
    hr.cool = 0.32;
    if (p.way === "heal") {
      const to = p.to || hr;
      hr.act = { kind: "cast", age: 0, dur: 0.45 }; ring(hr.g.position, 0x7dff6a);
      const show = () => { rise(to.g.position, 0x7dff6a); number(`+${fmt(p.amount)}` + (p.best && p.best.spell ? ` ${p.best.spell}` : ""), above(to), "#8dff8a", { crit: p.crit, small: !p.crit }); };
      if (to !== hr) projectile(lift(hr.g.position, 1.4), lift(to.g.position, 1.2), 0x7dff6a, "spell", show); else show();
      return;
    }
    const m = p.mob; if (!m) return;
    attack(hr, m, { kind: p.best.kind, school: p.best.school, amount: p.amount, crit: p.crit, spell: p.best.spell, miss: p.amount ? null : p.miss, n: p.n, pet: p.pet && p.best.pet });
  }
  function attack(hr, m, a) {
    const o = world && world.mobs.get(m); if (!o) return;
    const tp = lift(o.g.position, o.H * 0.55);
    const color = schoolColor(a.school), c3 = new THREE.Color(color);
    const label = a.miss ? (MISS[a.miss] || a.miss) : a.amount ? (a.spell && a.kind !== "swing" ? `${a.spell} ` : "") + fmt(a.amount) + (a.crit ? "!" : "") : null;
    const land = () => {
      o.flinch = a.amount ? 1 : 0;
      if (label) number(label, overMob(o), a.miss ? "#c9c2b6" : a.crit ? "#ffd84a" : a.kind === "swing" || a.kind === "range" ? "#ffffff" : color, { crit: a.crit && !a.miss, small: a.kind === "dot" || a.kind === "shield" });
      if (a.crit && !a.miss) { burst(tp, color === SCHOOL[1] ? 0xffd84a : color, 16, 3, 0.5); if (m.boss) cam.shake = 0.35; }
    };
    const from = a.pet && hr.pet ? hr.pet.position : hr.g.position;
    if (a.pet && hr.pet) { slash(tp, lift(from, 0.6), 0xffffff); land(); return; }
    if (a.kind === "swing") { hr.act = { kind: "swing", age: 0, dur: 0.36 }; setTimeout(() => { if (!alive_) return; slash(tp, lift(from, 1.1), a.crit ? 0xffd84a : 0xffffff); land(); }, 130); }
    else if (a.kind === "range") { hr.act = { kind: "shoot", age: 0, dur: 0.34 }; projectile(lift(from, 1.4), tp, c3, "range", land); }
    else if (a.kind === "dot" || a.kind === "shield") { burst(tp, c3, 8, 1.5, 0.45); land(); }
    else { hr.act = { kind: MELEE.has(hr.cls) ? "swing" : "cast", age: 0, dur: 0.4 }; ring(from, c3);
      if (MELEE.has(hr.cls)) setTimeout(() => { if (!alive_) return; slash(tp, lift(from, 1.1), color); spark(tp, c3, 1.5); land(); }, 130);
      else projectile(lift(from, 1.6), tp, c3, "spell", () => { spark(tp, c3, 1.8); land(); }); }
  }
  function flushMob(m) {
    const p = m.pend; m.pend = null; m.cool = 0.45; if (!p) return;
    const o = world && world.mobs.get(m); if (!o) return;
    const hr = p.hr;
    o.act = { kind: "swing", age: 0, dur: 0.4 }; o.lungeDir.set(hr.g.position.x - o.g.position.x, 0, hr.g.position.z - o.g.position.z).normalize();
    const pos = above(hr);
    setTimeout(() => { if (!alive_) return;
      if (p.miss) number(MISS[p.miss] || p.miss, pos, "#e8e8e8", { small: true });
      else if (p.amount) { number(`-${fmt(p.amount)}` + (p.crit ? "!" : ""), pos, "#ff6a5a", { crit: p.crit, small: !p.crit }); spark(lift(hr.g.position, 1.1), 0xff3b2f, 1.2); if (!hr.act) hr.act = { kind: "hurt", age: 0, dur: 0.3 }; if (m.boss && p.crit) cam.shake = 0.3; }
    }, 150);
  }

  function fire(e) {
    const hr = e.who ? heroOf.get(e.who) : null;
    const pos = hr ? hr.g.position.clone() : cam.target.clone();
    if (e.kind === "kill" || e.kind === "boss") {
      const o = world.mobs.get(e.m), p = o ? lift(o.g.position, o.H * 0.5) : lift(pos, 0.8);
      burst(p, e.kind === "boss" ? 0xd36bff : 0xff7a3c, e.kind === "boss" ? 140 : 34, e.kind === "boss" ? 9 : 5, e.kind === "boss" ? 1.6 : 0.9);
      if (e.kind === "boss") { number(`擊敗 ${e.m.title}！`, lift(p, 2.5), "#f0c6ff", { big: true }); flash.style.transition = "none"; flash.style.opacity = 0.55; requestAnimationFrame(() => { flash.style.transition = "opacity .9s"; flash.style.opacity = 0; }); showCard(`擊敗 ${e.m.title}`, clock(e.t)); cam.shake = 0.6; }
    } else if (e.kind === "wipe") { showCard(`${e.m.title}　首領戰失敗`, clock(e.t)); }
    else if (e.kind === "pull") { showCard(`首領戰：${e.m.title}`, clock(e.t)); }
    else if (e.kind === "level") { beam(pos, 0xffd76a); number("升級！", lift(pos, 2.6), "#ffe08a", { big: true }); }
    else if (e.kind === "death") { burst(lift(pos, 1), 0x9aa0a6, 30, 3, 1.2); number("☠", lift(pos, 2.6), "#e8e8e8", { big: true }); }
    else if (e.kind === "loot") { if (!fire.lootAt || now - fire.lootAt > 350) { fire.lootAt = now; spark(lift(pos, 1.2), 0xffd24a); number(e.text.replace(/^拾取 /, ""), lift(pos, 2.6), "#ffd86b", { small: true }); } }
    else if (e.kind === "quest") { burst(lift(pos, 1.2), 0xffe066, 40, 4, 1.1); number("！任務完成", lift(pos, 2.6), "#ffe066", { big: true }); }
    else if (e.kind === "achieve") { beam(pos, 0x8ad8ff); number("成就！", lift(pos, 2.6), "#bfe9ff", { big: true }); }
    else if (e.kind === "enter") { showCard(e.text, clock(e.t)); }
    else if (e.kind === "join") { beam(pos, 0x6ab8ff); }
    else if (e.kind === "leave") { burst(lift(pos, 1), 0x6ab8ff, 30, 3, 1); }
  }

  // ---- the loop
  let raf = 0, alive_ = true;
  const size = () => { const w = root.clientWidth, hh = root.clientHeight; renderer.setSize(w, hh, false); camera.aspect = w / hh; camera.updateProjectionMatrix(); };
  const ro = new ResizeObserver(size); ro.observe(root); size();
  function frame(ts) {
    if (!alive_) return;
    now = ts;
    const dt = Math.min(0.1, (ts - lastNow) / 1000); lastNow = ts;
    if (playing && !building && world && world.sc === sceneAt(g)) {
      g = Math.min(M.total, g + dt * +speed.value);
      slider.value = Math.round(g / M.total * 1000);
      if (g >= M.total) { setPlaying(false); credits(); }
    }
    update(dt);
    for (let k = fx.length - 1; k >= 0; k--) {
      const e = fx[k]; e.age += dt; const f = Math.min(1, e.age / e.life); e.step(dt, f);
      if (f >= 1) { fx.splice(k, 1); if (e.done) e.done(); scene.remove(e.o); dispose(e.o); }
    }
    cam.shake = Math.max(0, cam.shake - dt * 1.5);
    const j = cam.shake * cam.shake * 1.2, jit = () => (Math.random() - 0.5) * j;
    if (view === "chase") {
      camera.position.lerp(cam.pos, first ? 1 : Math.min(1, dt * 7)); camera.position.x += jit(); camera.position.y += jit();
      camera.lookAt(cam.look);
    } else {
      const s = Math.sin(cam.phi);
      camera.position.set(cam.target.x + cam.r * s * Math.sin(cam.theta) + jit(), cam.target.y + cam.r * Math.cos(cam.phi) + jit(), cam.target.z + cam.r * s * Math.cos(cam.theta) + jit());
      camera.lookAt(cam.target);
    }
    renderer.render(scene, camera);
    raf = requestAnimationFrame(frame);
  }
  raf = requestAnimationFrame(frame);
  close.focus();
  setTimeout(() => { if (alive_) setPlaying(true); }, 600);

  function shut() {
    alive_ = false; cancelAnimationFrame(raf); ro.disconnect();
    document.removeEventListener("keydown", onKey);
    dropFx(); disposeWorld(world);
    for (const list of Object.values(pools)) for (const o of list) dispose(o.g);
    for (const hr of heroes) { dispose(hr.g); if (hr.pet) dispose(hr.pet); }
    dispose(motes); sky.dispose();
    for (const geo of shared) geo.dispose();
    shadowMat.dispose();
    textures.forEach(p => p.then(t => t && t.dispose()));
    GLOW.dispose(); renderer.dispose(); renderer.forceContextLoss();
    root.remove(); document.body.style.overflow = prevOverflow;
    if (ctx.onClose) ctx.onClose();
  }
  return true;
}
