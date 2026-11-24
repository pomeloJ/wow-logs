# WoW Adventure Hub

《魔獸世界：永恆》（World of Warcraft: Forever）的個人冒險紀錄系統，全部跑在你自己的機器上：

```
遊戲內插件（Lua） → 本機同步程式（Python） → 你自己的伺服器（FastAPI + SQLite） → 網頁
```

- **插件**記錄角色狀態、位置、任務、擊殺、拾取、組隊。
- **同步程式**讀遊戲的存檔、截圖與戰鬥紀錄檔，上傳到伺服器。
- **伺服器**提供網頁總覽、可分享的組隊頁面（傷害統計、技能分析、目標研究）、REST API 與語音助理查詢。

沒有任何中央服務：資料只會送到你在設定檔裡填的那台伺服器。這個程式庫不含任何人的資料、權杖或伺服器位址。

## 自己架設

需要：遊戲本體（Forever 1.60.x）、Python 3.11 以上、一台能用 SSH 金鑰登入的 Linux 主機（或直接在本機跑）。
部署伺服器的腳本是給 macOS／Linux 用的；同步程式（用戶端）在 macOS 與 Windows 11 都測試過。

```bash
git clone <這個程式庫> && cd <資料夾>
python3 -m venv .venv
.venv/bin/pip install -r agent/requirements.txt -r server/requirements.txt

# 1. 設定：填入你的伺服器位址，並把 api_token 改成一段夠長的隨機文字
cp agent/config.example.toml agent/config.toml

# 2. 部署伺服器（systemd 服務；會把同一個權杖交給伺服器，並另外產生一個唯讀權杖）
./scripts/deploy-server.sh user@你的主機

# 3. 打開同步程式的視窗（Windows 與 macOS 相同）：填入網址與權杖、安裝插件、開始同步
cd agent && ../.venv/bin/python AdventureSync.pyw
```

**同步程式（AdventureSync）**是一個小視窗，Windows 和 macOS 用同一份：設定伺服器網址與權杖、一鍵安裝插件、
測試連線，並顯示等待上傳與已上傳的筆數。設定存在使用者資料夾的 `.adventuresync/config.toml`。
不想開視窗也可以用指令：`./scripts/run-agent-macos.sh run`（或 `python -m adventuresync run`）。

**插件單獨更新**：遊戲插件比同步程式更常改，所以可以單獨更新。同步程式會比對伺服器上的插件版本與遊戲裡安裝的版本
（插件 `.toc` 檔的 `## Version`），伺服器的比較新就在視窗上方提示，按「更新插件」會下載並換掉遊戲裡的插件，
Windows 與 macOS 相同，不需要換新的同步程式。更新後在遊戲裡輸入 `/reload` 生效。發布新版插件時記得把 `.toc` 與 `Core.lua` 裡的版本號一起調高。

**給別台電腦用**：伺服器的 `/setup` 頁面可以下載同步程式。原始碼版需要對方自己裝 Python 3.11 以上。
**對方沒有 Python 時**給單一檔案版：在那種系統上執行 `python scripts/build-client.py --upload`
（需要 `python -m pip install pyinstaller httpx pillow`），編好的檔案（Windows 是約 22 MB 的 `.exe`，裡面已包含
Python 與插件）會上傳並出現在 `/setup`，對方下載後點兩下就能用。不同系統要各自在該系統上編譯。
檔案沒有簽署，Windows 第一次開啟會顯示 SmartScreen 警告（「其他資訊 → 仍要執行」），少數防毒軟體也可能誤判。

**新版通知**：同步程式開啟時、之後每 6 小時，會問伺服器的 `/api/v1/setup`（不需要權杖）有沒有比自己新的版本，
有的話視窗最上面會出現「有新版本」和一個按鈕。Windows 的單一檔案版按下去會把新檔下載到舊檔旁邊
（檔名帶版本，例如 `AdventureSync-windows-x64-0.3.0.exe`）、開啟新版、關掉自己，舊檔可以刪；
macOS 與原始碼版則是用瀏覽器下載，自己換開新的。**發新版的步驟**：改 `agent/adventuresync/__init__.py` 的
`__version__`，再執行 `python scripts/build-client.py --upload`（版本會跟著檔案一起上傳）；原始碼版的版本
在重新部署伺服器後更新。直接用 scp 把檔案丟進 `data/builds/` 的話伺服器不知道它的版本，舊版就不會收到通知。

