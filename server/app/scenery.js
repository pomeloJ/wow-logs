// Made-up 3D surroundings for the replay's third-person view: terrain coloured after the map
// image, sky by the hour, trees, grass, a mine-like dungeon, and jointed figures. Everything is
// generated here from shapes and canvas textures; nothing is taken from the game.
import * as THREE from "./three.module.min.js";

export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const lerp = (a, b, f) => a + (b - a) * f;
export function rng(seed) { let s = seed >>> 0 || 1; return () => { s ^= s << 13; s >>>= 0; s ^= s >> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; }; }
export const hash = str => { let h = 2166136261; for (const c of String(str)) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619); } return h >>> 0; };

// ---------------------------------------------------------------- noise
export function noise(seed) {
  const s = (seed % 1000) * 0.731;
  const h = (i, j) => { const v = Math.sin(i * 127.1 + j * 311.7 + s) * 43758.5453; return v - Math.floor(v); };
  const one = (x, z) => {
    const i = Math.floor(x), j = Math.floor(z), fx = x - i, fz = z - j;
    const u = fx * fx * (3 - 2 * fx), v = fz * fz * (3 - 2 * fz);
    return lerp(lerp(h(i, j), h(i + 1, j), u), lerp(h(i, j + 1), h(i + 1, j + 1), u), v);
  };
  return (x, z, oct = 4) => { let a = 0, amp = 0.5, f = 1; for (let k = 0; k < oct; k++) { a += one(x * f, z * f) * amp; f *= 2.03; amp *= 0.5; } return a; };
}

export function canvasTexture(w, h, draw, repeat) {
  const c = document.createElement("canvas"); c.width = w; c.height = h;
  draw(c.getContext("2d"), w, h);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace;
  if (repeat) { t.wrapS = t.wrapT = THREE.RepeatWrapping; }
  return t;
}
const detailTex = seed => canvasTexture(256, 256, (c, w) => {
  const r = rng(seed); c.fillStyle = "#c8c8c8"; c.fillRect(0, 0, w, w);
  for (let k = 0; k < 5000; k++) { const v = 150 + r() * 105 | 0; c.fillStyle = `rgba(${v},${v},${v},.35)`; c.fillRect(r() * w, r() * w, 1 + r() * 3, 1 + r() * 3); }
}, true);
const brickTex = (seed, base) => canvasTexture(256, 256, (c, w) => {
  const r = rng(seed); c.fillStyle = "#1e1914"; c.fillRect(0, 0, w, w);
  for (let y = 0; y < 8; y++) for (let x = -1; x < 4; x++) {
    const v = base + r() * 28 | 0; c.fillStyle = `rgb(${v + 8},${v},${v - 10})`;
    c.fillRect(x * 64 + (y % 2) * 32 + 2, y * 32 + 2, 60, 28);
    for (let k = 0; k < 12; k++) { c.fillStyle = `rgba(0,0,0,${r() * .18})`; c.fillRect(x * 64 + (y % 2) * 32 + r() * 58, y * 32 + r() * 26, 4, 3); }
  }
}, true);
const flagTex = seed => canvasTexture(256, 256, (c, w) => {
  const r = rng(seed); c.fillStyle = "#3b342c"; c.fillRect(0, 0, w, w);
  for (let k = 0; k < 26; k++) {
    const x = r() * w, y = r() * w, s = 30 + r() * 50, v = 70 + r() * 40 | 0;
    c.fillStyle = `rgb(${v + 6},${v},${v - 8})`; c.beginPath();
    for (let a = 0; a < 6; a++) { const ang = a / 6 * Math.PI * 2 + r() * .4, rr = s * (.5 + r() * .3); c.lineTo(x + Math.cos(ang) * rr, y + Math.sin(ang) * rr); }
    c.fill();
  }
}, true);
const woodTex = seed => canvasTexture(64, 256, (c, w, h) => {
  const r = rng(seed); c.fillStyle = "#6b4a2b"; c.fillRect(0, 0, w, h);
  for (let k = 0; k < 40; k++) { c.fillStyle = `rgba(30,18,8,${.1 + r() * .25})`; c.fillRect(r() * w, 0, 1 + r() * 2, h); }
}, true);
const cloudTex = () => canvasTexture(256, 128, (c, w, h) => {
  const r = rng(11);
  for (let k = 0; k < 18; k++) { const x = w * (.2 + r() * .6), y = h * (.45 + r() * .2), rr = 20 + r() * 34;
    const g = c.createRadialGradient(x, y, 0, x, y, rr); g.addColorStop(0, "rgba(255,255,255,.75)"); g.addColorStop(1, "rgba(255,255,255,0)"); c.fillStyle = g; c.fillRect(0, 0, w, h); }
});

// ---------------------------------------------------------------- sky by the hour
const KEYS = [   // hour, zenith, horizon, sun colour, light
  [0, "#0b1230", "#232c4c", "#9fb4ff", 0.42], [4.5, "#0e1636", "#2c3656", "#9fb4ff", 0.44], [6, "#3b5a8f", "#f2a27a", "#ffb37a", 0.75],
  [8, "#3f7fd2", "#bcdcf2", "#fff1d8", 1.25], [16, "#3a7ad0", "#c4e0f2", "#fff1d8", 1.25], [18.5, "#35508e", "#ff9e62", "#ffad6a", 0.8],
  [20.5, "#151d40", "#3a3656", "#a0b0ff", 0.46], [24, "#0b1230", "#232c4c", "#9fb4ff", 0.42]];
export function skyAt(hour) {
  let k = 0; while (k < KEYS.length - 2 && KEYS[k + 1][0] <= hour) k++;
  const [h0, z0, o0, s0, l0] = KEYS[k], [h1, z1, o1, s1, l1] = KEYS[k + 1], f = clamp((hour - h0) / (h1 - h0), 0, 1);
  const mix = (a, b) => new THREE.Color(a).lerp(new THREE.Color(b), f);
  const elev = Math.sin((hour - 6) / 12 * Math.PI);              // the sun above the horizon by day, the moon by night
  const az = (hour / 24) * Math.PI * 2;
  const dir = new THREE.Vector3(Math.cos(az) * 0.8, Math.max(0.12, Math.abs(elev)), Math.sin(az) * 0.8).normalize();
  return { zenith: mix(z0, z1), horizon: mix(o0, o1), sun: mix(s0, s1), light: lerp(l0, l1, f), night: clamp((0.62 - lerp(l0, l1, f)) / 0.3, 0, 1), dir };
}
export function makeSky() {
  const group = new THREE.Group();
  const mat = new THREE.ShaderMaterial({
    uniforms: { top: { value: new THREE.Color() }, horizon: { value: new THREE.Color() }, sunDir: { value: new THREE.Vector3(0, 1, 0) }, sunColor: { value: new THREE.Color() } },
    vertexShader: "varying vec3 vDir; void main(){ vDir = normalize(position); vec4 p = projectionMatrix * modelViewMatrix * vec4(position, 1.0); gl_Position = p.xyww; }",
    fragmentShader: "uniform vec3 top; uniform vec3 horizon; uniform vec3 sunDir; uniform vec3 sunColor; varying vec3 vDir;" +
      "void main(){ float h = vDir.y; vec3 c = h > 0.0 ? mix(horizon, top, pow(h, 0.55)) : mix(horizon, horizon * 0.55, pow(-h, 0.5));" +
      "float s = max(dot(normalize(vDir), normalize(sunDir)), 0.0); c += sunColor * (pow(s, 900.0) * 3.0 + pow(s, 10.0) * 0.22); gl_FragColor = vec4(pow(c, vec3(1.0 / 2.2)), 1.0); }",
    side: THREE.BackSide, depthWrite: false, fog: false });
  const dome = new THREE.Mesh(new THREE.SphereGeometry(1000, 32, 16), mat); dome.renderOrder = -2; dome.frustumCulled = false;
  // stars for the night
  const n = 900, p = new Float32Array(n * 3), r = rng(5);
  for (let k = 0; k < n; k++) { const a = r() * Math.PI * 2, y = 0.08 + r() * 0.92, q = Math.sqrt(1 - y * y); p[k * 3] = Math.cos(a) * q * 950; p[k * 3 + 1] = y * 950; p[k * 3 + 2] = Math.sin(a) * q * 950; }
  const sg = new THREE.BufferGeometry(); sg.setAttribute("position", new THREE.BufferAttribute(p, 3));
  const stars = new THREE.Points(sg, new THREE.PointsMaterial({ color: 0xffffff, size: 2.2, sizeAttenuation: false, transparent: true, opacity: 0, depthWrite: false, fog: false }));
  stars.renderOrder = -1; stars.frustumCulled = false;
  // clouds drifting high up
  const ct = cloudTex(), clouds = [];
  for (let k = 0; k < 26; k++) {
    const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: ct, transparent: true, depthWrite: false, fog: false, opacity: 0.8 }));
    const a = r() * Math.PI * 2, d = 150 + r() * 500; s.position.set(Math.cos(a) * d, 110 + r() * 90, Math.sin(a) * d);
    s.scale.set(140 + r() * 120, 60 + r() * 40, 1); s.renderOrder = -1; clouds.push(s); group.add(s);
  }
  group.add(dome, stars);
  return {
    group,
    set(sky) {
      mat.uniforms.top.value.copy(sky.zenith); mat.uniforms.horizon.value.copy(sky.horizon);
      mat.uniforms.sunDir.value.copy(sky.dir); mat.uniforms.sunColor.value.copy(sky.sun);
      stars.material.opacity = sky.night;
      for (const c of clouds) { c.material.color.copy(sky.horizon).lerp(new THREE.Color(0xffffff), 0.55).multiplyScalar(0.4 + sky.light * 0.5); c.material.opacity = 0.75 - sky.night * 0.45; }
    },
    follow(pos, dt) { group.position.set(pos.x, 0, pos.z); for (const c of clouds) { c.position.x += dt * 2; if (c.position.x > 650) c.position.x -= 1300; } },
    dispose() { group.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); }); ct.dispose(); },
  };
}

