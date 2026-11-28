// What a place and a boss look like in the replay, as plain data: no drawing here. A dungeon gets
// its theme from the instance's name, an outdoor stretch its biome from the zone's name, a boss
// its style from its name and the kind of damage it dealt. A future "skin" (anime, cyberpunk…)
// would be another table like these plus its own materials, loaded only when chosen.

const hashOf = str => { let h = 2166136261; for (const c of String(str)) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619); } return h >>> 0; };

// particles in the air: colour, size, opacity, speed up (+) or down (−), sideways drift
const AIR = {
  dust: { color: 0xffc27a, size: 0.16, opacity: 0.55, vy: 0.2, drift: 0.2 },
  bubbles: { color: 0xa8e4ff, size: 0.26, opacity: 0.7, vy: 1.3, drift: 0.35, many: true },
  spores: { color: 0x9dff7a, size: 0.2, opacity: 0.75, vy: 0.25, drift: 0.5 },
  embers: { color: 0xff7a2a, size: 0.2, opacity: 0.9, vy: 1.6, drift: 0.6, many: true },
  wisps: { color: 0x8ad8ff, size: 0.22, opacity: 0.6, vy: 0.35, drift: 0.4 },
  snow: { color: 0xffffff, size: 0.2, opacity: 0.9, vy: -1.6, drift: 0.7, many: true },
  rain: { color: 0x9fb8d8, size: 0.11, opacity: 0.7, vy: -15, drift: 0.1, many: true },
  sand: { color: 0xe0c48a, size: 0.14, opacity: 0.6, vy: -0.1, drift: 4.5, many: true },
  leaves: { color: 0xd8a04a, size: 0.2, opacity: 0.8, vy: -0.7, drift: 1.2, many: true },
  ash: { color: 0x9a9a9a, size: 0.16, opacity: 0.7, vy: -0.5, drift: 0.8, many: true },
  motes: { color: 0xfff3c0, size: 0.16, opacity: 0.35, vy: 0.2, drift: 0.2 },
  fireflies: { color: 0xd8ff7a, size: 0.2, opacity: 0.85, vy: 0.15, drift: 0.5 },
};

