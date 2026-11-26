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


def characters(wow_path: str) -> List[Dict[str, Any]]:
    """The characters the addon has recorded on this computer: id, name, class and level."""
    found: Dict[str, Dict[str, Any]] = {}
    for path in find_saved_variables(wow_path):
        try:
            saved = (read_stable(path, settle=0.1).get(DB_GLOBAL) or {}).get("characters") or {}
        except Exception:                             # a file being written, or not ours: skip it
            continue
        for cid, snap in saved.items() if isinstance(saved, dict) else []:
            data = (snap or {}).get("data") if isinstance(snap, dict) else None
            data = data if isinstance(data, dict) else {}
            found[str(cid)] = {"id": str(cid), "name": data.get("name") or str(cid), "class": data.get("class"),
                               "level": data.get("level")}
    return sorted(found.values(), key=lambda c: str(c["name"]))


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

    def ask(self, what: str, cfg: Optional[Config] = None) -> None:
        self.requests.put((what, cfg))

    def _say(self, **report: Any) -> None:
        self.reports.put(report)

    def _status(self, store: Queue, cfg: Config) -> None:
        files = find_saved_variables(cfg.wow_path)
        # The newest save decides: that is the game folder being played in.
        combat = None
        try:
            newest = max(files, key=lambda f: f.stat().st_mtime, default=None)
            if newest is not None:
                combat = extras.combat_log_state(newest.parents[4], newest.stat().st_mtime)
        except OSError:
            pass
        self._say(counts=store.counts(), files=[str(f) for f in files], auto=self.auto, combat=combat)

    def run(self) -> None:
        store: Optional[Queue] = None
        up: Optional[Uploader] = None
        seen: Dict[Path, Any] = {}
        next_scan = next_sync = next_check = next_shots = next_settings = 0.0
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
                    self._say(characters=characters(cfg.wow_path if cfg else self._cfg.wow_path if self._cfg else ""))
                    continue
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
                        self._say(message="截圖上傳設定沒有改成：" + "、".join(dict.fromkeys(problems)), ok=False,
                                  shots=up.screenshots_on())
                    else:
                        self._say(message=("已開啟截圖上傳：之後在遊戲裡截的圖會在幾秒內上傳"
                                           + ("。" if self.auto else "（要先按「開始自動同步」）。")) if cfg
                                  else "已關閉截圖上傳。", ok=True, shots=bool(cfg))
                    next_settings = time.monotonic() + up.SETTINGS_EVERY
                    continue
                if what == "tick" and time.monotonic() >= next_settings:
                    next_settings = time.monotonic() + up.SETTINGS_EVERY
                    self._say(shots=up.screenshots_on())        # changed on another computer: shown here too
                if self.auto and time.monotonic() >= next_shots:
                    next_shots = time.monotonic() + 1           # a new screenshot goes up within seconds
                    scan_screenshots(store, up)
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