// ---------------------------------------------------------------- terrain, coloured after the map image
export function imageSampler(img) {
  if (!img) return null;
  const W = 256, H = Math.round(256 * img.height / img.width);
  const c = document.createElement("canvas"); c.width = W; c.height = H;
  const x = c.getContext("2d", { willReadFrequently: true });
  try { x.filter = "blur(2.5px)"; x.drawImage(img, 0, 0, W, H); var d = x.getImageData(0, 0, W, H).data; } catch (e) { return null; }
  return (u, v) => {                                              // u, v in 0..1 over the whole map
    const fx = clamp(u, 0, 1) * (W - 1), fy = clamp(v, 0, 1) * (H - 1), i = Math.floor(fx), j = Math.floor(fy), a = fx - i, b = fy - j;
    const px = (ii, jj) => { const o = (Math.min(H - 1, jj) * W + Math.min(W - 1, ii)) * 4; return [d[o] / 255, d[o + 1] / 255, d[o + 2] / 255]; };
    const p00 = px(i, j), p10 = px(i + 1, j), p01 = px(i, j + 1), p11 = px(i + 1, j + 1);
    return [0, 1, 2].map(k => lerp(lerp(p00[k], p10[k], a), lerp(p01[k], p11[k], a), b));
  };
}
const segDist = (x, z, a, b) => { const dx = b[0] - a[0], dz = b[1] - a[1], l = dx * dx + dz * dz || 1; const t = clamp(((x - a[0]) * dx + (z - a[1]) * dz) / l, 0, 1); return Math.hypot(x - a[0] - dx * t, z - a[1] - dz * t); };

// What the ground is like at a map colour: water, snow, forest, dry or grass.
function biome(rgb) {
  if (!rgb) return { water: 0, snow: 0, forest: 0.45, dry: 0 };
  const [r, g, b] = rgb, lum = 0.3 * r + 0.59 * g + 0.11 * b, sat = Math.max(r, g, b) - Math.min(r, g, b);
  return { water: clamp((b - Math.max(r, g * 0.92) - 0.04) * 8, 0, 1) * (lum < 0.8 ? 1 : 0), snow: clamp((lum - 0.72) * 6, 0, 1) * clamp(1 - sat * 5, 0, 1),
    forest: clamp((g - (r + b) / 2) * 6, 0, 1) * clamp((0.55 - lum) * 4, 0, 1), dry: clamp((r - b - 0.12) * 4, 0, 1) * clamp((r - g + 0.08) * 6, 0, 1) };
}

// opts: { cx, cz, size, seed, sample(x, z) -> map rgb or null, avoid: [[x, z], ...] polyline to keep clear, road }
export function terrain(opts) {
  const { size, seed } = opts, n2 = noise(seed), n3 = noise(seed + 17);
  const seg = clamp(Math.round(size / 1.7), 80, 360);
  const geo = new THREE.PlaneGeometry(size, size, seg, seg); geo.rotateX(-Math.PI / 2);
  geo.translate(opts.cx, 0, opts.cz);
  const pos = geo.attributes.position, col = new Float32Array(pos.count * 3);
  let avoid = opts.avoid || [];
  if (avoid.length > 300) { const k = Math.ceil(avoid.length / 300); avoid = avoid.filter((_, i) => i % k === 0 || i === avoid.length - 1); }
  const near = (x, z) => { let d = 1e9; for (let k = 1; k < avoid.length; k++) d = Math.min(d, segDist(x, z, avoid[k - 1], avoid[k])); return d; };
  // a coarse grid of distances to the path, so each vertex need not walk the whole path
  const G = 96, cell = size / G, grid = new Float32Array((G + 1) * (G + 1));
  for (let j = 0; j <= G; j++) for (let i = 0; i <= G; i++) grid[j * (G + 1) + i] = avoid.length > 1 ? near(opts.cx - size / 2 + i * cell, opts.cz - size / 2 + j * cell) : 1e9;
  const pathDist = (x, z) => { const fi = clamp((x - opts.cx + size / 2) / cell, 0, G), fj = clamp((z - opts.cz + size / 2) / cell, 0, G), i = Math.min(G - 1, Math.floor(fi)), j = Math.min(G - 1, Math.floor(fj)), a = fi - i, b = fj - j;
    return lerp(lerp(grid[j * (G + 1) + i], grid[j * (G + 1) + i + 1], a), lerp(grid[(j + 1) * (G + 1) + i], grid[(j + 1) * (G + 1) + i + 1], a), b); };
  const base = new THREE.Color(), c = new THREE.Color(), rock = new THREE.Color(0x7a7266), snow = new THREE.Color(0xeef2f6), dirt = new THREE.Color(0x8a6c47), sand = new THREE.Color(0xb8a27a);
  const fallback = opts.palette || [0.3, 0.46, 0.2], avg = new THREE.Color(0, 0, 0);
  const info = (x, z) => { const rgb = opts.sample ? opts.sample(x, z) : null; return { rgb, b: biome(rgb) }; };
  const heightRaw = (x, z, b) => {
    const hills = (n2(x * 0.006, z * 0.006, 4) - 0.45) * 26 + (n3(x * 0.03, z * 0.03, 3) - 0.5) * 2.2;
    let h = hills * (0.55 + b.snow * 0.9);
    h = lerp(h, -3.2, b.water);
    const pd = pathDist(x, z);                                       // the way walked stays fairly level
    if (pd < 14) h = lerp(h, h * 0.35 + 0.4, 1 - pd / 14);
    return h;
  };
  for (let k = 0; k < pos.count; k++) {
    const x = pos.getX(k), z = pos.getZ(k), { rgb, b } = info(x, z);
    const h = heightRaw(x, z, b); pos.setY(k, h);
    if (rgb) base.setRGB(rgb[0], rgb[1], rgb[2]).convertSRGBToLinear(); else base.setRGB(fallback[0], fallback[1], fallback[2]);
    const hsl = {}; base.getHSL(hsl); base.setHSL(hsl.h, clamp(hsl.s * 1.15, 0, 1), clamp(hsl.l * 1.05, 0, 0.8));
    c.copy(base).multiplyScalar(0.88 + n3(x * 0.15, z * 0.15, 2) * 0.24);
    if (b.water > 0.3) c.lerp(sand, 0.5);
    c.lerp(snow, b.snow * 0.6);
    if (opts.road) { const pd = pathDist(x, z); if (pd < 3.2) c.lerp(dirt, (1 - pd / 3.2) * 0.85); }
    col[k * 3] = c.r; col[k * 3 + 1] = c.g; col[k * 3 + 2] = c.b; avg.r += c.r; avg.g += c.g; avg.b += c.b;
  }
  avg.multiplyScalar(1 / pos.count);
  geo.computeVertexNormals();
  const nrm = geo.attributes.normal;                                // steep ground shows rock
  for (let k = 0; k < pos.count; k++) { const steep = clamp((0.86 - nrm.getY(k)) * 5, 0, 1); if (steep > 0) { c.setRGB(col[k * 3], col[k * 3 + 1], col[k * 3 + 2]).lerp(rock, steep); col[k * 3] = c.r; col[k * 3 + 1] = c.g; col[k * 3 + 2] = c.b; } }
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
  const detail = detailTex(seed); detail.repeat.set(size / 7, size / 7);
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ vertexColors: true, map: detail, roughness: 0.96 }));
  mesh.receiveShadow = true;
  const group = new THREE.Group(); group.add(mesh);
  // exact height from the mesh, so feet stand on what is drawn
  const step = size / seg, x0 = opts.cx - size / 2, z0 = opts.cz - size / 2;
  const height = (x, z) => {
    const fi = clamp((x - x0) / step, 0, seg - 0.001), fj = clamp((z - z0) / step, 0, seg - 0.001), i = Math.floor(fi), j = Math.floor(fj), a = fi - i, b = fj - j;
    const y = (ii, jj) => pos.getY(jj * (seg + 1) + ii);
    return lerp(lerp(y(i, j), y(i + 1, j), a), lerp(y(i, j + 1), y(i + 1, j + 1), a), b);
  };
  let water = null;
  const wet = (() => { let w = 0; for (let k = 0; k < 400; k++) { const r = info(opts.cx + (k % 20 - 10) / 20 * size, opts.cz + (Math.floor(k / 20) - 10) / 20 * size); w += r.b.water > 0.4; } return w; })();
  if (wet > 3) {
    water = new THREE.Mesh(new THREE.PlaneGeometry(size * 1.2, size * 1.2), new THREE.MeshStandardMaterial({ color: 0x2b5f7e, transparent: true, opacity: 0.82, roughness: 0.08, metalness: 0.35 }));
    water.rotation.x = -Math.PI / 2; water.position.set(opts.cx, -1.1, opts.cz); group.add(water);
  }
  const ground = (x, z) => Math.max(height(x, z), water ? -1.1 : -1e9);
  return { group, height, ground, info, pathDist, waterLevel: water ? -1.1 : null, size, seed, mesh, avgColor: avg };
}