// ---------------------------------------------------------------- dungeons
// wall/floor/roof tint the stone; supports: timber | column | rib | none; props stand along the walls
const DUNGEON = {
  mine: { wall: 0xb8b0a6, floor: 0xb8b8b8, roof: 0xb0a8a0, rock: false, fog: 0x0a0706, far: 48, hemi: [0xffd6a8, 0x1a120c, 0.32], torch: 0xffa040, hall: 0xff8a30, rune: 0xff9a3c,
    supports: "timber", props: ["barrel", "crate", "rubble"], rails: true, air: AIR.dust, glow: 0xffa040 },
  cave: { wall: 0x8ab87a, floor: 0x9ab88a, roof: 0x6a9a5a, rock: true, fog: 0x06100a, far: 52, hemi: [0xa8ffb8, 0x0c1a10, 0.4], torch: 0x8dff6a, hall: 0x5aff9a, rune: 0x5aff9a,
    supports: "rib", props: ["mushroom", "mushroom", "rubble", "thorn"], air: AIR.spores, glow: 0x6aff8a },
  deep: { wall: 0x6a9ad8, floor: 0x7aa8d0, roof: 0x4a78b8, rock: true, fog: 0x04101c, far: 44, hemi: [0x8ac8ff, 0x061420, 0.46], torch: 0x4ad8ff, hall: 0x3affd8, rune: 0x3ad8ff,
    supports: "column", props: ["coral", "crystal", "urn", "coral"], water: 0x1a5a8a, air: AIR.bubbles, glow: 0x4ad8ff },
  castle: { wall: 0xa8acb8, floor: 0xb0b0b8, roof: 0x8a8e9a, rock: false, fog: 0x07080c, far: 50, hemi: [0xc8d4ff, 0x10121a, 0.34], torch: 0x9ac0ff, hall: 0x7a9aff, rune: 0x8a6aff,
    supports: "column", props: ["urn", "crate", "bones", "barrel"], banner: 0x3a4a8a, air: AIR.wisps, glow: 0x9ac0ff },
  cathedral: { wall: 0xd8c8a8, floor: 0xd0c0a0, roof: 0xb8a888, rock: false, fog: 0x0c0806, far: 56, hemi: [0xffe8c0, 0x1a140c, 0.44], torch: 0xffd070, hall: 0xffe08a, rune: 0xffd24a,
    supports: "column", props: ["urn", "crate", "barrel"], banner: 0xa02020, air: AIR.dust, glow: 0xffd070 },
  crypt: { wall: 0x8a9a90, floor: 0x909a94, roof: 0x6a7a72, rock: false, fog: 0x050a08, far: 42, hemi: [0xa8ffd8, 0x0a1410, 0.3], torch: 0x5affc0, hall: 0x7affa0, rune: 0x5affc0,
    supports: "rib", props: ["coffin", "bones", "bones", "urn"], air: AIR.wisps, glow: 0x5affc0 },
  lava: { wall: 0xc8866a, floor: 0xb87a60, roof: 0x9a5a48, rock: true, fog: 0x120604, far: 46, hemi: [0xffa070, 0x2a0c06, 0.42], torch: 0xff5a1a, hall: 0xff3a10, rune: 0xff4a1a,
    supports: "rib", props: ["rubble", "crystal", "bones"], air: AIR.embers, glow: 0xff5a1a, molten: 0xff4a10 },
  tech: { wall: 0x8a9a8a, floor: 0x9aa09a, roof: 0x7a8a7a, rock: false, fog: 0x060a06, far: 46, hemi: [0xc8ffb0, 0x0c140a, 0.36], torch: 0x9dff3a, hall: 0xd8ff3a, rune: 0x9dff3a,
    supports: "column", props: ["pipe", "crate", "pipe", "barrel"], air: AIR.spores, glow: 0x9dff3a },
  thorn: { wall: 0xb89a7a, floor: 0xb09070, roof: 0x8a7050, rock: true, fog: 0x0c0804, far: 48, hemi: [0xffd8a0, 0x1a1008, 0.4], torch: 0xffb040, hall: 0xff7a3a, rune: 0xc8ff5a,
    supports: "rib", props: ["thorn", "thorn", "bones", "rubble"], air: AIR.dust, glow: 0xffb040 },
  ruins: { wall: 0xd0b88a, floor: 0xc8b080, roof: 0xa89068, rock: false, fog: 0x0e0a06, far: 54, hemi: [0xffe0a8, 0x1c140a, 0.44], torch: 0xffc060, hall: 0x6ae0ff, rune: 0x6ae0ff,
    supports: "column", props: ["urn", "rubble", "crystal", "rubble"], air: AIR.sand, glow: 0x6ae0ff },
  temple: { wall: 0x7aa880, floor: 0x8ab090, roof: 0x5a8a62, rock: false, fog: 0x050c08, far: 46, hemi: [0xb0ffc8, 0x0a1a10, 0.4], torch: 0x6affb0, hall: 0xc8ff5a, rune: 0x8aff5a,
    supports: "column", props: ["urn", "mushroom", "thorn", "bones"], water: 0x1a5a3a, air: AIR.spores, glow: 0x6affb0 },
};
const DUNGEON_BY_NAME = [
  [/礦|坑道/, "mine"], [/哀嚎|洞穴|巢穴|瑪拉頓/, "cave"], [/黑澗|黑暗深淵|深淵|水|海|毒蛇神殿/, "deep"], [/影牙|城堡|監獄|要塞|卡拉贊/, "castle"],
  [/修道院|教堂|大教堂|聖/, "cathedral"], [/通靈|斯坦索姆|納克薩|墓|陵|亡/, "crypt"], [/黑石|熔火|怒焰|火|熔/, "lava"], [/諾姆瑞根|諾莫瑞根|機|工廠/, "tech"],
  [/剃刀|荊棘/, "thorn"], [/奧達曼|祖爾法拉克|厄運|遺跡|廢墟/, "ruins"], [/神廟|神殿|祖爾/, "temple"],
];
export function dungeonTheme(name) {
  const n = name || "";
  const hit = DUNGEON_BY_NAME.find(([re]) => re.test(n));
  const key = hit ? hit[1] : ["mine", "cave", "castle", "crypt", "ruins", "temple"][hashOf(n) % 6];
  return { key, ...DUNGEON[key] };
}

