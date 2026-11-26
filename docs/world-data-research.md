# 世界資料、地圖與導航：現況與可行性（2026-11-26）

針對「AI 遊戲助手功能擴充規格」第三項：哪些資料已經有、哪些能取得、導航能做到什麼程度。

## 一、系統裡已經有的

| 資料 | 來源 | 規模 | 用在 |
| --- | --- | --- | --- |
| 任務、NPC、物件、物品、掉落、商人、配方 | NorgannonDB 1.7.0（Forever 版，build 1.60.1；整合 QuestieDB、pfQuest、cMaNGOS classic-db），GPL-3.0 | 任務 6,163、NPC 15,830、物件 10,784、物品 26,575、生成點 197,988 | `/world/*`、任務標記、語音 |
| 任務起點／交付／目標座標 | 同上，座標是「UiMapID + 地圖百分比」 | 每個 NPC／物件的所有生成點 | `/world/quest-pins`、`/world/route` |
| 區域地圖底圖 | Warcraft Wiki 的 coordinate-safe 地圖（Blizzard 版權，僅個人非商業使用） | 51 張（1411–1461：兩塊大陸與舊區域） | Dashboard、`/world/map`、導航圖 |
| 地圖的世界座標（碼） | **新增**：wago.tools 匯出的客戶端資料表 UiMap + UiMapAssignment（`wowf` 1.60.1.71139） | 60 張地圖，59 張有範圍，包含海加爾山 2482、Riverglades 2548、Shen'dralas 2652、Zephras Isle 2521 | 距離、跨區域換算、拼接地圖 |
| 同上（備援） | **新增**：插件 0.3.0 用 `C_Map.GetWorldPosFromMapPos` 回報（MAP_RECT 事件） | 玩家到過的每張地圖 | 資料表缺的地圖 |
| 玩家走過的路 | 插件的位置紀錄（LOCATION_SAMPLE，約 15 秒或移動 0.5% 一筆） | 目前 2,969 筆，以黑海岸、貧瘠之地、梣谷最多 | 導航時優先走「有人走過」的路 |

## 二、公開來源研究結果

| 來源 | 能拿到什麼 | 版本相容 | 授權／限制 |
| --- | --- | --- | --- |
| **wago.tools DB2 匯出** | UiMap、UiMapAssignment（地圖↔世界座標）、Map、TaxiNodes、TaxiPath 等客戶端資料表；`/db2/<表>/csv?build=` 免帳號 | **有 Forever（產品 `wowf`）**，跟著修正檔更新 | 沒找到使用條款；內容是從 Blizzard 客戶端取出的遊戲資料，只放在自己的伺服器、不公開散布 |
| HereBeDragons | 座標換算的程式邏輯；區域範圍是在遊戲內即時向客戶端查詢，本身只帶少量大陸資料 | TOC 已列 16001（Forever） | BSD（只見於 TOC 標示） |
| QuestieDB | 有 `data/Forever/`；`conversion.json` 帶 1411–1461 的世界範圍，以及 4 個在 Forever 改過框線的區域（莫高雷、東瘟疫之地、赤脊山、暴風城）的舊→新座標換算 | Forever | GPL-3.0，舊內容的授權說明有但書 |
| Blizzard Game Data API（Classic） | 物品、生物、媒體等；**沒有地圖或座標** | — | — |
| TrinityCore / cMaNGOS / vMaNGOS | maps（地形高度）、vmaps（建築碰撞）、mmaps（Recast 導航網格）的擷取工具 | cMaNGOS／vMaNGOS 只支援 1.12 MPQ 客戶端；TrinityCore 沒有 Classic Era 分支。**沒有任何現成工具支援 Forever 的 CASC 客戶端** | GPL-2.0 |
| Recast & Detour | 導航網格生成與尋路 | 與遊戲版本無關，但需要上面的地形資料 | zlib |
| namigator | 以 Recast 為基礎，附 Python 綁定 | 只讀 MPQ（到 WotLK），2024 年後未更新 | MIT |
| WoW Completionist 資料集 | 沒有找到公開資料 | — | — |

## 三、可行性結論

1. **地圖底圖與座標換算：可行，已完成。** UiMapAssignment 是遊戲本身的資料，誤差只來自客戶端。已驗證：金色鎮的世界座標換算到艾爾文森林地圖是 (42.4, 65.9)，和遊戲內位置一致。
2. **任務、NPC 位置：已經有。** NorgannonDB 是 Forever 版，Questie 提到的 4 個改框線區域需要找幾個已知座標抽查確認。
3. **真正的地形導航（Maps／VMaps／MMaps）：短期不可行。** 這個版本沒有可用的擷取工具，新區域也不在任何 1.12 資料裡。要做得自己移植 CASC 擷取器（TrinityCore master 的程式碼加上 CASC 讀取），屬於大型專案。估計完整資料約 1–3 GB（3.3.5a 約 2.8 GB 可供參考）。
4. **目前的折衷：用玩家軌跡加權的格子尋路（已實作）。** 玩家走過的格子成本低，其他地方可以通過但成本高。路線會盡量走真實走過的路，每段都標示 `walked` 是否成立，`verified_passable` 一律為 false。資料越多、走過的地方越多，路線越可靠。

## 四、建議下一步（依投入程度）

1. 伺服器匯入 UiMapAssignment（每次遊戲修正檔後重跑一次，指令見 `world_importer/uimaps.py`）。
2. 抽查那 4 個改框線區域的任務座標。
3. 飛行點：TaxiNodes／TaxiPath 也能從 wago.tools 取得，加上角色已開的飛行點（快照裡有），就能建議「先飛到哪裡再走」。
4. 人工路點：橋、山口、洞穴入口等，做成一張小表，可在 Dashboard 修正。
5. 長期：評估移植 CASC 地形擷取器、產生 Forever 的 mmaps，再換成 Detour 尋路。

## 五、新增的 API

- `GET /api/v1/world/map?map_id=` 或 `?q=名稱`：地圖的 UiMapID、中英文名稱、上層與大陸、底圖網址與像素大小、百分比↔像素↔碼的換算規則、資料來源。
- `GET /api/v1/world/route`：起點可以是 `character_id`（最後存檔的位置），也可以是 `from_map`、`from_x`、`from_y`（從 AI 截圖面板讀到的即時座標）。終點是 `to_map`／`to_x`／`to_y`、`npc_id`、`object_id` 或 `quest_id`（`role` 填 auto 時會自動判斷要去接任務、做目標還是交付）。回傳依行走順序排列的路點（各自標所在地圖）、距離（碼）、步行與騎乘時間、走過比例與可信程度、其他候選位置和警告。
- `GET /api/v1/world/route/image`：把同一條路線畫在拼接好的區域地圖上（JPEG）。藍色是起點（可帶 `facing` 畫出面向），紅色是目的地，綠色實線是有人走過的路段，橘色虛線是推測路段。
