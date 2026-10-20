# 知了 · 技术方案（阶段 1）

版本：v0.1 ｜ 更新：2026-10-06 ｜ 作者：知微

---

## 1. 一句话架构

**一个配置驱动的抓取内核 + 统一数据模型 + 一层 AI 提炼，对外只暴露一个信息流 API。**

```
                        ┌──────────────── Flutter App ────────────────┐
                        │  首页信息流 / 详情 / 今日简报 / AI 对话       │
                        │  来源管理 / 设置        本地加密凭据存储      │
                        └───────────────┬─────────────────────────────┘
                                        │  HTTPS + JSON
                        ┌───────────────▼─────────────────────────────┐
                        │            FastAPI 服务层                    │
                        │  /notifications  /sources  /ingest          │
                        │  /brief          /chat     /health          │
                        └───────────────┬─────────────────────────────┘
                                        │
        ┌───────────────────────────────┼──────────────────────────────┐
        │                               │                              │
┌───────▼────────┐            ┌─────────▼──────────┐         ┌─────────▼────────┐
│  抓取编排层     │            │   AI 提炼层         │         │  定时调度层       │
│  services/     │            │  services/          │         │  workers/        │
│  ingest.py     │            │  summarize.py       │         │  scheduler.py    │
│  fetch.py      │            │  brief.py           │         │  08:00 / 18:00   │
│  dedup.py      │            │  prompts/*.md       │         │  + 手动触发       │
└───────┬────────┘            └─────────┬──────────┘         └──────────────────┘
        │                               │
┌───────▼────────────────┐     ┌────────▼──────────┐    ┌─────────────────────┐
│  ★ 适配器注册表         │     │  智谱 GLM-4.7     │    │  PostgreSQL         │
│  html_list / rss / …   │     │  OpenAI 兼容协议   │    │  sources            │
│  一份 yaml = 一所学校   │     │  JSON 强约束输出   │    │  notifications      │
└───────┬────────────────┘     └───────────────────┘    │  fetch_logs         │
        │                                               └─────────────────────┘
   ┌────┴─────┬──────────┬───────────┬──────────┐
   ▼          ▼          ▼           ▼          ▼
 学校官网   教务处    图书馆 RSS  公众号链接   小程序/APP
 (html)    (html)     (rss)     (公开正文)  (分享导入)
```

---

## 2. 分层职责

| 层 | 目录 | 职责 | 不做什么 |
|---|---|---|---|
| 适配器层 | `app/adapters/` | 把一个来源的页面变成标准化 `RawItem` 列表 | 不碰数据库、不做 AI |
| 抓取层 | `app/services/fetch.py` | robots 校验、频控、重试、编码识别、UA | 不解析内容 |
| 编排层 | `app/services/ingest.py` | 按 source 取适配器 → 抓取 → 去重 → 入库 → 排队做 AI | 不含业务规则 |
| AI 层 | `app/services/summarize.py` + `prompts/` | 摘要、关键信息、重要程度、简报、问答 | 不写库（通过返回结构由编排层落库） |
| API 层 | `app/api/routes/` | 参数校验、分页、序列化 | 不含抓取与 AI 逻辑 |
| 调度层 | `app/workers/scheduler.py` | 定时触发 `ingest` | 不含业务逻辑 |

**关键设计原则：新增一所学校 = 在 `sources/` 加一个 yaml 文件。** 只有遇到非标准页面（JS 渲染、需要登录、奇怪的翻页）才需要写新适配器，写完在 `registry` 注册一行即可被配置引用。

---

## 3. 数据源适配器协议

### 3.1 适配器接口（`adapters/base.py`）

```python
class SourceAdapter(Protocol):
    kind: ClassVar[str]                                   # 配置里的 adapter 字段值
    async def fetch(self, spec: SourceSpec, ctx: FetchContext) -> list[RawItem]: ...
```

- `SourceSpec`：一条来源配置（name / url / adapter / config / school / category_hint）
- `FetchContext`：注入依赖（httpx client、settings、频控器），**适配器不自己 new 客户端**，便于测试与限流统一
- `RawItem`：统一输出 —— 标题、链接、发布时间、作者、正文 HTML/纯文本

### 3.2 yaml 配置规范（html_list 适配器）