def main(config: Optional[str] = None) -> int:
    import tkinter as tk
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
        except Exception:
            pass
    root = tk.Tk()
    root.title(f"AdventureSync {__version__}")
    # Text follows the display's scaling by itself; lengths given in pixels have to be scaled here.
    zoom = max(root.winfo_fpixels("1i") / 96, 1) if sys.platform.startswith("win") else 1
    px = lambda n: round(n * zoom)                                             # noqa: E731
    root.minsize(px(620), px(640))
    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    # shown above everything else, and only while the server offers a newer version
    bar = ttk.Frame(root, padding=(14, 12, 14, 0))
    bar_text = ttk.Label(bar, foreground="#b26a00")
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
        bar.pack(fill="x", before=frame)

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
    bar_button.pack(side="left", padx=(10, 0))

    # the same for the addon in the game, which can be brought up to date without a new program
    addon_bar = ttk.Frame(root, padding=(14, 10, 14, 0))
    addon_text = ttk.Label(addon_bar, foreground="#b26a00")
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
        addon_bar.pack(fill="x", before=frame)

    def get_addon() -> None:
        if addon_newer:
            addon_button.configure(text="更新中…", state="disabled")
            worker.ask("addon", dict(addon_newer))

    addon_button = ttk.Button(addon_bar, command=get_addon)
    addon_button.pack(side="left", padx=(10, 0))

    fields = {"url": tk.StringVar(value=cfg.url), "token": tk.StringVar(value=cfg.api_token),
              "wow": tk.StringVar(value=cfg.wow_path), "device": tk.StringVar(value=cfg.device_id)}
    labels = (("url", "伺服器網址"), ("token", "權杖（這台電腦的角色都用它）"), ("wow", "遊戲資料夾（留空自動尋找）"),
              ("device", "這台電腦的名稱"))
    entries: Dict[str, Any] = {}
    for row, (key, text) in enumerate(labels):
        ttk.Label(frame, text=text).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        entries[key] = ttk.Entry(frame, textvariable=fields[key], show="•" if key == "token" else "")
        entries[key].grid(row=row, column=1, sticky="ew", pady=4)

    def reveal() -> None:
        hidden = entries["token"].cget("show") != ""
        entries["token"].configure(show="" if hidden else "•")
        show_button.configure(text="隱藏" if hidden else "顯示")

    def browse() -> None:
        chosen = filedialog.askdirectory(title="選擇裡面有 _forever_ 的遊戲資料夾")
        if chosen:
            fields["wow"].set(chosen)

    show_button = ttk.Button(frame, text="顯示", width=6, command=reveal)
    show_button.grid(row=1, column=2, padx=(6, 0))
    ttk.Button(frame, text="選擇…", width=6, command=browse).grid(row=2, column=2, padx=(6, 0))

    # one row per character found in the game's saved data, each with a token of its own
    people = ttk.LabelFrame(frame, text="這台電腦的角色（通常不用填；只有某個角色要用另一個人的權杖時才填）", padding=8)
    people.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
    people.columnconfigure(1, weight=1)
    own: Dict[str, Any] = {cid: tk.StringVar(value=token) for cid, token in cfg.tokens.items()}

    def list_people(found: List[Dict[str, Any]]) -> None:
        for child in people.winfo_children():
            child.destroy()
        known = {c["id"] for c in found}
        rows = found + [{"id": cid, "name": cid} for cid in own if cid not in known and own[cid].get().strip()]
        if not rows:
            ttk.Label(people, text="還沒有找到角色：安裝插件、進遊戲按一次「存檔」之後，按「重新整理角色」。").grid(row=0, column=0, sticky="w")
        for row, c in enumerate(rows):
            own.setdefault(c["id"], tk.StringVar())
            about = " ".join(str(x) for x in (c.get("level") and f"{c['level']} 級", CLASSES.get(c.get("class"), c.get("class"))) if x)
            ttk.Label(people, text=f"{c['name']}" + (f"（{about}）" if about else "")).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            ttk.Entry(people, textvariable=own[c["id"]], show="•").grid(row=row, column=1, sticky="ew", pady=2)

    def current() -> Config:
        return Config(tokens={cid: v.get().strip() for cid, v in own.items() if v.get().strip()},
                      url=fields["url"].get().strip().rstrip("/"), api_token=fields["token"].get().strip(),
                      timeout=cfg.timeout, device_id=fields["device"].get().strip() or "pc", wow_path=fields["wow"].get().strip(),
                      sync_interval=cfg.sync_interval, batch_size=cfg.batch_size, db_path=cfg.db_path, log_level=cfg.log_level)

    def note(text: str, ok: Optional[bool] = None) -> None:
        message.configure(text=text, foreground="#2e7d32" if ok else "#c62828" if ok is False else "")

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

    def toggle() -> None:
        send("stop" if worker.auto else "start")

    buttons = ttk.Frame(frame)
    buttons.grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 4))

    def refresh() -> None:
        worker.ask("chars", current())

    for text, command in (("儲存設定", save), ("測試連線", lambda: send("test")), ("安裝插件到遊戲", addon), ("重新整理角色", refresh),
                          ("立即同步", lambda: send("sync"))):
        ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 8))
    auto_button = ttk.Button(buttons, text="開始自動同步", command=toggle)
    auto_button.pack(side="left")

    message = ttk.Label(frame, text=f"設定檔：{path}", wraplength=px(520), justify="left")
    message.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 10))

    status = ttk.LabelFrame(frame, text="同步狀態", padding=10)
    status.grid(row=7, column=0, columnspan=3, sticky="ew")
    status.columnconfigure(1, weight=1)
    shown = {}
    for row, (key, text) in enumerate((("connected", "伺服器"), ("auto", "自動同步"), ("addon", "遊戲插件"), ("files", "遊戲存檔"), ("combat", "戰鬥紀錄"), ("pending", "等待上傳"),
                                       ("uploaded", "已上傳"), ("rejected", "被拒絕"), ("waiting", "傳不上去的角色"), ("synced", "上次同步"))):
        ttk.Label(status, text=text).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=1)
        shown[key] = ttk.Label(status, text="—", wraplength=px(380), justify="left")
        shown[key].grid(row=row, column=1, sticky="w", pady=1)
    shown["auto"].configure(text="未啟動")

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

    ttk.Checkbutton(status, text="上傳遊戲截圖（讓 AI 語音看得到你的畫面；用同一個權杖的角色和電腦都跟著這個設定）",
                    variable=shots_var, command=shots_changed).grid(row=9, column=0, columnspan=2, sticky="w", pady=(8, 0))

    ttk.Label(frame, text="紀錄").grid(row=8, column=0, sticky="w", pady=(12, 2))
    box = tk.Text(frame, height=8, wrap="word", state="disabled")
    box.grid(row=9, column=0, columnspan=3, sticky="nsew")
    frame.rowconfigure(9, weight=1)

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
                if "update" in r:
                    offer(r["update"])
                if "addon" in r:
                    offer_addon(r["addon"])
                if r.get("addon_failed"):
                    addon_button.configure(text="再試一次", state="normal")
                if "addons" in r:
                    shown["addon"].configure(text="、".join(sorted({a["version"] or "版本不明" for a in r["addons"]}))
                                             if r["addons"] else "還沒安裝（按「安裝插件到遊戲」）")
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
                if "connected" in r:
                    shown["connected"].configure(text="連線正常" if r["connected"] else "連不上或權杖不對")
                if "synced_at" in r or "checked_at" in r:
                    shown["synced"].configure(text=time.strftime("%H:%M:%S", time.localtime(r.get("synced_at") or r["checked_at"]))
                                              + ("" if "synced_at" in r else "（沒有新資料）"))
                if "counts" in r:
                    c = r["counts"]
                    shown["pending"].configure(text=f"{c.get('pending', 0)} 筆事件、{c.get('pending_snapshots', 0)} 份快照")
                    shown["uploaded"].configure(text=f"{c.get('uploaded', 0)} 筆")
                    shown["rejected"].configure(text=f"{c.get('rejected', 0)} 筆")
                    shown["files"].configure(text="\n".join(r["files"]) if r["files"] else "找不到：請先安裝插件，進遊戲後按一次「存檔」")
                    shown["auto"].configure(text="進行中" if r["auto"] else "未啟動")
                    combat = r.get("combat")
                    if combat is not None:
                        when = time.strftime("%m/%d %H:%M", time.localtime(combat["last"])) if combat["last"] else ""
                        shown["combat"].configure(
                            foreground="#c0392b" if combat["missed"] else "",
                            text=(f"最後寫入 {when}" if not combat["missed"] else
                                  (f"上次遊玩時沒有開（最後寫入 {when}）" if when else "沒有開過") + "：在遊戲輸入 /combatlog 才有傷害統計"))
                    auto_button.configure(text="停止自動同步" if r["auto"] else "開始自動同步")
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
    root.after(300, poll)
    hook = getattr(main, "_started", None)              # lets a test drive the window
    if hook:
        hook(root, fields, worker, {"save": save, "send": send, "shown": shown, "message": message, "own": own,
                                    "people": people, "bar": bar, "bar_text": bar_text, "bar_button": bar_button,
                                    "addon_bar": addon_bar, "addon_text": addon_text, "addon_button": addon_button})
    root.mainloop()
    return 0
