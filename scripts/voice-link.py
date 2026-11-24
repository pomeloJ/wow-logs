#!/usr/bin/env python3
"""Create the short voice-assistant token for each character and write a note to paste
into ChatGPT (voice-chatgpt.md, not committed because it contains the token).

Usage (from the repo root):
    .venv/bin/python scripts/voice-link.py                          # LAN address from config
    .venv/bin/python scripts/voice-link.py --base https://my.host   # public address to put in the note
    .venv/bin/python scripts/voice-link.py --rotate                 # replace the tokens (old links stop working)
"""
import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
from adventuresync.core import load_config  # noqa: E402

NOTE = """# {name} 的《魔獸世界》冒險紀錄查詢

當我問到我的魔獸世界角色「{name}」的狀況時，請用 GET 開啟下面的網址取得資料，再用繁體中文口語回答我。
回傳內容是純文字。資料只在我遊戲存檔後才會更新，回答時請順便告訴我資料的更新時間。

基本網址（角色概況加今天的活動統計）：
{url}

可以在網址後面加上這些參數來篩選：

| 參數 | 用途 | 可用的值 |
| --- | --- | --- |
| show | 要看的項目，可用逗號合併多項 | overview 角色概況、activity 活動統計、quests 任務日誌、todo 目前所在區域還沒做的任務、bags 背包、gear 裝備與屬性、compare 背包裡的裝備和身上的比較、skills 專業技能、rep 聲望、stats 屬性、flights 飛行點、kills 擊殺排行、loot 拾取排行、events 事件紀錄、where 目前位置、all 全部、help 說明 |
| range | 時間範圍，影響 activity、kills、loot、events | today（預設）、yesterday、week、all，或 3h 表示最近三小時 |
| q | 名稱關鍵字，篩選任務、背包、聲望、擊殺、拾取、事件 | 任意文字 |
| type | 只用於 events，指定事件類型 | MOB_KILLED、ITEM_LOOTED、LEVEL_UP、QUEST_ACCEPTED、QUEST_COMPLETED、PLAYER_DIED、ZONE_CHANGED、SKILL_UP、MONEY_CHANGED |
| limit | 清單筆數上限 | 1 到 100，預設 20 |

範例：
- 我的任務有哪些：{url}&show=quests
- 我身上穿什麼、背包裡哪件比較好：{url}&show=gear,compare
- 這一區還有什麼任務沒做：{url}&show=todo
- 背包裡有沒有某樣東西：{url}&show=bags&q=亞麻
- 今天打了什麼怪、撿到什麼：{url}&show=kills,loot
- 這星期的進度：{url}&show=activity&range=week
- 最近一小時發生的事：{url}&show=events&range=1h
- 我現在在哪裡：{url}&show=where
- 全部資料：{url}&show=all

請不要更改網址裡的 t 參數，那是這個角色的讀取代碼。
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="address to put in the note (default: server url from agent/config.toml)")
    ap.add_argument("--rotate", action="store_true")
    args = ap.parse_args()
    cfg = load_config(ROOT / "agent" / "config.toml")
    api = cfg.url.rstrip("/")
    base = (args.base or api).rstrip("/")
    web = httpx.Client(timeout=cfg.timeout, headers={"Authorization": f"Bearer {cfg.api_token}"})
    chars = web.get(f"{api}/api/v1/characters").raise_for_status().json()["items"]
    if not chars:
        print("no characters on the server yet")
        return 1
    notes, links = [], []
    for c in chars:
        r = web.post(f"{api}/api/v1/characters/{c['id']}/voice-token",
                     params={"rotate": 1} if args.rotate else None).raise_for_status().json()
        url = f"{base}/voice?t={r['token']}"
        check = httpx.get(f"{api}/voice", params={"t": r["token"]}, timeout=cfg.timeout)
        print(f"{c.get('name') or c['id']}: token {'created' if r['created'] else 'unchanged'}, "
              f"query check HTTP {check.status_code}")
        notes.append(NOTE.format(name=c.get("name") or c["id"], url=url))
        # The dashboard reads the token from the URL fragment, which is never sent to the server.
        links.append(f"{c.get('name') or c['id']}: {base}/#token={r['token']}")
    out = ROOT / "voice-chatgpt.md"
    out.touch(mode=0o600, exist_ok=True)
    out.write_text("\n---\n\n".join(notes), encoding="utf-8")
    share = ROOT / "share-link.txt"
    share.touch(mode=0o600, exist_ok=True)
    share.write_text("\n".join(links) + "\n", encoding="utf-8")
    print(f"note written to {out.name}, dashboard share link to {share.name} (both contain the token)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