```yaml
name: 示例大学教务处通知
school: 示例大学
adapter: html_list
url: https://jwc.example.edu.cn/tzgg.htm
category_hint: 教务            # 教务/考试/奖助学金/活动/讲座/就业/后勤
enabled: true

config:
  transport: http             # http | file（file 用于离线样例与回归测试）
  list_selector: "ul.news-list li"
  fields:
    title: "a"                 # 相对列表项的 CSS 选择器
    url: "a@href"              # @ 语法取属性
    date: "span.date"
  url_base: https://jwc.example.edu.cn/     # 相对链接拼接基址
  date_format: "%Y-%m-%d"      # 先按固定格式解析
  date_regex: '(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})'   # 解析不了就正则兜底
  follow_detail: true          # 是否进详情页取正文
  detail:
    content_selector: "div.v_news_content"
  pagination:
    max_pages: 2
    next_selector: "a.next"
  max_items: 50
```

### 3.3 已实现的适配器

| kind | 适用场景 | 状态 |
|---|---|---|
| `html_list` | 绝大多数学校官网/教务处/学院/图书馆的通知列表页 | ✅ 阶段 1 |
| `rss` | 学校自带的 RSS、第三方 RSS 桥接（公众号） | ✅ 阶段 1 |
| `manual_link` | 用户分享进来的单个链接（公众号/小程序/APP） | 阶段 4 |
| `android_notification` | Android 通知监听上报（走 App 端，不走后端适配器） | 阶段 4 |
| `playwright` | JS 动态渲染站点（html_list 的 transport 扩展） | 阶段 4 预留 |

---

## 4. 数据模型

```
sources                                  notifications
─────────────────────────────            ──────────────────────────────────────
id            PK                         id             PK
name          TEXT                       source_id      FK → sources.id
school        TEXT                       dedup_key      UNIQUE   ← 去重核心
adapter       TEXT                       title          TEXT
url           TEXT                       url            TEXT
category_hint TEXT                       author         TEXT
enabled       BOOL                       published_at   TIMESTAMP  ← 信息流排序键
config        JSON                       content_html   TEXT
last_fetched_at TIMESTAMP                content_text   TEXT
last_status   TEXT  (ok/error/…)         content_hash   TEXT  ← 正文变更检测
last_error    TEXT                       ── AI 产物 ──
created_at    TIMESTAMP                  summary_lines  JSON  (3 行摘要)
                                         key_info       JSON  (截止/对象/地点/材料/联系人)
fetch_logs                               importance     TEXT  (urgent/important/normal)
─────────────────────                    category       TEXT
id, source_id, started_at,               tags           JSON
finished_at, status, item_count,         ai_status      TEXT  (pending/done/failed)
new_count, error                         ai_model       TEXT
                                         ── 用户状态（阶段 2 起同步到云端）──
                                         is_read        BOOL
                                         is_starred     BOOL
```

**去重策略（`services/dedup.py`）**
1. 主键：`dedup_key = sha1(canonical_url)`；无 URL 时退化为 `sha1(normalized_title + published_date)`
2. `canonical_url` 归一化：去 `utm_*` / `from` / `spm` 等跟踪参数、去 fragment、统一小写 host、去末尾斜杠
3. 二次校验：`content_hash` 变了才更新正文（应对「同一 URL 内容被编辑」），并标记需重新生成摘要
4. 标题近似去重（阶段 2）：同一学校内标题 Jaccard 相似度 > 0.9 且发布时间相差 < 24h → 合并为一条，保留链接更规范的

---

## 5. AI 层设计

### 5.1 固定输出结构（`schemas.py` 的 `AISummary`，这是硬契约）

```json
{
  "summary_lines": ["第一行", "第二行", "第三行"],
  "key_info": {
    "deadline":   "2026-10-15 17:00",
    "audience":   "2024 级全体本科生",
    "location":   "逸夫楼 305",
    "materials":  ["申请表", "成绩单", "获奖证明复印件"],
    "contact":    "李老师 010-12345678"
  },
  "importance": "urgent | important | normal",
  "category":   "教务 | 考试 | 奖助学金 | 活动 | 讲座 | 就业 | 后勤 | 其他",
  "tags":       ["奖学金", "材料提交"],
  "confidence": 0.0
}
```

**写入数据库前一定过 Pydantic 校验**：字段缺失 → 补 `null`；`importance` 非法值 → 降级 `normal`；`deadline` 无法解析成日期 → 保留原文但 `deadline_ts=null`（避免污染日历功能）。任何一步失败都写 `ai_status=failed` 并保留原始正文，**绝不因为 AI 失败而丢通知**。

