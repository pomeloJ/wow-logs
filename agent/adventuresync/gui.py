"""The sync program with a window: the same on Windows and macOS.

One window to type the server's address and the token, install the addon, and see whether
syncing works. Everything it does goes through the same functions as the command line; the
work runs on a thread of its own so the window never freezes.
"""
import logging
import queue as queues
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, extras, update
from .core import (DB_GLOBAL, Config, Queue, Uploader, find_saved_variables, load_config, read_stable, save_config, scan_screenshots,
                   scan, wow_roots)

CHECK_EVERY = 6 * 3600                               # seconds between looks for a newer version
HOME = Path("~/.adventuresync").expanduser()
CLASSES = {"WARRIOR": "戰士", "PALADIN": "聖騎士", "HUNTER": "獵人", "ROGUE": "盜賊", "PRIEST": "牧師", "SHAMAN": "薩滿",
           "MAGE": "法師", "WARLOCK": "術士", "DRUID": "德魯伊"}
log = logging.getLogger("adventuresync")


def settings_path() -> Path:
    """A config.toml beside the program wins (a checkout, or a folder someone set up by hand);
    otherwise the one in the user's own folder, which is where the window saves."""
    here = Path.cwd() / "config.toml"
    return here if here.is_file() else HOME / "config.toml"


def read_settings(path: Path) -> Config:
    try:
        return load_config(path)
    except (OSError, ValueError):
        return Config(url="", api_token="", device_id="windows-pc" if sys.platform.startswith("win") else "mac")


CARD_FIELDS = ("realm", "race", "faction", "money", "zone_name", "sub_zone", "xp", "xp_max", "rested_xp", "resting", "guild")
CLOUD_EVERY = 300                                    # seconds between asking the server for its copy of the characters
SAME_WITHIN = 60                                     # saves this close together count as the same state


def card_of(cid: str, data: Any, at: Any) -> Dict[str, Any]:
    """What a character card shows, from a snapshot's data (the game's file or the server's copy)."""
    data = data if isinstance(data, dict) else {}
    return {"id": str(cid), "name": data.get("name") or str(cid), "class": data.get("class"), "level": data.get("level"),
            "captured_at": at if isinstance(at, (int, float)) else None, **{k: data.get(k) for k in CARD_FIELDS}}


def merge_cards(local: List[Dict[str, Any]], cloud: List[Dict[str, Any]], chosen: Dict[str, str]) -> List[Dict[str, Any]]:
    """One entry per character, here or on the server: {"id", "local", "cloud", "view", "card"}.

    `view` is "local" or "cloud": the one picked in `chosen`, else the newer of the two (the
    server's when it holds a later save, from another computer say). `card` is that one."""
    here = {c["id"]: c for c in local}
    there = {c["id"]: card_of(c["id"], c.get("data"), c.get("captured_at")) | {"device_id": c.get("device_id")} for c in cloud}
    out = []
    for cid in dict.fromkeys([*here, *there]):
        mine, theirs = here.get(cid), there.get(cid)
        if mine and theirs:
            view = chosen.get(cid) or ("cloud" if (theirs["captured_at"] or 0) > (mine["captured_at"] or 0) + SAME_WITHIN else "local")
        else:
            view = "local" if mine else "cloud"
        out.append({"id": cid, "local": mine, "cloud": theirs, "view": view, "card": mine if view == "local" else theirs})
    return sorted(out, key=lambda e: -(e["card"].get("captured_at") or 0))      # the one played last comes first


def characters(wow_path: str) -> List[Dict[str, Any]]:
    """The characters the addon has recorded on this computer: id, name, class and level, and
    for the window's cards where they are, their gold and experience, and when the game last
    saved them (captured_at, in seconds since 1970)."""
    found: Dict[str, Dict[str, Any]] = {}
    for path in find_saved_variables(wow_path):
        try:
            saved = (read_stable(path, settle=0.1).get(DB_GLOBAL) or {}).get("characters") or {}
        except Exception:                             # a file being written, or not ours: skip it
            continue
        for cid, snap in saved.items() if isinstance(saved, dict) else []:
            data = (snap or {}).get("data") if isinstance(snap, dict) else None
            at = snap.get("captured_at") if isinstance(snap, dict) else None
            at = at if isinstance(at, (int, float)) else None
            if str(cid) in found and (found[str(cid)].get("captured_at") or 0) >= (at or 0):
                continue                              # the same character in two game folders: the newer one
            found[str(cid)] = card_of(str(cid), data, at)
    return sorted(found.values(), key=lambda c: str(c["name"]))


def saves_signature(wow_path: str) -> tuple:
    """Changes whenever the game saves: to read the characters again only then."""
    out = []
    for path in find_saved_variables(wow_path):
        try:
            st = path.stat()
            out.append((str(path), st.st_size, st.st_mtime_ns))
        except OSError:
            pass
    return tuple(out)


def addon_source() -> Optional[Path]:
    """The addon that came with the program: packed inside a built app, or in the repository."""
    packed = Path(getattr(sys, "_MEIPASS", "")) / "addon" / "AdventureCollector"
    here = Path(__file__).resolve().parent
    for folder in (packed, here.parent / "addon" / "AdventureCollector", here.parent.parent / "addon" / "AdventureCollector"):
        if (folder / "AdventureCollector.toc").is_file():
            return folder
    return None


def game_folders(wow_path: str) -> List[Path]:
    """Every <game>/_x_/Interface/AddOns folder there is, for the chosen game folder or the usual places."""
    found = []
    for root in wow_roots(wow_path):
        if not root.is_dir():
            continue
        for base in ([root] if (root / "Interface").is_dir() else [d for d in sorted(root.iterdir()) if d.is_dir()]):
            if (base / "Interface" / "AddOns").is_dir() and base / "Interface" / "AddOns" not in found:
                found.append(base / "Interface" / "AddOns")
    return found


def install_addon(wow_path: str) -> List[Path]:
    """Copy the addon into every game version found. Returns where it went."""
    source = addon_source()
    if source is None:
        raise RuntimeError("the addon files did not come with this program")
    targets = game_folders(wow_path)
    if not targets:
        raise RuntimeError("the game's AddOns folder was not found: choose the game folder first")
    for addons in targets:
        dest = addons / "AdventureCollector"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
    return [t / "AdventureCollector" for t in targets]


