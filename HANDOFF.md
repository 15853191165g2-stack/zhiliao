# HANDOFF · 接手必读

> 给下一个接手这个项目的 AI / 开发者。
> 写于 2026-10-23。仓库：https://github.com/15853191165g2-stack/zhiliao
>
> 这份文档只写「你必须知道，否则会做错」的东西。
> 完整背景在 `README.md`，历史踩坑在 `docs/02-gotchas.md`（59 条，按编号索引）。

---

## 1. 这是什么

**知了** —— 面向大学生的校园通知聚合 App。一句话：只开一个 App，看全学校所有通知，
内置 AI 把红头文件压成三行重点。

当前只接了**大连理工大学**（主校区 + 盘锦校区 + 校级机关），24 个启用的来源。
产品形态两个：Windows 免安装桌面版（Tkinter）+ Flutter 手机端（代码在，**未编译过**）。

品牌叫「知了」，但**技术标识一律 `quanzhi`**：目录 `quanzhi/`、环境变量 `QUANZHI_PORT` /
`QUANZHI_STATE_DIR`、库文件 `quanzhi.db`、Dart 包名、APK applicationId。
**不要改成 zhiliao** —— 改了会断掉验收脚本、状态目录和已装用户的升级链路。

## 2. 代码在哪

| 位置 | 内容 | 说明 |
|---|---|---|
| GitHub `zhiliao` 仓库根 | 全部源码 | public / MIT / `main` |
| `backend/` | FastAPI 服务 | Python 3.13，异步 SQLAlchemy 2.0 |
| `backend/sources/*.yaml` | 抓取来源配置 | **29 个配置，24 启用**，加来源不用写代码 |
| `backend/app/adapters/` | 渠道适配器 | `@register("kind")` |
| `app/` | Flutter 客户端 | 本机无 SDK，只能结构自检 |
| `知了桌面版.py` | Tkinter 桌面端（2600+ 行） | 与手机端共用同一套后端和库 |
| `scripts/` | 23 个验收脚本 | 每个都支持 `--port`，不打扰用户实例 |
| `docs/` | 架构 / 59 条坑 / 数据源 / 验收 / 桌面版 | |

## 3. 怎么跑

```bash
# 后端
cd quanzhi/backend
python -m uvicorn app.main:app --port 8765 --host 127.0.0.1

# 抓一轮（约 4-6 分钟，24 个来源）
python scripts/fetch_once.py

# 补摘要（分批 10 条提交，可断点续）
python scripts/resummarize.py --status pending

# 测试
pytest -q                     # 期望 130 passed

# 桌面版（需要带 tkinter 的解释器）
python 知了桌面版.py
```

**`backend/.env` 需要配置**（已被 gitignore，需要自己建）：

```
AI_API_KEY=<智谱 key>          # 不填也能跑，会降级本地规则
AI_MODEL=glm-4.7-flash
DATABASE_URL=sqlite+aiosqlite:///./quanzhi.db
```

## 4. 七条铁律（违反就算返工）

1. **不假装实现做不到的事。** 微信没有公开接口、iOS 读不到别家通知 —— 必须明说 + 给替代方案，
   集中登记在 `README.md` 的「诚实的边界说明」。
2. **每条通知必须有 3 行摘要**：是什么 / 关键信息 / 该怎么办。本地兜底也要补齐，不许空。
3. **AI 不是必需依赖。** 无 `AI_API_KEY` 时全链路照跑（降级本地规则）。
4. **抓取必须合规**：robots 校验 + 同域最小间隔 + 并发上限，全部在 `services/fetch.py`，
   适配器不许自建 HTTP 客户端。
5. **学校登录凭据只存手机本地**，后端零存储。
6. **时间**：库存 naive UTC，对外带 `+08:00`。
7. **验收/截图脚本不许弄脏用户数据。** 改数据的脚本一律走 `scripts/user_flags.py`
   （snapshot/diff/restore），别各写一份。

## 5. 最容易踩的八个坑

按踩坑代价排序，完整版见 `docs/02-gotchas.md`。