**Windows 上的指令**（PowerShell）：`py -3.11 -m venv .venv`、`.venv\Scripts\python -m pip install -r agent\requirements.txt`、
`cd agent; ..\.venv\Scripts\python -m adventuresync`（加上 `run`、`validate` 等就是不開視窗的用法）。
遊戲資料夾留空時，會依序找登錄檔記載的安裝位置與每顆硬碟上常見的資料夾。

部署完成後，第一次用 `dashboard-link.txt` 裡的連結開網頁（內含唯讀權杖，會記在瀏覽器裡）。
只想在本機試：`cd server && AH_API_TOKEN=<寫入權杖> AH_READ_TOKEN=<唯讀權杖> ../.venv/bin/uvicorn --factory app.main:create_app`。

**這些檔案只存在你的電腦，已列在 `.gitignore`，不要提交**：`agent/config.toml`、`.deploy-host`、
`server/.dashboard-token`、`dashboard-link.txt`、`share-link.txt`、`voice-chatgpt.md`、`maps-cache/`、`worlddata/`、所有 `*.db`、`handoff/`、`dist/`。

**沒有附帶、要自己取得的資料**：世界資料庫（任務、NPC、物品，見下方「世界資料庫」）與地圖底圖（見「地圖底圖」）。
兩者都有各自的授權與使用限制，沒有它們其餘功能照常運作。

**管理介面與個人權杖**：`http://<伺服器的區網位址>:8080/admin` 列出所有角色與個人上傳權杖，可以產生、解除綁定、刪除。
這一頁沒有密碼，所以只回應區域網路內的電腦；經過反向代理進來的請求，只要代理回報的來源不是區網位址，一律當作不存在（404）。
允許的網段可用環境變數 `AH_ADMIN_NETS` 調整（預設是各種私有位址）。**前面有反向代理時，代理必須帶上 `X-Forwarded-For` 之類的來源標頭**，
否則伺服器分不出外面來的請求。
個人權杖給其他人的同步程式用：一個權杖代表一個人，他的每個角色都用同一個。角色以遊戲裡的角色編號辨認；
一個角色第一次由哪個權杖上傳就歸誰，之後別人的權杖上傳同一個角色會被拒絕（409），資料會留在對方電腦上等著，不會遺失。
同一個權杖也能讓對方在瀏覽器看自己所有的角色並切換（`/#token=<權杖>`）。管理介面可以把角色改給別人或設為不屬於任何人。
設定檔裡的 `api_token` 填的就是這個權杖；伺服器主人自己的權杖不受限制。兩個人共用一台電腦時，
可以在同步程式視窗裡替個別角色另外指定權杖（設定檔的 `[characters]` 區段）。

**安全**：權杖放在請求標頭與分享連結裡。只在家裡區網用可以走 HTTP；要從外面連，請在前面放一層有 HTTPS 的反向代理，
否則權杖會以明文經過網路。伺服器以沒有登入權限的專用帳號執行，只能寫自己的資料夾。分享連結外流時用 `--rotate` 換掉。
伺服器前面有 Cloudflare 之類的防護時，自動化請求可能被擋：同步程式的 User-Agent 是 `AdventureSync/0.1`，
在防護那邊放行它或 `/api/` 開頭的路徑即可。被擋時同步程式會明說回應不是伺服器給的，資料留著稍後重傳。

**隱私**：聊天紀錄預設關閉；開啟後會包含其他玩家的訊息。組隊頁面的連結任何人拿到都能看，
內容含隊友的角色名稱與戰鬥數據（不含聊天），截圖也會出現在上面。要不要分享請自行判斷。

本專案與 Blizzard Entertainment 無關。以 [MIT 授權](LICENSE) 釋出：可以自由使用、修改、散布，不提供任何保證。

## 目錄

| 路徑 | 內容 |
| --- | --- |
| `addon/AdventureCollector/` | 遊戲插件（Lua） |
| `agent/adventuresync/` | 本機同步程式（Python 3.11+） |
| `server/app/` | API 伺服器（FastAPI + SQLite） |
| `tests/` | 自動測試（含模擬遊戲 API 的端對端測試） |
| `scripts/` | 安裝插件、執行 Agent、部署伺服器 |

## 使用