### 5.2 Prompt 管理（`app/prompts/`）

- 模板是独立 `.md` 文件，按版本命名：`summarize.v1.md`、`brief.v1.md`、`qa.v1.md`
- `services/prompts.py` 负责加载 + `{{变量}}` 替换；换 Prompt = 换文件，不改代码
- 每个模板首段都硬性要求：**只输出 JSON，不要 Markdown 代码块，不要解释**
- `ai_client.extract_json()` 做兜底：剥离 ```` ```json ```` 围栏、截取第一个 `{` 到最后一个 `}`，再 `json.loads`

### 5.3 调用与兜底

- 协议：OpenAI 兼容 `POST {AI_BASE_URL}`，`model=glm-4.7-flash`
- 重试：1 次指数退避；超时 60s
- **无 API Key 时走 `LocalSummarizer` 离线兜底**：按标点切句取前 3 句做摘要，正则抽取日期/地点/电话/材料关键词。保证**没有 Key 也能跑通全链路**，方便开发和演示
- 成本控制：同一 `content_hash` 只调用一次；简报按天缓存；对话只检索 Top-K 相关通知作为上下文

---

## 6. API 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 + 各依赖状态 |
| GET | `/api/notifications` | 信息流。参数：`source_id` `category` `importance` `q` `unread_only` `starred_only` `since` `page` `page_size` |
| GET | `/api/notifications/{id}` | 详情（原文 + AI 摘要 + 关键信息） |
| POST | `/api/notifications/{id}/read` | 标记已读 |
| POST | `/api/notifications/{id}/star` | 收藏 |
| POST | `/api/notifications/{id}/summarize` | 强制重跑 AI |
| GET | `/api/sources` | 来源列表 |
| POST | `/api/sources` | 新增来源（写 yaml 或直接传配置） |
| PATCH | `/api/sources/{id}` | 启用/停用/改配置 |
| DELETE | `/api/sources/{id}` | 删除来源 |
| POST | `/api/sources/{id}/fetch` | 单独抓取某来源 |
| POST | `/api/ingest/run` | 全量抓取（App 下拉刷新调用） |
| GET | `/api/brief/today` | 今日简报 |
| POST | `/api/chat` | 问答（传问题 + 可选时间范围） |

统一响应包：`{"code": 0, "message": "ok", "data": {...}}`，分页统一 `{"items": [], "total": 0, "page": 1, "page_size": 20}`。

---

## 7. 合规与安全

| 项 | 做法 |
|---|---|
| robots | 每次抓取前获取并缓存 `robots.txt`（TTL 24h），`urllib.robotparser` 校验；被禁路径跳过并记 `FetchLog` |
| 频率 | 同域名最小间隔 + 全局并发上限 + 单站点 `max_pages` 上限；失败退避 2^n 秒，最多 3 次 |
| 标识 | UA 形如 `QuanZhiBot/0.1 (+contact: 你的邮箱)`，方便站长联系 |
| 凭据 | 学校登录 Cookie / 账密**只存手机端**（Android Keystore / iOS Keychain），后端零存储；后端只接收用户主动分享的公开链接 |
| 传输 | 生产强制 HTTPS；本地调试允许 HTTP |
| AI 隐私 | 送往模型的内容仅限**已公开的通知正文**；不发送用户身份信息、不发送私有链接 |

---

## 8. 已知取舍

1. **PostgreSQL 是目标数据库**，但为了让阶段 1 能在任何人的机器上一条命令跑起来，默认 `DATABASE_URL` 用的是 SQLite（`sqlite+aiosqlite`）。切换 PG 只需改环境变量，代码层已全部走 SQLAlchemy 异步抽象，且 JSON 字段用的是通用 `JSON` 类型（PG 上会落成 JSONB）。
2. **单机 APScheduler 而非消息队列**：阶段 3 先用它跑通「每天两次」，并发规模上去后换成 RQ/Celery 只需替换 `workers/` 一层。
3. **全文检索先用 `LIKE`**：SQLite 下够用；上 PG 后换 `tsvector` 或 `pg_trgm`。
4. **向量检索暂不引入**：通知条数在个人使用量级（数千条）下，问答直接把 Top-K 关键词检索结果塞进 Prompt 就够了，不需要向量库。
