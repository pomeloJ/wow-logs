"""Double-click to open the sync program's window. Also what a one-file build starts from."""
import sys


def explain(problem: str) -> None:
    """Say what is wrong in a dialog. A double-clicked .pyw has no terminal, so without this
    a missing package would look like nothing happening at all."""
    text = (f"{problem}\n\n請在這個資料夾開啟終端機，輸入：\n    python -m pip install -r requirements.txt\n\n"
            "不想安裝 Python 的話，可以到伺服器的 /setup 頁面下載不需要 Python 的版本。")
    try:
        import tkinter
        from tkinter import messagebox
        tkinter.Tk().withdraw()
        messagebox.showerror("AdventureSync", text)
    except Exception:
        pass
    if sys.stderr:
        print(text, file=sys.stderr)


if sys.version_info < (3, 11):
    explain(f"AdventureSync 需要 Python 3.11 以上，這台電腦用來開啟它的是 {sys.version.split()[0]}。")
    sys.exit(1)
try:
    from adventuresync.gui import main
    import PIL  # noqa: F401  (screenshots and map images need it; better to say so now than fail quietly later)
except ImportError as e:
    explain(f"缺少必要的套件：{e.name or e}")
    sys.exit(1)

sys.exit(main())