```bash
./scripts/install-addon-macos.sh        # 安裝插件到遊戲
./scripts/deploy-server.sh user@主機    # 部署伺服器到 Ubuntu（需 SSH 金鑰登入）
./scripts/run-agent-macos.sh validate   # 檢查設定與 SavedVariables
./scripts/run-agent-macos.sh run        # 持續同步
.venv/bin/python -m pytest tests -q     # 跑測試
luacheck addon                          # Lua 靜態檢查
```

Agent 設定在 `agent/config.toml`（含 Token，不進版控）。
遊戲內指令：`/adventure`（狀態）、`/adventure save`（立即存檔）、`/adventure button`（顯示/隱藏存檔按鈕）、`/adventure on|off`、`/adventure help`。

**立即存檔**：畫面上方有可拖動的「存檔 (N)」按鈕，N 是上次存檔後的新事件數。按下會重新載入介面，讓遊戲把資料寫到磁碟（戰鬥中不會執行）。

**聊天紀錄**：預設全部關閉。用 `/adventure chat <頻道|all> on|off` 開關，頻道有 `say yell party raid guild whisper general trade other_channels`；`/adventure chat` 顯示目前設定。聊天內容含其他玩家的訊息，會一併上傳到你的伺服器；綜合與交易頻道訊息量大，會較快佔滿插件保留的 5000 筆事件。

**地圖底圖**：51 張區域地圖來自 Warcraft Wiki 的「座標安全地圖」（Forever 版，1002×668），用 `scripts/fetch-wiki-maps.py` 下載並上傳到自己的伺服器。圖片版權屬於 Blizzard Entertainment，僅限家用、非商業、個人使用；快取在 `maps-cache/`，不進版控，請勿公開散布。地圖編號沿用經典版對照表，其中 1415、1436 已和實際資料核對，其餘為推定。

Wiki 沒有的地圖（例如 Forever 新增的區域）可以用截圖補：在遊戲內輸入 `/adventure mapshot`。插件會打開世界地圖並截圖，按「存檔」後 Agent 會從遊戲的 `Screenshots` 資料夾找到該截圖、裁出地圖範圍並上傳，Dashboard 的路線就會疊在地圖上。每張地圖做一次即可；截圖前請把地圖縮到最小、滑鼠移出地圖。
也可以手動指定任何「完整、未裁切」的地圖圖片：`cd agent && ../.venv/bin/python -m adventuresync upload-map <map_id> <圖片檔> [名稱]`。

**Dashboard**：瀏覽器開 `http://<你的伺服器>:8080/`。第一次請用 `dashboard-link.txt` 裡的連結（內含唯讀 Token，會記在瀏覽器裡）。

## 已定案的設計決定

- **角色 ID**：優先用角色 GUID；取不到時用 `伺服器:角色名`。
- **事件 ID**：`installationId:序號`。插件首次載入時產生隨機 `installationId`，序號存在 SavedVariables。
  若 SavedVariables 遺失，會產生新的 `installationId`，不會和舊事件撞號。
- **時間**：插件存 epoch 秒；Agent 轉成 UTC ISO 8601；伺服器一律存 UTC。
- **座標**：`x`/`y` 是該 `map_id` 地圖上的 0–100 百分比，不同地圖不可互相比較。
- **位置取樣**：每 15 秒檢查；移動 ≥ 0.5 或超過 60 秒才記錄；換地圖一定記錄。
- **SavedVariables 容量**：插件只保留最新 5000 筆事件，舊的自動丟棄。Agent 不修改該檔案。
- **快照**：每角色只留最新一份。較新的才會覆蓋；時間相同時保留先到的。伺服器用快照建立角色資料。
- **上傳**：整批原子寫入，重複 `event_id` 自動略過。整批被拒時 Agent 會逐筆重送，
  把不合法的那一筆標為 `rejected`，不會卡住佇列。網路或 Token 錯誤則保留待傳。
- **伺服器**：systemd 背景服務，不用 Docker。SQLite 使用 WAL。
- **驗證**：除健康檢查外都需要 Bearer Token。`AH_READ_TOKEN`（選用）只能讀。

## API

`GET /api/v1/health`、`POST /api/v1/events/batch`、`PUT /api/v1/characters/{id}/snapshot`、
`GET /api/v1/characters`、`GET /api/v1/characters/{id}`、`GET /api/v1/characters/{id}/snapshot`、
`GET /api/v1/events`、`GET /api/v1/locations`、`GET /api/v1/stats/summary`。
互動文件在伺服器的 `/docs`。

