// Jointed figures for the replay: people (dressed by class or by name), beasts, spiders, oozes and
// the forms a shape-shifter takes. Every figure has pose({walk, phase, act, f, down, fall, t}).
import * as THREE from "./three.module.min.js";
import { lerp, glowTex } from "./scenery.js";

// ---------------------------------------------------------------- jointed figures
const BEAST = ["狼", "豹", "熊", "豬", "蜘蛛", "蛛", "鷹", "鷲", "蛇", "鱷", "狐", "狗", "鼠", "蠍", "獅", "虎", "蟹", "鳥", "猩", "蝙蝠", "迅猛龍", "山貓", "陸行鳥", "蜥蜴", "龜", "鹿", "牛", "羊", "馬", "駝", "貓", "蝠", "蟲"];
const BLOB = ["元素", "軟泥", "幽靈", "鬼魂", "泥漿", "精華", "怨靈"];
export const archetype = name => !name ? "humanoid" : BEAST.some(k => name.includes(k)) && !/人|魚人/.test(name) ? "beast" : BLOB.some(k => name.includes(k)) ? "blob" : "humanoid";
// the body of a beast, from its name
export const beastShape = name => !name ? "wolf" : /熊/.test(name) ? "bear" : /豹|貓|獅|虎|山貓/.test(name) ? "cat" : /鹿|羊|牛|馬|駝/.test(name) ? "stag"
  : /蜘蛛|蛛|蠍|蟹/.test(name) ? "spider" : /龜|鱷|蜥蜴|蛇/.test(name) ? "seal" : "wolf";
// how a person-shaped enemy looks, from its name
export function foeLook(name, hs) {
  const n = name || "";
  if (/豺狼人|狗頭人|鼠人|狼人/.test(n)) return { head: "snout", gear: "leather", weapon: ["axe", "mace", "dagger"][hs % 3], scale: /狗頭人/.test(n) ? 0.75 : 1.08 };
  if (/魚人/.test(n)) return { head: "fish", gear: "none", weapon: "dagger", scale: 0.85, skin: 0x5a8a6a };
  if (/骷髏|亡靈|殭屍|食屍鬼|骸骨|天譴|死亡/.test(n)) return { head: "skull", gear: "mail", weapon: ["sword", "axe"][hs % 2], skin: 0xd8d0b8 };
  if (/迪菲亞|盜匪|強盜|土匪|劫匪|盜賊|海盜/.test(n)) return { head: "bandana", gear: "leather", weapon: ["sword", "dagger", "axe"][hs % 3] };
  if (/法師|術士|祭司|巫|薩滿|先知|學徒|侍僧|教徒|秘法|召喚/.test(n)) return { head: "hood", gear: "cloth", robe: true, weapon: "staff", orb: [0xb98cff, 0xff8a3c, 0x8fd8ff][hs % 3] };
  if (/衛兵|騎士|士兵|守衛|戰士|隊長|指揮官|哨兵/.test(n)) return { head: "helm", gear: "plate", weapon: "sword", shield: true };
  return { head: ["none", "helm", "hood", "cap", "none"][hs % 5], gear: ["leather", "mail", "cloth"][hs % 3], weapon: ["sword", "axe", "mace", "dagger"][(hs >> 2) % 4] };
}
// how a player looks, from the class
export function heroLook(cls) {
  return ({
    WARRIOR: { gear: "plate", head: "helm", weapon: "sword", shield: true, spikes: true },
    PALADIN: { gear: "plate", head: "greathelm", weapon: "mace", shield: true },
    HUNTER: { gear: "mail", head: "cap", weapon: "bow" },
    ROGUE: { gear: "leather", head: "hood", weapon: "dagger", offhand: true, mask: true },
    SHAMAN: { gear: "mail", head: "helm", weapon: "mace", shield: true },
    MAGE: { gear: "cloth", head: "hat", weapon: "staff", robe: true, orb: 0x8fd8ff },
    WARLOCK: { gear: "cloth", head: "horns", weapon: "staff", robe: true, orb: 0xb98cff },
    PRIEST: { gear: "cloth", head: "hood", weapon: "staff", robe: true, orb: 0xffe680 },
    DRUID: { gear: "leather", head: "antlers", weapon: "staff", orb: 0x7dff6a },
  })[cls] || { gear: "mail", head: "none", weapon: "sword" };
}
// shape-shifts and states from the combat log's auras
export const FORMS = { "獵豹形態": "cat", "熊形態": "bear", "巨熊形態": "bear", "水棲形態": "seal", "旅行形態": "stag", "梟獸形態": "moonkin", "枭獸形態": "moonkin", "幽魂之狼": "ghostwolf" };
export const VEILS = { "潛行": "stealth", "隱形術": "stealth", "次級隱形術": "stealth", "影遁": "stealth", "暗影形態": "shadow" };

