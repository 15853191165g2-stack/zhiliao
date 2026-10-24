# 知了 · zhiliao

> **通知，知了。**
>
> 只开一个窗口，看全学校所有通知。内置 AI 自动提炼重点，三行讲完。

面向大学生的校园通知聚合工具。学校的通知散在教务处、学生处、研究生院、图书馆、团委、
各学院、公众号、校园门户十几个地方，每条都要自己点进去读一遍才知道跟自己有没有关系。
知了把它们抓到一起，用 AI 压成三行，让你三十秒看完今天所有事。

- **后端**：Python 3.11+ / FastAPI / SQLAlchemy 2.0（异步）
- **客户端**：Flutter（Android 优先，一套代码兼容 iOS）
- **桌面版**：Tkinter 便携包，双击即用，不装 Python
- **AI**：智谱 GLM-4.7-Flash（OpenAI 兼容协议）；**没有 API Key 也能跑**，自动降级本地规则

---

## 它做了什么

| 能力 | 说明 |
|---|---|
| **多源聚合** | 24 个启用来源（大连理工主校区 + 盘锦校区 + 校级官网），同一套后端、同一条信息流 |
| **加源不用写代码** | 桌面版「来源管理 → ＋ 添加来源」粘贴栏目网址即可：先试抓一次给你看真实标题，确认后入库 |
| **三行摘要** | 每条通知固定三行：是什么 / 关键信息（时间地点对象）/ 该怎么办。不是生成一段话让你自己找 |
| **重要度分级** | AI 判定 `high / normal / low`，评奖、补考、选课这类不会被埋在第 20 条 |
| **去重** | URL 判重 + 「标题去空白 + 发布日到天」跨来源判重（同一通知常被多部门转发，URL 不同） |
| **推送** | 可插拔通道：桌面气泡 / Webhook（企业微信、钉钉、Server酱、Bark）。失败留在队列里重试 |
| **诚实的降级** | 抓不到正文时**不调 AI**，直接说明「原文页面需要登录或无权限」，不编日期、不编流程 |

## 一次通知怎么走完全程

```
sources/*.yaml
   │  配置驱动：列表页选择器、日期正则、详情抽取规则
   ▼
adapters/           robots 校验 → 同域限速 2s → 并发上限 4
   ▼
services/fetch.py
   ▼
services/dedup.py   URL 去重 + 空壳保护（详情页空正文不覆盖已有内容）
   ▼
services/ingest.py  入库 + 跨来源标题判重
   ▼
services/summarize.py
   │  正文 < 30 字 → 跳过 AI，走诚实兜底（防止模型看着标题编事实）
   │  正文正常     → GLM 出三行摘要 + 关键信息 + 重要度
   ▼
services/push.py    本地通道 / Webhook 通道，各通道显式声明是否「消费」队列
   ▼
桌面气泡 / 手机系统通知 / 企业微信 / 钉钉
```

## 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 客户端 | Flutter (Dart 3) | 一套代码双端；Android 通知监听、TTS、日历写入的插件生态最完整 |
| 后端 | Python 3.11+ / FastAPI | 抓取生态与 AI SDK 最全，写适配器最快 |
| 数据库 | PostgreSQL 16（开发期回落到 SQLite） | JSONB 存 AI 结构化结果、全文检索、后续向量检索都顺 |
| 任务调度 | APScheduler（单机） | 量级上来再换 RQ / Celery |
| AI | 智谱 GLM-4.7-Flash | 中文强、官方长期免费、支持 JSON 输出；适配层可一行切别家 |
| 推送 | 本地通知 + Webhook | FCM 在国内无 GMS 手机上收不到；厂商推送要开发者账号 + 上架审核，个人拿不到 |

---

## 5 分钟跑起来

### 后端

```bash
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # 填 AI_API_KEY；不填也能跑，走本地规则摘要

pytest -q                                              # 130 个测试，含 25 个离线回放 + 48 个推送
python scripts/seed_demo.py                            # 导入离线样例，不联网先看效果
# 或者真抓：python scripts/e2e_dlut.py --reset          # 24 个来源，约 5 分钟（同域限速 2s）

uvicorn app.main:app --reload --port 8000
```

打开 http://127.0.0.1:8000/docs 看交互式 API 文档。

```bash
curl http://127.0.0.1:8000/api/health
curl "http://127.0.0.1:8000/api/notifications?page_size=5"
```

### 桌面版（Windows，免安装）

```bash
python quanzhi_desktop.py        # 需要本机有 Python 3.11+ 和 tkinter
# 打包成便携版见 docs/05-desktop.md
```

> 桌面版与手机端**共用同一套后端与数据库**，界面只是重新排了信息层级。
> 顶栏有 🔔 推送开关、🔄 刷新、三档筛选；打开详情会自动标已读。

