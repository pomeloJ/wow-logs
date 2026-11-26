# WoW Adventure Hub

《魔獸世界：永恆》（World of Warcraft: Forever）的個人冒險紀錄。全部跑在你自己的機器上，沒有中央服務。

```
遊戲插件 → 同步程式（玩遊戲的電腦） → 伺服器（你自己的主機） → 網頁
```

| 組件 | 做什麼 | 位置 |
| --- | --- | --- |
| 插件 AdventureCollector | 在遊戲裡記錄角色狀態、位置、任務、擊殺、拾取、組隊、銀行 | `addon/` |
| 同步程式 AdventureSync | 讀遊戲的存檔、截圖、戰鬥紀錄檔，上傳到伺服器 | `agent/` |
| 伺服器 | 存資料，提供網頁、分享頁面、API | `server/` |

## 功能

- **總覽網頁**：角色、裝備、背包、銀行、任務、地圖路線、截圖相簿。
- **組隊頁面**：每一場組隊一個可分享的連結，含傷害與治療統計、首領戰、技能分析、怪物防禦研究、路線動畫。
- **單人練功**：不在隊伍裡的戰鬥自動切成一段一段（超過 20 分鐘沒打就算下一段），每段有同樣的分析頁面。
- **語音助理查詢**：一個網址回傳純文字，給 ChatGPT 語音這類只能開網址的工具用。
- **REST API**：互動文件在伺服器的 `/docs`。

## 安裝

需要遊戲本體（Forever 1.60.x）、Python 3.11 以上、一台能用 SSH 金鑰登入的 Linux 主機。

### 1. 伺服器

```bash
git clone https://github.com/pomeloJ/wow-logs.git && cd wow-logs
python3 -m venv .venv
.venv/bin/pip install -r agent/requirements.txt -r server/requirements.txt

cp agent/config.example.toml agent/config.toml   # 填伺服器網址，api_token 改成一段夠長的隨機文字
./scripts/deploy-server.sh user@你的主機
```

`api_token` 是**主權杖**，能讀寫所有資料。部署腳本會把它交給伺服器，另外產生一個**唯讀權杖**，
並把第一次開網頁用的連結寫到 `dashboard-link.txt`。伺服器以 systemd 服務執行，連接埠 8080。

只想在本機試：

```bash
cd server && AH_API_TOKEN=<主權杖> AH_READ_TOKEN=<唯讀權杖> ../.venv/bin/uvicorn --factory app.main:create_app
```

### 2. 同步程式

在玩遊戲的電腦上開啟（Windows 與 macOS 是同一份程式）：

```bash
cd agent && ../.venv/bin/python AdventureSync.pyw      # Windows：..\.venv\Scripts\python AdventureSync.pyw
```

在視窗裡依序：填伺服器網址與權杖 → 儲存設定 → 測試連線 → 安裝插件到遊戲 → 開始自動同步。
「戰鬥紀錄」那一列打綠色勾，代表你的戰鬥有被記到；出現警告就是遊戲裡的戰鬥紀錄可能沒開。
設定存在使用者資料夾的 `.adventuresync/config.toml`。找不到遊戲時用「選擇…」指定裡面有 `_forever_` 的資料夾。

不開視窗的用法：`python -m adventuresync run`（另有 `sync-once`、`validate`、`test-connection`、`status`）。

### 3. 遊戲內

畫面上方有可拖動的「存檔 (N)」按鈕。遊戲只在登出或重新載入介面時才把資料寫到磁碟，所以**按了存檔，同步程式才讀得到新資料**。

要有傷害統計與副本內的擊殺，先輸入 `/combatlog` 開啟遊戲的戰鬥紀錄。進遊戲時插件會跳出視窗請你確認，按視窗上的「確認開啟」就會開著。

| 指令 | 作用 |
| --- | --- |
| `/adventure` | 顯示狀態 |
| `/adventure save` | 存檔（同按鈕） |
| `/adventure button` | 顯示或隱藏存檔按鈕 |
| `/adventure on` / `off` | 啟用或暫停記錄 |
| `/adventure chat <頻道\|all> on\|off` | 聊天紀錄，預設全部關閉 |
| `/adventure combatlog` | 戰鬥紀錄有沒有開；加 `auto` 每次登入自動開，加 `quiet` 不再提醒 |
| `/adventure mapshot` | 把目前的世界地圖截下來當網頁底圖 |

## 給其他人用

**發權杖**：在區域網路內開 `http://<伺服器區網位址>:8080/admin`，替每個人產生一個**個人權杖**。

- 一個權杖代表一個人，他的每個角色都用同一個。
- 角色第一次由哪個權杖上傳就歸誰；別人的權杖再上傳同一個角色會被拒絕，資料留在對方電腦上等著，不會遺失。
- 同一個權杖也能在瀏覽器看自己的角色：`<伺服器網址>/#token=<權杖>`。
- 管理介面可以把角色改給別人，或刪除權杖。

**讓對方下載同步程式**：請他開 `<伺服器網址>/setup`。頁面上有原始碼版（需要 Python），以及你上傳的免安裝版。

**製作免安裝版**：在要給人用的那種系統上執行，編好的單一檔案會自動上傳到 `/setup`：

```bash
python -m pip install pyinstaller
python scripts/build-client.py --upload
```

Windows 要在 Windows 上編，macOS 要在 macOS 上編。檔案沒有簽署，第一次開啟系統會警告：
Windows 選「其他資訊 → 仍要執行」，macOS 在檔案上按右鍵選「打開」。