// ---------------------------------------------------------------- trees, rocks and grass
export function scatter(T, opts) {
  const group = new THREE.Group(), r = rng(T.seed ^ 0x5151), { size } = T;
  const cx = opts.cx, cz = opts.cz;
  const kinds = {
    pine: { trunk: new THREE.CylinderGeometry(0.16, 0.28, 2.0, 6).translate(0, 1, 0), crown: mergeCones() },
    broad: { trunk: new THREE.CylinderGeometry(0.18, 0.3, 2.2, 6).translate(0, 1.1, 0), crown: new THREE.IcosahedronGeometry(1.5, 0).translate(0, 3.2, 0) },
    bush: { trunk: null, crown: new THREE.DodecahedronGeometry(0.8, 0).translate(0, 0.5, 0) },
    palm: { trunk: new THREE.CylinderGeometry(0.1, 0.17, 4.2, 6).translate(0, 2.1, 0), crown: mergeGeos(Array.from({ length: 7 }, (_, k) => new THREE.ConeGeometry(0.34, 2.3, 4).scale(1, 1, 0.25).rotateX(1.95).translate(0, 4.05, 0.95).rotateY(k / 7 * Math.PI * 2))) },
    dead: { trunk: new THREE.CylinderGeometry(0.1, 0.24, 2.6, 5).translate(0, 1.3, 0), crown: mergeGeos([[0.9, 0.7, 0], [-0.8, 1.0, 2.1], [0.6, 1.3, 4.2]].map(([t, l, a]) => new THREE.CylinderGeometry(0.03, 0.08, 1.5 * l, 4).translate(0, 0.75 * l, 0).rotateZ(t).rotateY(a).translate(0, 1.9, 0))), bare: true },
    cactus: { trunk: null, crown: mergeGeos([new THREE.CapsuleGeometry(0.22, 1.5, 3, 7).translate(0, 0.95, 0), new THREE.CapsuleGeometry(0.13, 0.5, 3, 6).translate(0.45, 1.25, 0), new THREE.CapsuleGeometry(0.13, 0.3, 3, 6).rotateZ(Math.PI / 2).translate(0.25, 0.95, 0),
      new THREE.CapsuleGeometry(0.12, 0.4, 3, 6).translate(-0.42, 1.0, 0), new THREE.CapsuleGeometry(0.12, 0.25, 3, 6).rotateZ(Math.PI / 2).translate(-0.24, 0.78, 0)]) },
    giant: { trunk: new THREE.CylinderGeometry(0.34, 0.6, 5.5, 7).translate(0, 2.75, 0), crown: mergeGeos([new THREE.IcosahedronGeometry(2.6, 0).scale(1, 0.55, 1).translate(0, 6.2, 0), new THREE.IcosahedronGeometry(1.7, 0).scale(1, 0.6, 1).translate(1.4, 5.0, 0.6), new THREE.IcosahedronGeometry(1.5, 0).scale(1, 0.6, 1).translate(-1.3, 5.3, -0.7)]) },
  };
  const bio = opts.biome || {}, weights = bio.trees ? Object.entries(bio.trees) : null, wsum = weights ? weights.reduce((n, w) => n + w[1], 0) : 0;
  const byBiome = () => { let v = r() * wsum; for (const [k, w] of weights) if ((v -= w) <= 0) return k; return weights[0][0]; };
  const leafTint = bio.leaf ? [new THREE.Color(bio.leaf[0]), bio.leaf[1]] : null;
  const count = weights && !wsum ? 0 : clamp(Math.round(size * size / 70), 400, 4200), picks = Object.fromEntries(Object.keys(kinds).map(k => [k, []]));
  let grown = 0;
  for (let k = 0; k < count * 3 && grown < count; k++) {
    const x = cx + (r() - 0.5) * size, z = cz + (r() - 0.5) * size;
    if (T.pathDist(x, z) < 4.5) continue;
    const { b } = T.info(x, z);
    if (b.water > 0.25) continue;
    const dense = 0.08 + b.forest * 0.9 + (opts.lush || 0) + (bio.lush || 0) - b.dry * 0.06;
    if (r() > dense) continue;
    grown++;
    const kind = weights ? byBiome() : b.snow > 0.3 || (b.forest > 0.5 && r() < 0.55) ? "pine" : b.dry > 0.5 ? (r() < 0.7 ? "bush" : "broad") : r() < 0.35 ? "pine" : r() < 0.82 ? "broad" : "bush";
    picks[kind].push([x, T.height(x, z), z, 0.7 + r() * 0.8, r() * 6, b]);
  }
  const m = new THREE.Matrix4(), q = new THREE.Quaternion(), s = new THREE.Vector3(), p = new THREE.Vector3(), up = new THREE.Vector3(0, 1, 0), c = new THREE.Color();
  const barkMat = new THREE.MeshStandardMaterial({ color: 0x5a3d26, roughness: 1 });
  const leafMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.85, flatShading: true });
  for (const [kind, list] of Object.entries(picks)) {
    if (!list.length) continue;
    const K = kinds[kind];
    const crown = new THREE.InstancedMesh(K.crown, leafMat, list.length), trunk = K.trunk ? new THREE.InstancedMesh(K.trunk, barkMat, list.length) : null;
    list.forEach(([x, y, z, sc, rot, b], i) => {
      q.setFromAxisAngle(up, rot); m.compose(p.set(x, y - 0.1, z), q, s.set(sc, sc * (0.9 + (i % 5) * 0.06), sc));
      crown.setMatrixAt(i, m); if (trunk) trunk.setMatrixAt(i, m);
      if (K.bare) c.setHSL(0.07 + r() * 0.03, 0.22, 0.2 + r() * 0.08);
      else { if (kind === "pine") c.setHSL(0.33 + r() * 0.05, 0.42, 0.17 + r() * 0.07); else if (kind === "broad" || kind === "giant") c.setHSL(0.24 + r() * 0.08, 0.48, 0.24 + r() * 0.1); else if (kind === "palm" || kind === "cactus") c.setHSL(0.28 + r() * 0.05, 0.5, 0.25 + r() * 0.08); else c.setHSL(0.2 + r() * 0.06, 0.38, 0.26 + r() * 0.08);
        if (leafTint) c.lerp(leafTint[0], leafTint[1]);
        if (b.snow > 0.3) c.lerp(new THREE.Color(0xe8eef2), 0.45); if (b.dry > 0.5 && !leafTint) c.lerp(new THREE.Color(0x9a8a4a), 0.4); }
      crown.setColorAt(i, c);
    });
    crown.castShadow = true; group.add(crown); if (trunk) { trunk.castShadow = true; group.add(trunk); }
  }
  // rocks
  const rocks = new THREE.InstancedMesh(new THREE.DodecahedronGeometry(0.7, 0), new THREE.MeshStandardMaterial({ color: 0x8a8378, roughness: 0.9, flatShading: true }), 420);
  let n = 0;
  for (let k = 0; k < 1500 && n < 420; k++) {
    const x = cx + (r() - 0.5) * size, z = cz + (r() - 0.5) * size;
    if (T.pathDist(x, z) < 2.5 || T.info(x, z).b.water > 0.3) continue;
    const sc = 0.4 + r() * r() * 2.4; q.setFromEuler(new THREE.Euler(r() * 3, r() * 3, r() * 3));
    m.compose(p.set(x, T.height(x, z) - sc * 0.3, z), q, s.set(sc, sc * 0.6, sc)); rocks.setMatrixAt(n++, m);
  }
  rocks.count = n; rocks.castShadow = true; group.add(rocks);
  // distant hills all around
  const hills = new THREE.Group(), R = size * 0.65 + 160;
  const hm = new THREE.MeshBasicMaterial({ color: 0xffffff, fog: false });
  for (let k = 0; k < 46; k++) { const a = k / 46 * Math.PI * 2 + r() * 0.1, hh = 40 + r() * 110; const rad = 70 + r() * 70; const cone = new THREE.Mesh(bio.mesa ? new THREE.CylinderGeometry(rad * 0.62, rad, hh * 0.7, 6) : new THREE.ConeGeometry(rad, hh, 5), hm); cone.position.set(cx + Math.cos(a) * R, hh / 2 - 12, cz + Math.sin(a) * R); cone.rotation.y = r() * 3; hills.add(cone); }
  group.add(hills);
  group.userData.tint = sky => hm.color.copy(sky.horizon).lerp(T.avgColor, 0.25).multiplyScalar(0.8);
  return group;
  function mergeCones() {
    const a = new THREE.ConeGeometry(1.3, 2.4, 7).translate(0, 2.6, 0), b = new THREE.ConeGeometry(1.0, 2.0, 7).translate(0, 3.7, 0), cc = new THREE.ConeGeometry(0.65, 1.5, 7).translate(0, 4.6, 0);
    return mergeGeos([a, b, cc]);
  }
}
// Things standing near the way, by the biome: camps, ruins, houses, lamps… Returns the group, the
// flames to flicker and the lights for the pool of lamps: { group, flames, lights: [{p, color, power}] }.
export function sights(T, opts, biome) {
  const group = new THREE.Group(), flames = [], lights = [], r = rng(T.seed ^ 0x9e37), kinds = (biome && biome.sights) || [];
  const path = opts.avoid || [];
  if (!kinds.length || path.length < 2) return { group, flames, lights };
  let len = 0; const cum = [0];
  for (let k = 1; k < path.length; k++) { len += Math.hypot(path[k][0] - path[k - 1][0], path[k][1] - path[k - 1][1]); cum.push(len); }
  const at = d => { let k = 1; while (k < cum.length - 1 && cum[k] < d) k++; const a = path[k - 1], b = path[k], l = cum[k] - cum[k - 1] || 1, f = clamp((d - cum[k - 1]) / l, 0, 1); return { x: lerp(a[0], b[0], f), z: lerp(a[1], b[1], f), dx: (b[0] - a[0]) / l, dz: (b[1] - a[1]) / l }; };
  const stone = new THREE.MeshStandardMaterial({ color: 0x9a9488, roughness: 0.95, flatShading: true }), wood = new THREE.MeshStandardMaterial({ color: 0x6b4a2b, roughness: 0.9 });
  const wall = new THREE.MeshStandardMaterial({ color: 0xd8c8a8, roughness: 0.95 }), roof = new THREE.MeshStandardMaterial({ color: (biome && biome.roof) || 0x8a3a2a, roughness: 0.85, flatShading: true });
  const cloth = new THREE.MeshStandardMaterial({ color: 0xb89a6a, roughness: 1, flatShading: true }), bone = new THREE.MeshStandardMaterial({ color: 0xe0d8c0, roughness: 0.8 });
  const dark = new THREE.MeshStandardMaterial({ color: 0x2b2b2b, roughness: 0.6, metalness: 0.5 }), gt = glowTex();
  const mk = (g, geo, m, x, y, z) => { const n = new THREE.Mesh(geo, m); n.position.set(x, y, z); n.castShadow = true; n.receiveShadow = true; g.add(n); return n; };
  const flame = (g, x, y, z, color, size, power) => {
    const f = new THREE.Sprite(new THREE.SpriteMaterial({ map: gt, color, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false }));
    f.position.set(x, y, z); f.scale.setScalar(size); f.userData.ph = r() * 6; g.add(f); flames.push(f);
    if (power) lights.push({ at: [g, x, y, z], color, power });
  };
  const make = {
    camp(g) { mk(g, new THREE.ConeGeometry(1.7, 2.3, 4), cloth, -1.6, 1.15, 0.4).rotation.y = 0.6; for (let k = 0; k < 3; k++) mk(g, new THREE.CylinderGeometry(0.07, 0.07, 0.9, 5), wood, 1.5, 0.12, 0).rotation.set(Math.PI / 2, 0, k * 2.1); flame(g, 1.5, 0.55, 0, 0xff9a40, 1.5, 16); mk(g, new THREE.BoxGeometry(0.6, 0.5, 0.6), wood, 0.2, 0.25, -1.4); },
    stones(g) { const n = 5 + Math.floor(r() * 3); for (let k = 0; k < n; k++) { const a = k / n * Math.PI * 2, h = 1.6 + r() * 1.6; mk(g, new THREE.BoxGeometry(0.7, h, 0.45), stone, Math.cos(a) * 3, h / 2 - 0.2, Math.sin(a) * 3).rotation.set((r() - 0.5) * 0.25, -a, (r() - 0.5) * 0.2); } },
    ruin(g) { mk(g, new THREE.BoxGeometry(5, 0.3, 4), stone, 0, 0.05, 0); for (let k = 0; k < 5; k++) { const h = 1 + r() * 3.2; mk(g, new THREE.CylinderGeometry(0.32, 0.38, h, 8), stone, (k % 3 - 1) * 2, h / 2, k < 3 ? -1.5 : 1.5); } mk(g, new THREE.CylinderGeometry(0.32, 0.32, 2.6, 8), stone, 0.6, 0.4, 2.6).rotation.set(Math.PI / 2, 0, 0.5); },
    house(g) { const w = 3.4 + r() * 1.4, d = 3 + r() * 1.2, m = Math.max(w, d); mk(g, new THREE.BoxGeometry(w, 2.4, d), wall, 0, 1.2, 0); const rf = mk(g, new THREE.ConeGeometry(m * 0.82, 1.8, 4), roof, 0, 3.3, 0); rf.rotation.y = Math.PI / 4; rf.scale.set(w / m, 1, d / m); mk(g, new THREE.BoxGeometry(0.8, 1.5, 0.08), wood, 0, 0.75, d / 2 + 0.02); mk(g, new THREE.BoxGeometry(0.4, 1, 0.4), stone, w * 0.25, 3.5, 0); },
    lamp(g) { mk(g, new THREE.CylinderGeometry(0.06, 0.09, 3, 6), dark, 0, 1.5, 0); mk(g, new THREE.BoxGeometry(0.28, 0.34, 0.28), dark, 0, 3.1, 0); flame(g, 0, 3.1, 0, 0xffc060, 1.0, 14); },
    lantern(g) { mk(g, new THREE.CylinderGeometry(0.07, 0.12, 2.6, 6), stone, 0, 1.3, 0); flame(g, 0, 2.9, 0, 0x8ac8ff, 1.2, 14); },
    brazier(g) { mk(g, new THREE.CylinderGeometry(0.2, 0.35, 1.2, 6), dark, 0, 0.6, 0); mk(g, new THREE.CylinderGeometry(0.8, 0.4, 0.5, 10), dark, 0, 1.4, 0); flame(g, 0, 2.2, 0, 0xff7a2a, 2.4, 22); },
    column(g) { const h = 9 + r() * 5; mk(g, new THREE.CylinderGeometry(0.9, 1.1, h, 10), stone, 0, h / 2, 0); mk(g, new THREE.BoxGeometry(2.6, 0.6, 2.6), stone, 0, 0.3, 0); mk(g, new THREE.BoxGeometry(2.4, 0.5, 2.4), stone, 0, h, 0); },
    well(g) { mk(g, new THREE.CylinderGeometry(1, 1, 0.8, 12), stone, 0, 0.4, 0); for (const x of [-0.9, 0.9]) mk(g, new THREE.BoxGeometry(0.14, 2, 0.14), wood, x, 1, 0); mk(g, new THREE.ConeGeometry(1.4, 0.8, 4), roof, 0, 2.3, 0).rotation.y = Math.PI / 4; },
    moonwell(g) { mk(g, new THREE.TorusGeometry(1.8, 0.3, 6, 18), stone, 0, 0.2, 0).rotation.x = Math.PI / 2; const w = mk(g, new THREE.CircleGeometry(1.7, 20), new THREE.MeshBasicMaterial({ color: 0x9ad8ff, transparent: true, opacity: 0.85 }), 0, 0.3, 0); w.rotation.x = -Math.PI / 2; w.castShadow = false; flame(g, 0, 0.9, 0, 0x8ac8ff, 3, 18); },
    bones(g) { for (let k = 0; k < 5; k++) mk(g, new THREE.TorusGeometry(1.5 - k * 0.12, 0.09, 5, 12, Math.PI), bone, 0, 0, -2 + k); mk(g, new THREE.SphereGeometry(0.55, 8, 6), bone, 0, 0.4, 3.3).scale.set(1, 0.8, 1.4); },
    totem(g) { let y = 0; for (let k = 0; k < 4; k++) { const h = 0.9 - k * 0.1; mk(g, new THREE.BoxGeometry(0.8 - k * 0.1, h, 0.8 - k * 0.1), k % 2 ? roof : wood, 0, y + h / 2, 0); y += h; } mk(g, new THREE.BoxGeometry(2.4, 0.3, 0.12), wood, 0, y - 0.6, 0); },
    tomb(g) { const n = 4 + Math.floor(r() * 4); for (let k = 0; k < n; k++) mk(g, new THREE.BoxGeometry(0.6, 0.9 + r() * 0.5, 0.16), stone, (k % 3 - 1) * 1.5 + (r() - 0.5) * 0.4, 0.4, Math.floor(k / 3) * 1.8 - 1).rotation.z = (r() - 0.5) * 0.3; },
    obelisk(g) { mk(g, new THREE.CylinderGeometry(0.3, 0.7, 6, 4), stone, 0, 3, 0); mk(g, new THREE.ConeGeometry(0.42, 0.8, 4), stone, 0, 6.4, 0); flame(g, 0, 7, 0, 0x6ae0ff, 1.4, 0); },
    tower(g) { mk(g, new THREE.CylinderGeometry(1.5, 1.8, 7, 10), stone, 0, 3.5, 0); mk(g, new THREE.ConeGeometry(2.1, 2.4, 10), roof, 0, 8.2, 0); },
    sign(g) { mk(g, new THREE.CylinderGeometry(0.06, 0.08, 2.2, 5), wood, 0, 1.1, 0); mk(g, new THREE.BoxGeometry(1.1, 0.3, 0.06), wood, 0.35, 1.9, 0).rotation.y = 0.2; mk(g, new THREE.BoxGeometry(0.9, 0.26, 0.06), wood, -0.3, 1.5, 0).rotation.y = -0.4; },
  };
  const every = 46 / (biome.dense || 1), n = clamp(Math.round(len / every), 3, 26);
  for (let k = 0; k < n; k++) {
    const kind = kinds[Math.floor(r() * kinds.length)], small = kind === "lamp" || kind === "lantern" || kind === "sign" || kind === "brazier";
    for (let tries = 0; tries < 6; tries++) {
      const p = at((k + 0.2 + r() * 0.6) / n * len), side = r() < 0.5 ? 1 : -1, off = small ? 3.2 + r() * 2 : 8 + r() * 13;
      const x = p.x - p.dz * side * off, z = p.z + p.dx * side * off;
      if (T.pathDist(x, z) < (small ? 2.6 : 6.5) || T.info(x, z).b.water > 0.25) continue;
      const g = new THREE.Group(); g.position.set(x, T.height(x, z) - 0.05, z); g.rotation.y = Math.atan2(p.x - x, p.z - z) + (r() - 0.5) * 0.6;
      make[kind](g); group.add(g); break;
    }
  }
  group.updateMatrixWorld(true);
  for (const l of lights) { const [g, x, y, z] = l.at; l.p = new THREE.Vector3(x, y, z).applyMatrix4(g.matrixWorld); delete l.at; }
  return { group, flames, lights };
}
export function mergeGeos(list) {
  const geos = list.map(g => g.index ? g.toNonIndexed() : g);
  const total = geos.reduce((n, g) => n + g.attributes.position.count, 0);
  const pos = new Float32Array(total * 3), nor = new Float32Array(total * 3); let o = 0;
  for (const g of geos) { pos.set(g.attributes.position.array, o * 3); nor.set(g.attributes.normal.array, o * 3); o += g.attributes.position.count; }
  const out = new THREE.BufferGeometry(); out.setAttribute("position", new THREE.BufferAttribute(pos, 3)); out.setAttribute("normal", new THREE.BufferAttribute(nor, 3));
  return out;
}