### 手机端

```bash
cd app
flutter pub get
flutter run          # 需要 Flutter SDK；Android 设备/模拟器
```

> App 默认指向 `http://10.0.2.2:8000`（Android 模拟器访问宿主机）。
> 真机调试改成电脑局域网 IP，在「设置 → 后端地址」里可直接改。
> 后端没启动时 App 会进入**离线演示模式**，用内置样例渲染界面，方便先看 UI。

---

## 加一所学校 = 加一个网址（或一个 yaml）

**最省事的路径**：桌面版 → 来源管理 → 「＋ 添加来源」→ 粘贴栏目的列表页网址 →
点「探测」。知了会真的打开那一页，自动认出列表结构，把抓到的前 8 条标题摊给你看，
确认后入库；勾「保存后立即抓取一次」当场就能知道抓不抓得到。

识别不出来的页面会**明确拒绝保存**，并说明原因（JS 动态渲染 / 这是首页不是栏目页 /
需要登录 / 404）—— 宁可加不进去，也不存一个每天静默失败的来源。

**要精细控制**（多种结构混排、要翻页、要改日期正则）就写 yaml。
不需要写 Python，在 `backend/sources/` 放一个文件：

```yaml
- id: myschool_jwc
  name: 某大学·教务处
  kind: html_list                 # 适配器类型，@register 注册过
  url: https://jwc.example.edu.cn/tzgg.htm
  enabled: true
  category: 教务
  list_selector: "div.list ul li" # 列表项
  fields:
    title: "a"
    url: "a"
    date: "span.date"
  date_extract_regex: '20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}'
  date_format: "%Y-%m-%d"
  follow_detail: true             # 是否进详情页取正文
```

```bash
python scripts/check_dlut_sources.py --source myschool_jwc   # 先看单来源健康度
```

抓歪了先别急着调选择器 —— `docs/03-dlut-sources.md` 记了 4 种列表结构和各种日期形态。

新增**渠道类型**（比如 RSS、单篇文章）才需要写代码：
在 `app/adapters/` 写一个类 + `@register("kind")`。

---

## 换个模型 = 换一个接入地址

内置默认是智谱 GLM-4.7-Flash，但**不绑死**。桌面版 AI 问答页右上角有个
「模型：glm-4.7-flash ▾」，点开可以接任何 **OpenAI 兼容协议**的服务
（DeepSeek、Kimi、通义、豆包、硅基流动，以及本机 Ollama）。

要填四项：

| 字段 | 例子 | 说明 |
|---|---|---|
| 配置名 | 我的 DeepSeek | 你自己看得懂就行 |
| 接入地址 | `https://api.deepseek.com` | **只填域名也可以**，会自动补 `/v1/chat/completions` |
| 模型名 | `deepseek-chat` | 服务商文档里那个 model id |
| API Key | `sk-...` | 只存本机 |

**先点「测试连接」再保存。** 地址填错时服务端不会说「你地址不对」，只会还一个
401 / 404 / 超时，分不清是哪一项错。所以测试结果里会直接回显**最终实际请求的地址**，
拼没拼对照一眼就知道。

切换是**全局生效**的：问答、文章摘要、今日简报都跟着换，不是只换问答。
用完想回默认，列表里点「默认 · glm-4.7-flash」那行的「启用」即可。

两个坑已经踩过并处理了（详见 `docs/02-gotchas.md` #62）：
本机/内网地址（Ollama）会被 Windows 系统代理拦成 502，代码里已按 host 自动直连；
`thinking` / `response_format` 并非所有服务商都认，报错点名时会自动摘掉重试。

---

## ⚠️ 诚实的边界说明

这些是当前技术条件下**做不到**或**只能部分做到**的部分，不假装已经实现：