const G = {};
function geos() {
  if (G.ready) return G;
  const cap = (r, l, s = 8) => new THREE.CapsuleGeometry(r, l, 3, s);
  Object.assign(G, { ready: true,
    pelvis: new THREE.CylinderGeometry(0.19, 0.17, 0.18, 14).scale(1, 1, 0.66), thigh: cap(0.1, 0.3).translate(0, -0.21, 0), shin: cap(0.082, 0.3).translate(0, -0.2, 0),
    knee: new THREE.SphereGeometry(0.075, 8, 6), boot: new THREE.BoxGeometry(0.17, 0.12, 0.3).translate(0, -0.43, 0.05), cuff: new THREE.CylinderGeometry(0.1, 0.095, 0.1, 10).translate(0, -0.32, 0),
    abdomen: new THREE.CylinderGeometry(0.2, 0.185, 0.24, 14).scale(1, 1, 0.66), chest: new THREE.CylinderGeometry(0.265, 0.205, 0.36, 16).scale(1, 1, 0.62),
    plate: new THREE.SphereGeometry(0.2, 14, 10, -Math.PI / 2, Math.PI, 0, Math.PI).scale(1, 0.75, 0.35),
    strap: new THREE.BoxGeometry(0.07, 0.48, 0.03), neck: new THREE.CylinderGeometry(0.065, 0.075, 0.12, 8),
    head: new THREE.SphereGeometry(0.185, 18, 14), nose: new THREE.BoxGeometry(0.045, 0.06, 0.06), brow: new THREE.BoxGeometry(0.075, 0.018, 0.02), eye: new THREE.SphereGeometry(0.028, 8, 6),
    ear: new THREE.ConeGeometry(0.035, 0.09, 4), hair: new THREE.SphereGeometry(0.2, 16, 10, 0, Math.PI * 2, 0, Math.PI * 0.55),
    upper: cap(0.074, 0.22).translate(0, -0.15, 0), fore: cap(0.066, 0.21).translate(0, -0.15, 0), elbow: new THREE.SphereGeometry(0.07, 8, 6), glove: new THREE.SphereGeometry(0.075, 10, 8),
    padPlate: new THREE.SphereGeometry(0.17, 12, 8, 0, Math.PI * 2, 0, Math.PI * 0.6), padSoft: new THREE.SphereGeometry(0.13, 10, 8, 0, Math.PI * 2, 0, Math.PI * 0.55), spike: new THREE.ConeGeometry(0.04, 0.16, 6),
    belt: new THREE.CylinderGeometry(0.205, 0.205, 0.07, 16).scale(1, 1, 0.68), buckle: new THREE.BoxGeometry(0.08, 0.06, 0.02), tabard: new THREE.PlaneGeometry(0.24, 0.46).translate(0, -0.23, 0),
    robe: new THREE.CylinderGeometry(0.21, 0.37, 0.66, 12, 1, true).translate(0, -0.33, 0), cape: new THREE.PlaneGeometry(0.54, 1.02).translate(0, -0.51, 0),
    helm: new THREE.SphereGeometry(0.215, 16, 10, 0, Math.PI * 2, 0, Math.PI * 0.6), guard: new THREE.BoxGeometry(0.035, 0.13, 0.03),
    great: new THREE.CylinderGeometry(0.2, 0.215, 0.32, 14), slit: new THREE.BoxGeometry(0.22, 0.025, 0.02), plume: new THREE.ConeGeometry(0.05, 0.32, 6),
    hood: new THREE.SphereGeometry(0.235, 16, 12, 0, Math.PI * 2, 0, Math.PI * 0.72), hoodTip: new THREE.ConeGeometry(0.09, 0.22, 8),
    hat: new THREE.ConeGeometry(0.2, 0.52, 14), brim: new THREE.CylinderGeometry(0.33, 0.33, 0.025, 20), horn: new THREE.ConeGeometry(0.045, 0.26, 6),
    circlet: new THREE.TorusGeometry(0.19, 0.018, 6, 20), tine: new THREE.CylinderGeometry(0.012, 0.022, 0.2, 5), capTop: new THREE.SphereGeometry(0.205, 14, 8, 0, Math.PI * 2, 0, Math.PI * 0.42),
    feather: new THREE.ConeGeometry(0.03, 0.3, 4), mask: new THREE.BoxGeometry(0.3, 0.11, 0.08), band: new THREE.TorusGeometry(0.19, 0.03, 6, 18),
    snoutHead: new THREE.BoxGeometry(0.3, 0.28, 0.32), snout: new THREE.BoxGeometry(0.17, 0.13, 0.2), fin: new THREE.ConeGeometry(0.06, 0.24, 4), fishHead: new THREE.SphereGeometry(0.24, 14, 12),
    socket: new THREE.SphereGeometry(0.045, 8, 6), beak: new THREE.ConeGeometry(0.06, 0.16, 6),
    blade: new THREE.BoxGeometry(0.065, 0.82, 0.022).translate(0, 0.52, 0), xguard: new THREE.BoxGeometry(0.28, 0.045, 0.06).translate(0, 0.1, 0), grip: new THREE.CylinderGeometry(0.028, 0.028, 0.22, 6),
    pommel: new THREE.SphereGeometry(0.04, 8, 6).translate(0, -0.12, 0), staff: new THREE.CylinderGeometry(0.028, 0.034, 1.6, 6), orb: new THREE.SphereGeometry(0.095, 12, 10),
    prong: new THREE.ConeGeometry(0.025, 0.2, 5), maceHead: new THREE.DodecahedronGeometry(0.13, 0), axeHead: new THREE.BoxGeometry(0.03, 0.22, 0.26).translate(0, 0, 0.1),
    bow: new THREE.TorusGeometry(0.46, 0.022, 5, 22, Math.PI), shield: new THREE.CylinderGeometry(0.32, 0.32, 0.05, 16).rotateX(Math.PI / 2), boss: new THREE.SphereGeometry(0.08, 8, 6),
  });
  return G;
}
const mat = (c, kind = "cloth") => new THREE.MeshStandardMaterial({ color: c, transparent: true, roughness: kind === "metal" ? 0.32 : kind === "leather" ? 0.7 : 0.88, metalness: kind === "metal" ? 0.7 : 0.04 });