// ---------------------------------------------------------------- outdoors
// trees: how often each kind grows (null: by the colour of the map); leaf: [tint, how much];
// sights: what stands near the way; cavern: a city under a mountain, no sky
const BIOME = {
  temperate: { trees: null, sights: ["camp", "stones", "ruin", "sign", "tower"], air: null },
  elven: { trees: { giant: 3, broad: 2, pine: 1, bush: 1 }, leaf: [0x4a6a9a, 0.4], lush: 0.12, sights: ["moonwell", "lantern", "ruin", "stones"], air: AIR.fireflies, fog: [0x3a4a7a, 0.3] },
  coast: { trees: { pine: 3, dead: 2, bush: 1, broad: 1 }, leaf: [0x4a5a6a, 0.35], sights: ["ruin", "stones", "bones", "camp"], air: AIR.wisps, fog: [0x5a6a8a, 0.45], near: 40 },
  snow: { trees: { pine: 5, dead: 1 }, leaf: [0xdfe8ee, 0.35], sights: ["camp", "stones", "tower", "sign"], air: AIR.snow, fog: [0xdfe8f2, 0.4] },
  swamp: { trees: { dead: 3, broad: 3, bush: 2 }, leaf: [0x5a6a3a, 0.4], lush: 0.1, sights: ["bones", "ruin", "totem", "camp"], air: AIR.rain, fog: [0x4a5a4a, 0.5], near: 35 },
  haunted: { trees: { dead: 5, pine: 2, bush: 1 }, leaf: [0x3a3a4a, 0.5], sights: ["tomb", "tomb", "ruin", "lantern"], air: AIR.ash, fog: [0x2a3040, 0.55], near: 35 },
  savanna: { trees: { broad: 2, bush: 4, dead: 1 }, leaf: [0xa89a4a, 0.45], lush: -0.03, sights: ["bones", "totem", "camp", "stones"], air: AIR.motes },
  desert: { trees: { cactus: 4, bush: 2, dead: 1, palm: 1 }, leaf: [0x8a9a5a, 0.3], lush: -0.04, sights: ["bones", "ruin", "obelisk", "camp"], air: AIR.sand, mesa: true, fog: [0xe0c890, 0.3] },
  scorched: { trees: { dead: 6 }, leaf: [0x2a2420, 0.7], lush: -0.04, sights: ["bones", "obelisk", "ruin"], air: AIR.embers, mesa: true, fog: [0x4a2a1a, 0.5] },
  jungle: { trees: { palm: 4, broad: 3, bush: 2, giant: 1 }, leaf: [0x2a8a3a, 0.3], lush: 0.25, sights: ["ruin", "totem", "stones", "camp"], air: AIR.leaves, fog: [0x4a7a4a, 0.25] },
  town: { trees: { broad: 2, bush: 1 }, lush: -0.05, sights: ["house", "lamp", "house", "well", "lamp", "sign"], dense: 2.2, air: AIR.motes },
  elftown: { trees: { giant: 3, broad: 1 }, leaf: [0x5a6aa8, 0.4], sights: ["moonwell", "lantern", "house", "lantern"], dense: 2, air: AIR.fireflies, fog: [0x3a4a7a, 0.3], roof: 0x5a4a8a },
  forge: { trees: {}, grass: false, sights: ["brazier", "column", "brazier", "house", "column"], dense: 2.4, air: AIR.embers, roof: 0x4a4a52,
    cavern: { fog: 0x140a06, hemi: [0xffb070, 0x2a1408, 0.7], sun: [0xff9a50, 0.5] } },
  crypttown: { trees: { dead: 2 }, grass: false, sights: ["lantern", "column", "tomb", "column"], dense: 2, air: AIR.wisps, roof: 0x3a4a3a,
    cavern: { fog: 0x060c08, hemi: [0xa0ffc0, 0x0a140c, 0.6], sun: [0x8affb0, 0.4] } },
  sky: { trees: null, sights: [], air: null },
};
const BIOME_BY_NAME = [
  [/鐵爐堡/, "forge"], [/幽暗城/, "crypttown"], [/達納蘇斯/, "elftown"], [/暴風城|奧格瑪|雷霆崖|旅店|鎮|村|哨站|營地|港|要塞/, "town"],
  [/丹莫洛|冬泉|奧特蘭克/, "snow"], [/黑海岸/, "coast"], [/泰達希爾|灰谷|梣谷|費伍德|月光林地/, "elven"],
  [/濕地|沼澤/, "swamp"], [/暮色|提里斯法|瘟疫|銀松|詛咒/, "haunted"], [/貧瘠|莫高雷|荒野|阿拉希/, "savanna"],
  [/塔納利斯|希利蘇斯|杜洛塔|千針|凄涼|淒涼|荒蕪|沙漠/, "desert"], [/燃燒平原|灼熱|黑石/, "scorched"], [/荊棘谷|菲拉斯|安戈洛|叢林/, "jungle"],
];
export function biomeOf(name, continent) {
  if (continent) return { key: "sky", ...BIOME.sky };
  const hit = BIOME_BY_NAME.find(([re]) => re.test(name || ""));
  const key = hit ? hit[1] : "temperate";
  const b = { key, ...BIOME[key] };
  if (key === "temperate") {                                       // unknown places still differ from one another
    const h = hashOf(name || "");
    b.sights = [["camp", "stones", "ruin", "sign"], ["tower", "camp", "sign", "well"], ["ruin", "stones", "bones", "camp"]][h % 3];
    b.air = [null, AIR.leaves, AIR.motes][(h >> 3) % 3];
  }
  return b;
}

