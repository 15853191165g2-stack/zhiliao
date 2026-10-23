# 05 · 桌面版（Windows）

知了有两种形态，**共用同一套后端与数据库**，只是界面不同：

| 形态 | 入口 | 适用 |
|---|---|---|
| 手机端 | `app/`（Flutter） | 随身看、系统通知 |
| 电脑版 | `知了桌面版.py`（Tkinter） | 坐在电脑前，右下角弹气泡 |

电脑版是**免安装便携包**：自带 Python 运行时，双击 `知了.bat` 即用，删文件夹即卸载。

## 从源码跑

需要本机有 Python 3.11+（**要带 tkinter**，Windows 官方便装版自带）。

```bash
python quanzhi_desktop.py          # 正常启动
python quanzhi_desktop.py --port 8799   # 换个端口，不打扰已开着的实例
```

启动器会自己把后端拉起来（`uvicorn app.main:app`），窗口起来后即可用。

> **为什么入口叫 `quanzhi_desktop.py` 而不是直接跑 `知了桌面版.py`？**
> Windows 下 `python.exe` / `pythonw.exe` 直接执行**文件名为中文**的 `.py` 时
> 会**静默退出**（返回码 0、零输出、无 traceback），排查成本极高。
> ASCII 名入口内部用 `importlib` 把中文名的实现加载进来 —— 中文名不影响 `import`，
> 只影响「直接作为脚本执行」。

## 打包成便携版

目标结构：

```
知了/
├── python/              # 自带 Python 运行时（约 175MB，不进 git）
├── backend/             # 同一套后端
├── 知了桌面版.py
├── quanzhi_desktop.py
├── 知了.bat
└── scripts/             # 验收脚本
```

`知了桌面版.py` 里的 `_resolve_root()` 会依次找「含 `python\python.exe` 和 `backend\`」的目录，
所以只要保持这个结构，拷到 U 盘、D 盘、任何路径都能跑。

`知了.bat` 已经做了兼容：有自带运行时就用它，没有就回落到系统 `pythonw`。

## 环境变量

| 变量 | 作用 |
|---|---|
| `QUANZHI_PORT` | 后端端口，默认 `8765` |
| `QUANZHI_STATE_DIR` | 界面状态目录，默认 `state/` |

两个变量是为了**跑验收脚本时不打扰你自己开着的那个实例**。

## 添加来源（粘贴网址即可）

来源管理页右上角「＋ 添加来源」：粘贴一个栏目列表页的网址 → 点「探测」→
**看到真实标题预览** → 确认后保存。不写 yaml、不碰 CSS 选择器。

流程与判据（细节见 `docs/02-gotchas.md` 第 61 条）：

| 步骤 | 发生了什么 |
|---|---|
| 探测 | `POST /sources/probe`，走 `Fetcher`（robots + 同域 2 秒限速），自动识别列表结构 |
| 预览 | 复用 `HtmlListAdapter._parse_node` 解析，返回 8 条真实标题 + 日期 |
| 保存 | `POST /sources`，配置（`list_selector` / `fields` / `item_url_filter`）一并入库 |
| 首抓 | 勾选「保存后立即抓取一次」就调 `/sources/{id}/fetch`，抓不到当场提示 |

**探测失败时保存按钮保持禁用** —— 不允许存一个抓不到的来源，否则就是每天静默失败。
识别不出来的常见原因会在弹窗里直接写清：JS 动态渲染、这是首页不是栏目页、需要登录、404。

命令行验收（只探测、不写库，可以对着真实后端跑）：

```bash
python scripts/probe_add_source.py --port 8781 \
    --url http://teach.dlut.edu.cn/xwkd/xwkd.htm          # 正向：预览出真标题、保存可用
python scripts/probe_add_source.py --port 8781 \
    --url http://www.lib.dlut.edu.cn/xw/xwdt.htm --expect-fail   # 反向：404 时保存必须禁用
```

## 验收脚本（`scripts/`）

脚本全部支持 `--port`，会自己起一个隔离的后端和状态目录：

```bash
python scripts/launch_probe.py --port 8799     # 双击启动链路：进程存活 + 后端拉起 + 窗口 PID 匹配
python scripts/verify_all.py   --port 8792     # 界面一体化 40 项
python scripts/verify_push.py  --port 8791     # 推送端到端 46 项
python scripts/smoke_desktop.py --port 8794    # 桌面冒烟 58 项（需先起后端）
python scripts/selfcheck_ui.py --port 8795     # 读控件真实文字（比截图可靠）
python scripts/probe_flags.py  --port 8796     # 筛选与已读 33 项
python scripts/probe_add_source.py --port 8781 # 添加来源界面链路（需先起后端）
python scripts/probe_push_live.py              # 用真实通知驱动真窗口
python scripts/probe_push_cursor.py            # 换机首次启动不漏消息
```

## 四个已经踩过的坑（改 UI 前先读）

1. **别只靠截图判断 UI** —— 截图只能证明「有像素」，不能证明文字对。
   用 `selfcheck_ui.py` 读控件真实文字。
2. **工作线程不能直接调 Tk** —— `widget.after()` 不是线程安全的，
   必须线程入队 + 主线程 `_ui_pump` 消费。
3. **验收脚本不许弄脏用户数据** —— 「点下去产品会做什么」才是验收标准（打开详情会自动标已读，
   所以注入点击 = 写数据）。改库的脚本一律走 `scripts/user_flags.py` 做快照/还原。
4. **别假设用户没标记过** —— `/pin`、`/star` 是切换语义。拿列表第一条当测试目标，
   用户一旦置顶/收藏了它，脚本就会先把它取消，然后断言失败（假红）。
   选目标时排除已置顶/已收藏，断言用显式值（`{"is_pinned": true}`）。

更多见 `docs/02-gotchas.md` 第 22、23、45、46、48、49、50、60 条。