// A person facing +Z, about 1.95 tall. o: { armor, trim, skin, hair, eyes, gear, head, weapon, orb, shield, offhand, robe, cape, spikes, mask, scale }
export function humanoid(o) {
  const g = geos(), root = new THREE.Group(), body = new THREE.Group(); root.add(body);
  const gear = o.gear || "mail", metal = gear === "plate" || gear === "mail";
  const armor = mat(o.armor, gear === "plate" ? "metal" : gear === "leather" ? "leather" : "cloth");
  const trim = mat(o.trim, metal ? "metal" : "leather"), skin = mat(o.skin || 0xe0b48c), dark = mat(o.hair || 0x2a2018, "leather");
  const leather = mat(0x4a3424, "leather"), gold = mat(0xd8b048, "metal"), steel = mat(0xd8dde3, "metal"), wood = mat(0x6b4a2b, "leather");
  const eyeM = new THREE.MeshBasicMaterial({ color: o.eyes || 0x1a1410, transparent: true });
  const mats = [armor, trim, skin, dark, eyeM, leather, gold, steel, wood];
  const mk = (geo, m, x, y, z, parent = body) => { const n = new THREE.Mesh(geo, m); n.position.set(x, y, z); n.castShadow = true; parent.add(n); return n; };
  const pair = f => [-1, 1].map(f);
  // legs: hip → knee
  const hips = pair(s => { const hip = new THREE.Group(); hip.position.set(0.11 * s, 0.95, 0); body.add(hip);
    mk(g.thigh, o.robe ? dark : trim, 0, 0, 0, hip);
    const knee = new THREE.Group(); knee.position.y = -0.43; hip.add(knee); mk(g.knee, metal ? trim : dark, 0, 0, 0.02, knee);
    mk(g.shin, o.robe ? dark : trim, 0, 0, 0, knee); mk(g.cuff, leather, 0, 0, 0, knee); mk(g.boot, leather, 0, 0, 0, knee);
    return { hip, knee }; });
  mk(g.pelvis, o.robe ? armor : dark, 0, 0.97, 0);
  const torso = new THREE.Group(); torso.position.y = 1.05; body.add(torso);
  mk(g.abdomen, gear === "plate" ? trim : armor, 0, 0.12, 0, torso);
  const chest = mk(g.chest, armor, 0, 0.38, 0, torso);
  if (gear === "plate") mk(g.plate, trim, 0, 0.38, 0.1, torso);
  else if (gear === "leather" || gear === "mail") mk(g.strap, leather, 0.02, 0.3, 0.14, torso).rotation.z = 0.55;
  mk(g.belt, leather, 0, 0, 0, torso); mk(g.buckle, gold, 0, 0, 0.13, torso);
  if (gear === "plate") { const tb = mk(g.tabard, armor, 0, -0.02, 0.125, torso); tb.material = tb.material.clone(); tb.material.side = THREE.DoubleSide; tb.material.color.set(o.armor).multiplyScalar(0.85); mats.push(tb.material); }
  let robe = null;
  if (o.robe) { robe = mk(g.robe, armor, 0, 0.02, 0, torso); robe.material = armor.clone(); robe.material.side = THREE.DoubleSide; mats.push(robe.material); }
  if (o.cape) { const cm = mat(o.cape); cm.side = THREE.DoubleSide; mats.push(cm); const cape = mk(g.cape, cm, 0, 0.53, -0.16, torso); cape.castShadow = false; root.userData.cape = cape; }
  // head
  const neck = new THREE.Group(); neck.position.y = 0.57; torso.add(neck);
  mk(g.neck, skin, 0, 0.04, 0, neck);
  const head = new THREE.Group(); head.position.y = 0.2; neck.add(head);
  headgear(o.head || "none", head, { skin, dark, armor, trim, eyeM, gold, steel, mats, mask: o.mask }, mk, g);
  // shoulders and arms: shoulder → elbow → hand
  const arms = pair(s => {
    if (gear === "plate" || gear === "mail") { const p = mk(g.padPlate, trim, 0.3 * s, 0.53, 0, torso); p.scale.set(1.1, 0.85, 1.05);
      if (o.spikes) for (const z of [-0.05, 0.06]) { const sp = mk(g.spike, steel, 0.33 * s, 0.66, z, torso); sp.rotation.z = -0.5 * s; } }
    else mk(g.padSoft, gear === "cloth" ? trim : leather, 0.29 * s, 0.51, 0, torso).scale.set(1, 0.8, 1);
    const sh = new THREE.Group(); sh.position.set(0.31 * s, 0.48, 0); torso.add(sh);
    mk(g.upper, armor, 0, 0, 0, sh);
    const el = new THREE.Group(); el.position.y = -0.31; sh.add(el); mk(g.elbow, metal ? trim : armor, 0, 0, 0, el);
    mk(g.fore, gear === "cloth" ? armor : trim, 0, 0, 0, el); mk(g.glove, leather, 0, -0.31, 0, el);
    const hand = new THREE.Group(); hand.position.y = -0.33; el.add(hand);
    return { sh, el, hand }; });
  // what they carry
  let glow = null;
  const w = weapon(o.weapon, { steel, wood, gold, mats, orb: o.orb }, mk, g);
  if (w) { if (o.weapon === "bow") arms[0].hand.add(w); else arms[1].hand.add(w); glow = w.userData.glow || null; }
  if (o.offhand) { const w2 = weapon(o.weapon, { steel, wood, gold, mats }, mk, g); arms[0].hand.add(w2); }
  if (o.shield) { const sh = new THREE.Group(); mk(g.shield, trim, 0, 0, 0, sh); mk(g.boss, gold, 0, 0, 0.04, sh); sh.position.set(-0.06, 0.02, 0.08); sh.rotation.y = -Math.PI / 2; arms[0].hand.add(sh); }
  const S = o.scale || 1; root.scale.setScalar(S);
  if (o.bulk) body.scale.set(o.bulk, 1, o.bulk);
  const rig = { root, body, torso, chest, head, neck, legs: hips, arms, robe, mats, glow, base: mats.map(m => m.color.clone()), kind: "humanoid", height: 2.0 * S };
  rig.pose = st => humanoidPose(rig, st);
  return rig;
}
function headgear(kind, head, m, mk, g) {
  const face = (skin, eyes = true) => {
    mk(g.head, skin, 0, 0, 0, head).scale.set(1, 1.08, 1);
    if (eyes) { mk(g.eye, m.eyeM, -0.065, 0.02, 0.165, head); mk(g.eye, m.eyeM, 0.065, 0.02, 0.165, head);
      mk(g.brow, m.dark, -0.065, 0.065, 0.17, head); mk(g.brow, m.dark, 0.065, 0.065, 0.17, head); mk(g.nose, skin, 0, -0.02, 0.185, head); }
  };
  if (kind === "snout") {                                         // a dog-like head
    mk(g.snoutHead, m.skin, 0, 0, 0.02, head); mk(g.snout, m.dark, 0, -0.05, 0.22, head);
    mk(g.eye, m.eyeM, -0.08, 0.05, 0.17, head); mk(g.eye, m.eyeM, 0.08, 0.05, 0.17, head);
    for (const s of [-1, 1]) { const e = mk(g.ear, m.skin, 0.11 * s, 0.19, -0.02, head); e.scale.set(1.3, 1.6, 1); }
    return;
  }
  if (kind === "fish") {
    mk(g.fishHead, m.skin, 0, 0.02, 0.02, head).scale.set(1, 1.1, 1.05);
    for (const s of [-1, 1]) { mk(g.eye, m.eyeM, 0.12 * s, 0.07, 0.17, head).scale.setScalar(1.8); const f = mk(g.fin, m.dark, 0.2 * s, 0.05, -0.05, head); f.rotation.z = -1.2 * s; }
    mk(g.fin, m.dark, 0, 0.28, -0.04, head).scale.set(1, 1.3, 2.4);
    return;
  }
  if (kind === "owl") {
    mk(g.head, m.skin, 0, 0, 0, head).scale.set(1.25, 1.1, 1.15);
    for (const s of [-1, 1]) { const e = mk(g.eye, m.eyeM, 0.08 * s, 0.03, 0.19, head); e.scale.setScalar(2.4); const t = mk(g.ear, m.dark, 0.13 * s, 0.24, 0, head); t.scale.set(1.2, 2, 1); }
    const b = mk(g.beak, m.gold, 0, -0.05, 0.23, head); b.rotation.x = Math.PI / 2 + 0.3;
    return;
  }
  if (kind === "skull") {
    mk(g.head, m.skin, 0, 0, 0, head).scale.set(0.95, 1.08, 1);
    const sock = new THREE.MeshBasicMaterial({ color: 0x0a0806, transparent: true }); m.mats.push(sock);
    mk(g.socket, sock, -0.065, 0.02, 0.15, head); mk(g.socket, sock, 0.065, 0.02, 0.15, head);
    mk(g.eye, m.eyeM, -0.065, 0.02, 0.17, head).scale.setScalar(0.6); mk(g.eye, m.eyeM, 0.065, 0.02, 0.17, head).scale.setScalar(0.6);
    return;
  }
  face(m.skin);
  if (kind === "none" || kind === "bandana" || kind === "antlers" || kind === "cap") mk(g.hair, m.dark, 0, 0.02, -0.01, head);
  if (kind === "helm") { mk(g.helm, m.trim, 0, 0.02, 0, head); mk(g.guard, m.trim, 0, 0.0, 0.2, head); }
  else if (kind === "greathelm") { mk(g.great, m.trim, 0, 0.03, 0, head); const sl = new THREE.MeshBasicMaterial({ color: 0x0a0806, transparent: true }); m.mats.push(sl); mk(g.slit, sl, 0, 0.04, 0.205, head); mk(g.plume, m.armor, 0, 0.32, -0.02, head); }
  else if (kind === "hood") { const hd = mk(g.hood, m.armor, 0, 0.03, -0.03, head); hd.rotation.x = -0.25; const tp = mk(g.hoodTip, m.armor, 0, 0.05, -0.24, head); tp.rotation.x = -1.9; }
  else if (kind === "hat") { mk(g.brim, m.armor, 0, 0.12, 0, head); const h = mk(g.hat, m.armor, 0, 0.38, -0.03, head); h.rotation.x = -0.15; mk(g.hair, m.dark, 0, 0.0, -0.02, head); }
  else if (kind === "horns") { const hd = mk(g.hood, m.armor, 0, 0.03, -0.03, head); hd.rotation.x = -0.25; for (const s of [-1, 1]) { const hn = mk(g.horn, m.trim, 0.13 * s, 0.2, -0.02, head); hn.rotation.z = -0.6 * s; hn.rotation.x = -0.3; } }
  else if (kind === "antlers") { const c = mk(g.circlet, m.gold, 0, 0.09, 0, head); c.rotation.x = Math.PI / 2; for (const s of [-1, 1]) for (let k = 0; k < 3; k++) { const t = mk(g.tine, m.trim, (0.12 + k * 0.05) * s, 0.2 + k * 0.06, -0.02, head); t.rotation.z = (-0.5 - k * 0.25) * s; } }
  else if (kind === "cap") { mk(g.capTop, m.armor, 0, 0.04, 0, head); const f = mk(g.feather, m.gold, 0.12, 0.2, -0.08, head); f.rotation.z = -0.6; f.rotation.x = -0.5; }
  else if (kind === "bandana") { const mm = mat(0xa02020); m.mats.push(mm); mk(g.mask, mm, 0, -0.07, 0.11, head); const b = mk(g.band, mm, 0, 0.09, 0, head); b.rotation.x = Math.PI / 2; }
  if (m.mask) mk(g.mask, m.dark, 0, -0.07, 0.11, head);
}
function weapon(kind, m, mk, g) {
  if (!kind) return null;
  const w = new THREE.Group();
  if (kind === "sword" || kind === "dagger") {
    const k = kind === "dagger" ? 0.5 : 1;
    const b = mk(g.blade, m.steel, 0, 0, 0, w); b.scale.y = k; b.position.y = (1 - k) * -0.1;
    mk(g.xguard, m.gold, 0, 0, 0, w); mk(g.grip, m.wood, 0, 0, 0, w); mk(g.pommel, m.gold, 0, 0, 0, w);
    w.rotation.x = Math.PI / 2;
  } else if (kind === "mace" || kind === "axe") {
    const st = mk(g.staff, m.wood, 0, 0.25, 0, w); st.scale.y = 0.45;
    if (kind === "mace") { mk(g.maceHead, m.steel, 0, 0.62, 0, w); for (let k = 0; k < 4; k++) { const p = mk(g.prong, m.steel, Math.cos(k * 1.57) * 0.11, 0.62, Math.sin(k * 1.57) * 0.11, w); p.rotation.z = Math.cos(k * 1.57) * -1.57; p.rotation.x = Math.sin(k * 1.57) * 1.57; } }
    else mk(g.axeHead, m.steel, 0, 0.56, 0, w);
    w.rotation.x = Math.PI / 2;
  } else if (kind === "staff") {
    mk(g.staff, m.wood, 0, 0.2, 0, w);
    for (let k = 0; k < 3; k++) { const p = mk(g.prong, m.gold, Math.cos(k * 2.1) * 0.07, 1.0, Math.sin(k * 2.1) * 0.07, w); p.rotation.z = Math.cos(k * 2.1) * -0.5; p.rotation.x = Math.sin(k * 2.1) * 0.5; }
    const om = new THREE.MeshBasicMaterial({ color: m.orb || 0xffffff, transparent: true }); m.mats.push(om); mk(g.orb, om, 0, 1.06, 0, w);
    const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTex(), color: m.orb || 0xffffff, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false, opacity: 0.75 }));
    glow.position.y = 1.06; glow.scale.setScalar(0.6); w.add(glow); m.mats.push(glow.material); w.userData.glow = glow;
  } else if (kind === "bow") { const bw = mk(g.bow, m.wood, 0, 0, 0, w); bw.rotation.y = Math.PI / 2; }
  return w;
}
function humanoidPose(rig, { walk = 0, phase = 0, act = null, f = 0, down = false, fall = 1, t = 0 }) {
  const dn = down ? fall * fall * (3 - 2 * fall) : 0;              // falling over, eased
  const sw = Math.sin(phase) * 0.65 * walk;
  const [L, R] = rig.legs, [aL, aR] = rig.arms;
  L.hip.rotation.x = -sw; R.hip.rotation.x = sw;
  L.knee.rotation.x = Math.max(0, Math.sin(phase + 1.5)) * 1.1 * walk + 0.04; R.knee.rotation.x = Math.max(0, Math.sin(phase + 1.5 + Math.PI)) * 1.1 * walk + 0.04;
  let sL = sw * 0.6, sR = -sw * 0.6, eL = -0.2 - walk * 0.3, eR = -0.2 - walk * 0.3, zL = 0.07, zR = -0.07, twist = 0, lean = 0;
  if (!walk) { sL = Math.sin(t * 1.6) * 0.04; sR = -sL; }
  if (act === "swing") { if (f < 0.35) { const k = f / 0.35; sR = lerp(0, -2.7, k); eR = lerp(-0.2, -1.3, k); } else { const k = Math.min(1, (f - 0.35) / 0.3); sR = lerp(-2.7, 0.3, k); eR = lerp(-1.3, -0.1, k); } twist = Math.sin(f * Math.PI) * 0.45; }
  else if (act === "cast") { const k = Math.sin(f * Math.PI); sR = -1.3 * k - 0.1; eR = -0.5 * k - 0.15; sL = -1.1 * k; eL = -0.6 * k - 0.15; zL = 0.07 + k * 0.3; zR = -0.07 - k * 0.3; }
  else if (act === "shoot") { const k = Math.sin(Math.min(1, f * 1.5) * Math.PI / 2); sL = -1.55 * k; eL = -0.05; sR = -1.45 * k; eR = -1.8 * k; twist = -0.45 * k; }
  else if (act === "hurt") lean = Math.sin(f * Math.PI) * 0.18;
  aL.sh.rotation.x = sL; aR.sh.rotation.x = sR; aL.el.rotation.x = eL; aR.el.rotation.x = eR; aL.sh.rotation.z = zL; aR.sh.rotation.z = zR;
  rig.torso.rotation.y = twist; rig.torso.rotation.x = -lean + walk * 0.06;
  rig.chest.scale.y = 1 + Math.sin(t * 1.6) * 0.012;
  rig.head.rotation.y = Math.sin(t * 0.4) * 0.12 * (1 - walk);
  if (rig.robe) rig.robe.rotation.x = sw * 0.12;
  rig.body.rotation.x = -Math.PI / 2 * 0.95 * dn; rig.body.position.y = 0.22 * dn;
  if (rig.root.userData.cape) rig.root.userData.cape.rotation.x = 0.12 + walk * 0.4 + Math.sin(t * 3) * 0.04;
}