給工具用的物品與裝備：`GET /api/v1/characters/{id}/items`，回傳身上裝備與背包的結構化資料——物品等級、部位、
類型、屬性（`stats`）、賣店價（`sell_price`）、背包總值，以及 `comparisons`：背包裡每件可裝備的物品和身上同部位的
屬性差（`stat_diff`）、能不能穿（`usable`，依等級與職業護甲類型；武器為 null）。屬性與價格由插件從遊戲讀取，
舊版插件的資料 `has_stats` 為 false。語音查詢對應 `show=gear` 與 `show=compare`。

給工具（MCP、腳本）用的已接任務：`GET /api/v1/characters/{id}/quests`，回傳任務日誌的結構化資料——每個目標的
`current` / `required` 進度、是否可交（`complete`），以及來自世界資料庫的 `world`：區域、交付對象與座標、起點、
經驗（`xp` 原值、`xp_now` 依目前等級實得）、獎勵物品、後續任務。資料庫沒有的任務 `world` 為 null。

## 語音助理查詢（`/voice`）

給只能開網址的語音助理（例如 ChatGPT 語音）用：一個 GET 網址，回傳繁體中文純文字。

```
/voice?t=<角色代碼>&show=<項目>&range=<時間>&q=<關鍵字>&type=<事件類型>&limit=<筆數>
```

- `show`（可逗號合併，預設 `overview,activity`）：`overview` `activity` `quests` `bags` `gear`
  `skills` `rep` `stats` `flights` `kills` `loot` `events` `where` `todo`（所在區域還沒做的任務）`all` `help`
- `range`（預設 `today`）：`today` `yesterday` `week` `all`，或 `3h` 表示最近三小時
- `q` 名稱關鍵字；`type` 只用於 events；`limit` 1–100；`tz` 時區（預設 8）

代碼是 12 碼、每個角色一組、唯讀，永遠不回傳聊天內容，也不能寫入。
同一組代碼也能開 dashboard 給別人看：`https://你的公開網域/#token=<角色代碼>`，只看得到那一個角色
（連結寫在不進版控的 `share-link.txt`）。
產生代碼與可貼給 ChatGPT 的說明（寫到不進版控的 `voice-chatgpt.md`）：

```bash
.venv/bin/python scripts/voice-link.py --base https://你的公開網域
```

加 `--rotate` 會換新代碼，舊網址立即失效。代碼在網址裡，會出現在代理與存取紀錄中，外流就換掉。

## 截圖相簿與戰鬥紀錄檔（同步程式讀遊戲資料夾）

同步程式除了 SavedVariables，還會讀遊戲資料夾裡的兩種檔案，進度記在佇列資料庫，不會重複上傳：

- **截圖**（`<flavor>/Screenshots`）：每張新截圖縮成最寬 1920 的 JPEG 加一張縮圖後上傳。伺服器不存地點，顯示時才從
  角色的事件找出最接近的位置與當下發生的事（`GET /api/v1/screenshots`）。`/adventure mapshot` 拍的地圖底圖不進相簿。
  Dashboard 的「相簿」分頁與組隊頁面都會顯示。**截圖會拍到聊天視窗**，而分享連結與組隊頁面都看得到截圖；
  刪除用 `DELETE /api/v1/screenshots/{id}`（寫入 token）。
- **戰鬥紀錄檔**（`<flavor>/Logs/WoWCombatLog*.txt`）：這個客戶端不讓插件讀戰鬥紀錄，但遊戲自己的 `/combatlog` 會
  把它寫成文字檔。同步程式逐步讀取新增的內容，把玩家、隊友與寵物的傷害和治療按分鐘加總成 `GROUP_METER` 事件
  （`source: "combatlog"`，`""` 代表玩家自己，治療只算實際補到的量）。要先在遊戲裡輸入 `/combatlog` 開啟。

## 組隊場次（`/party/<代碼>`）

插件會記錄組隊的開始、成員加入與離開、結束（`GROUP_JOINED`、`GROUP_MEMBER_JOINED`、`GROUP_MEMBER_LEFT`、
`GROUP_LEFT`），組隊期間隊友補最後一擊的擊殺也會記下（`MOB_KILLED` 的 `killed_by`）。中途 `/reload` 或斷線不會把
一場拆成兩場；離線期間隊伍解散的話，下次登入時結束。