| # | 坑 | 一句话 |
|---|---|---|
| 52 | **AI 会编造事实** | 正文 <30 字就**不许**调模型。它不说「不知道」，会把标题补成像真的假事实（实测编造出国庆放假日期） |
| 56 | **200 响应被判成限流** | 限流只能在**失败**响应上判断。`resp.text` 里含模型正文，撞到「429/1305」就误判 → 退避 4 次后降级。**所有指标都是绿的**，只有 `ai_model` 悄悄变了 |
| 49 | **三种假绿** | ①探活打进别人的后端 ②窗口按标题匹配到别人的 ③壳内 `API` 常量没跟 `--port` 走。验收前必须 `guard_port` + 按 PID 认窗口 + 隔离 `QUANZHI_STATE_DIR` |
| 57 | **「新增 0」可能是假的** | 9 个来源全 502，汇总照样写「新增 0、退出码 0」。`fetch_once.py` 现已输出「失败来源 N/24」，**N>0 时这一轮不算完成** |
| 58 | **中文文件名被 git 转义** | `git ls-files -s` 受 `core.quotepath` 影响，`知了.bat` 会输出成 `"\347\237\245..."`。脚本用它会凭空造出乱名文件。一律用 `-z` |
| 55 | **「无权访问」被当正文** | 博达返回 200 + 系统提示页，正好 29 字（卡在 30 字阈值下），本地规则把提示语切成了第二行摘要。已加 `_looks_like_error_page()` |
| 53 | **跨部门转发重复** | 同一通知多部门转发，URL 不同 → URL 判重失效。入库层补「标题去空白 + 发布日到天」判重 |
| 50 | **首次游标吞通知** | 模拟器/新机器首次启动对齐到 `max_id`，队列里已有的通知永久静默。正确起点是「最老一条 id − 1」，且必须带 `since_id=0`（不带时后端按发布时间**倒序**返回） |

**判据**：这些坑的共同特征是**指标全绿、事实错**。格式合规 ≠ 内容可信，
退出码 0 ≠ 抓全了，测试通过 ≠ 断言有效（写过两条假绿断言，都用变异测试才暴露）。

## 6. 数据现状（2026-10-23）

```
通知总数   264 条  （全部 done）
摘要模型   glm-4.7-flash 215 条 · local-no-content 49 条
3 行合规   264/264
来源       29 个配置 / 24 启用
时间跨度   2019-06-17 ~ 2026-10-18
分类       后勤 77 · 活动 62 · 其他 53 · 教务 45 · 奖助学金 13 · 就业 7 · 考试 4 · 讲座 3
```

`local-no-content` 是**诚实兜底**，不是失败：这些详情页是纯图片/PDF/SPA（JS 渲染）或需要登录，
抓不到正文。摘要写明「原文信息较少，建议点开看全文」，不编内容。

## 7. 还没做的

- **Flutter 端从未编译过** —— 本机无 SDK，只过了静态自检 `app/scripts/check_dart.py`。
  上真机前必须验证 Android `POST_NOTIFICATIONS` 权限、通知图标、以及 `applicationId`。
- **公众号自动发现** —— `mp.weixin.qq.com` robots 是 `Disallow: /`，批量抓一律禁止。
  只允许用户手动粘贴单条链接。要做自动发现，需用户自备 RSS 桥接（`docs/03` 路线 B）。
- **只覆盖大工** —— 其他学校要新增 `backend/sources/<school>_*.yaml`，博达模板有 4 种结构，
  盘锦与主校区是**独立实例，选择器不能互套**。
- **桌面版信息流不自动刷新** —— 只有推送轮询在跑，用户要点顶栏「🔄 刷新」。

## 8. 交接时**无法**通过文档转移的东西

下面这些只存在于原机器，接手方必须自己重建：

| 项 | 原位置 | 怎么办 |
|---|---|---|
| AI API Key | `backend/.env`（已 gitignore） | 自己申请智谱 key，`glm-4.7-flash` 长期免费 |
| 通知数据库 | `D:\QuanZhi\backend\quanzhi.db`（3.7 MB / 264 条） | 已导出 CSV：`notifications_264.csv`；或从空库重抓 |
| 便携包运行时 | `D:\QuanZhi\python\`（175 MB） | 不进仓库；要用就自己下载 embeddable Python |
| SSH 私钥 | `C:/Users/Public/zhiliao_ssh/id_ed25519` | 原机器专用，不转移；接手方自己生成 |
| 用户个人状态 | `state/desktop.json`（已读/置顶/推送开关） | 属个人数据，不转移 |

## 9. 环境事实（会直接影响判断）

- **原机器打不开 `github.com` 网页**：`20.205.243.166:443` 被阻断（直连超时、走代理 000），
  但 `api.github.com`（.168）和 `ssh.github.com`（.160）**都通**。
  网页类操作只能靠手机或换网络；命令行可走 API + SSH 22 端口。
- **Windows 下别用中文文件名执行 .py**：Python 会静默退出（返回 0、零输出）。中文名配 ASCII 入口 import。
- **本机有系统代理**（端口会变）。访问 `127.0.0.1` 会被拦成 502，看起来像「后端没起来」。
  联调必须 `NO_PROXY=localhost,127.0.0.1` 或 `ProxyHandler({})`。
- **全量 pytest 可能撞 safe-delete 钩子**；要数测试用 `pytest --collect-only -q`。

---

## 10. 接手第一天建议做的三件事

1. `cd backend && pytest -q` —— 确认 130 passed，环境是通的
2. `python scripts/fetch_once.py` —— 看「失败来源」是不是 0/24，不是就重跑
3. 读 `docs/02-gotchas.md` 的 49、52、56、57 四条 —— 这四条是本项目最容易「看起来对、其实错」的地方