// Four legs (or eight). o: { color, shape: wolf|cat|bear|stag|seal|spider, scale, ghost }
const SHAPES = {
  wolf: { body: [0.48, 0.42, 1.0], leg: 0.36, legR: 0.075, head: [0.32, 0.29, 0.38], snout: [0.17, 0.14, 0.24], ears: "point", tail: 0.55, neckY: 0.86 },
  cat: { body: [0.4, 0.36, 1.08], leg: 0.34, legR: 0.068, head: [0.3, 0.26, 0.3], snout: [0.14, 0.1, 0.1], ears: "point", tail: 0.8, neckY: 0.78 },
  bear: { body: [0.82, 0.72, 1.25], leg: 0.36, legR: 0.13, head: [0.46, 0.4, 0.42], snout: [0.22, 0.17, 0.18], ears: "round", tail: 0.1, neckY: 1.0, hump: true },
  stag: { body: [0.44, 0.44, 1.0], leg: 0.62, legR: 0.055, head: [0.22, 0.24, 0.36], snout: [0.12, 0.12, 0.14], ears: "point", tail: 0.15, neckY: 1.28, antlers: true },
  seal: { body: [0.55, 0.36, 1.3], leg: 0.1, legR: 0.09, head: [0.34, 0.3, 0.34], snout: [0.16, 0.12, 0.12], ears: null, tail: 0.3, neckY: 0.5 },
};
export function beast(o) {
  if (o.shape === "spider") return spider(o);
  const P = SHAPES[o.shape] || SHAPES.wolf, root = new THREE.Group(), body = new THREE.Group(); root.add(body);
  const fur = new THREE.MeshStandardMaterial({ color: o.color, roughness: 0.85, flatShading: true, transparent: true });
  const dark = new THREE.MeshStandardMaterial({ color: new THREE.Color(o.color).multiplyScalar(0.5), roughness: 0.9, transparent: true });
  const eyeM = new THREE.MeshBasicMaterial({ color: o.ghost ? 0xd8f4ff : 0xffd23c, transparent: true });
  const horn = new THREE.MeshStandardMaterial({ color: 0xd8cbb0, roughness: 0.6, transparent: true });
  if (o.ghost) for (const m of [fur, dark]) { m.emissive = new THREE.Color(0x4aa8ff); m.emissiveIntensity = 0.7; }
  const mk = (geo, m, x, y, z, parent = body) => { const n = new THREE.Mesh(geo, m); n.position.set(x, y, z); n.castShadow = true; parent.add(n); return n; };
  const lh = P.leg + 0.16, [bw, bh, bl] = P.body;
  const trunk = mk(new THREE.CapsuleGeometry(bh / 2, bl - bh, 4, 10).rotateX(Math.PI / 2), fur, 0, lh + bh * 0.35, 0); trunk.scale.x = bw / bh;
  if (P.hump) mk(new THREE.SphereGeometry(bh * 0.45, 10, 8), fur, 0, lh + bh * 0.75, bl * 0.18);
  const neck = new THREE.Group(); neck.position.set(0, P.neckY, bl / 2 + 0.02); body.add(neck);
  if (P.neckY - (lh + bh * 0.35) > 0.25) { const len = P.neckY - (lh + bh * 0.35); const nk = mk(new THREE.CylinderGeometry(0.08, 0.12, len + 0.1, 8), fur, 0, -len / 2, -0.05, neck); nk.rotation.x = 0.35; }
  const [hw, hh, hl] = P.head, [sw, sh, sl] = P.snout;
  mk(new THREE.BoxGeometry(hw, hh, hl), fur, 0, 0.04, hl * 0.4, neck);
  mk(new THREE.BoxGeometry(sw, sh, sl), dark, 0, -0.02, hl * 0.4 + hl / 2 + sl / 2 - 0.02, neck);
  for (const s of [-1, 1]) {
    mk(new THREE.SphereGeometry(0.032, 8, 6), eyeM, hw * 0.32 * s, hh * 0.22, hl * 0.4 + hl / 2, neck);
    if (P.ears === "point") mk(new THREE.ConeGeometry(0.06, 0.16, 4), dark, hw * 0.3 * s, hh / 2 + 0.07, hl * 0.25, neck);
    else if (P.ears === "round") mk(new THREE.SphereGeometry(0.07, 8, 6), dark, hw * 0.38 * s, hh / 2 + 0.02, hl * 0.2, neck);
    if (P.antlers) for (let k = 0; k < 3; k++) { const t = mk(new THREE.CylinderGeometry(0.012, 0.025, 0.28, 5), horn, (0.08 + k * 0.06) * s, hh / 2 + 0.12 + k * 0.1, hl * 0.2 - k * 0.04, neck); t.rotation.z = (-0.5 - k * 0.3) * s; t.rotation.x = -0.3; }
  }
  const legGeo = new THREE.CapsuleGeometry(P.legR, P.leg, 3, 6).translate(0, -(P.leg / 2 + P.legR), 0);
  const legs = [[-1, 1], [1, 1], [-1, -1], [1, -1]].map(([sx, sz]) => { const l = new THREE.Group(); l.position.set(bw * 0.36 * sx, lh, bl * 0.36 * sz); body.add(l); mk(legGeo, dark, 0, 0, 0, l); return l; });
  const tail = new THREE.Group(); tail.position.set(0, lh + bh * 0.5, -bl / 2); body.add(tail);
  if (P.tail > 0.12) mk(new THREE.CylinderGeometry(0.025, 0.06, P.tail, 6).rotateX(-1.1).translate(0, P.tail * 0.2, -P.tail * 0.45), dark, 0, 0, 0, tail);
  else mk(new THREE.SphereGeometry(0.07, 6, 5), dark, 0, 0, 0, tail);
  const mats = [fur, dark, eyeM, horn];
  if (o.ghost) for (const m of mats) m.opacity = 0.55;
  const S = o.scale || 1; root.scale.setScalar(S);
  const rig = { root, body, neck, legs, tail, mats, base: mats.map(m => m.color.clone()), kind: "beast", height: (P.neckY + hh / 2 + 0.1) * S, ghost: !!o.ghost };
  rig.pose = ({ walk = 0, phase = 0, act = null, f = 0, down = false, fall = 1, t = 0 }) => {
    const s = Math.sin(phase * 1.3) * 0.6 * walk, dn = down ? fall * fall * (3 - 2 * fall) : 0;
    legs[0].rotation.x = s; legs[3].rotation.x = s; legs[1].rotation.x = -s; legs[2].rotation.x = -s;
    const bite = act === "swing" || act === "cast" ? Math.sin(f * Math.PI) : 0;
    neck.rotation.x = -bite * 0.6 + Math.sin(t * 2) * 0.04;
    neck.position.z = bl / 2 + 0.02 + bite * 0.25;
    tail.rotation.y = Math.sin(t * 4) * 0.4;
    body.rotation.z = Math.PI / 2 * 0.95 * dn; body.position.y = down ? -0.2 * dn : Math.abs(s) * 0.05;
  };
  return rig;
}
function spider(o) {
  const root = new THREE.Group(), body = new THREE.Group(); root.add(body);
  const shell = new THREE.MeshStandardMaterial({ color: o.color, roughness: 0.5, flatShading: true, transparent: true });
  const dark = new THREE.MeshStandardMaterial({ color: new THREE.Color(o.color).multiplyScalar(0.45), roughness: 0.6, transparent: true });
  const eyeM = new THREE.MeshBasicMaterial({ color: 0xff3030, transparent: true });
  const mk = (geo, m, x, y, z, parent = body) => { const n = new THREE.Mesh(geo, m); n.position.set(x, y, z); n.castShadow = true; parent.add(n); return n; };
  mk(new THREE.SphereGeometry(0.42, 12, 10), shell, 0, 0.6, -0.4).scale.set(1, 0.85, 1.15);
  const front = new THREE.Group(); front.position.set(0, 0.55, 0.15); body.add(front);
  mk(new THREE.SphereGeometry(0.24, 10, 8), dark, 0, 0, 0, front);
  for (const s of [-1, 1]) for (let k = 0; k < 2; k++) mk(new THREE.SphereGeometry(0.035, 6, 5), eyeM, (0.06 + k * 0.06) * s, 0.08 + k * 0.03, 0.21, front);
  const up = new THREE.CapsuleGeometry(0.035, 0.42, 3, 5).translate(0, 0.25, 0), low = new THREE.CapsuleGeometry(0.03, 0.5, 3, 5).translate(0, -0.28, 0);
  const legs = [];
  for (const s of [-1, 1]) for (let k = 0; k < 4; k++) {
    const hip = new THREE.Group(); hip.position.set(0.18 * s, 0.55, 0.22 - k * 0.15); hip.rotation.y = (-0.6 + k * 0.4) * s; body.add(hip);
    const a = new THREE.Group(); a.rotation.z = -1.0 * s; hip.add(a); mk(up, dark, 0, 0, 0, a);
    const b = new THREE.Group(); b.position.y = 0.5; b.rotation.z = 1.9 * s; a.add(b); mk(low, dark, 0, 0, 0, b);
    legs.push({ a, s, k });
  }
  const mats = [shell, dark, eyeM];
  const S = o.scale || 1; root.scale.setScalar(S);
  const rig = { root, body, legs, mats, base: mats.map(m => m.color.clone()), kind: "beast", height: 1.1 * S };
  rig.pose = ({ walk = 0, phase = 0, act = null, f = 0, down = false, fall = 1 }) => {
    const dn = down ? fall * fall * (3 - 2 * fall) : 0;
    for (const l of legs) l.a.rotation.x = Math.sin(phase * 2 + l.k * 1.6 + (l.s > 0 ? Math.PI : 0)) * 0.35 * walk;
    front.rotation.x = act ? -Math.sin(f * Math.PI) * 0.5 : 0;
    body.rotation.z = Math.PI * 0.95 * dn; body.position.y = 0.9 * dn;
  };
  return rig;
}