// Grass in square patches around the camera, made as the camera moves; it sways in the wind.
export function grass(T, time) {
  const CH = 12, RAD = 4, group = new THREE.Group(), chunks = new Map();
  const mat = new THREE.MeshLambertMaterial({ vertexColors: true, side: THREE.DoubleSide });
  mat.onBeforeCompile = sh => {
    sh.uniforms.uTime = time;
    sh.vertexShader = "uniform float uTime;\nattribute float aTip;\n" + sh.vertexShader.replace("#include <begin_vertex>",
      "#include <begin_vertex>\nvec4 wp = modelMatrix * vec4(transformed, 1.0);\ntransformed.x += sin(uTime * 1.7 + wp.x * 0.35 + wp.z * 0.2) * 0.18 * aTip;\ntransformed.z += cos(uTime * 1.3 + wp.z * 0.3) * 0.1 * aTip;");
  };
  const make = (ci, cj) => {
    const r = rng(T.seed ^ (ci * 73856093) ^ (cj * 19349663)), N = 420;
    const pos = [], col = [], tip = [], c = new THREE.Color();
    for (let k = 0; k < N; k++) {
      const x = (ci + r()) * CH, z = (cj + r()) * CH, { rgb, b } = T.info(x, z);
      if (b.water > 0.2 || b.snow > 0.5 || r() < b.dry * 0.7) continue;
      const y = T.height(x, z), h = 0.18 + r() * 0.34, w = 0.035 + r() * 0.03, a = r() * Math.PI;
      const dx = Math.cos(a) * w, dz = Math.sin(a) * w, lean = (r() - 0.5) * 0.15;
      pos.push(x - dx, y, z - dz, x + dx, y, z + dz, x + lean, y + h, z + lean * 0.5);
      if (rgb) c.setRGB(rgb[0], rgb[1], rgb[2]).convertSRGBToLinear(); else c.setRGB(0.3, 0.46, 0.2);
      c.multiplyScalar(0.75 + r() * 0.35);
      const flower = r() < 0.025 ? new THREE.Color([0xffe066, 0xffffff, 0xc58cff, 0xff8a8a][Math.floor(r() * 4)]) : null;
      const base = c.clone().multiplyScalar(0.55), top = flower || c.clone().lerp(new THREE.Color(0xd8e8a0), 0.15);
      col.push(base.r, base.g, base.b, base.r, base.g, base.b, top.r, top.g, top.b); tip.push(0, 0, 1);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3)); g.setAttribute("color", new THREE.Float32BufferAttribute(col, 3)); g.setAttribute("aTip", new THREE.Float32BufferAttribute(tip, 1));
    g.computeVertexNormals();
    const mesh = new THREE.Mesh(g, mat); mesh.receiveShadow = true; group.add(mesh); return mesh;
  };
  return {
    group,
    update(cx, cz) {
      const ci = Math.floor(cx / CH), cj = Math.floor(cz / CH), want = new Set();
      let made = 0;
      for (let dj = -RAD; dj <= RAD; dj++) for (let di = -RAD; di <= RAD; di++) {
        if (di * di + dj * dj > RAD * RAD + 1) continue;
        const key = (ci + di) + "," + (cj + dj); want.add(key);
        if (!chunks.has(key) && made < 4) { chunks.set(key, make(ci + di, cj + dj)); made++; }
      }
      for (const [key, mesh] of chunks) if (!want.has(key)) { group.remove(mesh); mesh.geometry.dispose(); chunks.delete(key); }
    },
    dispose() { for (const m of chunks.values()) m.geometry.dispose(); mat.dispose(); },
  };
}

