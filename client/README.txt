AdventureSync - WoW Adventure Hub 的同步程式

Windows 與 macOS 用的是同一份程式。

不想安裝 Python？
    這一包是原始碼版，需要 Python。伺服器的 /setup 頁面如果列出「Windows」或「macOS」的下載，
    那是單一檔案版（Windows 是一個 .exe），下載後點兩下就能用，不必安裝任何東西。
    頁面上沒有的話，請伺服器的主人編一份放上去。
    第一次開啟時 Windows 可能顯示「Windows 已保護您的電腦」：按「其他資訊」再按「仍要執行」。

執行方式（原始碼版）：
1. 安裝 Python 3.11 以上（https://www.python.org/downloads/ ，Windows 請選 64 位元安裝程式，
   安裝時勾選「Add python.exe to PATH」）。
2. 在這個資料夾開啟終端機（Windows：在資料夾空白處按住 Shift 再按右鍵，選「在終端機中開啟」），輸入：
       python -m pip install -r requirements.txt
   （macOS 用 python3）
3. 對 AdventureSync.pyw 點兩下開啟視窗。缺少套件時會跳出說明；完全沒反應的話在終端機輸入：
       python AdventureSync.pyw

在視窗裡：
- 填入伺服器網址與寫入權杖，按「儲存設定」再按「測試連線」。
- 按「安裝插件到遊戲」。遊戲資料夾留空時會自動尋找（Windows 會找每一顆硬碟上常見的位置）；
  找不到就按「選擇…」，選裡面有 _forever_ 的那個資料夾。
- 進遊戲一次，按畫面上方的「存檔」。
- 按「開始自動同步」，視窗開著就會持續同步。

有新版本時，視窗最上面會出現「有新版本」和一個按鈕：
- Windows 的單一檔案版（.exe）：按下去會把新版下載到原本那個檔案的旁邊並自動開啟，舊的檔案可以刪掉。
- macOS 與原始碼版：按下去會用瀏覽器下載，下載後關掉視窗，改開新的。
設定都存在使用者資料夾，換新版不用重新填。

設定存在你的使用者資料夾下的 .adventuresync/config.toml，裡面有權杖，不要傳給別人。
資料只會送到你填的那台伺服器。

遊戲插件有新版本時，視窗上方會出現提示，按「更新插件」就會換掉遊戲裡的插件，
不用重新下載這個程式。更新後在遊戲裡輸入 /reload。