export function blob(opts) {
  const root = new THREE.Group(), body = new THREE.Group(); root.add(body);
  const m = new THREE.MeshStandardMaterial({ color: opts.color, roughness: 0.25, metalness: 0.1, transparent: true, opacity: 0.88, emissive: new THREE.Color(opts.color).multiplyScalar(0.25), flatShading: true });
  const eyeM = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true });
  const core = new THREE.Mesh(new THREE.IcosahedronGeometry(0.62, 1), m); core.position.y = 0.65; core.castShadow = true; body.add(core);
  for (const x of [-0.18, 0.18]) { const e = new THREE.Mesh(new THREE.SphereGeometry(0.06, 8, 6), eyeM); e.position.set(x, 0.85, 0.52); body.add(e); }
  const S = opts.scale || 1; root.scale.setScalar(S);
  const rig = { root, body, core, mats: [m, eyeM], base: [m.color.clone(), eyeM.color.clone()], kind: "blob", height: 1.35 * S };
  rig.pose = ({ walk = 0, act = null, f = 0, down = false, fall = 1, t = 0 }) => {
    const sq = Math.sin(t * (walk ? 9 : 3)) * (walk ? 0.12 : 0.05) + (act === "swing" ? Math.sin(f * Math.PI) * 0.3 : 0);
    core.scale.set(1 + sq * 0.5, 1 - sq, 1 + sq * 0.5);
    body.position.y = down ? -0.4 * fall : 0; body.scale.y = down ? 1 - 0.65 * fall : 1;
  };
  return rig;
}