| 需求 | 现实情况 | 替代方案 |
|---|---|---|
| 微信公众号文章自动抓取 | 微信**没有公开接口**。`mp.weixin.qq.com` 的 robots 是 `Disallow: /`，批量抓一律禁止；第三方「RSS 桥接」本质是逆向，随时失效且有法律风险 | 用户手动粘贴单条链接导入（公开可访问的正文），后端已做越权例外 |
| 微信小程序通知 | 小程序通知**不落地到任何可访问的地方**，没有 API | 只能靠用户主动分享链接/截图导入 |
| 学校 APP 内通知（Android） | 可行：`NotificationListenerService` 监听本机通知栏 | 需在设置里手动授权「通知使用权」 |
| 学校 APP 内通知（iOS） | **不可行**：iOS 沙盒禁止 App 读取其他 App 的通知 | iOS 分享扩展导入文本/链接，或「截图 → OCR」 |
| **手机端「关掉 App 也能收到通知」** | **做不到**。现在的实现是「轮询 + 系统本地通知」，App 被系统杀掉后定时器就停了。真正的后台推送必须走 FCM 或厂商推送，而 FCM 在国内无 GMS 手机上收不到，厂商推送要开发者账号 + 上架审核 | ① 推送做成可插拔通道（`register_channel`），有凭据时加 `fcm`/`getui` 即可，业务代码不用改；② Webhook 推到企业微信/钉钉/Bark，人在哪都能看到 |
| 桌面版关掉后收不到通知 | 气泡是本程序自己开的窗口，进程退出就没了 | 同上走 Webhook；或把后端常驻成 Windows 服务 |
| 学校官网抓取 | 每所学校页面结构不同，部分站点 JS 动态渲染 / 有反爬 | 适配器配置驱动；动态站点预留 Playwright transport 开关 |
| 打包 release APK | 需要本机 Android SDK + JDK，签名文件必须你自己生成 | 签名密钥必须由本人保管，本项目不代生成 |
| 自定义大模型的 API Key 存储 | 存在本机 SQLite（`llm_profiles.api_key`）里，**明文，没有加密**。不假装加密：本地单机应用里自己造一把密钥，密钥也在同一台机器上，只是把明文换成一眼看不出来的样子，反而会让人误以为它是安全的 | 真正做到的只有两条：① 所有接口只回打码值（`sk-a****mnop`），编辑时把打码值原样回传即视为「没改」；② 这个库文件不出本机，知识库导出与 git 都不带它。要更高安全性请用系统凭据管理器自行接管 |
| 非 OpenAI 协议的大模型 | 只支持 **OpenAI 兼容协议**（`/chat/completions`）。Anthropic 原生、Gemini 原生、部分国产私有协议**不能直连** | 用 one-api / new-api 之类的网关转一层成 OpenAI 格式，再把网关地址填进来 |

三条硬约束已经写进代码、不是口头承诺：

1. **robots 协议**：每次请求前拉取并校验目标站点 `robots.txt`，被禁止的路径直接跳过（默认开启）。
2. **频率限制**：同域名请求间隔 ≥ `FETCH_MIN_INTERVAL`（默认 2s），并发上限 4，User-Agent 带联系方式。
3. **凭据本地化**：学校登录态只存手机端加密存储（Keystore / Keychain），**不上传服务端**；
   后端只接收用户主动分享的公开链接。

---

## 测试与验收

```bash
cd backend && pytest -q                        # 222 passed / 1 skipped
python scripts/check_dlut_sources.py           # 单来源健康度
python scripts/check_migration.py              # 迁移幂等
python scripts/check_ai.py                     # AI 连通性
python scripts/probe_llm_model.py --port 8791  # 模型选择界面链路（需先起隔离后端）
python scripts/verify_all.py --port 8795       # 便携包一体化验收 40 项
python scripts/smoke_desktop.py --port 8796    # 桌面端冒烟 58 项（需先起后端）
```

这个项目对「测试通过」有额外的洁癖，因为它踩过太多**表面全绿、实际是错的**情况：

- **变异测试**：每个关键断言都要证明「把修复改回去它会红」。项目里抓到过两条**假绿**断言
  （一条匹配到了注释里的字符串，一条用单行输入导致无论如何都通过）。
- **200 响应里出现数字 ≠ 限流**：曾经因为 `resp.text` 里带「429/1305」（日期、电话）
  把一个合法响应判成限流，退避重试 4 次后降级。所有指标都是绿的，只有模型名悄悄变了。
- **正文空却调 AI**：模型不会说「不知道」，而是照着标题编出一个像真的假期安排。
  现在正文 < 30 字直接跳过 AI。
- **抓取失败 ≠ 没有新通知**：曾经 9 个站点全 502，汇总仍写「新增 0、退出码 0」。
  现在只要有来源失败就顶一行警告。

更多见 `docs/02-gotchas.md`（**57 条**实战踩坑，每条对应已修代码位置）。

## 文档

| 文件 | 内容 |
|---|---|
| `docs/01-architecture.md` | 架构图、适配器协议、数据模型、AI 输出契约、API 表、合规设计 |
| `docs/02-gotchas.md` | 57 条踩坑清单：异步 Session、robots 递归、GLM 思维链吃 token、推送 `consume` 语义、游标卡死、Tk 线程安全、验收脚本假绿…… |
| `docs/03-dlut-sources.md` | 大工数据源接入：来源矩阵、选择器证据、博达分页怪癖、能力边界 |
| `docs/04-verification.md` | 各阶段真实验收数字 |
| `app/README.md` | 客户端设计与 Android 通知权限说明 |

## 许可证

[MIT](LICENSE)

> 抓取行为请遵守目标站点的 robots 协议与服务条款。本项目默认限速 2s/域，
> 请不要把间隔调小 —— 学校站点扛不住，而且这会让所有学生都用不了。