## 更新

同步程式會定期問伺服器有沒有新版，有的話視窗上方出現提示。

| 更新什麼 | 使用者怎麼做 | 你怎麼發布 |
| --- | --- | --- |
| 同步程式 | 按提示的按鈕下載新版 | 調高 `agent/adventuresync/__init__.py` 的 `__version__`，部署伺服器，重新製作免安裝版 |
| 只有插件 | 按「更新插件」，然後在遊戲裡 `/reload` | 調高 `addon/AdventureCollector/AdventureCollector.toc` 與 `Core.lua` 的版本，部署伺服器 |

只改插件時不需要重新製作免安裝版。

## 分享

- **組隊頁面**：每一場組隊自動得到 `<伺服器網址>/party/<代碼>`，拿到連結的人不用登入就能看那一場。場次列表在總覽網頁。
- **單一角色的唯讀連結與語音查詢**：執行下面的指令，會替每個角色產生一組 12 碼的**角色代碼**，
  並把分享連結寫到 `share-link.txt`、把可以貼給 ChatGPT 的說明寫到 `voice-chatgpt.md`。

```bash
.venv/bin/python scripts/voice-link.py --base https://你的公開網域   # 加 --rotate 換新代碼，舊的立即失效
```

分享連結與角色代碼都是唯讀，而且不會顯示聊天內容。

## 選用資料

這兩樣沒有附在程式庫裡，沒有它們其餘功能照常運作。

**世界資料庫**（任務、NPC、掉落、物品）：來源是 [NorgannonDB](https://github.com/zmarotrix/NorgannonDB) v1.7.0（GPL-3.0）。
匯入後才有任務進度、物品來源、怪物等級這些資訊。下載的資料與產生的 `world.db` 只供個人使用，不要公開散布。

```bash
mkdir -p worlddata/norgannondb-v1.7.0
curl -L https://github.com/zmarotrix/NorgannonDB/archive/refs/tags/v1.7.0.tar.gz | tar -xz -C worlddata/norgannondb-v1.7.0 --strip-components=1
.venv/bin/python -m world_importer import --source norgannondb --path worlddata/norgannondb-v1.7.0
./scripts/deploy-server.sh user@你的主機        # 會一併上傳 world.db
```

**地圖底圖**：`scripts/fetch-wiki-maps.py` 從 Warcraft Wiki 下載區域地圖並上傳到你的伺服器。
圖片版權屬於 Blizzard Entertainment，僅限個人、非商業使用。Wiki 沒有的地圖用遊戲內的 `/adventure mapshot` 補。

## 安全與隱私

- **不要提交這些檔案**（已在 `.gitignore`）：`agent/config.toml`、`.deploy-host`、`server/.dashboard-token`、
  `dashboard-link.txt`、`share-link.txt`、`voice-chatgpt.md`、`handoff/`。它們含有權杖或你的伺服器位址。
- **對外請用 HTTPS**。權杖放在請求標頭與連結裡，走 HTTP 會以明文經過網路；同步程式的自動更新也會下載並執行檔案。
  在伺服器前面放一層有 HTTPS 的反向代理。
- **管理介面沒有密碼**，只回應區域網路內的電腦。經過反向代理進來的請求，要靠代理附上的來源標頭
  （例如 `X-Forwarded-For`）才分得出是不是外面來的，所以代理一定要帶這個標頭。允許的網段可用環境變數 `AH_ADMIN_NETS` 調整。
- **前面有 Cloudflare 之類的防護時**，同步程式可能被當成機器人擋下。放行 User-Agent `AdventureSync/0.1` 或 `/api/` 開頭的路徑。
- **聊天紀錄**預設關閉；開啟後會包含其他玩家的訊息。
- **截圖會拍到聊天視窗**，而截圖會出現在組隊頁面與分享連結上。
- **組隊頁面**含隊友的角色名稱與戰鬥數據，連結任何人拿到都能看。

## 已知限制

- 銀行只有在遊戲裡打開銀行視窗時才讀得到，網頁顯示的是上次打開時的內容。
- 資料不是即時的：要按存檔（或登出）遊戲才會寫出，同步程式才讀得到。
- 戰鬥紀錄只記得到你附近發生的事，沒開 `/combatlog` 的場次沒有傷害統計。
- 副本內沒有地圖座標，路線動畫在副本那一段是空的。
- 截圖歸給最近玩的那個角色，連續換角色截圖可能歸錯。
- 世界資料庫以經典版資料為底，Forever 新增的任務與物品不完整。
- 天賦尚未蒐集。

## 開發

```bash
.venv/bin/pip install pytest lupa
.venv/bin/python -m pytest tests -q      # 自動測試，用模擬的遊戲環境
luacheck addon                           # 插件的靜態檢查
```

| 路徑 | 內容 |
| --- | --- |
| `addon/AdventureCollector/` | 遊戲插件（Lua） |
| `agent/adventuresync/` | 同步程式（Python） |
| `server/app/` | 伺服器（FastAPI + SQLite）與網頁 |
| `world_importer/` | 世界資料庫匯入工具 |
| `scripts/` | 部署、製作免安裝版、產生分享連結、下載地圖 |
| `tests/` | 自動測試 |

`scripts/deploy-server.sh` 的主機位址也可以寫在程式庫根目錄的 `.deploy-host`（一行 `user@主機`），之後不用每次輸入。

## 授權

[MIT](LICENSE)。本專案與 Blizzard Entertainment 無關。