// A shape-shifted player: the form's own body, coloured for the class.
export function formRig(form) {
  if (form === "moonkin") return humanoid({ armor: 0x6a5a44, trim: 0x4a3c2c, skin: 0x8a7458, hair: 0x4a3c2c, eyes: 0xffe680, gear: "none", head: "owl", bulk: 1.45, scale: 1.18 });
  if (form === "ghostwolf") return beast({ color: 0x8ac8ff, shape: "wolf", ghost: true, scale: 1.15 });
  const look = { cat: [0x3a3046, "cat", 1.1], bear: [0x5a3a24, "bear", 1.15], seal: [0x6a7a8a, "seal", 1.0], stag: [0x8a6a44, "stag", 1.05] }[form] || [0x6b5034, "wolf", 1];
  return beast({ color: look[0], shape: look[1], scale: look[2] });
}

// A winged mount for a flight path: flap() beats the wings.
export function bird(color) {
  const root = new THREE.Group();
  const fur = new THREE.MeshStandardMaterial({ color, roughness: 0.8, flatShading: true, transparent: true });
  const pale = new THREE.MeshStandardMaterial({ color: 0xe8e0d0, roughness: 0.8, flatShading: true, transparent: true });
  const beak = new THREE.MeshStandardMaterial({ color: 0xe0a030, roughness: 0.6, transparent: true });
  const mk = (geo, m, x, y, z, parent = root) => { const n = new THREE.Mesh(geo, m); n.position.set(x, y, z); n.castShadow = true; parent.add(n); return n; };
  mk(new THREE.CapsuleGeometry(0.42, 1.3, 4, 10).rotateX(Math.PI / 2), fur, 0, 0, 0);
  mk(new THREE.SphereGeometry(0.34, 12, 10), pale, 0, 0.42, 1.1);
  mk(new THREE.ConeGeometry(0.13, 0.45, 6).rotateX(Math.PI / 2), beak, 0, 0.36, 1.5);
  mk(new THREE.ConeGeometry(0.3, 1.0, 4).rotateX(-Math.PI / 2).scale(1, 0.3, 1), fur, 0, 0.05, -1.3);
  const wingGeo = new THREE.BufferGeometry();
  wingGeo.setAttribute("position", new THREE.Float32BufferAttribute([0, 0, 0.6, 0, 0, -0.5, 1.5, 0, 0.1, 1.5, 0, 0.1, 0, 0, -0.5, 1.7, 0, -0.9, 1.5, 0, 0.1, 1.7, 0, -0.9, 3.0, 0, -0.4], 3));
  wingGeo.computeVertexNormals();
  const wm = fur.clone(); wm.side = THREE.DoubleSide;
  const wings = [-1, 1].map(s => { const w = new THREE.Group(); w.position.set(0.3 * s, 0.15, 0.1); const m = new THREE.Mesh(wingGeo, wm); m.scale.x = s; m.castShadow = true; w.add(m); root.add(w); return w; });
  const mats = [fur, pale, beak, wm];
  return { root, mats, base: mats.map(m => m.color.clone()), flap(t) { const a = Math.sin(t * 7) * 0.55; wings[0].rotation.z = -a; wings[1].rotation.z = a; root.position.y = Math.sin(t * 7 + 1) * 0.12; } };
}
