AdventureSync - WoW Adventure Hub 的同步程式

Windows 與 macOS 用的是同一份程式。

執行方式（原始碼版）：
1. 安裝 Python 3.11 以上（https://www.python.org/downloads/ ，Windows 請選 64 位元安裝程式，
   安裝時勾選「Add python.exe to PATH」）。
2. 在這個資料夾開啟終端機（Windows：在資料夾空白處按住 Shift 再按右鍵，選「在終端機中開啟」），輸入：
       python -m pip install -r requirements.txt
   （macOS 用 python3）
3. 對 AdventureSync.pyw 點兩下開啟視窗。打不開時在終端機輸入：
       python AdventureSync.pyw

在視窗裡：
- 填入伺服器網址與寫入權杖，按「儲存設定」再按「測試連線」。
- 按「安裝插件到遊戲」。
- 進遊戲一次，按畫面上方的「存檔」。
- 按「開始自動同步」，視窗開著就會持續同步。

設定存在你的使用者資料夾下的 .adventuresync/config.toml，裡面有權杖，不要傳給別人。
資料只會送到你填的那台伺服器。
