"""World database importer: static game data (quests, NPCs, spawns, items) into SQLite.

The world database is a separate file from the character database, so it can be rebuilt
or replaced at any time without touching recorded adventures. Source files are only
parsed as data; no Lua is ever executed.
"""
import sqlite3

SCHEMA = """
CREATE TABLE world_data_sources (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, version TEXT, flavor TEXT, game_build TEXT,
  interface INTEGER, generated TEXT, license TEXT, url TEXT, upstream TEXT, counts TEXT,
  imported_at TEXT NOT NULL);
CREATE TABLE world_maps (
  area_id INTEGER PRIMARY KEY, ui_map_id INTEGER, parent_area_id INTEGER,
  name TEXT, name_zh TEXT, source_id INTEGER);
CREATE INDEX world_maps_ui ON world_maps (ui_map_id);
CREATE TABLE world_quests (
  quest_id INTEGER PRIMARY KEY, name TEXT, name_zh TEXT, level INTEGER, min_level INTEGER,
  area_id INTEGER, ui_map_id INTEGER, hidden INTEGER NOT NULL DEFAULT 0,
  xp INTEGER, xp_confirmed INTEGER NOT NULL DEFAULT 0, next_quest INTEGER,
  data TEXT, text_zh TEXT, source_id INTEGER);
CREATE INDEX world_quests_map ON world_quests (ui_map_id);
CREATE TABLE world_npcs (
  npc_id INTEGER PRIMARY KEY, name TEXT, name_zh TEXT, sub_name TEXT, sub_name_zh TEXT,
  min_level INTEGER, max_level INTEGER, rank INTEGER NOT NULL DEFAULT 0, reaction TEXT,
  area_id INTEGER, respawn INTEGER, quest_starts TEXT, quest_ends TEXT, source_id INTEGER);
CREATE TABLE world_objects (
  object_id INTEGER PRIMARY KEY, name TEXT, name_zh TEXT, area_id INTEGER,
  quest_starts TEXT, quest_ends TEXT, source_id INTEGER);
CREATE TABLE world_spawns (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, entity_id INTEGER NOT NULL,
  area_id INTEGER NOT NULL, ui_map_id INTEGER, x REAL NOT NULL, y REAL NOT NULL,
  respawn INTEGER, source_id INTEGER);
CREATE INDEX world_spawns_entity ON world_spawns (kind, entity_id);
CREATE INDEX world_spawns_pos ON world_spawns (ui_map_id, x);
CREATE TABLE world_items (
  item_id INTEGER PRIMARY KEY, name TEXT, name_zh TEXT, level INTEGER, required_level INTEGER,
  class INTEGER, sub_class INTEGER, start_quest INTEGER, reward_of TEXT, source_id INTEGER);
CREATE TABLE world_quest_items (
  item_id INTEGER NOT NULL, quest_id INTEGER NOT NULL, PRIMARY KEY (item_id, quest_id)) WITHOUT ROWID;
CREATE TABLE world_recipes (
  spell_id INTEGER PRIMARY KEY, name TEXT, skill_id INTEGER, skill TEXT, skill_zh TEXT,
  item_id INTEGER, makes INTEGER NOT NULL DEFAULT 1, learn INTEGER, levels TEXT, reagents TEXT,
  source TEXT, source_id INTEGER);
CREATE INDEX world_recipes_item ON world_recipes (item_id);
CREATE TABLE world_recipe_reagents (
  item_id INTEGER NOT NULL, spell_id INTEGER NOT NULL, amount INTEGER NOT NULL,
  PRIMARY KEY (item_id, spell_id)) WITHOUT ROWID;
CREATE TABLE world_drops (
  source_kind TEXT NOT NULL, source_entity INTEGER NOT NULL, item_id INTEGER NOT NULL,
  chance REAL NOT NULL, PRIMARY KEY (source_kind, source_entity, item_id)) WITHOUT ROWID;
CREATE INDEX world_drops_item ON world_drops (item_id);
CREATE TABLE world_vendors (
  npc_id INTEGER NOT NULL, item_id INTEGER NOT NULL, stock INTEGER,
  PRIMARY KEY (npc_id, item_id)) WITHOUT ROWID;
CREATE INDEX world_vendors_item ON world_vendors (item_id);
"""


def create(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn
