"""持续存活测试：开一个窗口，每 2 秒把时间戳写进心跳文件，看它能活多久。

用法：python\\pythonw.exe scripts\\heartbeat.py
"""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEAT = ROOT / "logs" / "heartbeat.log"
BEAT.parent.mkdir(parents=True, exist_ok=True)

f = open(BEAT, "w", encoding="utf-8", buffering=1)
f.write(f"启动于 {time.strftime('%H:%M:%S')}\n")
f.flush()

root = tk.Tk()
root.title("心跳测试窗口")
root.geometry("420x220")
tk.Label(root, text="心跳测试", font=("Microsoft YaHei UI", 20)).pack(pady=30)
tk.Label(root, text="此窗口每 2 秒写一次心跳", font=("Microsoft YaHei UI", 10)).pack()

ticks = {"n": 0}


def beat() -> None:
    ticks["n"] += 1
    f.write(f"#{ticks['n']:>4}  {time.strftime('%H:%M:%S')}\n")
    f.flush()
    root.after(2000, beat)


def on_close() -> None:
    f.write("窗口被用户关闭\n")
    f.flush()
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_close)
root.after(2000, beat)
root.mainloop()

f.write("mainloop 已退出\n")
f.close()