新版插件另外記錄：隊友的拾取（`PARTY_LOOT`）、隊友死亡（`PARTY_MEMBER_DIED`）、隊友升級（`GROUP_MEMBER_LEVEL_UP`）、
同一張地圖上隊友的位置（`PARTY_POSITIONS`，只記有移動的人）、每場戰鬥的傷害與治療加總（`GROUP_METER`，寵物算在
主人身上，`""` 代表紀錄者自己）。這些事件量大，事件列表可用 `exclude_type=A,B,C` 一次排除多種。

每一場自動得到一組 12 碼代碼，`https://你的公開網域/party/<代碼>` 是那一場的獨立頁面，拿到連結的人不用登入就能看：
成員、時間、擊殺與誰補的最後一擊、紀錄者的經驗與拾取、路線、經過。連結只涵蓋那一場的時間範圍，不含聊天，
也打不開其他 API。場次列表在 dashboard 的「總覽」分頁，或 `GET /api/v1/groups?character_id=`。

## 世界資料庫（靜態遊戲資料）

任務、NPC、出沒座標、掉落、商人、物品，來源是 [NorgannonDB](https://github.com/zmarotrix/NorgannonDB)
v1.7.0（GPL-3.0，針對 Forever 1.60.1，內含 QuestieDB 與 pfQuest 資料）。存成獨立的 `world.db`，
與角色紀錄分開，可隨時重建。來源檔只當資料解析，不執行任何 Lua。

```bash
# 1. 下載並解壓到 worlddata/（不進版控）
mkdir -p worlddata/norgannondb-v1.7.0
curl -L https://github.com/zmarotrix/NorgannonDB/archive/refs/tags/v1.7.0.tar.gz | tar -xz -C worlddata/norgannondb-v1.7.0 --strip-components=1
# 2. 匯入（約 6 秒，產生 worlddata/world.db 約 36 MB）
.venv/bin/python -m world_importer import --source norgannondb --path worlddata/norgannondb-v1.7.0
# 3. 部署（腳本會一併上傳 world.db）
./scripts/deploy-server.sh user@主機
```

資料表：`world_data_sources`（來源、版本、遊戲版本、匯入時間）、`world_maps`、`world_quests`、
`world_npcs`、`world_objects`、`world_spawns`、`world_items`、`world_drops`、`world_vendors`、
`world_quest_items`（哪些任務要收集這件物品）、`world_recipes`、`world_recipe_reagents`（專業配方與材料）。

API（唯讀 token）：`GET /api/v1/world/sources`、`/world/maps`、`/world/quests`、`/world/quests/{id}`
（加 `character_id` 會回 `status`：`in_log` 進行中、`completed` 已記錄完成、`unknown` 進度未知）、
`/world/npcs`、`/world/npcs/{id}`、`/world/items`、`/world/items/{id}`、`/world/names`、
`/world/nearby?map_id=&x=&y=&radius=`、`/world/quest-pins?character_id=`（任務日誌的地圖標記）。
物品明細包含分類、來源、任務需求、能做成什麼（`reagent_for`）與製作方式（`crafted_by`）。
`/world/quest-zones?character_id=` 是各區任務完成度，`/world/zone-quests?character_id=&map_id=` 列出一區的任務與狀態
（`available` 現在可接、`in_log`、`too_low` 等級不足、`locked` 缺前置、`completed`），並依角色等級算出實際可得經驗。
已完成任務來自插件讀取的遊戲清單（快照的 `quests_done`）；沒有這份清單時回應的 `known` 為 false。沒有 `world.db` 時這些回 503，其餘功能照常。

注意事項：
- 資料以經典版為底逐步修正，Forever 新增的任務與物品不完整；任務經驗值只有 `xp_confirmed` 的才是
  Forever 實測值。沒有來源對應到 build 71037。
- `unknown` 不等於沒做過：安裝插件之前完成的任務沒有紀錄。
- 掉落機率 `null` 代表「會掉、機率未知」。
- 衍生的 `world.db` 與下載的資料只供個人使用，不要公開發布或放進公開程式庫。
- 更新版本：下載新版到新資料夾後重跑匯入；失敗時舊的 `world.db` 不會被動到。
- 未採用規格中的 SQLAlchemy 與遷移工具：世界資料庫每次整份重建，不需要遷移。

## 已知限制

- 資料只在登出、離開遊戲或 `/reload` 時寫入磁碟，不是即時同步。
- Forever 測試版曾有「冷啟動不載入帳號層級 SavedVariables」的問題；正式版 build 71037 實測已正常載入。
  插件仍保留防護：存檔遺失時會換新的 `installationId`，不會撞號。
- 此客戶端的 `GetRealmName()` 與 `GetNormalizedRealmName()` 都回傳空值，`realm` 欄位為 null；角色以 GUID 識別，不受影響。
- 副本等不提供座標的地方不會有位置事件。
- `QUEST_REMOVED` 代表任務被移除但沒有對應的交付，可能是放棄或失敗，遊戲不區分。
- 拾取事件是從聊天訊息裡的物品連結解析；組隊時的擲骰訊息可能造成誤判。
- 擊殺事件靠 `PARTY_KILL`，只記錄自己打出最後一擊的怪；名稱只在曾經選取過該目標時才有。
- 副本、首領戰、成就、死亡、任務接取／完成事件與聊天紀錄已通過模擬測試，**尚未在遊戲內驗證**；其餘蒐集項目已實機確認。
- 擊殺事件只記自己打出最後一擊的怪；實測 11 次獲得經驗中有 1 次沒有對應的擊殺事件。
- 登入後 3 秒插件會查詢一次遊戲時間，聊天視窗會多兩行系統訊息；不想要可把存檔裡的 `requestPlayed` 設為 `false`。
- 天賦尚未蒐集（這個版本改用新的天賦介面，需另外研究）。

## 遊戲內驗收清單

自動測試使用模擬的遊戲 API，**不等於遊戲內驗證**。實機驗證狀態（Mac）：

| 項目 | 狀態 |
| --- | --- |
| 伺服器部署於區網主機，Mac 可連線且 Token 有效 | PASS |
| 插件載入並產生事件（Interface 16001，build 71037） | PASS（2026-11-21） |
| `/adventure` 顯示角色名稱、等級、地圖與座標 | NOT_TESTED |
| 移動時座標變化並產生位置事件 | PASS（2026-11-21） |
| `/reload` 後事件數不歸零 | NOT_TESTED |
| 存檔寫入 `WTF/Account/<帳號>/SavedVariables/AdventureCollector.lua` | PASS（2026-11-21） |
| 完全關閉遊戲再登入，序號延續（14 → 17），冷啟動有載入存檔 | PASS（2026-11-21，build 71037） |
| Agent `sync-once` 後可從 API 查到角色與位置 | PASS（2026-11-21，13 筆事件） |
| 快照含地圖、座標與正確金錢；登入第一筆位置不再是大陸地圖 | PASS（2026-11-21） |
| 「存檔」按鈕能觸發重新載入介面並寫入存檔 | PASS（2026-11-21） |
| `/adventure save` 指令（與按鈕呼叫同一個函式） | NOT_TESTED |
| 快照含裝備、背包、任務日誌、專業技能、經驗值 | PASS（2026-11-21） |
| 接任務／交任務／放棄任務產生對應事件 | NOT_TESTED |
| 拾取物品、擊殺怪物事件（含怪物名稱與編號） | PASS（2026-11-21，10 次擊殺、20 筆拾取） |
| 死亡事件 | NOT_TESTED |
| `/adventure mapshot` 能截到世界地圖並上傳 | PASS（2026-11-21） |
| 截圖裁切位置（修正瀏海造成的垂直偏移後） | NOT_TESTED |
| Wiki 地圖底圖與路線位置吻合（西部荒野） | PASS（2026-11-21，哨兵嶺位置相符） |
| Wiki 地圖編號：暴風城 1453、艾爾文森林 1429、暮色森林 1431 與遊戲回報的名稱相符 | PASS（2026-11-21） |
| 其餘 46 張 Wiki 地圖的編號對應 | NOT_TESTED |
| 快照含聲望、屬性、遊戲時間、飛行點 | PASS（2026-11-21） |
| 金錢、經驗、技能提升、戰鬥、NPC 互動、起飛降落事件 | PASS（2026-11-21） |
| 副本、首領戰、成就事件 | NOT_TESTED |
| 聊天紀錄（開啟頻道後） | NOT_TESTED |
| 走進沒有底圖的地圖時提醒 | NOT_TESTED |
| Windows 客戶端 | NOT_TESTED |