// ---------------------------------------------------------------- bosses
const BOSS_COLOR = { 4: [0x7a2418, 0xff6a2a], 16: [0x2a4a7a, 0x7ad0ff], 8: [0x2f5a2a, 0x7dff6a], 32: [0x4a2266, 0xc040ff], 64: [0x6a2a6a, 0xff8ff0], 2: [0x8a6a1a, 0xffe680] };
const BOSS_PLAIN = [[0x8a8270, 0xfff0c0], [0x6a1a1a, 0xff4030], [0x3a4654, 0xa8c8ff], [0x4a2266, 0xc040ff], [0x1a5a5a, 0x4affd8], [0x4a5a24, 0xc8ff5a]];
const BOSS_BODY = [
  { head: "horns", gear: "leather", weapon: "axe", bulk: 1.5, scale: 2.3 },                    // a brute
  { head: "hood", gear: "cloth", robe: true, weapon: "staff", bulk: 1.0, scale: 2.2 },         // a caster
  { head: "greathelm", gear: "plate", weapon: "sword", shield: true, spikes: true, bulk: 1.25, scale: 2.2 },   // a knight
  { head: "snout", gear: "mail", weapon: "mace", bulk: 1.35, scale: 2.4 },                     // a beast lord
  { head: "skull", gear: "cloth", robe: true, weapon: "staff", bulk: 0.95, scale: 2.5 },       // a wraith
  { head: "helm", gear: "mail", weapon: "axe", offhand: true, spikes: true, bulk: 1.2, scale: 2.1 },   // a warlord
];
// school: the kind of damage the boss dealt most, when the combat log has it; look: what its name already says
export function bossStyle(name, school, look) {
  const hs = hashOf(name || "boss");
  const magic = [4, 16, 8, 32, 64, 2].find(b => school & b);
  const [armor, aura] = magic ? BOSS_COLOR[magic] : BOSS_PLAIN[hs % BOSS_PLAIN.length];
  const named = look && ["snout", "fish", "skull", "bandana"].includes(look.head);
  const body = named ? { ...look, bulk: 1.25, scale: 2.2 * (look.scale || 1) } : look && look.robe ? { ...BOSS_BODY[hs % 2 ? 1 : 4], orb: aura } : BOSS_BODY[(hs >> 4) % BOSS_BODY.length];
  return { ...body, orb: aura, armor, aura, orbs: 3 + (hs >> 9) % 3 };
}