// ---------------------------------------------------------------- the dungeon: a walled, roofed tunnel
// segs: [{a:{x,z}, b:{x,z}, l, s}], rooms: [{x, z}] where a boss waits; theme: how this instance looks
// (see themes.js). Returns { group, lights: [{p, color, power}], inside, W, H, inRoom }.
export function tunnel(segs, rooms, seed, theme) {
  const W = 9, H = 6.2, r = rng(seed), group = new THREE.Group();
  const pts = [segs[0].a, ...segs.map(s => s.b)];
  // left and right edges with mitred corners, so the walls meet
  const L = [], R = [];
  for (let k = 0; k < pts.length; k++) {
    const d1 = k > 0 ? norm(pts[k].x - pts[k - 1].x, pts[k].z - pts[k - 1].z) : null, d2 = k < pts.length - 1 ? norm(pts[k + 1].x - pts[k].x, pts[k + 1].z - pts[k].z) : null;
    const n1 = d1 && [-d1[1], d1[0]], n2 = d2 && [-d2[1], d2[0]];
    let m = n1 && n2 ? norm(n1[0] + n2[0], n1[1] + n2[1]) : (n1 || n2), len = W / 2;
    if (n1 && n2) len = W / 2 / Math.max(0.5, m[0] * n1[0] + m[1] * n1[1]);
    L.push([pts[k].x + m[0] * len, pts[k].z + m[1] * len]); R.push([pts[k].x - m[0] * len, pts[k].z - m[1] * len]);
  }
  const inRoom = (x, z, pad = 0) => rooms.some(c => Math.hypot(x - c.x, z - c.z) < 13 + pad);
  const floorB = builder(), ceilB = builder(), wallB = builder();
  let run = 0;
  for (let k = 0; k < pts.length - 1; k++) {
    const l = segs[k].l;
    floorB.quad([L[k][0], 0, L[k][1]], [L[k + 1][0], 0, L[k + 1][1]], [R[k + 1][0], 0, R[k + 1][1]], [R[k][0], 0, R[k][1]], [0, run / 3, 0, (run + l) / 3, W / 3, (run + l) / 3, W / 3, run / 3]);
    // walls and roof in pieces, left out where a boss hall opens up
    const pieces = Math.max(1, Math.ceil(l / 3));
    for (let p = 0; p < pieces; p++) {
      const f0 = p / pieces, f1 = (p + 1) / pieces;
      const at = (E, f) => [lerp(E[k][0], E[k + 1][0], f), lerp(E[k][1], E[k + 1][1], f)];
      const l0 = at(L, f0), l1 = at(L, f1), r0 = at(R, f0), r1 = at(R, f1), u0 = (run + l * f0) / 3, u1 = (run + l * f1) / 3;
      const mid = [(l0[0] + r1[0]) / 2, (l0[1] + r1[1]) / 2];
      if (inRoom(mid[0], mid[1], 1)) continue;
      ceilB.quad([l0[0], H, l0[1]], [r0[0], H, r0[1]], [r1[0], H, r1[1]], [l1[0], H, l1[1]], [0, u0, W / 3, u0, W / 3, u1, 0, u1]);
      wallB.quad([l0[0], 0, l0[1]], [l1[0], 0, l1[1]], [l1[0], H, l1[1]], [l0[0], H, l0[1]], [u0, 0, u1, 0, u1, H / 3, u0, H / 3]);
      wallB.quad([r1[0], 0, r1[1]], [r0[0], 0, r0[1]], [r0[0], H, r0[1]], [r1[0], H, r1[1]], [u1, 0, u0, 0, u0, H / 3, u1, H / 3]);
    }
    run += l;
  }
  const tint = hex => new THREE.Color(hex).multiplyScalar(2);       // over the dark stone textures
  const stone = flagTex(seed), brick = theme.rock ? flagTex(seed + 5) : brickTex(seed, 62), ceil = theme.rock ? flagTex(seed + 9) : brickTex(seed + 3, 38);
  const floor = new THREE.Mesh(floorB.geo(), new THREE.MeshStandardMaterial({ map: stone, color: tint(theme.floor), roughness: 0.95 })); floor.receiveShadow = true;
  const walls = new THREE.Mesh(wallB.geo(), new THREE.MeshStandardMaterial({ map: brick, color: tint(theme.wall), roughness: 0.95, side: THREE.DoubleSide })); walls.receiveShadow = true;
  const roof = new THREE.Mesh(ceilB.geo(), new THREE.MeshStandardMaterial({ map: ceil, color: tint(theme.roof), roughness: 1, side: THREE.DoubleSide }));
  group.add(floor, walls, roof);
  const xs = pts.map(p => p.x), zs = pts.map(p => p.z), bx = [Math.min(...xs) - 20, Math.max(...xs) + 20], bz = [Math.min(...zs) - 20, Math.max(...zs) + 20];
  const sheet = (y, m) => { const n = new THREE.Mesh(new THREE.PlaneGeometry(bx[1] - bx[0], bz[1] - bz[0]), m); n.rotation.x = -Math.PI / 2; n.position.set((bx[0] + bx[1]) / 2, y, (bz[0] + bz[1]) / 2); group.add(n); return n; };
  if (theme.water) sheet(0.17, new THREE.MeshStandardMaterial({ color: theme.water, transparent: true, opacity: 0.55, roughness: 0.06, metalness: 0.5, depthWrite: false }));
  if (theme.molten) sheet(0.04, new THREE.MeshBasicMaterial({ color: theme.molten, transparent: true, opacity: 0.16, blending: THREE.AdditiveBlending, depthWrite: false }));
  // supports, torches and things along the walls
  const wood = new THREE.MeshStandardMaterial({ map: woodTex(seed), roughness: 0.9 }), pillar = new THREE.MeshStandardMaterial({ map: brick, color: tint(theme.wall).multiplyScalar(1.15), roughness: 0.9 });
  const posts = [], beams = [], torches = [], props = [], flags = [], lights = [];
  const total = run;
  for (let s = 2; s < total - 1; s += 5) {
    const p = along(s); if (inRoom(p.x, p.z, 2)) continue;
    const step = Math.round(s / 5);
    posts.push([p.x - p.dz * (W / 2 - 0.35), p.z + p.dx * (W / 2 - 0.35), p.ang], [p.x + p.dz * (W / 2 - 0.35), p.z - p.dx * (W / 2 - 0.35), p.ang]);
    beams.push([p.x, p.z, p.ang]);
    if (step % 2 === 0) { const side = Math.round(s / 10) % 2 ? 1 : -1; torches.push(new THREE.Vector3(p.x - p.dz * side * (W / 2 - 0.45), 3.4, p.z + p.dx * side * (W / 2 - 0.45))); }
    else if (theme.banner) { const side = step % 4 === 1 ? 1 : -1; flags.push([p.x - p.dz * side * (W / 2 - 0.5), p.z + p.dx * side * (W / 2 - 0.5), p.ang + Math.PI / 2]); }
    if (r() < 0.62) { const side = r() < 0.5 ? 1 : -1, off = 1.2 + r() * 2.5; props.push([p.x - p.dz * side * (W / 2 - 1.1) + p.dx * off, p.z + p.dx * side * (W / 2 - 1.1) + p.dz * off, theme.props[Math.floor(r() * theme.props.length)], r() * 6, 0.8 + r() * 0.5]); }
  }
  const im = (geo, mat, list, place) => { if (!list.length) return null; const m = new THREE.InstancedMesh(geo, mat, list.length); const M = new THREE.Matrix4(), q = new THREE.Quaternion(), S = new THREE.Vector3(1, 1, 1), P = new THREE.Vector3(), up = new THREE.Vector3(0, 1, 0);
    list.forEach((it, i) => { const [x, y, z, a, sx, sy, sz] = place(it); q.setFromAxisAngle(up, a); M.compose(P.set(x, y, z), q, S.set(sx || 1, sy || 1, sz || 1)); m.setMatrixAt(i, M); }); m.castShadow = true; m.receiveShadow = true; group.add(m); return m; };
  if (theme.supports === "timber") {
    im(new THREE.BoxGeometry(0.45, H, 0.45), wood, posts, ([x, z, a]) => [x, H / 2, z, a]);
    im(new THREE.BoxGeometry(0.4, 0.45, W), wood, beams, ([x, z, a]) => [x, H - 0.45, z, a + Math.PI / 2]);
  } else if (theme.supports === "column") {
    im(new THREE.CylinderGeometry(0.42, 0.5, H, 10), pillar, posts, ([x, z, a]) => [x, H / 2, z, a]);
    im(new THREE.BoxGeometry(1.2, 0.4, 1.2), pillar, posts, ([x, z, a]) => [x, 0.2, z, a]);
  } else if (theme.supports === "rib") {
    im(new THREE.TorusGeometry(W / 2 - 0.25, 0.3, 6, 16, Math.PI), pillar, beams, ([x, z, a]) => [x, 0, z, a, 1, (H - 0.1) / (W / 2 - 0.25), 1.3]);
  }
  if (flags.length) im(new THREE.PlaneGeometry(1.1, 2.6), new THREE.MeshStandardMaterial({ color: theme.banner, roughness: 1, side: THREE.DoubleSide }), flags, ([x, z, a]) => [x, 3.5, z, a]);
  const g3 = new THREE.Color(theme.glow), lit = k => ({ color: g3, emissive: g3, emissiveIntensity: k, roughness: 0.5, flatShading: true });
  const oct = (rad, h, x, z, tilt = 0) => new THREE.OctahedronGeometry(rad, 0).scale(1, h, 1).rotateZ(tilt).translate(x, rad * h * 0.8, z);
  const PROP = {
    barrel: () => [new THREE.CylinderGeometry(0.42, 0.42, 1.0, 10).translate(0, 0.5, 0), { color: 0x6a4527, roughness: 0.8 }],
    crate: () => [new THREE.BoxGeometry(0.9, 0.9, 0.9).translate(0, 0.45, 0), { color: 0x7a5a36, roughness: 0.9 }],
    rubble: () => [mergeGeos([new THREE.DodecahedronGeometry(0.5, 0).translate(0, 0.25, 0), new THREE.DodecahedronGeometry(0.3, 0).translate(0.55, 0.15, 0.2), new THREE.DodecahedronGeometry(0.22, 0).translate(-0.4, 0.1, -0.3)]), { color: tint(theme.wall).multiplyScalar(0.6), roughness: 1, flatShading: true }],
    mushroom: () => [mergeGeos([new THREE.CylinderGeometry(0.07, 0.11, 0.8, 6).translate(0, 0.4, 0), new THREE.SphereGeometry(0.42, 10, 6, 0, Math.PI * 2, 0, Math.PI / 2).scale(1, 0.6, 1).translate(0, 0.75, 0),
      new THREE.CylinderGeometry(0.04, 0.06, 0.4, 5).translate(0.4, 0.2, 0.15), new THREE.SphereGeometry(0.2, 8, 5, 0, Math.PI * 2, 0, Math.PI / 2).scale(1, 0.6, 1).translate(0.4, 0.38, 0.15)]), lit(0.75), true],
    crystal: () => [mergeGeos([oct(0.3, 2.6, 0, 0), oct(0.2, 2.2, 0.32, 0.1, -0.5), oct(0.16, 2, -0.25, -0.15, 0.45)]), lit(0.9), true],
    coral: () => [mergeGeos(Array.from({ length: 6 }, (_, k) => new THREE.ConeGeometry(0.09, 0.9 + (k % 3) * 0.3, 5).translate(0, 0.5 + (k % 3) * 0.15, 0).rotateZ((k % 2 ? 1 : -1) * (0.25 + k * 0.08)).rotateY(k * 1.1))), { color: 0xff8a9a, emissive: 0xa02a4a, emissiveIntensity: 0.5, roughness: 0.6, flatShading: true }, true],
    bones: () => [mergeGeos([new THREE.SphereGeometry(0.17, 8, 6).translate(0, 0.17, 0), ...[0.4, 1.7, 2.9].map(a => new THREE.CylinderGeometry(0.03, 0.03, 0.8, 4).rotateZ(Math.PI / 2).rotateY(a).translate(Math.cos(a) * 0.3, 0.04, 0.35))]), { color: 0xe0d8c0, roughness: 0.8 }],
    coffin: () => [new THREE.BoxGeometry(0.8, 0.55, 2.0).translate(0, 0.28, 0), { color: 0x3a2c20, roughness: 0.9 }],
    urn: () => [mergeGeos([new THREE.SphereGeometry(0.34, 10, 8).scale(1, 1.2, 1).translate(0, 0.45, 0), new THREE.CylinderGeometry(0.16, 0.2, 0.22, 8).translate(0, 0.92, 0), new THREE.CylinderGeometry(0.2, 0.24, 0.1, 8).translate(0, 0.05, 0)]), { color: 0x9a7a5a, roughness: 0.7 }],
    thorn: () => [mergeGeos(Array.from({ length: 5 }, (_, k) => new THREE.ConeGeometry(0.1, 1.6 + (k % 2) * 0.7, 5).translate(0, 0.8, 0).rotateZ((k % 2 ? 1 : -1) * (0.3 + k * 0.12)).rotateY(k * 1.3))), { color: 0x4a3420, roughness: 0.9, flatShading: true }],
    pipe: () => [mergeGeos([new THREE.CylinderGeometry(0.2, 0.2, H, 8).translate(0, H / 2, 0), new THREE.TorusGeometry(0.27, 0.06, 5, 12).rotateX(Math.PI / 2).translate(0, 1.2, 0), new THREE.TorusGeometry(0.27, 0.06, 5, 12).rotateX(Math.PI / 2).translate(0, 3.6, 0)]), { color: 0x7a8a7a, metalness: 0.7, roughness: 0.4 }],
  };
  for (const kind of new Set(theme.props)) {
    const list = props.filter(p => p[2] === kind); if (!list.length) continue;
    const [geo, m, glows] = PROP[kind]();
    im(geo, new THREE.MeshStandardMaterial(m), list, ([x, z, , a, sc]) => [x, 0, z, a, sc, sc, sc]);
    if (glows) list.forEach(([x, z], i) => { if (i % 2 === 0) lights.push({ p: new THREE.Vector3(x, 1.0, z), color: theme.glow, power: 9 }); });
  }
  if (theme.rails) {                                               // rails down the middle
    const sleepers = [], rails = [];
    for (let s = 0.5; s < total; s += 1.1) { const p = along(s); if (!inRoom(p.x, p.z)) sleepers.push([p.x, p.z, p.ang]); }
    for (let k = 0; k < segs.length; k++) { const g = segs[k], ang = Math.atan2(g.b.x - g.a.x, g.b.z - g.a.z), dx = (g.b.x - g.a.x) / g.l, dz = (g.b.z - g.a.z) / g.l;
      for (const side of [-0.55, 0.55]) rails.push([(g.a.x + g.b.x) / 2 - dz * side, (g.a.z + g.b.z) / 2 + dx * side, ang, g.l]); }
    im(new THREE.BoxGeometry(1.7, 0.08, 0.28), wood, sleepers, ([x, z, a]) => [x, 0.04, z, a]);
    im(new THREE.BoxGeometry(0.08, 0.1, 1), new THREE.MeshStandardMaterial({ color: 0x8b8b8b, metalness: 0.7, roughness: 0.4 }), rails, ([x, z, a, l]) => [x, 0.12, z, a, 1, 1, l]);
  }
  const flameTex = glowTex(), flames = [];
  const flame = (x, y, z, color, size) => { const f = new THREE.Sprite(new THREE.SpriteMaterial({ map: flameTex, color, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false })); f.position.set(x, y, z); f.scale.setScalar(size); f.userData.ph = r() * 6; group.add(f); flames.push(f); return f; };
  const bracket = new THREE.MeshStandardMaterial({ color: 0x2b2b2b, metalness: 0.6, roughness: 0.5 });
  for (const t of torches) {
    const stick = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.08, 0.7, 6), bracket); stick.position.copy(t).setY(3.05); group.add(stick);
    flame(t.x, 3.55, t.z, theme.torch, 1.1); lights.push({ p: t, color: theme.torch, power: 18 });
  }
  // the way in: a swirling gate behind where the party starts
  const p0 = along(1.2), gate = new THREE.Group(); gate.position.set(p0.x, 2.6, p0.z); gate.rotation.y = p0.ang;
  gate.add(new THREE.Mesh(new THREE.TorusGeometry(2.5, 0.28, 8, 30), pillar));
  const swirl = new THREE.Mesh(new THREE.CircleGeometry(2.4, 30), new THREE.MeshBasicMaterial({ color: theme.glow, transparent: true, opacity: 0.5, blending: THREE.AdditiveBlending, side: THREE.DoubleSide, depthWrite: false }));
  gate.add(swirl); group.add(gate); flame(p0.x, 2.6, p0.z, theme.glow, 4.5); lights.push({ p: new THREE.Vector3(p0.x, 2.6, p0.z), color: theme.glow, power: 22 });
  // boss halls: a big round cavern with braziers
  for (const c of rooms) {
    const fl = new THREE.Mesh(new THREE.CircleGeometry(14, 48), new THREE.MeshStandardMaterial({ map: stone, color: tint(theme.floor), roughness: 0.95 }));
    fl.rotation.x = -Math.PI / 2; fl.position.set(c.x, 0.02, c.z); fl.receiveShadow = true; group.add(fl);
    const rune = new THREE.Mesh(new THREE.RingGeometry(5.4, 5.9, 64), new THREE.MeshBasicMaterial({ color: theme.rune, transparent: true, opacity: 0.6, side: THREE.DoubleSide }));
    rune.rotation.x = -Math.PI / 2; rune.position.set(c.x, theme.water ? 0.2 : 0.05, c.z); group.add(rune); (group.userData.runes = group.userData.runes || []).push(rune);
    for (let k = 0; k < 12; k++) { const a = k / 12 * Math.PI * 2; const p = new THREE.Mesh(new THREE.CylinderGeometry(0.8, 1, 11, 8), pillar); p.position.set(c.x + Math.cos(a) * 13.5, 5.5, c.z + Math.sin(a) * 13.5); p.castShadow = true; group.add(p); }
    for (let k = 0; k < 4; k++) { const a = k / 4 * Math.PI * 2 + Math.PI / 4; const x = c.x + Math.cos(a) * 9, z = c.z + Math.sin(a) * 9;
      const bowl = new THREE.Mesh(new THREE.CylinderGeometry(0.7, 0.35, 0.5, 10), bracket); bowl.position.set(x, 1.3, z); group.add(bowl);
      const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.2, 1.1, 6), bracket); leg.position.set(x, 0.55, z); group.add(leg);
      flame(x, 2.1, z, theme.hall, 2.2); lights.push({ p: new THREE.Vector3(x, 2.2, z), color: theme.hall, power: 30 }); }
  }
  group.userData.flames = flames; group.userData.swirl = swirl;
  // how far a point is from the middle of the way: for keeping the camera inside
  const inside = (x, z) => { if (rooms.some(c => Math.hypot(x - c.x, z - c.z) < 12)) return 0; let d = 1e9; for (const g of segs) d = Math.min(d, segDist(x, z, [g.a.x, g.a.z], [g.b.x, g.b.z])); return d; };
  return { group, lights, inside, W, H, inRoom };

  function along(s) { const g = segs.find(x => s >= x.s && s < x.s + x.l) || segs[segs.length - 1]; const f = clamp((s - g.s) / g.l, 0, 1), dx = (g.b.x - g.a.x) / g.l, dz = (g.b.z - g.a.z) / g.l; return { x: lerp(g.a.x, g.b.x, f), z: lerp(g.a.z, g.b.z, f), dx, dz, ang: Math.atan2(dx, dz) }; }
  function norm(x, z) { const l = Math.hypot(x, z) || 1; return [x / l, z / l]; }
}
export const glowTex = () => canvasTexture(64, 64, (c, w) => { const g = c.createRadialGradient(w / 2, w / 2, 0, w / 2, w / 2, w / 2);
  g.addColorStop(0, "rgba(255,255,255,1)"); g.addColorStop(.35, "rgba(255,255,255,.55)"); g.addColorStop(1, "rgba(255,255,255,0)"); c.fillStyle = g; c.fillRect(0, 0, w, w); });
function builder() {
  const pos = [], uv = [];
  return {
    quad(a, b, c, d, t) { pos.push(...a, ...b, ...c, ...a, ...c, ...d); uv.push(t[0], t[1], t[2], t[3], t[4], t[5], t[0], t[1], t[4], t[5], t[6], t[7]); },
    geo() { const g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3)); g.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2)); g.computeVertexNormals(); return g; },
  };
}