class Worker(threading.Thread):
    """Does the talking to the server and the reading of game files. The window sends it
    requests and reads back what it reports; neither waits on the other."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.requests: "queues.Queue[tuple]" = queues.Queue()
        self.reports: "queues.Queue[Dict[str, Any]]" = queues.Queue()
        self.auto = False
        self._cfg: Optional[Config] = None
        self._url = ""                                # the server to ask about newer versions
        self._wow = ""                                # the chosen game folder, to look for the installed addon
        self._saves: Optional[tuple] = None           # the game's saves when the characters were last read
        self._next_saves = 0.0

    def ask(self, what: str, cfg: Optional[Config] = None) -> None:
        self.requests.put((what, cfg))

    def _say(self, **report: Any) -> None:
        self.reports.put(report)

    def _status(self, store: Queue, cfg: Config) -> None:
        files = find_saved_variables(cfg.wow_path)
        try:
            combat = extras.combat_log_check(store)
        except Exception:                                   # a status line must never stop syncing
            combat = None
        self._say(counts=store.counts(), files=[str(f) for f in files], auto=self.auto, combat=combat)

    def run(self) -> None:
        store: Optional[Queue] = None
        up: Optional[Uploader] = None
        seen: Dict[Path, Any] = {}
        next_scan = next_sync = next_check = next_shots = next_settings = next_cloud = 0.0
        failures = 0
        while True:
            try:
                what, cfg = self.requests.get(timeout=1)
            except queues.Empty:
                what, cfg = "tick", None
            if what == "quit":
                return
            if isinstance(cfg, Config):
                self._wow = cfg.wow_path
            if isinstance(cfg, Config) and cfg.url.startswith(("http://", "https://")) and cfg.url != self._url:
                self._url, next_check = cfg.url, 0.0            # another server: ask it right away
            if self._url and (what == "update" or (what == "tick" and time.monotonic() >= next_check)):
                next_check = time.monotonic() + CHECK_EVERY
                try:
                    self._say(update=update.check(self._url))
                    self._say(addon=update.check_addon(self._url, game_folders(self._wow)))
                except Exception as e:                          # no answer now: nothing to offer, ask again later
                    log.debug("could not ask for a newer version: %s", e)
            if what in ("update", "addon"):
                self._say(addons=update.installed_addons(game_folders(self._wow)))
            if what == "update":
                continue
            if what == "addon":                                 # cfg is what update.check_addon found
                try:
                    done = update.fetch_addon(cfg)
                    self._say(addon_done=cfg["version"], addon=None, addons=update.installed_addons(game_folders(self._wow)),
                              message=f"遊戲插件已更新到 {cfg['version']}（{len(done)} 處）。遊戲開著的話，在遊戲裡輸入 /reload 才會生效。", ok=True)
                except Exception as e:
                    self._say(message=f"插件更新失敗：{e}", ok=False, addon_failed=True)
                continue
            if what == "fetch":                                 # cfg is what update.check found
                try:
                    told = [0.0]

                    def progress(got: int, size: Optional[int]) -> None:
                        if time.monotonic() - told[0] >= 0.3:
                            told[0] = time.monotonic()
                            self._say(fetching=(got, size))
                    self._say(fetched=str(update.fetch(cfg, progress=progress)))
                except Exception as e:
                    self._say(message=f"新版下載失敗：{e}", ok=False, fetch_failed=True)
                continue
            try:
                if what == "chars":                             # needs no server: just the game's files
                    wow = cfg.wow_path if cfg else self._cfg.wow_path if self._cfg else ""
                    self._saves = saves_signature(wow)
                    self._say(characters=characters(wow))
                    continue
                if what == "tick" and time.monotonic() >= self._next_saves:   # the game saved: show the new state
                    self._next_saves = time.monotonic() + 5
                    wow = self._cfg.wow_path if self._cfg else self._wow
                    sig = saves_signature(wow)
                    if self._saves is not None and sig != self._saves:
                        self._saves = sig
                        self._say(characters=characters(wow))
                if isinstance(cfg, Config) and cfg != self._cfg:     # new settings: start afresh with them
                    self._cfg, store, up, seen = cfg, Queue(cfg.db_path), None, {}
                    up = Uploader(cfg, store)
                    next_settings = 0.0                                 # and ask the server what it holds for them
                if self._cfg is None or store is None or up is None or not self._cfg.url \
                        or not (self._cfg.api_token or self._cfg.tokens):
                    if what != "tick":
                        self._say(message="請先填入伺服器網址與至少一個權杖並儲存。", ok=False)
                    continue
                if what == "shots":                             # cfg is the box's new state
                    problems = up.set_screenshots(bool(cfg))
                    if problems:
                        self._say(message="截圖上傳設定沒有改成：" + "、".join(dict.fromkeys(problems)), ok=False)
                    else:
                        self._say(message=("已開啟截圖上傳：之後在遊戲裡截的圖會在幾秒內上傳"
                                           + ("。" if self.auto else "（要先按「開始自動同步」）。")) if cfg
                                  else "已關閉截圖上傳。", ok=True)
                    self._say(shots=up.screenshots_on(), shots_info=up.settings())
                    next_settings = time.monotonic() + up.SETTINGS_EVERY
                    continue
                if what == "keep":                              # cfg is how many screenshots to keep
                    problems = up.set_settings(keep_screenshots=int(cfg))
                    if problems:
                        self._say(message="保留張數沒有改成：" + "、".join(dict.fromkeys(problems)), ok=False)
                    else:
                        self._say(message=f"伺服器上的截圖改為最多保留 {int(cfg)} 張，超過時刪掉最舊的。", ok=True)
                    self._say(shots_info=up.settings(fresh=True))
                    next_settings = time.monotonic() + up.SETTINGS_EVERY
                    continue
                if what == "tick" and time.monotonic() >= next_settings:
                    next_settings = time.monotonic() + up.SETTINGS_EVERY
                    held = up.settings()                        # changed on another computer: shown here too
                    self._say(shots=bool(held.get("upload_screenshots")), shots_info=held)
                if what == "cloud" or (what == "tick" and time.monotonic() >= next_cloud):
                    next_cloud = time.monotonic() + CLOUD_EVERY
                    try:                                        # the server's copy, perhaps saved on another computer
                        self._say(cloud=up.cloud_characters([c["id"] for c in characters(self._cfg.wow_path)]))
                    except Exception as e:                      # only the cards miss out: never stops syncing
                        log.debug("could not ask for the server's characters: %s", e)
                    if what == "cloud":
                        continue
                if self.auto and time.monotonic() >= next_shots:
                    next_shots = time.monotonic() + 1           # a new screenshot goes up within seconds
                    if scan_screenshots(store, up):
                        self._say(shots_info=up.settings(fresh=True))     # one more on the server (and perhaps one fewer old)
                if what == "test":
                    info = up.test_connection()
                    names = {c["id"]: c["name"] for c in characters(self._cfg.wow_path)}
                    lines = []
                    for t in info["tokens"]:
                        label = "權杖" if t["character_id"] is None else names.get(t["character_id"], t["character_id"])
                        held = "、".join(c.get("name") or c["id"] for c in t["characters"])
                        if t["ok"]:
                            lines.append(f"{label}：正確" + ("（伺服器主人的權杖）" if t["kind"] == "owner" else
                                                         f"（名下角色：{held}）" if held else "（還沒上傳過角色）"))
                        elif t["kind"] == "read":
                            lines.append(f"{label}：這是只能看的權杖，不能上傳")
                        elif t["status"] == 200:
                            lines.append(f"{label}：這個角色已經屬於別人的權杖，上傳會被拒絕")
                        else:
                            lines.append(f"{label}：被拒絕（HTTP {t['status']}）")
                    self._say(message="連線正常。" + "　".join(lines), ok=info["token_ok"], connected=info["token_ok"])
                elif what in ("start", "stop"):
                    self.auto = what == "start"
                    next_scan = next_sync = 0.0
                    self._say(message="已開始自動同步。" if self.auto else "已停止自動同步。", ok=True)
                due = self.auto and time.monotonic() >= next_scan
                if what == "sync" or due:
                    scan(self._cfg, store, seen if what != "sync" else {}, up)
                    next_scan = time.monotonic() + 5
                    if what == "sync" or time.monotonic() >= next_sync:
                        res = up.sync_once()
                        failures = failures + 1 if res["error"] else 0
                        if res["snapshots"]:
                            next_cloud = 0.0                    # the server's copy just changed: show it

                        next_sync = time.monotonic() + min(self._cfg.sync_interval * (2 ** min(failures, 5)), 600)
                        if res.get("waiting"):
                            names = {c["id"]: c["name"] for c in characters(self._cfg.wow_path)}
                            self._say(waiting="、".join(names.get(c, c) for c in res["waiting"]))
                        else:
                            self._say(waiting="")
                        if res["error"]:
                            self._say(message=f"同步失敗：{res['error']}", ok=False, connected=False)
                        elif what == "sync" or res["uploaded"] or res["snapshots"] or res["rejected"]:
                            self._say(message=f"已同步：事件 {res['uploaded']} 筆、角色快照 {res['snapshots']} 份"
                                      + (f"、被拒絕 {res['rejected']} 筆" if res["rejected"] else ""),
                                      ok=True, connected=True, synced_at=time.time())
                        else:
                            self._say(connected=True, checked_at=time.time())
                if what != "tick" or due:
                    self._status(store, self._cfg)
            except Exception as e:  # report it and keep going: the window must stay usable
                self._say(message=f"發生錯誤：{e}", ok=False)


class _ToWindow(logging.Handler):
    def __init__(self, reports: "queues.Queue[Dict[str, Any]]") -> None:
        super().__init__(logging.INFO)
        self.reports = reports

    def emit(self, record: logging.LogRecord) -> None:
        self.reports.put({"log": self.format(record)})


RACES = {"Human": "人類", "Dwarf": "矮人", "NightElf": "夜精靈", "Gnome": "地精", "Orc": "獸人", "Scourge": "不死族",
         "Tauren": "牛頭人", "Troll": "食人妖", "BloodElf": "血精靈", "Draenei": "德萊尼"}
FACTIONS = {"Alliance": "聯盟", "Horde": "部落"}
CLASS_COLORS = {"WARRIOR": "#C69B6D", "PALADIN": "#F48CBA", "HUNTER": "#AAD372", "ROGUE": "#E6D94C", "PRIEST": "#C9CED6",
                "SHAMAN": "#2E8BE6", "MAGE": "#3FC7EB", "WARLOCK": "#8788EE", "DRUID": "#FF7C0A"}
# The window's colours: one light theme, the same on Windows and macOS.
BG, CARD, INK, MUTED, LINE = "#f3f4f7", "#ffffff", "#1f2430", "#6b7280", "#e2e5ea"
ACCENT, ACCENT_DARK, HEAD, OK, BAD, WARN, XP = "#3b5bdb", "#2f4ac0", "#1d2840", "#2e7d32", "#c62828", "#a15c00", "#8e6cef"


def money_text(copper: Any) -> str:
    if not isinstance(copper, (int, float)):
        return "—"
    c = int(copper)
    return f"{c // 10000:,} 金 {c // 100 % 100} 銀 {c % 100} 銅"


def ago_text(epoch: Any, suffix: str = "存檔") -> str:
    if not isinstance(epoch, (int, float)):
        return "還沒有存檔紀錄"
    s = max(0, time.time() - epoch)
    if s < 90:
        return "剛剛" + suffix
    if s < 3600:
        return f"{round(s / 60)} 分鐘前{suffix}"
    if s < 86400:
        return f"{round(s / 3600)} 小時前{suffix}"
    return f"{round(s / 86400)} 天前{suffix}"


def combat_text(c: Dict[str, Any]) -> Dict[str, Any]:
    """The 戰鬥紀錄 line, short, and a banner for the top of the window when something is wrong."""
    hm = lambda t: time.strftime("%H:%M", time.localtime(t))      # noqa: E731
    if c["state"] == "ok":
        return {"row": f"✓ 有收到（{hm(c['last_seen'])}）", "color": OK, "banner": None}
    if c["state"] == "missing":
        return {"row": "✗ 沒收到", "color": BAD, "bad": True,
                "banner": f"✗ 戰鬥紀錄沒開：{hm(c['newest_fight'])} 的戰鬥沒記到　遊戲裡輸入 /adventure combatlog on"}
    # Neither is a sign of anything wrong: no banner. The game writes its log file minutes late,
    # and the addon tells of its fights only when it saves; the game itself reminds at login.
    if c["state"] == "checking":
        return {"row": "… 等遊戲寫入（會慢幾分鐘）", "color": MUTED, "bad": False, "banner": None}
    return {"row": "— 還沒有戰鬥可對照", "color": MUTED, "bad": False, "banner": None}


def main(config: Optional[str] = None) -> int:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, ttk

    path = Path(config).expanduser() if config else settings_path()
    cfg = read_settings(path)
    worker = Worker()
    handler = _ToWindow(worker.reports)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("adventuresync").addHandler(handler)
    logging.getLogger("adventuresync").setLevel(logging.INFO)

    if sys.platform.startswith("win"):
        # Without this Windows draws the window small and stretches it on a scaled display: blurry text.
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            # a name of its own, so the taskbar shows this program's icon, not Python's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AdventureSync")
        except Exception:
            pass
    root = tk.Tk()
    root.title(f"AdventureSync {__version__}")
    root.configure(bg=BG)
    assets = Path(__file__).resolve().parent / "assets"            # packed beside this file in a build too
    try:
        # every size drawn beforehand, so the title bar's 16 px one is sharp; kept, as Tk forgets
        # an image nothing in Python holds. (Not the .ico: Tk cannot read how Pillow writes it.)
        root._icons = [tk.PhotoImage(file=str(f)) for f in [assets / "icon.png"] + sorted(assets.glob("icon-*.png"))]
        root.iconphoto(True, *root._icons)
    except tk.TclError:                                             # no icon files (an old source copy): Tk's own
        pass
    # Text follows the display's scaling by itself; lengths given in pixels have to be scaled here.
    zoom = max(root.winfo_fpixels("1i") / 96, 1) if sys.platform.startswith("win") else 1
    px = lambda n: round(n * zoom)                                             # noqa: E731
    root.minsize(px(760), px(700))
    root.geometry(f"{px(780)}x{px(760)}")

    # ---------------------------------------------------------------- look
    families = set(tkfont.families())
    family = next((f for f in ("Microsoft JhengHei UI", "Microsoft JhengHei", "PingFang TC", "Heiti TC", "Noto Sans CJK TC")
                   if f in families), tkfont.nametofont("TkDefaultFont").actual("family"))
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
        tkfont.nametofont(name).configure(family=family, size=10)
    font = lambda size, weight="normal": (family, size, weight)                # noqa: E731
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=INK, font=font(10), bordercolor=LINE, lightcolor=BG, darkcolor=BG,
                    troughcolor=LINE, focuscolor=ACCENT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=INK)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Head.TFrame", background=HEAD)
    style.configure("HeadTitle.TLabel", background=HEAD, foreground="#ffffff", font=font(15, "bold"))
    style.configure("HeadSub.TLabel", background=HEAD, foreground="#aab4cf", font=font(9))
    style.configure("Pill.TLabel", background=HEAD, foreground="#aab4cf", font=font(10, "bold"))
    style.configure("Card.TFrame", background=CARD, bordercolor=LINE, lightcolor=CARD, darkcolor=CARD, relief="solid", borderwidth=1)
    style.configure("Inner.TFrame", background=CARD)
    style.configure("Card.TLabel", background=CARD, foreground=INK)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=font(9))
    style.configure("CardTitle.TLabel", background=CARD, foreground=INK, font=font(13, "bold"))
    style.configure("CardKey.TLabel", background=CARD, foreground=MUTED)
    style.configure("Section.TLabel", background=CARD, foreground=INK, font=font(11, "bold"))
    style.configure("Level.TLabel", background="#eef1fb", foreground=ACCENT, font=font(10, "bold"), padding=(8, 2))
    style.configure("Banner.TFrame", background="#fff6e0")
    style.configure("Banner.TLabel", background="#fff6e0", foreground=WARN, font=font(10, "bold"))
    style.configure("TButton", padding=(12, 5), background="#ffffff", bordercolor=LINE, lightcolor="#ffffff", darkcolor="#ffffff")
    style.map("TButton", background=[("active", "#eef1fb"), ("disabled", BG)], bordercolor=[("active", ACCENT)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT, lightcolor=ACCENT,
                    darkcolor=ACCENT, font=font(10, "bold"), padding=(16, 6))
    style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#9aa8dc")], bordercolor=[("active", ACCENT_DARK)])
    style.configure("Stop.TButton", background="#ffffff", foreground=BAD, bordercolor="#f0c4c4", font=font(10, "bold"), padding=(16, 6))
    style.map("Stop.TButton", background=[("active", "#fdecec")])
    style.configure("TEntry", fieldbackground="#ffffff", bordercolor=LINE, lightcolor="#ffffff", darkcolor="#ffffff", padding=5)
    style.configure("TSpinbox", fieldbackground="#ffffff", bordercolor=LINE, lightcolor="#ffffff", darkcolor="#ffffff",
                    arrowcolor=MUTED, background="#ffffff", padding=3)
    style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
    # The theme's own box shows an X when ticked, which reads as "no": a drawn box with a check mark instead.
    def box_image(ticked: bool) -> Any:
        n = px(16)
        img = tk.PhotoImage(width=n, height=n)
        img.put(ACCENT if ticked else "#aeb6c4", to=(0, 0, n, n))
        img.put(ACCENT if ticked else "#ffffff", to=(1, 1, n - 1, n - 1))
        if ticked:
            t = max(2, n // 8)
            for (x0, y0), (x1, y1) in (((0.22, 0.50), (0.42, 0.70)), ((0.42, 0.70), (0.78, 0.28))):
                steps = n * 2
                for i in range(steps + 1):
                    x = round((x0 + (x1 - x0) * i / steps) * n - t / 2)
                    y = round((y0 + (y1 - y0) * i / steps) * n - t / 2)
                    img.put("#ffffff", to=(max(x, 0), max(y, 0), min(x + t, n), min(y + t, n)))
        return img
    root._boxes = (box_image(False), box_image(True))     # kept: Tk forgets an image nothing in Python holds
    style.element_create("Tick.indicator", "image", root._boxes[0], ("selected", root._boxes[1]),
                         width=px(16) + px(9), sticky="w")         # the extra width is the gap before the text
    style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Tick.indicator", {"side": "left", "sticky": ""}),
        ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [("Checkbutton.label", {"sticky": "nswe"})]})]})])
    style.configure("TCheckbutton", background=CARD, foreground=INK, font=font(10, "bold"))
    style.map("TCheckbutton", background=[("active", CARD)])
    style.configure("TNotebook", background=BG, borderwidth=0, bordercolor=LINE, lightcolor=BG, darkcolor=BG,
                    tabmargins=(px(18), px(10), px(18), 0))
    style.configure("TNotebook.Tab", padding=(px(18), px(7)), background=BG, foreground=MUTED, borderwidth=1,
                    bordercolor=BG, lightcolor=BG, darkcolor=BG, font=font(10, "bold"))
    style.map("TNotebook.Tab", background=[("selected", CARD), ("active", "#e9ecf3")], foreground=[("selected", ACCENT)],
              lightcolor=[("selected", CARD)], darkcolor=[("selected", CARD)], bordercolor=[("selected", LINE)],
              expand=[("selected", (0, 0, 0, 0))], padding=[("selected", (px(18), px(7)))])
    style.configure("XP.Horizontal.TProgressbar", troughcolor="#eceaf8", background=XP, bordercolor="#eceaf8",
                    lightcolor=XP, darkcolor=XP, thickness=px(8))
    style.configure("Vertical.TScrollbar", background="#d5d9e1", troughcolor=BG, bordercolor=BG, lightcolor="#d5d9e1",
                    darkcolor="#d5d9e1", arrowcolor=MUTED, gripcount=0, arrowsize=px(11))
    style.map("Vertical.TScrollbar", background=[("active", "#b9bfcb")])

    def card(parent: Any, title: Optional[str] = None, padding: int = 14) -> Any:
        """A white box with an optional heading; returns the frame to put things in."""
        outer = ttk.Frame(parent, style="Card.TFrame", padding=px(padding))
        if title:
            ttk.Label(outer, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, px(8)))
        return outer

    # ---------------------------------------------------------------- header
    head = ttk.Frame(root, style="Head.TFrame", padding=(px(18), px(12)))
    head.pack(fill="x")
    titles = ttk.Frame(head, style="Head.TFrame")
    titles.pack(side="left")
    ttk.Label(titles, text="AdventureSync", style="HeadTitle.TLabel").pack(anchor="w")
    ttk.Label(titles, text=f"冒險紀錄同步程式　版本 {__version__}", style="HeadSub.TLabel").pack(anchor="w")

    def toggle() -> None:
        send("stop" if worker.auto else "start")

    auto_button = ttk.Button(head, text="開始自動同步", style="Accent.TButton", command=toggle)
    auto_button.pack(side="right")
    pill = ttk.Label(head, text="● 已停止", style="Pill.TLabel")
    pill.pack(side="right", padx=(0, px(14)))

    def show_auto(on: bool) -> None:
        auto_button.configure(text="停止自動同步" if on else "開始自動同步", style="Stop.TButton" if on else "Accent.TButton")
        pill.configure(text="● 自動同步中" if on else "● 已停止", foreground="#7ee2a0" if on else "#aab4cf")
        shown["auto"].configure(text="進行中" if on else "未啟動")

    # the message line, at the very bottom whatever tab is open
    foot = ttk.Frame(root, padding=(px(18), px(6), px(18), px(10)))
    foot.pack(side="bottom", fill="x")
    message = ttk.Label(foot, text=f"設定檔：{path}", style="Muted.TLabel", wraplength=px(720), justify="left")
    message.pack(anchor="w")

    # ---------------------------------------------------------------- update banners
    # shown under the header, and only while the server offers a newer version
    bar = ttk.Frame(root, style="Banner.TFrame", padding=(px(18), px(8)))
    bar_text = ttk.Label(bar, style="Banner.TLabel")
    bar_text.pack(side="left")
    newer: Dict[str, Any] = {}

    def offer(found: Optional[Dict[str, Any]]) -> None:
        newer.clear()
        if not found:
            bar.pack_forget()
            return
        newer.update(found)
        bar_text.configure(text=f"有新版本 {found['version']}（這個是 {__version__}）")
        bar_button.configure(text="下載並開啟新版" if update.can_fetch(found) else "下載新版", state="normal")
        bar.pack(fill="x", after=head)

    def get_update() -> None:
        if not newer:
            return
        if update.can_fetch(newer):
            bar_button.configure(text="下載中…", state="disabled")
            worker.ask("fetch", dict(newer))
            return
        import webbrowser
        webbrowser.open(newer["url"])
        note("已用瀏覽器下載新版。" + ("解壓後蓋掉舊的資料夾，" if newer["kind"] == "source" else "下載完成後，") + "關掉這個視窗，改開新的 AdventureSync。", True)

    def opened(path: str) -> None:
        try:
            update.launch(Path(path))
        except OSError as e:
            note(f"新版已下載到 {path}，但開不起來（{e}）。請關掉這個視窗後自己點兩下開啟。", False)
            bar_button.configure(text="下載並開啟新版", state="normal")
            return
        note(f"新版已下載到 {path}，正在開啟；這個視窗會自己關掉。舊的檔案可以刪除。", True)
        root.after(1500, close)

    bar_button = ttk.Button(bar, command=get_update)
    bar_button.pack(side="right")

    # the same for the addon in the game, which can be brought up to date without a new program
    addon_bar = ttk.Frame(root, style="Banner.TFrame", padding=(px(18), px(8)))
    addon_text = ttk.Label(addon_bar, style="Banner.TLabel")
    addon_text.pack(side="left")
    addon_newer: Dict[str, Any] = {}

    def offer_addon(found: Optional[Dict[str, Any]]) -> None:
        addon_newer.clear()
        if not found:
            addon_bar.pack_forget()
            return
        addon_newer.update(found)
        addon_text.configure(text=f"遊戲插件有新版本 {found['version']}（遊戲裡的是 {found['installed']}）")
        addon_button.configure(text="更新插件", state="normal")
        addon_bar.pack(fill="x", after=head)

    def get_addon() -> None:
        if addon_newer:
            addon_button.configure(text="更新中…", state="disabled")
            worker.ask("addon", dict(addon_newer))

    addon_button = ttk.Button(addon_bar, command=get_addon)
    addon_button.pack(side="right")

    # the combat log: a banner that cannot be missed while the player's fights are not coming through
    combat_bar = tk.Frame(root, padx=px(18), pady=px(10))
    combat_label = tk.Label(combat_bar, font=font(12, "bold"), anchor="w", justify="left")
    combat_label.pack(fill="x")

    def show_combat(c: Dict[str, Any]) -> None:
        said = combat_text(c)
        shown["combat"].configure(text=said["row"], foreground=said["color"])
        if not said["banner"]:
            combat_bar.pack_forget()
            return
        back, ink = ("#fde2e1", BAD) if said.get("bad") else ("#ffefc9", WARN)
        combat_bar.configure(background=back)
        combat_label.configure(text=said["banner"], background=back, foreground=ink)
        combat_bar.pack(fill="x", after=head)

    # ---------------------------------------------------------------- tabs
    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True)
    tab_chars, tab_sync, tab_setup, tab_log = (ttk.Frame(tabs, padding=(px(18), px(14))) for _ in range(4))
    for tab, text in ((tab_chars, "角色"), (tab_sync, "同步"), (tab_setup, "設定"), (tab_log, "紀錄")):
        tabs.add(tab, text=text)

    # ---- 角色: one card per character the addon has recorded on this computer
    top = ttk.Frame(tab_chars)
    top.pack(fill="x", pady=(0, px(10)))
    summary = ttk.Label(top, text="讀取角色中…", style="Muted.TLabel")
    summary.pack(side="left")

    def refresh() -> None:
        now = current()
        worker.ask("chars", now)
        if now.url and (now.api_token or now.tokens):
            worker.ask("settings", now)
            worker.ask("cloud", now)

    ttk.Button(top, text="重新整理", command=refresh).pack(side="right")
    scroller = tk.Canvas(tab_chars, bg=BG, highlightthickness=0, bd=0)
    scrollbar = ttk.Scrollbar(tab_chars, orient="vertical", command=scroller.yview)
    scroller.configure(yscrollcommand=scrollbar.set)
    scroller.pack(side="left", fill="both", expand=True)
    grid = ttk.Frame(scroller)
    grid_window = scroller.create_window((0, 0), window=grid, anchor="nw")

    def fit(_: Any = None) -> None:
        """The scroll bar only when the cards do not fit."""
        scroller.configure(scrollregion=scroller.bbox("all"))
        if grid.winfo_reqheight() > scroller.winfo_height() + 2:
            if not scrollbar.winfo_ismapped():
                scrollbar.pack(side="right", fill="y", before=scroller)
        elif scrollbar.winfo_ismapped():
            scrollbar.pack_forget()
            scroller.yview_moveto(0)
    grid.bind("<Configure>", fit)
    scroller.bind("<Configure>", lambda e: (scroller.itemconfigure(grid_window, width=e.width), fit()))
    grid.columnconfigure((0, 1), weight=1, uniform="cards")

    def wheel(event: Any) -> None:
        if grid.winfo_height() > scroller.winfo_height():
            scroller.yview_scroll(int(-event.delta / (120 if sys.platform.startswith("win") else 1)), "units")
    scroller.bind("<Enter>", lambda e: scroller.bind_all("<MouseWheel>", wheel))
    scroller.bind("<Leave>", lambda e: scroller.unbind_all("<MouseWheel>"))

    def where_from(cloud: Dict[str, Any]) -> str:
        device = cloud.get("device_id")
        return "這台電腦" if device and device == fields["device"].get().strip() else (device or "另一台電腦")

    def source_row(body: Any, e: Dict[str, Any]) -> None:
        """Which copy the card shows, how old the other one is, and a link to switch between them."""
        mine, theirs, view = e["local"], e["cloud"], e["view"]
        row = ttk.Frame(body, style="Inner.TFrame")
        row.pack(fill="x", pady=(px(8), 0))
        shown_at = ago_text(e["card"].get("captured_at"))
        label = f"☁ 雲端（來自 {where_from(theirs)}）· {shown_at}" if view == "cloud" else f"本機 · {shown_at}"
        ttk.Label(row, text=label, style="CardMuted.TLabel").pack(side="left")
        if not (mine and theirs):
            if theirs:
                ttk.Label(body, text="這台電腦沒有這個角色的存檔", style="CardMuted.TLabel").pack(anchor="w")
            return
        gap = (theirs["captured_at"] or 0) - (mine["captured_at"] or 0)
        if abs(gap) <= SAME_WITHIN:
            ttk.Label(row, text="　✓ 和雲端一致", style="CardMuted.TLabel", foreground=OK).pack(side="left")
            return
        other = "local" if view == "cloud" else "cloud"
        link = tk.Label(row, text="看本機" if other == "local" else "看雲端", bg=CARD, fg=ACCENT, cursor="hand2",
                        font=font(9, "underline"))
        link.pack(side="right")
        link.bind("<Button-1>", lambda _: (chosen.__setitem__(e["id"], other), draw_cards()))
        if gap > 0:
            hint = f"雲端有較新的資料：本機的是 {ago_text(mine['captured_at'], '')}" if view == "cloud" else \
                f"這是本機的舊資料，雲端有 {ago_text(theirs['captured_at'], '')}的更新"
            color = WARN if view == "local" else MUTED
        else:
            hint = "本機的比雲端新，還沒上傳" if view == "local" else f"這是雲端的舊資料，本機有 {ago_text(mine['captured_at'], '')}的存檔"
            color = WARN if view == "cloud" else MUTED
        ttk.Label(body, text=hint, style="CardMuted.TLabel", foreground=color, wraplength=px(300), justify="left").pack(anchor="w")

    def character_card(parent: Any, e: Dict[str, Any]) -> Any:
        c = e["card"]
        outer = ttk.Frame(parent, style="Card.TFrame")
        tk.Frame(outer, bg=CLASS_COLORS.get(c.get("class") or "", "#c3c8d2"), width=px(6)).pack(side="left", fill="y")
        body = ttk.Frame(outer, style="Inner.TFrame", padding=(px(14), px(12)))
        body.pack(side="left", fill="both", expand=True)
        line = ttk.Frame(body, style="Inner.TFrame")
        line.pack(fill="x")
        ttk.Label(line, text=c["name"], style="CardTitle.TLabel").pack(side="left")
        if c.get("level"):
            ttk.Label(line, text=f"Lv {c['level']}", style="Level.TLabel").pack(side="right")
        who = [RACES.get(c.get("race") or "", c.get("race")), CLASSES.get(c.get("class") or "", c.get("class")),
               FACTIONS.get(c.get("faction") or "", c.get("faction"))]
        ttk.Label(body, text="　".join(str(x) for x in who if x) or "—", style="CardMuted.TLabel").pack(anchor="w", pady=(px(2), 0))
        if c.get("realm") or c.get("guild"):
            ttk.Label(body, text="　".join(str(x) for x in (c.get("realm"), c.get("guild") and f"<{c['guild']}>") if x),
                      style="CardMuted.TLabel").pack(anchor="w")
        facts = ttk.Frame(body, style="Inner.TFrame")
        facts.pack(fill="x", pady=(px(10), 0))
        facts.columnconfigure(1, weight=1)
        place = "・".join(str(x) for x in (c.get("zone_name"), c.get("sub_zone")) if x) or "—"
        for row, (key, value) in enumerate((("位置", place + ("（休息中）" if c.get("resting") else "")), ("金錢", money_text(c.get("money"))))):
            ttk.Label(facts, text=key, style="CardKey.TLabel").grid(row=row, column=0, sticky="w", padx=(0, px(12)), pady=px(1))
            ttk.Label(facts, text=value, style="Card.TLabel", wraplength=px(240), justify="left").grid(row=row, column=1, sticky="w", pady=px(1))
        xp, top_xp = c.get("xp"), c.get("xp_max")
        if isinstance(xp, (int, float)) and isinstance(top_xp, (int, float)) and top_xp > 0:
            bar_xp = ttk.Progressbar(body, style="XP.Horizontal.TProgressbar", maximum=top_xp, value=min(xp, top_xp))
            bar_xp.pack(fill="x", pady=(px(10), px(3)))
            rested = c.get("rested_xp")
            ttk.Label(body, text=f"經驗 {int(xp):,} / {int(top_xp):,}（{xp * 100 / top_xp:.0f}%）"
                      + (f"　休息加成 +{int(rested):,}" if isinstance(rested, (int, float)) and rested > 0 else ""),
                      style="CardMuted.TLabel").pack(anchor="w")
        source_row(body, e)
        return outer

    held: Dict[str, List[Dict[str, Any]]] = {"local": [], "cloud": []}      # the latest of each, to draw again
    chosen: Dict[str, str] = {}                                           # character id -> the copy picked by hand

    def draw_cards(local: Optional[List[Dict[str, Any]]] = None, cloud: Optional[List[Dict[str, Any]]] = None) -> None:
        if local is not None:
            held["local"] = local
        if cloud is not None:
            held["cloud"] = cloud
        entries = merge_cards(held["local"], held["cloud"], chosen)
        for child in grid.winfo_children():
            child.destroy()
        if not entries:
            empty = card(grid, "還沒有找到角色")
            ttk.Label(empty, text="1. 到「設定」分頁按「安裝插件到遊戲」\n2. 進遊戲，按畫面上的「存檔」按鈕（或輸入 /reload）\n3. 回來按右上的「重新整理」",
                      style="Card.TLabel", justify="left").pack(anchor="w")
            empty.grid(row=0, column=0, columnspan=2, sticky="ew")
            summary.configure(text="這台電腦還沒有角色資料")
            return
        newest = max((e["card"].get("captured_at") or 0 for e in entries), default=0)
        fresher = sum(1 for e in entries if e["local"] and e["cloud"]
                      and (e["cloud"]["captured_at"] or 0) > (e["local"]["captured_at"] or 0) + SAME_WITHIN)
        summary.configure(text=f"{len(entries)} 個角色（這台電腦 {len(held['local'])} 個）"
                          + (f"　·　最近一次存檔：{ago_text(newest, '')}" if newest else "")
                          + (f"　·　☁ 雲端有 {fresher} 個較新" if fresher else ""))
        for i, e in enumerate(entries):
            character_card(grid, e).grid(row=i // 2, column=i % 2, sticky="nsew", padx=(0 if i % 2 == 0 else px(6), px(6) if i % 2 == 0 else 0),
                                         pady=(0, px(12)))

    # ---- 同步: where things stand, and what is sent
    sync_actions = ttk.Frame(tab_sync)
    sync_actions.pack(fill="x", pady=(0, px(12)))
    status = card(tab_sync, "同步狀態")
    status.pack(fill="x")
    rows = ttk.Frame(status, style="Inner.TFrame")
    rows.pack(fill="x")
    rows.columnconfigure((1, 3), weight=1, uniform="values")
    shown = {}
    # two to a line, so the tab fits the window; the long ones get a line of their own
    for row, pair in enumerate(((("connected", "伺服器"), ("auto", "自動同步")), (("synced", "上次同步"), ("addon", "遊戲插件")),
                                (("pending", "等待上傳"), ("uploaded", "已上傳")), (("rejected", "被拒絕"), ("waiting", "傳不上去的角色")),
                                (("combat", "戰鬥紀錄"),), (("files", "遊戲存檔"),))):
        for i, (key, text) in enumerate(pair):
            ttk.Label(rows, text=text, style="CardKey.TLabel").grid(row=row, column=i * 2, sticky="nw", padx=(0 if i == 0 else px(18), px(14)), pady=px(3))
            shown[key] = ttk.Label(rows, text="—", style="Card.TLabel", wraplength=px(560 if len(pair) == 1 else 220), justify="left")
            shown[key].grid(row=row, column=i * 2 + 1, columnspan=3 if len(pair) == 1 else 1, sticky="w", pady=px(3))
    shown["auto"].configure(text="未啟動")

    sharing = card(tab_sync, "分享")
    sharing.pack(fill="x", pady=(px(12), 0))
    # Kept on the server, one choice per token: every character and computer using the same
    # token follows it. Off until the player turns it on; shown as the server holds it.
    shots_var = tk.BooleanVar(value=False)

    def shots_changed() -> None:
        now = save(quiet=True)
        if now is None:
            shots_var.set(not shots_var.get())
            return
        note("處理中…")
        worker.ask("settings", now)                     # the worker uses these settings from now on
        worker.ask("shots", shots_var.get())

    ttk.Checkbutton(sharing, text="上傳遊戲截圖", variable=shots_var, command=shots_changed).pack(anchor="w")
    ttk.Label(sharing, text="讓 AI 語音看得到你的畫面。只上傳勾選之後截的圖，要開著自動同步；用同一個權杖的角色和電腦都跟著這個設定。",
              style="CardMuted.TLabel", wraplength=px(640), justify="left").pack(anchor="w", padx=(px(25), 0), pady=(px(2), 0))
    # how many are on the server, and how many it keeps: also one choice per token, kept on the server
    keep_row = ttk.Frame(sharing, style="Inner.TFrame")
    keep_row.pack(anchor="w", padx=(px(25), 0), pady=(px(10), 0))
    stored_label = ttk.Label(keep_row, text="伺服器上的截圖：—", style="Card.TLabel")
    stored_label.pack(side="left", padx=(0, px(18)))
    ttk.Label(keep_row, text="最多保留", style="Card.TLabel").pack(side="left")
    keep_var = tk.StringVar(value="50")
    ttk.Spinbox(keep_row, from_=1, to=500, increment=10, width=5, textvariable=keep_var).pack(side="left", padx=px(6))
    ttk.Label(keep_row, text="張", style="Card.TLabel").pack(side="left", padx=(0, px(8)))

    def keep_changed() -> None:
        try:
            n = int(keep_var.get())
        except ValueError:
            n = 0
        if not 1 <= n <= 500:
            note("保留張數請填 1 到 500。", False)
            return
        now = save(quiet=True)
        if now is None:
            return
        note("處理中…")
        worker.ask("settings", now)
        worker.ask("keep", n)

    ttk.Button(keep_row, text="套用", command=keep_changed).pack(side="left")
    ttk.Label(sharing, text="超過時，伺服器會刪掉最舊的截圖（你這個權杖所有角色合計）。", style="CardMuted.TLabel").pack(
        anchor="w", padx=(px(25), 0), pady=(px(4), 0))

    # ---- 設定: the server, the token, the game
    fields = {"url": tk.StringVar(value=cfg.url), "token": tk.StringVar(value=cfg.api_token),
              "wow": tk.StringVar(value=cfg.wow_path), "device": tk.StringVar(value=cfg.device_id)}
    connection = card(tab_setup, "連線")
    connection.pack(fill="x")
    form = ttk.Frame(connection, style="Inner.TFrame")
    form.pack(fill="x")
    form.columnconfigure(1, weight=1)
    labels = (("url", "伺服器網址"), ("token", "權杖（這台電腦的角色都用它）"), ("wow", "遊戲資料夾（留空自動尋找）"),
              ("device", "這台電腦的名稱"))
    entries: Dict[str, Any] = {}
    for row, (key, text) in enumerate(labels):
        ttk.Label(form, text=text, style="CardKey.TLabel").grid(row=row, column=0, sticky="w", pady=px(4), padx=(0, px(12)))
        entries[key] = ttk.Entry(form, textvariable=fields[key], show="•" if key == "token" else "")
        entries[key].grid(row=row, column=1, sticky="ew", pady=px(4))

    def reveal() -> None:
        hidden = entries["token"].cget("show") != ""
        entries["token"].configure(show="" if hidden else "•")
        show_button.configure(text="隱藏" if hidden else "顯示")

    def browse() -> None:
        chosen = filedialog.askdirectory(title="選擇裡面有 _forever_ 的遊戲資料夾")
        if chosen:
            fields["wow"].set(chosen)

    show_button = ttk.Button(form, text="顯示", width=6, command=reveal)
    show_button.grid(row=1, column=2, padx=(px(8), 0))
    ttk.Button(form, text="選擇…", width=6, command=browse).grid(row=2, column=2, padx=(px(8), 0))
    setup_actions = ttk.Frame(connection, style="Inner.TFrame")
    setup_actions.pack(fill="x", pady=(px(12), 0))

    # one row per character found in the game's saved data, each with a token of its own
    people_card = card(tab_setup, "個別角色的權杖")
    people_card.pack(fill="x", pady=(px(12), 0))
    ttk.Label(people_card, text="通常不用填；只有某個角色要用另一個人的權杖時才填。", style="CardMuted.TLabel").pack(anchor="w", pady=(0, px(6)))
    people = ttk.Frame(people_card, style="Inner.TFrame")
    people.pack(fill="x")
    people.columnconfigure(1, weight=1)
    own: Dict[str, Any] = {cid: tk.StringVar(value=token) for cid, token in cfg.tokens.items()}

    def list_people(found: List[Dict[str, Any]]) -> None:
        for child in people.winfo_children():
            child.destroy()
        known = {c["id"] for c in found}
        rows = found + [{"id": cid, "name": cid} for cid in own if cid not in known and own[cid].get().strip()]
        if not rows:
            ttk.Label(people, text="還沒有找到角色：安裝插件、進遊戲按一次「存檔」之後，到「角色」分頁按「重新整理」。",
                      style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        for row, c in enumerate(rows):
            own.setdefault(c["id"], tk.StringVar())
            about = " ".join(str(x) for x in (c.get("level") and f"{c['level']} 級", CLASSES.get(c.get("class"), c.get("class"))) if x)
            ttk.Label(people, text=f"{c['name']}" + (f"（{about}）" if about else ""), style="Card.TLabel").grid(
                row=row, column=0, sticky="w", padx=(0, px(12)), pady=px(2))
            ttk.Entry(people, textvariable=own[c["id"]], show="•").grid(row=row, column=1, sticky="ew", pady=px(2))

    def current() -> Config:
        return Config(tokens={cid: v.get().strip() for cid, v in own.items() if v.get().strip()},
                      url=fields["url"].get().strip().rstrip("/"), api_token=fields["token"].get().strip(),
                      timeout=cfg.timeout, device_id=fields["device"].get().strip() or "pc", wow_path=fields["wow"].get().strip(),
                      sync_interval=cfg.sync_interval, batch_size=cfg.batch_size, db_path=cfg.db_path, log_level=cfg.log_level)

    def note(text: str, ok: Optional[bool] = None) -> None:
        message.configure(text=text, foreground=OK if ok else BAD if ok is False else MUTED)

    def save(quiet: bool = False) -> Optional[Config]:
        now = current()
        if not now.url.startswith(("http://", "https://")):
            note("伺服器網址要以 http:// 或 https:// 開頭。", False)
            return None
        if not now.api_token and not now.tokens:
            note("請填入權杖。", False)
            return None
        save_config(path, now)
        if not quiet:
            note(f"設定已儲存到 {path}", True)
        return now

    def send(what: str) -> None:
        now = save(quiet=True)
        if now is not None:
            note("處理中…")
            worker.ask(what, now)

    def addon() -> None:
        try:
            note("插件已安裝到：" + "、".join(str(p) for p in install_addon(fields["wow"].get().strip())), True)
            worker.ask("update", current())
        except (OSError, RuntimeError) as e:
            note(f"插件安裝失敗：{e}", False)

    ttk.Button(setup_actions, text="儲存設定", style="Accent.TButton", command=save).pack(side="left", padx=(0, px(8)))
    ttk.Button(setup_actions, text="測試連線", command=lambda: send("test")).pack(side="left", padx=(0, px(8)))
    ttk.Button(setup_actions, text="安裝插件到遊戲", command=addon).pack(side="left")
    ttk.Button(sync_actions, text="立即同步", style="Accent.TButton", command=lambda: send("sync")).pack(side="left", padx=(0, px(8)))
    ttk.Button(sync_actions, text="測試連線", command=lambda: send("test")).pack(side="left")

    # ---- 紀錄
    log_card = card(tab_log, padding=4)
    log_card.pack(fill="both", expand=True)
    box = tk.Text(log_card, height=8, wrap="word", state="disabled", bg=CARD, fg=INK, relief="flat", bd=0,
                  font=("Consolas" if sys.platform.startswith("win") else "Menlo", 9), padx=px(8), pady=px(6),
                  highlightthickness=0)
    log_scroll = ttk.Scrollbar(log_card, orient="vertical", command=box.yview)
    box.configure(yscrollcommand=log_scroll.set)
    log_scroll.pack(side="right", fill="y")
    box.pack(side="left", fill="both", expand=True)

    def write(line: str) -> None:
        box.configure(state="normal")
        box.insert("end", time.strftime("%H:%M:%S ") + line + "\n")
        box.delete("1.0", "end-300l")
        box.see("end")
        box.configure(state="disabled")

    def poll() -> None:
        try:
            while True:
                r = worker.reports.get_nowait()
                if "log" in r:
                    write(r["log"])
                if "message" in r:
                    note(r["message"], r.get("ok"))
                    write(r["message"])
                if "characters" in r:
                    list_people(r["characters"])
                    draw_cards(local=r["characters"])
                if "cloud" in r:
                    draw_cards(cloud=r["cloud"])
                if "update" in r:
                    offer(r["update"])
                if "addon" in r:
                    offer_addon(r["addon"])
                if r.get("addon_failed"):
                    addon_button.configure(text="再試一次", state="normal")
                if "addons" in r:
                    shown["addon"].configure(text="、".join(sorted({a["version"] or "版本不明" for a in r["addons"]}))
                                             if r["addons"] else "還沒安裝（到「設定」分頁按「安裝插件到遊戲」）")
                if "fetching" in r:
                    got, size = r["fetching"]
                    bar_button.configure(text=f"下載中… {got * 100 // size}%" if size else f"下載中… {got / 1048576:.0f} MB")
                if r.get("fetch_failed"):
                    bar_button.configure(text="再試一次", state="normal")
                if "fetched" in r:
                    opened(r["fetched"])
                if "waiting" in r:
                    shown["waiting"].configure(text=(r["waiting"] + "：沒有可用的權杖，資料先留在這台電腦") if r["waiting"] else "沒有")
                if "shots" in r:
                    shots_var.set(bool(r["shots"]))
                if r.get("shots_info"):
                    info = r["shots_info"]
                    if "screenshots_stored" in info:            # a server from before these two existed says neither
                        stored_label.configure(text=f"伺服器上的截圖：{info['screenshots_stored']} 張")
                    try:
                        typing = root.focus_get() is not None and root.focus_get().winfo_class() == "TSpinbox"
                    except (KeyError, tk.TclError):
                        typing = False
                    if "keep_screenshots" in info and not typing:      # never while the player is typing a number
                        keep_var.set(str(info["keep_screenshots"]))
                if "connected" in r:
                    shown["connected"].configure(text="連線正常" if r["connected"] else "連不上或權杖不對",
                                                 foreground=OK if r["connected"] else BAD)
                if "synced_at" in r or "checked_at" in r:
                    shown["synced"].configure(text=time.strftime("%H:%M:%S", time.localtime(r.get("synced_at") or r["checked_at"]))
                                              + ("" if "synced_at" in r else "（沒有新資料）"))
                if "counts" in r:
                    c = r["counts"]
                    shown["pending"].configure(text=f"{c.get('pending', 0)} 筆事件、{c.get('pending_snapshots', 0)} 份快照")
                    shown["uploaded"].configure(text=f"{c.get('uploaded', 0)} 筆")
                    shown["rejected"].configure(text=f"{c.get('rejected', 0)} 筆")
                    shown["files"].configure(text="\n".join(r["files"]) if r["files"] else "找不到：請先安裝插件，進遊戲後按一次「存檔」")
                    combat = r.get("combat")
                    if combat is not None:
                        show_combat(combat)
                    show_auto(bool(r["auto"]))
        except queues.Empty:
            pass
        root.after(300, poll)

    def close() -> None:
        worker.ask("quit")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    worker.start()
    list_people([])
    worker.ask("chars", cfg)
    worker.ask("update", cfg)
    if cfg.url and (cfg.api_token or cfg.tokens):       # settings from last time: show where things stand
        worker.ask("test", cfg)
    else:
        tabs.select(tab_setup)                          # nothing set up yet: start where that is done
    root.after(300, poll)
    hook = getattr(main, "_started", None)              # lets a test drive the window
    if hook:
        hook(root, fields, worker, {"save": save, "send": send, "shown": shown, "message": message, "own": own,
                                    "people": people, "bar": bar, "bar_text": bar_text, "bar_button": bar_button,
                                    "addon_bar": addon_bar, "addon_text": addon_text, "addon_button": addon_button,
                                    "tabs": tabs, "cards": grid})
    root.mainloop()
    return 0
