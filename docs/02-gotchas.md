# 踩坑清单（阶段 1 实战记录）

这些都是写代码时真踩到的坑，记下来避免阶段 2-6 重复踩。
每一条都对应仓库里已经修好的代码位置。

---

## 1. SQLite 连接串必须用正斜杠

```python
# ❌ Windows 上 Path 会给出反斜杠，SQLAlchemy 解析 URL 会出问题
f"sqlite+aiosqlite:///{BASE_DIR / 'quanzhi.db'}"

# ✅
f"sqlite+aiosqlite:///{(BASE_DIR / 'quanzhi.db').as_posix()}"
```

位置：`app/core/config.py`

---

## 2. 异步 SQLAlchemy：绝不要访问未加载的关系

这是本项目最容易踩、且报错信息最不友好的坑。

```python
row = (await session.execute(select(Notification))).scalars().first()
row.source.name   # ❌ MissingGreenlet: greenlet_spawn has not been called
```

异步 Session 下，惰性加载会尝试在同步上下文发查询，直接炸。
**凡是序列化时要读 `row.source`，查询就必须带预加载：**

```python
select(Notification).options(selectinload(Notification.source))
```

已经踩到的位置：`api/routes/notifications.py`、`services/brief.py`、`services/search.py`

---

## 3. feedparser 的时间已经是 UTC，别再转一次

```python
# ❌ published_parsed 是 UTC 的 struct_time，
#    直接 datetime(*parsed[:6]) 得到的"naive"被当成北京时间，会再偏 8 小时
datetime(*entry.published_parsed[:6])

# ✅ 先明确标成 UTC
datetime(*parsed[:6], tzinfo=timezone.utc)
```

位置：`app/adapters/rss.py::_entry_time`

表现症状：RSS 的 `pubDate` 是 `10:30 +0800`，入库后显示成 `02:30 +08:00`。

### 顺带定下的时间约定

- **数据库里一律存 naive UTC**（SQLite / PG 行为一致，不会有隐式时区转换的意外）
- **对外输出一律转成带 `+08:00` 的 ISO 字符串**（`core/timeutil.to_local_iso`）
- 解析无时区的文本时间时，按北京时间处理（学校通知的默认语境）

---

## 4. 日期解析要三档兜底，且宁可为空也不要猜错

顺序：配置里的 `date_format` → 内置正则表 → `dateutil` 兜底 → 返回 `None`。

**绝不能**在解析失败时随便塞一个当天日期 —— `deadline` 是「一键加日历」的输入，
写错比留空害处大得多。前端会显示「时间未知」，用户还能自己看原文。

位置：`app/core/timeutil.py::parse_dt`

---

## 5. 去重要保留微信文章的关键参数

`canonical_url()` 会剥掉 `utm_*` / `from` / `spm` 等跟踪参数。
但微信文章的 `__biz` / `mid` / `idx` / `sn` **不能剥**，剥掉就 404。

```python
KEEP_PARAMS_ALWAYS = {"__biz", "mid", "idx", "sn", "chksm"}
```

同时：`href="#"` 这类无有效链接的条目**照样入库**（用标题+日期做兜底去重键），
不要把「当天的通知」因为链接不规范就丢掉。

位置：`app/services/dedup.py`

---

## 6. 「3 行摘要」是硬契约，本地兜底也必须凑满 3 行

前端卡片是按 3 行排版的。正文为空（例如 RSS 只给了标题）时，
如果只返回 2 行，卡片高度会跳变。

所以 `_build_lines()` 结尾必须补齐到正好 3 行，缺的位置填
「原文信息较少，建议点开看全文」，而不是少给一行。

位置：`app/services/summarize.py::_build_lines`

---

## 7. 材料抽取要用两档正则

单条宽松正则会踩坑：

```
正文：请将申请材料提交至所在学院学生工作办公室（逸夫楼305）。
     需提交的材料包括：申请表、成绩单原件。
```

`(?:携带|提交|...)` 会先匹配到「提交**至**所在学院…」，把地点当成材料。

```python
# 第一档：明确的「材料包括：xxx」
_MATERIAL_ANCHORED_RE = r"材料(?:包括|如下|有|为)?\s*[:：]\s*([^\n。；;]{2,80})"
# 第二档：宽松匹配 + 噪声过滤（剔除「至/到/在」开头、以「办公室」结尾的）
_MATERIAL_LOOSE_RE = r"(?:携带|上交|附上|(?:需|须)提供)\s*[:：]?\s*([^\n。；;]{2,60})"
```

先跑第一档，有结果就不跑第二档。

位置：`app/services/summarize.py::_find_materials`

---

## 8. 关键词去重会误删领域词

抽关键词时，如果先抽领域词（奖学金）再抽 n-gram 窗口，
最后做「去掉被更长词包含的短词」这一步会把 `奖学金` 删掉
（因为它被包含在 `奖学金要交什么材料` 里）。

**领域词必须钉死，不参与包含关系剔除。**

位置：`app/services/search.py::extract_keywords`

---

## 9. 无 API Key 时必须能跑通全链路

`AI_API_KEY` 为空时，`AIClient.enabled == False`，`SummarizeService` 自动降级到
`LocalSummarizer`（正则抽时间/地点/材料/联系人 + 关键词判分类和重要程度）。

意义：**开发和演示不该被 API Key 卡住**；同时线上 Key 失效时业务不中断，
只是 `ai_model` 字段会变成 `local-fallback`，`confidence` 标低到 0.35，
前端可以据此提示「建议查看原文」。

位置：`app/services/summarize.py`

---

## 10. robots 校验要跳过自己

`RobotsCache` 拉取 `robots.txt` 用的是**裸 HTTP 客户端**，
不能走 `Fetcher._request()` —— 否则会陷入「为了知道能不能抓 robots.txt，
先去校验能不能抓 robots.txt」的递归。

另外拿不到 `robots.txt`（404 / 超时）时按**放行**处理并记日志，
这是社区通行做法；拿到了才严格执行 `Disallow`。

位置：`app/services/fetch.py`

---

## 11. Windows 下测试脚本里的中文查询参数要转义

```python
# ❌ UnicodeEncodeError: 'ascii' codec can't encode characters
urlopen("http://127.0.0.1:8000/api/notifications?q=奖学金")

# ✅
urlopen(base + urllib.parse.quote(path, safe="/?=&:%"))
```

位置：`backend/scripts/smoke_api.py`

---

## 12. 为什么默认用 SQLite，而目标却是 PostgreSQL

需求写的是 PostgreSQL，代码也全部按 PG 的能力设计（异步驱动、JSON 字段、
`selectinload`、索引）。但 `DATABASE_URL` 默认给了 SQLite，理由只有一个：

**让任何人 `pip install` 之后一条命令就能把完整链路跑起来**，不需要先装 PG、
建库、配用户。切换只改一个环境变量：

```
DATABASE_URL=postgresql+asyncpg://quanzhi:password@127.0.0.1:5432/quanzhi
```

代码层零改动。JSON 字段用的是通用 `JSON` 类型，在 PG 上落成 JSONB。

已知差异（阶段 2 上 PG 后要处理）：
- `LIKE` 全文检索 → 换 `tsvector` 或 `pg_trgm`
- `nulls_last()` 在 SQLite 上由 SQLAlchemy 模拟，PG 原生支持
- 建表用 `create_all`，上生产前接 Alembic 做迁移

---

## 13. 别在同一个文件上并行发多个编辑

这不是项目代码的问题，是本项目开发过程中实际发生的：对同一个文件同时提交多个
Edit，后一个会基于旧快照覆盖前一个，导致前面那处修改**静默丢失**。
本项目因此返工了 3 次（`base.py`、`summarize.py`、`rss.py`）。

**规则：同一个文件的多处修改必须串行提交，改完立刻 grep 确认。**

---

## 14. Python 的 `\w` 会匹配汉字，写「邮箱正则」时是致命的

阶段 2 抽联系人时踩到的，症状极具迷惑性：

```python
# ❌ 想匹配邮箱，实际把整句中文都吞进去了
_EMAIL_RE = r"[\w.\-+]+@[\w\-]+\.[\w.\-]+"

re.search(_EMAIL_RE, "登记表请发送至邮箱dgzz@dlut.edu.cn")
# Match(span=(0, 25), match='登记表请发送至邮箱dgzz@dlut.edu.cn')
#                    ↑ 本地部分匹配成了「登记表请发送至邮箱dgzz」
```

Python 的 `re` 默认 `\w` = `[a-zA-Z0-9_]` **加上所有 Unicode 词字符（含汉字）**。
所以 `[\w.\-+]+` 会一路吃到汉字，`match.start()` 变成 0，
后续「取匹配前的文字当人名」的前缀清洗直接失效，最后把整句话当成了联系方式。

```python
# ✅ 邮箱一定限成 ASCII
_EMAIL_RE = r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+"
```

**通用规则：凡是要匹配「英文/数字」的字符类，永远显式写 `[A-Za-z0-9]`，
不要用 `\w`。** 想匹配纯汉字反过来用 `[\u4e00-\u9fa5]`。

位置：`app/services/summarize.py`（`_EMAIL_RE`）

---

## 15. 全角标点不清理，「人名判定」会静默失败

接着上一条。清理引导词后，`"李老师，联系电话："` 里的**全角逗号 `，`**
不在清洗字符集里，残留成 `"李老师，"`，长度和字符集都过不了「像人名」的校验，
于是 `"李老师 010-62781234"` 被静默降级成只剩号码 `"010-62781234"`。

```python
# ✅ 清洗完引导词后，再按「只留中文人名」收敛一遍，比逐个列标点靠谱
name = _NON_NAME_CHAR_RE.sub("", name)   # r"[^\u4e00-\u9fa5·]"
```

**教训：中文文本里全角/半角标点混用极普遍，白名单式「保留想要的」
比黑名单式「删掉不要的」稳。**

位置：`app/services/summarize.py`（`_contact_or_raw`）

---

## 16. GLM-4.7-Flash 是推理模型，思维链会吃掉 max_tokens（接 Key 后第一个坑）

**症状**：配好 `AI_API_KEY` 后，摘要全部失败，报

```
无法从响应中取出文本：{"choices":[{"finish_reason":"length","index":0,
  "message":{"content":"","reasoning_content":"1. **分析请求：**\n ..."}}]}
```

看着像「模型不听话没返回 JSON」，其实**根本不是格式问题**。

**根因**：`glm-4.7-flash` 是推理模型，响应里除 `message.content` 外还有一个
`message.reasoning_content`（思维链），而**思维链计入 `max_tokens`**。
原来 `max_tokens=1500`，思维链一吐就是 1000+ 字，额度被吃光 →
`content` 返回空字符串、`finish_reason="length"`。

实测对照（同一个请求，只改一个变量）：

| 配置 | finish_reason | content | reasoning_content |
|------|---------------|---------|-------------------|
| 开思考，max_tokens=120 | `length` | **0 字** ❌ | 256 字 |
| 关思考，max_tokens=120 | `stop` | **26 字** ✅ | 0 字 |

**修法（两处一起改才稳）**

```python
# 1) 关掉思考：摘要属于「抽取」不是「推理」，关掉既省额度又快得多
payload["thinking"] = {"type": "disabled"}

# 2) 额度留足余量（1500 → 2000）
payload = await self.ai.chat_json(system, user, max_tokens=2000)
```

**另外加了两道防护**（比修 bug 本身更重要）：

1. `_pick_content` 现在能**明确诊断**截断：检测到 `finish_reason=="length"`
   且有 `reasoning_content` 时，直接报
   「输出被 max_tokens 截断：思维链已消耗 N 字 …… 请关闭 thinking」，
   而不是含糊的「取不出文本」。
2. `scripts/check_ai.py` 加了「**真实摘要预演**」：填完 Key 跑一次，
   用真实 prompt 验证 `summary_lines` 是否满 3 行，不足就提示调小 `max_tokens`。

位置：`app/services/ai_client.py`、`app/core/config.py`（`ai_thinking`）、
`scripts/check_ai.py`

---

## 17. 免费档限流很频繁，必须「退避重试 + 优雅降级」

`glm-4.7-flash` 免费档并发只有 1，实测连续摘要时会频繁拿到：

```
HTTP 429 {"error":{"code":"1302","message":"您的账户已达到速率限制"}}
HTTP 429 {"error":{"code":"1305","message":"该模型当前访问量过大，请您稍后再试"}}
```

**只退避一次（5s）不够** —— 实测仍然失败。改成**指数退避 4 次**
（5s → 10s → 20s → 40s），并且把限流从其它错误里**单独识别出来**：
只有限流才值得重试，内容类错误（如 JSON 不合法）重试纯属浪费额度。

```python
_RATE_LIMIT_HINTS = ("429", "1302", "1305", "访问量过大", "速率限制", ...)
```

最关键的一点：**降级路径必须真的能用**。限流耗尽重试后不是报错中断，
而是落到本地规则摘要，`ai_model` 记 `local-fallback`、`ai_error` 记下原因，
前端照常显示 3 行摘要。这就是「AI 不是必需依赖」这条项目铁律的价值。

位置：`app/services/summarize.py`（`_RateLimited`、`_is_rate_limited`）

---

## 18. 知识：GLM 免费额度的边界（2026-10 官网核实）

- **`glm-4.7-flash` 是官方长期免费模型**：输入/输出/缓存全免费，200K 上下文。
  免费模型只有两个（文本 `glm-4.7-flash` + 视觉 `glm-4.6v-flash`）。
- 新用户注册送 2000 万 tokens（永久有效），**手机号注册即可**，无需信用卡。
- 免费档限制：**并发 1**。所以本项目 `fetch_concurrency=4` 只影响抓取，
  **摘要必须串行**（`ingest.py` 里本来就是 for 循环串行，恰好合适）。
- `glm-4.5-flash` 已于 2026-01 下线，会自动路由到 `glm-4.7-flash`。

---

## 19. 坑（Windows / 打包分发）：中文文件名的脚本会被静默杀掉

做「电脑版便携包」时踩的，排查花了很久。

**现象**：双击启动脚本，什么都没发生 —— 没有窗口、没有黑框、没有报错。
手工执行也一样，**返回码 0，零输出**。

**原因**：`python.exe` / `pythonw.exe` 执行**文件名为中文**的 `.py` 脚本时，
在 Windows 上会直接静默退出。不是编码问题、不是路径权限、也不是缺依赖。

**验证方法**（两个脚本内容完全一样，只改文件名）：
```
pythonw.exe  D:/x/ascii_name.py   → 正常执行
pythonw.exe  D:/x/中文名字.py     → 静默退出，返回 0
```

**规避**：启动器固定调用 **ASCII 名入口** `quanzhi_desktop.py`，
中文名的 `知了桌面版.py` 只作为**模块被 import**。
中文文件名不影响 `import`（importlib 走的是 unicode 路径），只影响「直接作为脚本执行」。

**顺带踩到的同类问题**：不能只用 `__file__` 推包根。
脚本被复制到 `scripts/` 下运行时，`__file__` 会推出 `scripts\`，
于是去找 `scripts\python\python.exe` → `FileNotFoundError`。
改成「多候选目录 + 按 `python\python.exe` 与 `backend\` 两个特征向上找」。

位置：`quanzhi_desktop.py`（入口）、`知了桌面版.py` 的 `_resolve_root()`

---

## 20. 坑（本机联调）：系统代理会拦截 127.0.0.1 请求

**现象**：后端明确在跑、`curl` 也能通，但桌面端就是显示「后端未连接」。
用 urllib 请求 `http://127.0.0.1:8765/api/health` 得到 **502 Bad Gateway**。

**原因**：机器上设了 `HTTP_PROXY` / `HTTPS_PROXY`（Clash 之类会设）。
`urllib` 默认会把**本地回环地址也丢给代理**，代理当然不认识
`127.0.0.1:8765`，就回 502。注意 502 是**代理**返回的，不是后端返回的 ——
这是最容易误判的地方（会以为后端挂了）。

**解决**（两处都要改）：
1. 客户端：给本地请求建一个**不使用代理**的 opener
   ```python
   _NO_PROXY_OPENER = urllib.request.build_opener(
       urllib.request.ProxyHandler({})   # 空 dict = 全部直连
   )
   ```
2. 后端子进程：启动时**清掉代理环境变量**并设 `NO_PROXY`，
   否则后端自己抓校园网站会走代理；同时保证端口探测直连。
   ```python
   for k in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY",
             "http_proxy","https_proxy","all_proxy"):
       env.pop(k, None)
   env["NO_PROXY"] = "localhost,127.0.0.1,::1"
   ```

位置：`知了桌面版.py` 的 `http_json()` 与 `Backend.start()`

---

## 21. 修正：限流退避不只该在写摘要时做

第 17 条只在 `summarize.py` 里做了指数退避，`ai_client.chat()` 仍是
「重试 2 次、零等待」。结果：抓取/简报能过（它们串行、间隔自然错开），
**AI 问答却频繁被 429 打回本地兜底** —— 同一个 Key、同一个模型，
不同入口行为不一致，体验很割裂。

已把退避策略上移到 `ai_client.chat()` 统一处理（5→10→20→40 秒，
限流特征单独识别），`summarize.py` 那套保持不变（它对单条通知更激进）。

**注意**：`code 1305「该模型当前访问量过大」是服务端容量问题**，
退避重试也只能缓解、不能保证成功。免费档并发为 1，这是客观限制。
重试耗尽后**必须落到本地兜底并如实标注** `generated_by=local`，不要假装答过。

位置：`app/services/ai_client.py`（`_RATE_LIMIT_*`、`_is_rate_limited`）

---

## 22. 卡片点击用的是「手写绑定清单」，漏了一层 —— 点了没反应

**现象**：用户反馈「通知打不开，看不到全文」。

排查发现详情逻辑本身**完全正常**（直接在进程内调 `open_detail()`，
781 字正文、3 行摘要、关键信息全都渲染正确）。问题在**事件绑定**：

原来是这样绑的（只列举了「我记得有哪些控件」）：
```python
for w in (card, inner, head, body, foot):          # 只覆盖这几个容器
    w.bind("<Button-1>", open_detail)
for w in body.winfo_children() + foot.winfo_children():   # 只覆盖一层子控件
    w.bind("<Button-1>", open_detail)
for w in head.winfo_children():
    ...
```
但卡片里还有 `chip_row`（关键信息标签行，📍 地点 / ☎ 联系人），
它是 `inner` 的子控件、且**内部还有 Label** —— 二层嵌套，
两个循环都碰不到它。结果：**点关键信息标签那一块完全没反应**。
用户点到那里就以为「打不开」。

**修法**：别手写清单，**递归绑定**。
```python
def bind_all_click(widget):
    if widget is star_lbl:
        return                        # 收藏星要单独处理
    widget.bind("<Button-1>", open_detail)
    for child in widget.winfo_children():
        bind_all_click(child)

bind_all_click(card)                  # 一次搞定所有后代
star_lbl.bind("<Button-1>", toggle_star)   # 再覆盖回收藏星
```
好处：以后卡片里**再加任何一行都不用改绑定代码**，不会再漏。

**验证方式**（可复用）：递归遍历卡片内所有控件，
断言每一个都 `bind("<Button-1>")` 非空，并且实际 `event_generate` 一次。
本项目的 `scripts/probe_click.py` 就是干这个的 —— 修前 18 个控件里漏 4 个。

---

## 23. `bind_all("<MouseWheel>")` 会互相覆盖，且永不解绑 —— 详情里滚不动

**现象**：详情窗口打开了，但**正文滚不动，看不到全文**。

根因是 `bind_all` 的三个致命特性：
1. **它是全局的**（绑在 root 上），任何控件收到滚轮都会触发；
2. **后绑的会盖掉先绑的** —— 主信息流 `bind_all` 一次、收藏页又 `bind_all` 一次、
   详情窗口再 `bind_all` 一次，于是**最后绑的那个永远赢**；
3. **不会自动解绑** —— 详情窗口 `destroy()` 之后，滚轮事件还在往那个
   已经销毁的 canvas 上滚，主界面滚轮就彻底废了。

合起来的效果：打开详情后，滚轮滚的是**背后那个隐藏的信息流**，
详情自己的内容纹丝不动 → 表现就是「看不到全文」。

**修法**：不要用 `bind_all` 抢全局，改成「**单一全局处理器 + 显式归属**」：
```python
# 初始化
self._active_canvas = None    # 主界面当前页的 canvas
self._wheel_target  = None    # 非空时（详情开着）滚轮只作用于它

# root 上只绑一次
self.root.bind_all("<MouseWheel>", on_wheel, add="+")

def on_wheel(event):
    target = self._wheel_target or self._active_canvas
    if target is None or not target.winfo_exists():
        self._wheel_target = None                 # 目标没了就交还
        target = self._active_canvas
    target.yview_scroll(-1 * (event.delta // 120), "units")
```
开详情时 `self._wheel_target = detail_canvas`；关闭时置回 `None`。
**并且一定要挂 `<Destroy>` 兜底** —— 用户可能用 Alt+F4、点任务栏关闭，
不一定走你那个「关闭」按钮：
```python
win.bind("<Destroy>", lambda e: setattr(self, "_wheel_target", None)
         if e.widget is win else None, add="+")
```

**验证**：真窗口（不能 `withdraw()`，否则 canvas 高度算成 1，测不准）下断言
`yview()` 在滚轮前后发生变化、能到 `1.0`、且主界面 `yview()` 保持不动。
见 `scripts/probe_scroll2.py`。

---

## 24. 验收脚本给 AI 接口的超时太短，会把「模型排队」判成「接口坏了」

`verify_all.py` / `smoke_desktop.py` 原来对 `/brief/today` 用默认 15~20 秒超时。
但这两个接口**会调 GLM**，而免费档并发为 1、限流时要退避 5→10→20→40 秒，
一次请求正常就可能花掉一两分钟 → 脚本直接 `TimeoutError` 崩掉，
看起来像「后端坏了」，实际后端好得很。

**修法**：
- 给 AI 接口单独一个宽裕超时（本项目用 240 秒）+ 重试 2 次；
- 捕获顶层异常，**打印可读结论**而不是甩 traceback，
  并明确提示「这是免费档排队，可隔几分钟重跑，或临时清空 AI_API_KEY 走本地摘要」；
- **不要把超时算成 PASS** —— 那是自欺欺人。

另外：`generated_by=local`（降级）**应当算通过**。
项目的铁律就是「AI 不是必需依赖」，降级到本地规则是**设计行为**，
不是失败。断言里要写 `in ("local", "glm-4.7-flash")`。

---

## 25. 截图脚本抓到「僵尸窗口」—— 截图看着永远没变

**现象**：改完代码截图，文件大小和哈希**每次都一模一样**，
图里还是旧界面（甚至显示「共 0 条」的空态），让人误以为界面有 bug。

根因：上一次截图脚本**中途崩了**（`NameError: vw is not defined`），
没执行到收尾的 `gui.terminate()` —— 那个 Tk 窗口就**一直活着**，
并且占着「知了」这个窗口标题。之后每次枚举窗口找到的都是这个**旧窗口**，
截的当然是陈旧画面。

**修法**（三条一起上）：
1. **开场先清残留**：截图前 `tasklist` 找出所有 `pythonw.exe` 全杀掉；
2. **只认自己的 PID**：用 `GetWindowThreadProcessId(hwnd)` 反查窗口归属，
   只接受本次 `Popen` 出来的那个 PID，杜绝抓到别人的窗口；
3. **收尾放 `finally`**：中间任何一步炸了也要 `gui.terminate()` / `kill()`，
   不给下一次留雷。

**顺带记录**：抓取前把鼠标挪到屏幕角落（`SetCursorPos`），
否则光标停在下拉框上会触发 hover 高亮，截出来的图里第一个选项看着像「已选中」，
极易误导排查方向。

位置：`scripts/capture_shot.py`

---

## 26. 收藏页的 `holder` 建了却没用 —— 整页永远是白的

**现象**：左侧点「我的收藏」，页面一片空白，一条都没有。
但后端 `starred_only=true` 明明能查到数据。

根因：`_build_starred_page()` 老老实实建了 `self.star_canvas` 和
`self.star_holder`，注释还写着「复用信息流卡片区（同一套渲染，换一个 canvas）」，
—— **但 `_render_cards()` 里写死的是 `self.card_holder`**。
于是切到收藏页时，卡片被画进了**信息流页那个隐藏的 holder**，
收藏页自己的 holder 永远空着。数据和接口全对，纯渲染目标错位。

**修法**：显式维护「当前渲染目标」这一对，切页时一起换：
```python
if key == "starred":
    self.render_canvas, self.render_holder = self.star_canvas, self.star_holder
else:
    self.render_canvas, self.render_holder = self.canvas, self.card_holder
```
然后 `_render_cards` / `_render_message` / `_build_card` 一律用
`self.render_holder`，不再出现具体的 canvas 名字。

**教训**：凡是「多个页面共用一套渲染函数」的结构，
渲染函数里**不能出现任何一个具体页面控件的名字**。
出现了就说明这个抽象漏了，早晚会有一页是白的。

---

## 27. 筛选状态跨页泄漏 —— 从收藏页切回信息流，只剩收藏那几条

**现象**：点「我的收藏」再点回「信息流」，列表里只剩收藏的那几条。

根因：`switch_page("starred")` 设了 `self.only_starred = True`，
但切回 `feed` 时**没有任何分支去把它设回 False**（`if/elif` 链里根本没有
`feed` 这一支）。于是「收藏」这个筛选条件跟着用户回了信息流页。

**修法**：每个页面进入时显式声明自己的筛选口径，别指望别的页面帮你清理：
```python
if key == "feed":
    self.only_starred = False
    self._restore_feed_filters()      # 恢复离开前的筛选
elif key == "starred":
    self.only_starred = True
    self.only_unread = self.only_read = self.only_pinned = False
    self.keyword = ""
```
离开 `feed` 时用 `_save_feed_filters()` 把它自己的筛选存起来再走。

**教训**：`if/elif` 链式的页面切换里，「状态变量」要**每页都写一遍**，
不能只写「进入时要设成 True」而忘了「离开时谁把它关掉」。
这种 bug 不会报错，只会静默给出错误的列表。

---

## 28. 验收脚本自己写错，比产品 bug 更费时间

两个真实例子（写 `probe_flags.py` 时踩的）：

**(1) 用「有没有绑定」判断「绑的是不是对的东西」**
```python
check("收藏星没有被绑上「打开详情」", not star_lbl.bind("<Button-1>"))   # ❌
```
收藏星**本来就绑着** `toggle_star`，所以这个断言必然失败 —— 但产品是对的。
应该做**功能性**判断：把各个 handler 换成记录器，真的 `event_generate`
一下，看触发的是哪个函数：
```python
app.open_detail = lambda it: fired.append("open_detail")
star_lbl.event_generate("<Button-1>")
assert fired == ["toggle_star"]        # ✅ 这才是「没被劫持」的证据
```

**(2) 直接调 `toggle_read()` 去测勾选框**
`toggle_read` 的设计是「读勾选框的当前值当用户意图」。
直接调用它时勾选框**并没有被翻过**，于是它把「未读」标成「未读」，
什么也没发生 —— 脚本报一堆 FAIL，产品其实完全正常。
应该用 `chk.invoke()`，那才是真实点击（翻变量 + 触发 command）。

**教训**：
- 测 UI 事件，要**复现用户的操作路径**（`invoke()` / `event_generate`），
  不要绕过控件直接调内部函数；
- 断言的**性质**要和想验证的**性质**一致。
  「有没有绑」和「绑得对不对」是两回事；
- 断言失败时先问「是不是我测错了」—— 本例中 9 个 FAIL 全是脚本自己的问题。

顺带一个测试强度问题：置顶测试原本挑「当前第一条」去置顶，
它本来就排第一 → 「置顶后排到最前」永远成立，**测了等于没测**。
改成挑第 20 条，并额外断言 `pos_before > 0`。
**设计用例时要让「不修 bug 就会失败」**，否则它只是个装饰。

---

## 29. 批量接口把基础条件写死成单向，反方向静默失效

**现象**：`POST /notifications/read-all` 传 `{"ids": [74], "is_read": false}`
想「标为未读」，返回 `updated=0`，状态没变；验收脚本里表现为
「批量后状态正确」FAIL。

根因：
```python
stmt = update(Notification).where(Notification.is_read.is_(False))   # ❌ 写死
...
stmt = stmt.values(is_read=body.is_read)
```
基础条件写死成 `is_read == False`（只捞未读的），
当目标值是 `False`（标为未读）时，**没有一行满足 `is_read == False` 且又需要改成 False**，
于是永远 0 行；而且 `updated` 恒为 0，看起来像接口没生效。

**修法**：基础条件应该是「**状态与目标相反**」，这样两个方向都成立：
```python
stmt = update(Notification).where(Notification.is_read.is_(not body.is_read))
```
改完两个方向都能用，`updated` 也重新有了意义
（＝真正发生变更的行数，再跑一遍自然是 0）。

**教训**：凡是带 `is_read: bool` 这种「目标值可正可反」的批量接口，
`WHERE` 条件一定要由目标值推导，不要写死其中一个方向。
写死的那一半不会报错，只会在用户点「取消」时静默什么都不做。

位置：`backend/app/api/routes/notifications.py::mark_all_read`
回归用例：`backend/tests/test_user_flags.py::test_bulk_can_also_mark_unread`

---

## 30. 截图整图红蓝互换：GDI 是 BGRA，PNG 是 RGBA

**现象**：截图看着「没错」—— 只是整体颜色偏暖。界面上蓝色的主色
（`SEED = #2F6BFF`）在截图里变成橙色 `#FF6B2F`；导航高亮、logo、
「未读」胶囊全从蓝变红橙。因为中性色（白/灰/黑）不受影响，
只有饱和色出问题，所以肉眼很容易当成「设计就是这样」。

**根因**：`GetDIBits` 取到的 32 位 DIB，内存里每个像素是
**B, G, R, A**（little-endian 的 `0x00RRGGBB`）；而 PNG 的
color type 6 规范顺序是 **R, G, B, A**。原来的代码把原始字节
直接当 RGBA 写进 PNG：

```python
rows = [b"\x00" + raw[(y * vw + x0) * 4: ...]]     # ❌ raw 是 BGRA
```

**定位证据**（关键是拿到**精确**匹配，而不是「看着差不多」）：

| 源码常量 | 文件里实际存的值 | 关系 |
|---|---|---|
| 已读状态条 `#D5DAE5` | `#E5DAD5` | R↔B 互换 |
| 已读卡片底 `#F7F8FA` | `#FAF8F7` | R↔B 互换 |
| 置顶卡片底 `#FFFAF0` | `#F0FAFF` | R↔B 互换 |

`#D5DAE5` → `#E5DAD5` 这种**逐字节精确互换**才能证明是色序问题，
否则只说明「颜色不对」。

**修法**：逐行把第 0 和第 2 字节对调。
```python
line = bytearray(raw[(y * vw + x0) * 4:(y * vw + x0) * 4 + w * 4])
line[0::4], line[2::4] = line[2::4], line[0::4]     # B <-> R
rows.append(b"\x00" + bytes(line))
```

**为什么「读回像素」这件事本身也有坑**（三个都踩过）：
- 托管 venv 和便携运行时都**没装 Pillow**；
- Tk 的 `PhotoImage.get()` 在本机返回的也是 **BGR** 顺序 ——
  用它复核会把「已修好」看成「还是错的」；
- PowerShell 的 `Add-Type`（System.Drawing）被本机安全策略**拦截**。

所以最终写了个只用标准库 `zlib` 的最小 PNG 解码器
`D:\QuanZhi\scripts\png_pixel.py`（IHDR + 5 种 filter 全实现），
它按规范逐字节解释文件，是唯一可信的第三方裁判。

**教训**：任何「自己拼 PNG / 自己拼位图」的代码，都要先问一句
「我这串字节的通道顺序和格式规范一致吗」。
验证时必须用**独立的解码器**，不能拿同一个可能出错的工具自我验证。

位置：`D:\QuanZhi\scripts\capture_shot.py::grab`（另有两份拷贝在
`shot_detail.py` / `shot_scroll.py`，已一并修）

---

## 31. 截图进程自己不感知 DPI —— 只截到窗口左上角一块

**现象**：修完第 30 条之后截图颜色对了，但**窗口右侧整块不见了**：
搜索框、`清空筛选`、右上角「后端在线」全没了。
截图尺寸 1707x1033，而窗口实际是 2560x1550。

**根因**：本机 150% 缩放。截图进程**不感知 DPI** 时，
`GetSystemMetrics` / `GetWindowRect` 返回**虚拟坐标**
（2560x1600 被报成 1707x1067），而 `GetDC(0)` + `BitBlt` 拷的是
**物理像素**。两者单位不一致 → 只拷到左上角 1707 物理像素。

**注意别误判**：一度以为是被截的**应用**没做 DPI 感知（即坑 8 失效）。
实测否掉了这个假设：
```
探针自身已 DPI 感知：屏幕 = 2560x1600
  进程 DPI 感知 = 1 (SYSTEM_DPI_AWARE（清晰）)      ← 应用是对的
  窗口矩形 = 1262 x 876
```
应用是好的，坏的是**截图脚本自己**。

**修法**：截图脚本在任何 GDI 调用之前自己先感知 DPI。
```python
ctypes.windll.shcore.SetProcessDpiAwareness(1)
```

**教训**：跨进程截图时，「谁量」和「谁拷」必须在**同一个 DPI 上下文**中。
一半感知一半不感知，就会出现「量出来的框」和「拷出来的像素」错位，
而它表现得像个 UI bug（控件不见了），极易往错的方向查。
诊断脚本：`scripts/probe_dpi.py`（设置时机）、`scripts/probe_dpi_live.py`
（量运行中进程的真实 awareness 与窗口尺寸）。

位置：`D:\QuanZhi\scripts\shot_detail.py` / `shot_scroll.py` 顶部

---

## 32. 「滚到底」的两张截图字节完全相同 —— 图像存在 ≠ 证据成立

**现象**：`shot_scroll.py` 正常跑完，`detail_top.png` 和
`detail_bottom.png` 都生成了，看着都「有内容」。
但两个文件的 **md5 完全一样**，逐字节相同。

**根因**：详情窗原先是 `ShowWindow(dw, 3)`（最大化）。本机物理分辨率
2560x1600，最大化后详情窗有 1600px 高，**正文一屏就装下了** ——
没有可滚动区域，滚轮怎么滚画面都不变。
（在 920x760 的小窗口下内容 908px > 可视 557px，是能滚的；
换到大屏最大化后就滚不动了。**测试环境变了，结论就失效了**。）

**修法**：
1. 显式给详情窗一个较小的尺寸，保证内容真的溢出：
   ```python
   user32.SetWindowPos(wt.HWND(dw), wt.HWND(HWND_TOPMOST), 120, 90, 1100, 780, 0)
   ```
2. 加断言：两张图的哈希**必须不同**，否则直接报「没滚动起来，
   截图不能作为证据」。

**教训**：一份「证据」要能**失败**才算证据。图能生成、看着有内容，
不代表它证明了你想证明的事。凡是「A 之后拍一张、B 之后拍一张」
的对比型证据，都要显式断言**两张图不一样**。

位置：`D:\QuanZhi\scripts\shot_scroll.py`

---

## 33. 靠 `SetForegroundWindow` 抢前台，然后注入真实鼠标点击

**现象**（前面几轮反复出现）：截图截到的是 Microsoft Store、
是 IDE、是桌面，而不是自己的窗口。

**根因**：`SetForegroundWindow` 会被 Windows 的**前台锁**
（foreground lock）静默忽略 —— 返回成功但窗口没到前台。
而截图/点击都建立在「我的窗口在最前面」这个假设上：
`GetDC(0)` + `BitBlt` 抓的是**屏幕上实际显示的内容**，
窗口没到前台，抓到的就是盖在它上面的那个程序。

**修法**（三层）：
1. **抬层级而不是抢焦点**：`SetWindowPos(hwnd, HWND_TOPMOST, ...)`。
   它走窗口管理器层级，和前台焦点无关，可靠得多。
   ⚠️ 必须声明 argtypes，否则 `HWND_TOPMOST = -1` 这个**指针值**
   会被当成 32 位 int 传、零扩展成 `0x00000000FFFFFFFF`，置顶静默失效：
   ```python
   user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint]
   user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002)
   ```
2. **点击前先验票**：注入真实鼠标点击前，用 `WindowFromPoint`
   确认该坐标上最顶层的窗口确实属于本程序，否则**放弃点击**。
   不加这一步的话，一旦置顶失败，那一下会点到用户的别的程序上
   （会真的动他的鼠标和键盘焦点）。
   ```python
   top = int(user32.WindowFromPoint(wt.POINT(x, y)) or 0)
   if pid_of(top) != gui.pid:
       print("!! 该坐标上不是本程序的窗口，放弃点击"); sys.exit(1)
   ```
3. **找窗口按 PID 找**：`EnumWindows` 里用
   `GetWindowThreadProcessId` 只认自己的 PID，并在多个命中里
   取**最宽**的那个（Tk 启动期会短暂出现 158x26、237x39 等小窗口）。
   另外，`find_win("通知")` 会**把主窗口也算进去** ——
   主窗标题是「知了 · 只开一个窗口，看全学校所有通知」，里面就含「通知」，
   于是会把主窗口当详情窗口截下来。改成「枚举本进程所有有标题的窗口
   再排除主窗口」。

**教训**：任何「拿屏幕上的像素当证据」的自动化，都必须先**证明**
自己的窗口真的在前面。`SetForegroundWindow` 不是可靠的前提条件。
另外，模拟点击会操作**用户的真实鼠标**，动手前一定要验票。

位置：`D:\QuanZhi\scripts\shot_detail.py` / `shot_scroll.py` / `capture_shot.py`

---

## 34. 截图脚本会悄悄改用户数据（"打开详情自动标已读" × 注入点击）

**现象**：数据库里「已读」从 1 条变成了 2 条 —— 多出来的那条
（id=54，《关于2026年下半年后勤处领导接待日安排的通知》）没人主动标过。

**追查过程**（比结果更重要）：
1. `SELECT id, is_read ... WHERE is_read=1` → 多出 id=54；
2. 翻后端日志 `logs/backend.log`，找到一条**孤立**的
   `POST /api/notifications/54/read`，前后只有
   `GET /api/notifications/54`，**没有配对的反向请求**；
3. 对照源码发现 `知了桌面版.py::open_detail` 里有：
   ```python
   data = http_json(f"/notifications/{nid}")
   detail = data.get("data") or item
   if nid and not detail.get("is_read"):
       http_json(f"/notifications/{nid}/read", method="POST", payload={})
   ```
   —— **打开详情会自动标记已读**（同邮件客户端）。这也解释了为什么
   打开本来已读的那条（id=74）不会产生额外请求。

于是因果链闭合：截图脚本注入真实鼠标点击 → 打开了一条**未读**通知 →
桌面版自动标已读 → 脚本没还原 → 用户数据被改。

**根因（结构性问题）**：三个截图脚本各写各的"快照/还原"，
`shot_flags.py` 写了（因为它要演示置顶和勾选），
`shot_detail.py` / `shot_scroll.py` 没写（因为它们的作者只把它们当成
"只读的截图工具"，没意识到**注入点击本身就会写数据**）。

**修法**：
1. 把快照/还原收敛成**唯一一份实现** `scripts/user_flags.py`
   （`snapshot()` / `diff()` / `restore()` / `verify()`），
   三个脚本全部调用它 —— 消除"各写各的"这个根因。
2. 快照用 `sqlite3.connect(f"file:{DB}?mode=ro", uri=True)` **只读**打开，
   不参与写锁，避免和后端（uvicorn）抢库。
3. `restore()` 只改**发生变化的行**（用 `diff()` 算），
   不整表回写，避免误伤。
4. 还原时点：必须在**后端关闭之前**（还原是调接口做的）；
   另外注册 `atexit` 兜底 —— 中途异常也不会留下脏数据。
   曾经 `shot_flags.py` 把还原放在 `bk.terminate()` 之后，
   靠"端口还没释放"侥幸成功，属于碰运气。
5. 还原后再 `verify()` 一次，断言与快照完全一致。

**实测证据**：故意制造一处改动（`POST /notifications/54/read`，
`diff` 精确报出 `[(54, 'is_read', False, True)]`）→ `restore` 还原 1 处 →
`verify` 通过 → 且**没有**动到本来就是已读的 id=74。

**教训**：
- 「只读的自动化」是个幻觉。**任何会注入输入/点击的工具都可能写数据**。
  判断标准不是"我调的是不是 GET"，而是"我点下去之后产品会做什么"。
- 同一件事出现三份实现，就会出现「有的做了、有的没做」。
  要么抽成一份，要么在评审时逐个确认。
- 残留数据要能被**证伪**：只看到"总数变了"不够，
  要能在日志里找到那条孤立请求、并在源码里找到对应的自动行为。

位置：`D:\QuanZhi\scripts\user_flags.py`（共用实现）、
`shot_flags.py` / `shot_detail.py` / `shot_scroll.py`（调用方）

---

## 35. 屏幕休眠 / 会话锁定时，桌面抓图整块全黑（而且照样报「完成」）

**现象**：`shot_flags.py` 正常跑完、正常打出「=== 完成 ===」，
但产出的 PNG 只有 9.8KB，**整张纯黑**。

**定位**：
```python
r = wt.RECT(0, 0, 400, 300)
rows, w, h = cs.grab(r)      # GetDC(0) + BitBlt
Counter(im.rgb(x, y) ...)    # → {((0,0,0), 1200)}   唯一色数 = 1
```
屏幕尺寸还正常报 2560x1600（本进程是 DPI 感知的），但抓到的**全黑** ——
会话已锁定 / 显示器已休眠，`GetDC(0)` + `BitBlt` 拿的是
「屏幕上此刻显示的内容」，此时什么都没显示。

**根因**：`BitBlt` 抓屏幕这条路**天生依赖屏幕是活的**。

**修法**：主路径改用 `PrintWindow`，让窗口**自己重绘**：
```python
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, ctypes.c_uint]
PW_RENDERFULLCONTENT = 0x00000002
if not user32.PrintWindow(wt.HWND(hwnd), mem_dc, PW_RENDERFULLCONTENT):
    return None          # 有些窗口不吃这一套，交给调用方兜底
```
它与 z 序、前台焦点、屏幕电源状态**都无关**。实测（同一时刻、屏幕全黑）：

| 方式 | 结果 |
|---|---|
| `GetDC(0)` + `BitBlt` | 400x300 抽样 1200 点全 `(0,0,0)` |
| `PrintWindow(PW_RENDERFULLCONTENT)` | 三条色带取色精确为 `#2F6BFF` / `#FFFAF0` / `#F7F8FA` |

**两个细节**：
- Tk 的 `winfo_id()` 给的是**客户区子窗口**，要
  `GetAncestor(hwnd, GA_ROOT)` 往上找到带标题栏的顶层窗口再 PrintWindow；
- `PrintWindow` 出来的图，原点 = 窗口矩形左上角，
  所以控件坐标要换算：`图内 x = winfo_rootx() - rect.left`。

位置：`D:\QuanZhi\scripts\capture_shot.py::grab_window`

---

## 36. 用坏图覆盖好图（坏结果必须失败，而不是覆盖好结果）

**现象**：承接第 35 条 —— 那一次把一张 **165KB 的正常演示图**，
覆盖成了 **9.8KB 的纯黑图**。而且脚本一路「成功」退出，
如果不看文件大小根本发现不了。

原来的代码是：
```python
if looks_blank(rows, w, h):
    cs.log("!! 截到的是纯色，窗口可能没渲染出来")   # ← 只打了一行日志
cs.write_png(OUT, rows, w, h)                      # ← 然后照样覆盖
```

**修法**：检测到纯色就**不写正式文件**，改存 `*.solid.png` 供排查，
并以非零退出码报告：
```python
if cs.looks_solid(rows, w, h):
    bad_path = OUT.with_name(OUT.stem + ".solid.png")
    cs.write_png(bad_path, rows, w, h)
    cs.log(f"!! 纯色图，本次不写 {OUT.name}，改存 {bad_path.name}；原文件保持不变")
    return 3
cs.write_png(OUT, rows, w, h)
```

**教训**：自动化的产出**宁可失败，也不要覆盖掉已有的好产出**。
「检测到异常 → 打一行日志 → 继续覆盖」是最糟的组合：
日志没人看，好数据没了。

位置：`D:\QuanZhi\scripts\shot_flags.py::main`

---

## 37. ★ 验收脚本对着「残留的旧后端」跑，全绿也毫无意义

**现象**：修复过程中发现数据库里多出一条不该有的「已读」（第 34 条），
顺手查 `logs/verify_backend.log` 时看到了：

```
ERROR: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8765)
```

一共 **6 条**。也就是说：`verify_all.py` 自己启的那个 uvicorn
**根本没绑上端口就退出了**，而它的 `/health` 探测照样成功 ——
因为有一个**残留的旧后端**在应答。
**那一轮测试仍然显示「38 通过 / 0 失败」。**

**根因**：验收脚本的启动逻辑是「起后端 → 轮询 `/health` 直到有响应」。
这个判据**只能证明"有人应答"，不能证明"应答的是我启的那个"**。
端口被占时，自己的后端死了，测试全程在跟**旧版本代码**对话。

**修法**（两步，缺一不可）：
1. **启动前**确认端口是空的：
   ```python
   def port_in_use(port, host="127.0.0.1", timeout=0.8) -> bool:
       with socket.socket() as s:
           s.settimeout(timeout)
           return s.connect_ex((host, port)) == 0
   ```
   占用则打印人话提示并 `sys.exit(2)`（实测已拦下）。
2. 五个会自己拉后端的脚本（`verify_all` / `probe_flags` /
   `shot_flags` / `shot_detail` / `shot_scroll` / `capture_shot`）
   统一调用 `user_flags.guard_port(8765)`，把这一步收敛成一份实现。

**根治**：修完在干净端口上重跑，并在日志里确认
`Application startup complete` 出现、且该次运行之后 `10048` 计数为 0 ——
这样报出来的数字才真正来自当前代码。

**教训**：「等到有响应」永远不能作为「连上了我的服务」的证据。
**要先证明端口是空的，再证明自己的进程还活着**，
否则你测的可能是任何一个恰好占着这个端口的旧进程。

位置：`D:\QuanZhi\scripts\user_flags.py::port_in_use / guard_port`，
调用方见上

---

## 38. ★ 推送通道「发成功」不等于「可以消费掉」—— local 通道会让推送静默失效

**现象**：`PushService` 每次推送后要把通知标成「已推送」（`notified_at`）。
本地通道（桌面版气泡 / 手机端轮询）的 `send()` 实现只是「把消息留在待拉取队列里」，
它返回成功。于是推送循环把这条消息标成 `notified_at` → **下一轮它就不在队列里了**。
客户端每次去拉，拿到的都是空数组。
**服务端日志显示全部成功，客户端一条都收不到。**

**根因**：把「通道调用没报错」当成了「这条消息已经送达」。
对 webhook / FCM 这类**外部通道**，发出去就等于送达，可以消费；
对**本地队列通道**，发出去只是「放在那儿等人来取」，消费掉就等于删了。

**修法**：给通道加一个显式属性，别让调用方猜：

```python
class PushChannel:
    #: 这个通道成功发送，是否代表「通知已经送达、可以从队列移除」
    consume: bool = True

class LocalChannel(PushChannel):
    is_external = False
    consume = False          # ← 只入队，不消费
```

标记逻辑相应改成分开计数：

```python
consuming_ok = any(r.ok for r in results if r.consume)
queued       = any(r.ok for r in results if not r.consume)
if consuming_ok:   row.notified_at = now_utc()   # 外发成功 → 消费
elif queued:       stats["queued"] += 1          # 只进队列，notified_at 保持 NULL
else:              row.notify_error = ...; stats["failed"] += 1
```

**怎么证明这个防护是真的**：做了变异测试 —— 把 `LocalChannel.consume` 改回 `True`，
**恰好 4 个测试失败**（`test_local_channel_does_not_consume_queue`、
`test_baseline_does_not_run_inside_push_pending`、`test_channels_endpoint`、
`test_quiet_hours_only_blocks_external`）。改回去，120 项全绿。
如果改坏了测试还全绿，说明那些断言是空的。

**教训**：涉及「已读 / 已送达 / 已处理」这类**不可逆的标记**时，
先问一句「这个成功信号，是谁的成功？」—— 通道的成功和业务的结果不是一回事。

位置：`app/services/push.py::PushChannel.consume` / `PushService.push_pending`

---

## 39. Webhook 返回 HTTP 200，但业务是失败的

**现象**：企业微信、钉钉、Server 酱这类 webhook，
**请求失败时也返回 HTTP 200**，真正的错误在响应体里：

```json
{"errcode": 93000, "errmsg": "invalid webhook url"}
```

只检查 `resp.status_code == 200` 的话，会把「发送失败」记成「发送成功」，
然后把通知标成已推送 —— 消息就这么丢了，而且日志一片祥和。

**修法**：成功判定要看**两层**：

```python
def _webhook_business_error(resp) -> str | None:
    try:
        body = resp.json()
    except Exception:
        return None                       # 不是 JSON 就不管，交给状态码判断
    for key in ("errcode", "code", "status"):
        if key in body:
            val = body[key]
            if isinstance(val, int) and val != 0:
                return f"{key}={val} {body.get('errmsg') or body.get('message') or ''}"
    return None
```

**教训**：第三方 webhook 的「成功」几乎从来不是 HTTP 状态码能表达的。
接一个新通道时，第一件事是查它**失败时长什么样**。

位置：`app/services/push.py::_webhook_business_error`

---

## 40. ★ 时间窗过滤必须下推到 SQL —— 否则游标永远卡在一条不在窗口里的记录上

**现象**：客户端用 `?since_id=N` 拉新消息，服务端返回 `id > N` 的消息。
同时还有一个「只看最近 48 小时」的时间窗限制。
一开始时间窗是在 Python 里过滤的：

```python
rows = (await session.execute(
    select(Notification).where(Notification.id > since_id).order_by(Notification.id)
)).scalars().all()
rows = [r for r in rows if _eligible(r)]      # ← 过滤在内存里做
```

结果：如果按 id 升序取到的一条**不在时间窗内**，它会被 Python 过滤掉，
但客户端的游标推进是「这一批里最大的 id」—— 那一批是空的，
游标**永远不动**，后面所有消息都拉不到。**推送彻底停摆，且不报任何错。**

**修法**：把过滤条件做成和 `_eligible()` **语义完全相同**的 SQL 子句，
下推到查询里，让「不在窗口内的行」根本不进入结果集：

```python
def _eligible_clause(self):
    ts = func.coalesce(Notification.published_at, Notification.created_at)
    conds = [Notification.notified_at.is_(None), ts >= cutoff]
    ...
```

**教训**：游标分页 + 过滤条件组合时，过滤**必须**在数据库侧完成。
「先取一批再筛」的做法在游标场景下一定会卡死 —— 因为被筛掉的行也在推进位置。

位置：`app/services/push.py::_eligible_clause`

---

## 41. 首次推送基线必须放在「应用启动时」，不能塞进 push_pending

**现象**：库里已经有 153 条历史通知。如果什么都不做，
用户第一次打开推送，153 条会一起弹出来。
直觉的做法是：在推送函数里判断「如果是第一次，就先把现有的都标成已推送」。
**这是错的。**

**根因**：抓取和推送是同一个定时任务的两步。
如果基线建立在 `push_pending()` **内部**，那么某一次运行里
「刚抓进来的新通知」也会被算进「现有的」而一起标掉 ——
用户会看到「订阅成功了，但之后什么都收不到」。

**修法**：基线放在**应用启动**时（FastAPI lifespan），
和抓取完全分开；用一个持久化标记保证只做一次：

```python
if settings.push_enabled and settings.push_silent_first_run:
    async with SessionLocal() as session:
        res = await PushService(session, settings).ensure_baseline()
    if res["done"]:
        logger.info("推送基线已建立：%d 条历史通知不会再被推送", res["marked"])
```

实测（拷贝真实库）：`baseline_count=153`，
`/push/pending` 返回 **0 条**，启动过程中 webhook 请求 **0 次**；
之后新插一条 id=154，队列里立刻只有它。

**教训**：「第一次」这种状态，要在**生命周期里明确的时刻**处理，
不要藏在「每次都会跑的例行函数」里 —— 那里的「第一次」判断依赖调用顺序，
而调用顺序会随着功能增加而改变。

位置：`app/main.py`（lifespan）、`app/services/push.py::ensure_baseline`

---

## 42. 幂等 ADD COLUMN 迁移：类型名要按方言给

**现象**：给已有的库加字段，用的是「检查列是否存在 → 不存在就 ALTER TABLE ADD COLUMN」。
用 SQLite 开发时一切正常；换 PostgreSQL 起不动 ——
**服务器看起来「没起来」，但其实是在 `init_db` 阶段就抛了异常。**

**根因**：SQLite 和 PostgreSQL 的时间列类型名不同：
SQLite 认 `DATETIME`，PostgreSQL 认 `TIMESTAMP`。
硬编码任意一种，另一种方言下就是语法错误。

**修法**：DDL 里留一个占位符，按连接方言填：

```python
_EXTRA_COLUMNS = {
    "notifications": [
        ("is_pinned",   "BOOLEAN NOT NULL DEFAULT 0"),   # 两种方言都认
        ("notified_at", "{ts} NULL"),
        ("notify_error", "TEXT NULL"),
    ],
}

def _timestamp_type(conn) -> str:
    name = getattr(getattr(conn, "dialect", None), "name", "sqlite")
    return "TIMESTAMP" if name in ("postgresql", "postgres") else "DATETIME"
```

**验证方式**：`scripts/check_migration.py` 拿**真实库的副本**跑两次 `init_db()`，
对比前后 schema。实测：加 2 列 + 2 张表 + 1 个索引，**零数据丢失**，跑两次结果一致（幂等）。

**教训**：迁移代码要在**目标方言**上验证，不要用开发方言的通过当证据。

位置：`app/db.py::_ensure_extra_columns`

---

## 43. 多个测试文件各设一遍 `DATABASE_URL` —— 谁先 import 谁说了算

**现象**：后端全部测试单独跑某个文件都通过，一起跑就有 4 个报
`no such table: sources`。

**根因**：`app/db.py` 在**模块级**就建好了 engine。
所以「哪一个测试文件先 `import app.db`」决定了整个进程用哪个数据库。
两个文件各自设了不同的 `DATABASE_URL`，后一个设的**完全没用** ——
写进 B 库，读的却是 A 库。

第二个原因：建表只发生在 FastAPI 的 lifespan 里。
不走 `TestClient` 的测试（直接调 service 层）自然找不到表。

**修法**：把环境变量收敛到**一个** `tests/conftest.py`，
再用一个 session 级 autouse fixture 跑一次 lifespan：

```python
@pytest.fixture(scope="session", autouse=True)
def _ensure_schema():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app):
        yield
```

**验证**：把两个测试文件的执行顺序对调，都是 53 passed。

**教训**：**模块级副作用**（在这里是「模块级建 engine」）会让测试之间隐式耦合。
测试隔离问题不要靠「每个文件自己设一遍」来解决，那只是把冲突藏起来。

位置：`backend/tests/conftest.py`

---

## 44. 自己写 PNG 的时候，像素必须按 IHDR 里的通道数写

**现象**：截图脚本新增一个「多张图拼一张」的功能，
拼出来的文件用自己的解码器打开直接抛：

```
ValueError: 未知 filter 类型 246
```

246 是 `0xF6` —— 正好是背景色的 R 分量。

**根因**：写入函数固定用 **RGBA**（IHDR `color type=6`，4 字节/像素），
而拼接时我只写了 3 字节（RGB）。于是每行的**字节数比声明的少 1/4**，
解码器按 `w*4` 推进，读到的「filter 字节」其实是上一个像素的颜色值。

**修法**：跟着写入器的约定走，补上 alpha：

```python
scan.append(0)                       # 每行的 filter 字节
for r, g, b in row:
    scan += bytes((r, g, b, 255))    # ← 必须是 4 字节
```

**教训**：手写二进制格式时，「行距 / stride」算错**不会报越界**，
只会安静地错位。写之前先确定：IHDR 声明的通道数是多少？
解码端和编码端必须用**同一个**约定，最好在写入函数的 docstring 里写死。

位置：`D:/QuanZhi/scripts/shot_toast.py`

---

## 45. ★ Tk 的 `after` 不是线程安全的 —— 写在 `except` 里会把线程直接打死

**现象**：探针脚本里冒出一串

```
RuntimeError: main thread is not in main loop
  File "知了桌面版.py", line 664, in work
    self.root.after(0, lambda: self._start_poll(delay=5000, init_first=True))
```

注意**它在 `except` 分支里**。

**根因**：工作线程里直接调 `root.after(...)`。Tk 的 `after` 内部走
`createcommand`，**不是线程安全的**；只要主线程还没进入 `mainloop()`
（启动那一瞬间，或者用 `root.update()` 驱动的测试脚本里）就会抛。

**为什么这个坑特别毒**：那一句写在「请求失败 → 稍后重试」的兜底分支里。
于是链条变成：后端还没起来 → 请求失败 → 兜底分支调 `after` → **再抛一次** →
异常冒出 `work()` → 线程死亡。表现是
**「后端明明起来了，通知永远不来」，而且不打任何错误日志**。

**修法**：线程只往队列里放东西，主线程用 `after` 泵出来执行：

```python
def _ui_pump(self) -> None:
    while True:
        try:
            fn = self._ui_queue.get_nowait()
        except queue.Empty:
            break
        try:
            fn()
        except Exception:
            traceback.print_exc()
    try:
        self.root.after(40, self._ui_pump)
    except Exception:
        pass        # 窗口已销毁，停止泵

def _post(self, fn) -> None:
    """线程安全地把回调交给主线程执行。工作线程里请一律用它。"""
    self._ui_queue.put(fn)
```

改完全项目 **4 处**（游标初始化、轮询拉取、AI 问答、AI 自检）统一走 `_post`。
实测：探针里那条 `RuntimeError` 彻底消失，控件绑定数 23/22 与改前一致。

**教训**：**异常处理分支里发生的异常最危险** —— 它会绕过你精心写的重试逻辑。
凡是「兜底代码」，都要问一句：兜底代码自己失败了会怎样？

位置：`D:/QuanZhi/知了桌面版.py::QuanZhiApp._ui_pump / _post`

---

## 46. 通知气泡的堆叠方向：最新的一条必须贴角

**现象**：合成了三个气泡的演示图，看起来挺正常：
最新的在最上面，最早的在最下面（贴着屏幕右下角）。

**根因**：`_reflow()` 按 `self.alive` 的**插入顺序**从下往上摆，
于是**第一个弹出来的**占了贴角的位置，后来的都被顶到上方。
结果是「越新的通知离眼睛越远」，和 Windows 通知中心 / 微信的观感正好相反。

**修法**：倒序遍历：

```python
for alive in reversed(self.alive):
    ...
```

并在截图脚本里加一条断言钉住它（后弹的底边 y 必须更大），
免得以后又改回去 —— 这种「看着没坏」的顺序问题，
只有断言能防住。

**教训**：涉及「新 vs 旧」的排列，先明确约定「新的在哪一端」，
并把约定写成断言。靠肉眼看截图，方向反了也未必看得出来。

位置：`D:/QuanZhi/知了桌面版.py::ToastManager._reflow`

---

## 47. Windows 的 `netstat` 输出是 GBK，`text=True` 会炸在读取线程里

**现象**：用 `subprocess.run([...], capture_output=True, text=True)` 拿 `netstat -ano`，
报的是：

```
AttributeError: 'NoneType' object has no attribute 'splitlines'
```

上面还有一条被埋掉的真实原因：
`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbb in position 2`。

**根因**：中文 Windows 的 `netstat` 输出是 **GBK**。
Python 按 UTF-8 解码失败，而异常是在 `subprocess` 的**读取线程**里抛的 ——
主流程只看到 `stdout` 变成了 `None`，于是报出完全不相关的 `AttributeError`。

**修法**：先取字节，再按 GBK 容错解码（我们只要端口和 PID 两列，纯 ASCII）：

```python
proc = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                      capture_output=True, timeout=20)     # 不要 text=True
out = (proc.stdout or b"").decode("gbk", errors="replace")
```

**教训**：Windows 上跑系统命令，**默认不要用 `text=True`**。
另外，当异常信息看起来和现场不相干时，往上翻 —— 读取线程里的异常很容易被埋掉。

位置：`D:/QuanZhi/scripts/launch_probe.py::listening_pids`

---

## 48. ★ 验收脚本自己会弄脏用户数据：`smoke_desktop` 置过已读却从不还原

**现象**：`smoke_desktop.py` 的 A 部分有这两句：

```python
r1 = post(f"/notifications/{nid}/read", {})     # 置已读
s1 = post(f"/notifications/{nid}/star", {})     # 收藏
s2 = post(f"/notifications/{nid}/star", {})     # 取消收藏
```

收藏翻了两次回到原状，但**已读只单向置 true，从来没有还原**。
每跑一次，就有一条真实通知被悄悄标成已读。

**根因**：`verify_all.py` / `probe_flags.py` 都老老实实走了
`user_flags` 的快照 / 还原，唯独这个脚本漏了 ——
「各写一份」是这类遗漏的温床（第 34 条已经吃过一次同样的亏）。

**修法**：`main()` 里包一层统一的快照还原：

```python
before = uf.snapshot()
try:
    test_api()
finally:
    n = uf.restore(before, post=post)
    same = uf.verify(before)
    print(f"用户状态还原：{n} 处；与快照比对 = {'一致' if same else '仍有差异！'}")
```

修完实测输出：`用户状态还原：0 处；与快照比对 = 一致`。

**教训**：判断标准不是「我调的是不是 GET」，而是**「我点下去之后产品会做什么」**。
（第 34 条：桌面版打开详情会自动标已读，所以「注入点击去拍详情图」本身就是写操作。）
所有会改数据的验收路径，一律走同一份 `user_flags`。

位置：`D:/QuanZhi/scripts/smoke_desktop.py::main`

---

## 49. ★ 界面测试对着「用户自己开着的那个实例」跑 —— 三种假绿方式

**现象**：用户自己开着「知了」（GUI pid 30568 + 后端占着 8765）。
此时跑界面验收脚本，会以两种方式得到**假的通过**：

1. **探活假绿**：脚本固定探测 `127.0.0.1:8765/api/health`。
   8765 上跑着用户的后端，`/health` 有响应 → 即使脚本自己启的后端
   因为端口被占而**绑定失败退出**，也会被判成「后端就绪」。
2. **窗口假绿**：`launch_probe` 是「枚举所有可见窗口，标题里含『知了』就算」。
   用户那个窗口同样满足条件 —— 到底测的是不是刚启动的进程，根本不知道。

更坑的是 `pythonw` 被 `terminate()` 之后，它拉起来的 uvicorn 是**子进程**，
Windows 不会连带杀掉，于是又留下一个「残留的旧后端」（第 37 条）。

**修法**（三件事，缺一不可）：

1. **端口可指定，且启动前必须为空**：
   桌面壳加 `QUANZHI_PORT` 环境变量覆盖，验收脚本加 `--port`，
   并统一调用 `user_flags.guard_port(PORT)`。
   已改造：`verify_all` / `smoke_desktop` / `probe_flags` /
   `selfcheck_ui` / `probe_click` / `probe_scroll2` / `launch_probe`。
2. **窗口按 PID 认，不按标题认**：
   ```python
   user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
   if pid.value != target: return True      # 不是我的进程，跳过
   ```
3. **收尾把孤儿后端一起清掉**，并断言端口真的释放了：
   ```
   清理孤儿后端 pid: [23768]
   端口是否已释放: True
   RESULT: PASS
   ```

3. **第三种假绿：界面里的接口地址没跟着 `--port` 走。**
   `probe_flags.py` 起了自己的后端在 `--port`，但 `QuanZhiApp` 内部走的是
   **模块级的 `API` 常量**，脚本从没覆盖它 —— 于是一直在连**默认的 8765**。
   8765 上如果恰好有别的实例，脚本照样「通过」，但验证的是别人的后端。
   暴露方式很偶然：某次 8765 空着，脚本直接崩在 `views[0]`
   （`_views` 为空 —— 连不上后端就没数据），才想起来查这一层。
   修法：`exec_module` 之后补一句

   ```python
   m.API = f"http://127.0.0.1:{PORT}/api"
   ```

   ⚠️ 只有 `probe_flags.py` 漏了；`selfcheck_ui` / `probe_scroll2` /
   `probe_click` 本来就有。改完之后**在 8765 空着的情况下重跑通过**，
   才算是真的验过了。

**状态文件也必须隔离**（`QUANZHI_STATE_DIR`）：这些脚本会创建真正的
`QuanZhiApp`，它一旦轮询到新通知就会把游标写进 `state\desktop.json` ——
用户下次打开就**看不到**那条通知了（后果与第 50 条相同）。
已隔离：`launch_probe` / `verify_push` / `probe_push_live` / `probe_push_cursor` /
`probe_flags` / `selfcheck_ui` / `probe_scroll2` / `probe_click` / `shot_flags`。
验证方式：跑之前记下 `state\desktop.json` 的 mtime，跑完再比 —— 实测未变。

还有个连带坑：隔离目录是**空的**，App 等于首次运行，于是它会去拉队列里的通知，
而 `_on_push` 里会顺带 `load_feed(reset=True)` 把 `_views` 清空重建 ——
撞上「等 1.5 秒后检查卡片」就会看到 **0 张卡片**。
所以隔离副本里预置 `{"notify_enabled": false}` 把通知关掉：
这些脚本测的不是通知，不该被它干扰。

**教训**：「有没有人应答」和「是不是我的进程在应答」是两件事；
「有没有一个叫知了的窗口」和「我的进程画出窗口了没」也是两件事。
**验收脚本必须能证明「测的是本次启动的那份代码」，否则那 38/38 毫无意义。**

位置：`D:/QuanZhi/知了桌面版.py`（`PORT` / `STATE_DIR` 支持环境变量覆盖）、
`D:/QuanZhi/scripts/*.py` 的 `--port`

---

## 50. ★ 客户端游标初始化对齐 max_id —— 换台机器就永久漏通知

**现象**：在一个「全新的 state」上首次启动（把便携包拷到另一台电脑、
或者删掉 `state/` 目录想重置一下），**队列里已经存在的通知一条都不弹** ——
不报错、日志干净、`/push/pending` 也明明返回非空。

**复现**（`D:/QuanZhi/scripts/probe_push_cursor.py`，修之前）：

```
队列（since_id=0，升序）: total=1 max_id=159
   #159  关于图书馆静音舱启用线上预约使用的通知
启动后 state 里的游标 = 159  ready=True
屏幕上出现过的气泡文字：
   （一个气泡都没有）
[FAIL] 队列里的 #159 被弹出来了
```

**根因**：初始游标取的是后端返回的 `max_id`，之后只拉 `id > 游标`。
而队列里那些待推送的通知**正好**满足 `id <= max_id` —— 于是被整体跳过。

```
游标 = max_id = 159  ─┐
队列 = [#159]        ─┴─→  "159 > 159" 为假 → 永远拉不到
```

当初对齐 `max_id` 的动机是「别让首次启动炸出 153 条历史」。
但那个问题**不该由客户端游标来兜** —— 它已经由服务端的**启动基线**负责了
（见第 41 条）。两道防线做同一件事，第二道就会误伤。

**修法**：初始游标取「队列里最老那条的 id − 1」；队列为空时才退化成 `max_id`。

```python
# ⚠️ 必须带 since_id=0：不带 since_id 时后端按**发布时间倒序**返回，
#    items[0] 是最新的那条。拿它当起点会跳过所有更老的队列条目 ——
#    等于把同一个缺口换个地方再犯一次。
d = http_json("/push/pending?since_id=0&limit=1", timeout=10)
queued = (d.get("data") or {}).get("items") or []
start = max(0, int(queued[0]["id"]) - 1) if queued else int(data.get("max_id") or 0)
```

修完实测：同样场景正常弹出气泡，`probe_push_cursor.py` **3/3**；
`verify_push.py` 里「队列为空」那条分支行为与从前完全一致（**46/46**）。

**顺带发现的第二处（同样的味道）**：手机端 `poll()` 推进游标时写的是
`r.nextSinceId > since ? r.nextSinceId : r.maxId` —— 这个 `maxId` 兜底会
**自己造出跳号**。场景：抓取刚跑完、AI 还在串行摘要，新通知的
`ai_status` 还是 `pending`，进不了队列（后端过滤 `ai_status == done`），
此时 `items` 为空而 `maxId` 已经很大 → 游标被推到 maxId → 等这批摘要好，
游标早走过去了，**永远不推**。改成 `: since`（不推进），与桌面版保持一致。
（桌面版 `_on_push` 只认 `next_since_id`，本来就没有这个分支。）

**怎么防住它再回来**：静态断言 + 变异测试。
`app/scripts/check_dart.py` 加了三条游标安全断言，并逐个做了变异：

| 变异 | 结果 |
|---|---|
| 推进游标用 `maxId` 兜底 | ✓ 被抓住（退出码 1） |
| 初始化不带 `sinceId: 0` | ✓ 被抓住 |
| 初始游标退回对齐 `maxId` | ✓ 被抓住 |

⚠️ 第一版断言写的是「文件里有没有出现 `sinceId: 0`」——
**注释里也写了这个词**，于是变异 B 改坏了却不报（退出码 0）。
断言要匹配**调用形式**（`fetchPushPending(sinceId: 0`），不是裸词。

**教训**：两道防线防同一个问题时，先问清楚「谁是主责」。
主责之外的兜底往往会**静默吃掉**本该发生的行为。
另外，**「服务端返回了」不等于「客户端会处理」** ——
`/push/pending` 非空、和用户真的看到通知，中间隔着客户端的游标算术。

位置：`D:/QuanZhi/知了桌面版.py::_init_push_cursor`、
`quanzhi/app/lib/core/push_service.dart::_ensureCursor / poll`

---

## 51. 验收脚本的「干净起点」假设会被真实数据打破

**现象**：`verify_push.py` 报 **45/46**，失败项是
「历史通知一条都没进推送队列 实际返回 1 条」。

**第一反应是「刚才改坏了什么」** —— 但这次不是。查下去：

- 该断言查的是**后端队列**，客户端代码根本没参与这一步；
- 真库里 #159 的 `notified_at` 是 NULL（抓取时本地通道只入队不消费，
  见第 38 条），所以副本的队列里本来就有 1 条；
- 脚本标题写着「**[3] 首次基线**」，但它拿的是一个**已经建过基线**的库
  （`app_meta` 里有标记，`ensure_baseline` 直接返回跳过），
  所以 `baseline_count` 还是上一次的 `153` —— 测的和标题不符。

**修法**：拷贝真实库之后，把副本退回「还没启用过推送」的起点：

```python
con.execute("delete from app_meta where key = 'push_baseline_at'")
con.execute("update notifications set notified_at = null")
```

重跑：`baseline_count=159`（这次是真标记出来的）、队列 0 条、**46/46**。

**教训**：拿**真实数据**做验收，脚本就会继承真实数据的当前状态 ——
包括「角落里还留着一条待推送」这种和本次测试无关的细节。
断言要么显式声明前提（「本测试要求队列为空，先清空」），要么就把前提做出来。
指望真实库恰好处于某个状态，迟早会遇到反例。

**另外**：先确认「失败的是不是我刚改的东西」，再去改代码。
这次失败的那一段客户端代码压根碰不到 —— 花十分钟看清归属，
比回滚代码瞎试划算得多。

位置：`D:/QuanZhi/scripts/verify_push.py`（准备段）

---

## 52. ★★ 正文为空时仍调用 AI —— 模型会把标题「补全」成看起来很像真的假事实

**现象**（用户会直接踩到的那种）：抓进来的大工校级通知里有 7 条是公众号链接
（`mp.weixin.qq.com`，robots 是 `Disallow: /`），正文一律抓不到，`content_text` 为空。
但摘要照样生成了，而且**编得有模有样**：

```
标题：关于2026年中秋节、国庆节放假安排的通知
  1. 大连理工大学发布2026年中秋国庆放假安排
  2. 9月25日（周五）至10月7日（周三）放假调休     ← 原文一个日期都没有
  3. 10月8日（周四）正常上课，请提前安排行程        ← 也不存在
```

**这三行没有一句来自原文**，但用户看到会照着订票。

**根因**：`SummarizeService.summarize()` 只看 `self.ai.enabled`，不看正文有没有：

```python
text = normalize_text(content) or ""
if self.ai.enabled:          # ← 这里没有判空
    return await self._ai_once(...)
```

模型手上只有一行标题，被要求「输出 3 行摘要 + 关键信息」，于是它**按套路编**。
越规范的标题（「关于 X 的通知」）越容易被补全成完整的假通知。

**为什么这个 bug 特别危险**：它不是报错、不是空白，而是**高质量的错误信息**。
数据体检里 `ai_status=done`、3 行齐全、合规检查全过 —— 所有指标都是绿的。

**修法**：正文短于阈值就不进 AI 分支，直接用本地规则（第 2、3 行是诚实的兜底文案），
并把 `model` 标成 `local-no-content`，让统计能一眼看出这批是「没正文的」。

```python
_MIN_AI_CONTENT_CHARS = 30      # 30 字以下没有被摘要的价值

if len(text.strip()) < _MIN_AI_CONTENT_CHARS:
    logger.info("正文仅 %d 字，跳过 AI 改用本地规则：%s", len(text.strip()), title[:40])
    return SummarizeResult(
        summary=self._local_summarize(title=title, content=text, category_hint=category_hint),
        model="local-no-content",
    )
```

本地规则在空正文时输出的是：

```
1. 关于2026年中秋节、国庆节放假安排的通知
2. 原文信息较少，建议点开看全文
3. 原文信息较少，建议点开看全文
```

**不好看，但它不骗人。**

**历史和兼容**：已有的 33 条「空正文却有条理摘要」的条目全部标回 `pending` 重摘，
重摘后走本地规则（不调 AI，48 条几秒跑完）。全库现在 48 条 `local-no-content`。

**教训**：**「有输出」不等于「有依据」。**
只要存在「输入不足」的可能，就必须显式判空 —— 模型不会说「我不知道」，
它会给你一个最像答案的答案。

位置：`app/services/summarize.py::summarize`（`_MIN_AI_CONTENT_CHARS`）

---

## 53. 跨来源转发被存成多条 —— 同一篇通知在信息流里出现三次

**现象**：信息流里连着三条一模一样的通知：

```
关于征集大连理工大学盘锦校区“揭榜挂帅”学生攻关榜题的通知   ← 团委
关于征集大连理工大学盘锦校区“揭榜挂帅”学生攻关榜题的通知   ← 学生工作部
关于征集大连理工大学盘锦校区“揭榜挂帅”学生攻关榜题的通知   ← 校区门户
```

实测 100 条新通知里，有 4 条是这么来的（5 组重复、多 6 行）。

**根因**：`make_dedup_key()` **优先用 URL**：

```python
canon = canonical_url(url)
basis = canon if canon else f"{norm_title}|{date}"
```

三个部门各发一遍，URL 完全不同 → 三个 dedup_key → 各存一条。
标题+日期那条兜底只有在 `url` 为空时才走得到。

**修法**：入库时再按「标题（去掉所有空白）+ 发布日（到天）」判一次。
两条约束都是为了避免误杀：

```python
@staticmethod
def _title_day_key(title, published_at) -> str | None:
    t = re.sub(r"[\s\u3000]+", "", title or "")
    if len(t) < 10:          # 「通知」「公告」「名单」撞车太容易
        return None
    day = published_at.strftime("%Y-%m-%d") if published_at else ""
    return f"{t}|{day}"
```

- 标题短于 10 字 **不参与**跨源判重；
- 日期必须同一天（每年都发的「放假通知」是两件事）。

命中就 `stats["cross_dupe"] += 1` 并跳过，跑完打一条 info 日志。
`seen_td` 同时包含「库里已有的」和「本批刚插入的」，所以同一次抓取里
两个 URL 指向同一篇也会被挡住。

**测试**（`tests/test_cross_source_dedup.py`，4 条）：
跨来源跳过 / 同批次跳过 / 短标题不误杀 / 跨年同名不合并。
变异测试：把判断改成 `if False` → **恰好那 2 条「应该跳过」的变红**，
另外 2 条不受影响，说明断言打得准。

**历史数据**：库里已有的 6 行没删（删了会丢「这条也出现在团委」的信息，
而且批量删除风险高）。**只是今后不会再产生新的**。

**教训**：去重键的选择本身就是产品决策。
用 URL 判重最准，但它假设「一条通知只有一个 URL」—— 在「多部门转发」的
校园场景里这个假设不成立。

位置：`app/services/ingest.py::_upsert` / `_title_day_key`

---

## 54. `resummarize.py` 默认全量重跑 —— 「补待办」会顺手把已完成的也重算

**现象**：库里 104 条 `pending`，只想把没摘要的补上，于是跑 `resummarize.py`。
脚本第一步是 `update notifications set ai_status='pending' where content_text is not null`
—— 已经完成的 155 条被一起打回待办。要跑 259 条而不是 104 条，时间翻 2.5 倍。

**隐藏的第二个坑**：`--limit 3` 只限制「重置哪些行」，**不限制「跑多少条」**。
`summarize_pending` 只按**全局 pending** 取行，而库里本来就躺着 104 条 pending，
所以「冒烟跑 3 条」实际跑了整整一批 10 条。冒烟比预期贵 3 倍，而且这个偏差
在不看库的情况下完全不可见（日志只说「完成 3 条」，那是重置条数）。

**修法**：

1. `--status {all,pending,done,failed}`，默认 `all` 保持老行为；
   日常补摘要一律 `--status pending`。文件头注释里写清楚这个默认值会重算 done。
   **`local-fallback` / `local-no-content` 是 `ai_model` 的值，不是 `ai_status`** ——
   想按它们筛要用 `--model`。写成 `--status local-fallback` 会静默地「没有记录」，
   比报错更坑（第一版就是这么写的，跑完才发现一条都没动）。
2. `summarize_pending(limit, ids=None)` 加 `ids` 参数，把范围精确限定到一批行。
   脚本**先定下 `target_ids` 再打回 pending**，之后每批只传这 10 个 id。
   顺带解决另一个隐患：`--model local-fallback` 时不会把几百条无关 pending 一起带上。
3. 分批 10 条 `commit` 一次。免费档 GLM 限流严重（1305 很常见），
   一次 200 条在最后统一提交，中途被 Ctrl-C / 超时杀掉就**全部丢失**；
   分批才能断点续跑，而且能打印真实进度与 ETA。

**教训**：「只补没做的那部分」和「全部重算」是两个不同的操作。
脚本的默认值不能默默替用户选一个 —— 要么保守（只补 pending），要么在
`--help` 和文件头里用一行字说明代价。这次两个都做了。

位置：`backend/scripts/resummarize.py` / `app/services/ingest.py::summarize_pending`

---

## 55. 「您无权访问此页面」被当成正文 —— 摘要第二行原样展示系统提示

**现象**（2026-10-19 补摘要时发现）：14 条港澳台项目通知的摘要长这样：

```
1. （项目通知）关于2022年台湾元智大学寒假线上讲座通知
2. 您无权访问此页面
3. 原文信息较少，建议点开看全文
```

用户点开详情看到的是学校站点的报错，不是通知内容。

**根因**：这些详情页 HTTP 200，返回的是博达的

```
系统提示
抱歉
可能是由下列问题导致的：
您无权访问此页面
```

共 29 字 —— 刚好卡在 `_MIN_AI_CONTENT_CHARS = 30` 下面，所以没进 AI（这一步是对的）。
但本地规则的 `_build_lines` 把正文切句后取 `sentences[1]` 当第二行，
而 `sentences` 是 `["可能是由下列问题导致的：", "您无权访问此页面"]` —— 于是提示语上了卡片。

**修法**：加 `_looks_like_error_page()`，在**长度检查之前**判定：

```python
_ERROR_PAGE_MARKERS = ("无权访问", "您访问的页面不存在", "页面不存在", "访问受限",
                       "403Forbidden", "404NotFound", "AccessDenied", "系统提示抱歉")
_ERROR_PAGE_MAX_CHARS = 150      # 两千字的通知不可能整篇都是提示语

blocked = _looks_like_error_page(text)
if blocked:
    text = ""                    # 按「没抓到正文」处理，不给本地规则任何素材
```

命中就走专门的兜底文案（不进 AI，也不拼提示语）：

```
1. <标题>
2. 原文页面需要登录或无权限，本条只抓到标题
3. 点开原文查看；校内页面通常需在校园网内登录后访问
```

`tags` 加「需登录」，用户能筛掉这一批。

**为什么 marker 里没有「请先登录」**：正常通知里教用户登录太常见，
短通知会因为一句话被整篇废掉。只放强特征（`无权访问` / `404` / `403` 等）。

**测试**（`tests/test_summarize_guard.py`）：断言摘要里不得出现「无权访问 / 系统提示 / 抱歉」，
且 tags 含「需登录」。

**测试本身的假绿**：第一版测试用的是单行输入 `"系统提示 抱歉 您无权访问此页面"`。
切句后只剩一句 `["您无权访问此页面"]`，第二行走的是 `_FALLBACK_LINE` ——
**这个断言无论代码对错都会通过**。必须用库里**真实的多行文案**：
`"系统提示\n抱歉\n可能是由下列问题导致的：\n您无权访问此页面"`。
变异测试（把 `blocked` 强制改成 `False`）才如实地红在「无权访问」这一条上。

**教训**：断言只检查「有没有调 AI」是不够的 ——
**不调 AI 不等于给用户的文案是对的**。

位置：`app/services/summarize.py::_looks_like_error_page` / `_local_summarize(blocked=...)`

---

## 56. ★ 200 响应里出现「429 / 1305」被判成限流 —— 模型明明答对了却降级

**现象**：重摘一条迎新通知时，日志写着

```
AI 摘要失败，降级到本地规则：AI 调用最终失败：HTTP 200: {"choices":[{"finish_reason":"stop",
"message":{"content":"{\"summary_lines\":[\"集成电路学院欢迎2026级新生\"...
```

**HTTP 200 + `finish_reason=stop` + 三行摘要都在**，却报「最终失败」。

**根因**：`ai_client.chat()` 里这一行

```python
if resp.status_code == 429 or _is_rate_limited(resp.text):
```

`resp.text` 是**整个响应体**，包含模型生成的正文。
`_RATE_LIMIT_HINTS = ("429", "1302", "1305", ...)` 只要在正文里撞上这几个数字
（日期、电话、房间号、金额、编号都可能）→ 成功的 200 被判成限流 →
退避重试 4 次（5+10+20+35 秒）→ 抛 `AIError("AI 调用最终失败：HTTP 200")` → 降级本地规则。

**这个 bug 的所有表面指标都是绿的**：没有异常冒出来、`ai_status=done`、
3 行齐全、合规检查全过 —— 只有 `ai_model` 悄悄从 `glm-4.7-flash` 变成 `local-fallback`。
而且它**只影响部分条目**（正文里恰好有那几个数字的），很难从小样本上看出来。

**修法**：限流是**传输层**的事实，只能在失败响应上判断。

```python
if resp.status_code == 200:
    return self._pick_content(resp.json())      # 成功就是成功，不猜

if resp.status_code == 429 or _is_rate_limited(resp.text):
    ...退避重试...
```

**测试**（`tests/test_ai_client_status.py`，2 条）：
① 200 + 正文含「1305/429/1302」→ 必须直接返回内容，且**只请求 1 次**；
② 真 429 → 必须退避重试到上限再抛错（别为了修误判把限流逻辑改没了）。
变异测试：把 200 分支短路 → ① 立刻红，错误信息与生产日志一字不差。

**教训**：给「成功响应」做内容级关键词判定，等于让被调用方决定要不要重试。
状态码能回答的问题，不要去读 body。

位置：`app/services/ai_client.py::AIClient.chat`

---

## 57. 一轮抓取 9 个来源全 502，汇总仍写「新增 0」—— 覆盖率不足是新的假绿

**现象**（2026-10-19 15:25，`D:/QuanZhi/scripts/fetch_once.py`）：

```
总条目      : 116
新增        : 0
更新        : 0
robots 跳过 : 17
详情失败    : 34
空壳保护    : 20
退出码      : 0
```

看起来是「今天没有新通知」。实际是**9 个盘锦站点全部 HTTP 502**：

```
抓取失败 [盘锦学生工作部·通知公告]：HTTP 502
抓取失败 [盘锦总务部·工作通知]：HTTP 502
抓取失败 [盘锦图书馆·新闻通知]：HTTP 502
...
```

**7 分钟后同一条命令再跑**：274 条 / 新增 1 / 502 只剩 1 次（重试后成功）。
所以不是站点挂了，是**本机代理链路瞬时故障**——同一时刻用 curl 单发、并发 10 都是 200。

**为什么这是最危险的一类 bug**：

- 「新增 0」有两个完全不同的含义：① 今天确实没有新通知；② 三分之一站点根本没抓到。
  汇总表上看不出区别。
- 退出码 0，没有任何自动化会拦住它。
- 它和 #52（正文空却让 AI 编）、#56（200 判成限流）是同一类：
  **指标绿 ≠ 事实对**。

**已经有的重试救不了它**：`fetch_retries=2` + 指数退避（1s、2s），一个来源要连吃 3 次 502
才报错；9 个来源同时这样，说明故障持续了不止一瞬。重试只能扛抖动，扛不住链路整体断掉。

**修法**（`scripts/fetch_once.py`）：汇总里显式报覆盖率，有失败就顶一行警告：

```
失败来源    : 9 / 24
!! 有 9 个来源本轮抓取失败（下方 !! 行）——不代表这些站没有新通知，请稍后重跑
```

**判断标准**：**失败来源 > 0 时，这一轮不算完成**，不能拿「新增 0」当结论。

**顺带一个数据源事实**：这轮唯一新增的 #260 来自 `portal.dlut.edu.cn/tp/index.html#act=...`，
是校园门户的 **SPA 详情页**（hash 路由 + JS 渲染），列表能抓到、正文抓不到 ——
所以它进了 `local-no-content`。和招生网/Vue、就业网/JS 是同一类，不是本次新引入的问题。

位置：`D:/QuanZhi/scripts/fetch_once.py`

---

## 58. 用 Git Data API 推送：三个坑（代理挡域名、空仓库 409、中文文件名被转义）

**背景**：这台机器到 `github.com:443` 被系统代理挡成 **502**（`api.github.com` 却正常），
`git push` 走不了。改用 Git Data API 逐 blob 上传，踩了三处。

### 坑 1 · 先确认到底是哪个域名不通

`curl -v https://github.com/` 才看得见真相：

```
* Trying 127.0.0.1:59966...
> CONNECT github.com:443 HTTP/1.1
< HTTP/1.1 502 Bad Gateway
```

同一时刻 `api.github.com` 返回 200、`github.com:22` 的 SSH 握手也通。
所以「GitHub 连不上」是个过于笼统的结论 —— **443 不通不代表 22 不通，
主域名不通不代表 api 子域不通**。先分域名、分端口测，再决定走哪条路。

### 坑 2 · 空仓库建 blob 返回 409

```
POST /repos/{owner}/{repo}/git/blobs → HTTP 409 {"message":"Git Repository is empty."}
```

新仓库没有任何提交时，git 对象库还没初始化，blob / tree 全都不让建。
而且 `GET /git/ref/heads/main` 对空仓库返回的也是 **409 而不是 404**
（有仓库但没这个分支才是 404），只用 404 判断会漏。

**做法**：先用 Contents API `PUT /contents/.gitkeep` 造一个初始化提交，
再把真正的第一个 commit（`parents: []`）用 `PATCH /git/refs/heads/main {force: true}`
强制指过去 —— 占位提交变成不可达对象，仓库历史里只剩一条真提交。

### 坑 3 · `core.quotepath` 把中文文件名转义成字面量

第一版用 `git ls-files -s` 取路径，拿到的是：

```
100644 195e1f5... 0	"\347\237\245\344\272\206.bat"
```

引号和 `\347\237\245` 是 **git 对非 ASCII 路径的显示转义**（`core.quotepath` 默认开启）。
脚本把这个字符串当成真实文件名发给了 GitHub，于是仓库里多了两个名字含引号和
反斜杠的文件，顶层 tree 的排序也变得莫名其妙（`"` 的字节序 0x22 排在字母前面）。

**这个 bug 有多隐蔽**：152 个 blob 的内容 SHA **全部正确**，文件数量也对得上，
逐个比对「本地 blob sha vs 远端 blob sha」是**全绿**的 —— 错的只有名字。
真正暴露它的是 tree SHA 对不上，以及 `git status` 突然报告两个 rename。

**做法**：一律 `git ls-files -s -z`（`-z` 关闭转义、用 NUL 分隔），
并在推送前加一道断言：路径里出现 `"` 或 `\` 直接拒绝。

### 顺带：远端 commit SHA 与本地不同是正常的

GitHub 会重新排序 tree 条目、并规范化 commit message 的尾部换行，
所以远端 commit SHA ≠ 本地 commit SHA，哪怕内容逐字节相同。
两边就成了两个**不相干的首个提交**，以后 `git push` 会被判非快进而拒绝。

**对齐办法**：把远端 tree 递归取回来，在本地按 git 对象格式（`mode name\0` + 20 字节 sha）
自底向上重建，校验重建后的 tree SHA 与远端一致；再重建 commit 对象，
**枚举时区和尾部换行**（实测命中 `+0800` + 消息末尾 `\n\n`），校验 SHA 后
`git update-ref refs/heads/main <远端 sha>`。本地工作区内容不变，`git status` 干净。

位置：`scripts/` 之外的运维脚本，未进仓库；结论记在此处

---

## 59. `github.com` 网页打不开时，怎么把 SSH 公钥装上去

**场景**（2026-10-21~23）：这台机器到 `github.com:443` 的 TCP 被阻断 ——
直连超时 21s、走代理 000，连续两天 100% 失败。但 `api.github.com` 正常（0.53s）、
`ssh.github.com:443` 的 SSH 握手也正常。DNS 没被污染（`20.205.243.166` 是真实 IP），
同一个 /24 网段里 `.168`（api）和 `.160`（ssh）通、`.166`（网页）不通。

结果就是：**命令行能调 API，浏览器却打不开设置页**，而添加 SSH 公钥偏偏只能走网页。

**绕法**：`admin:public_key` 权限的 token 可以直接通过 API 登记公钥，
完全不需要打开 github.com。

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Accept: application/vnd.github+json" \
     https://api.github.com/user/keys \
     -d '{"title":"zhiliao-desktop","key":"ssh-ed25519 AAAA... user@host"}'
```

返回 `{"id":167391341,...}` 即成功，公钥**立即生效**，接着 `ssh -T git@github.com`
就能拿到 `Hi <user>! You've successfully authenticated`。

**前提条件**：token 必须显式带 `admin:public_key`。只有 `repo` 时这个端点返回 **404**
（不是 403 —— 容易误判成「接口地址写错了」）。
生成时可以直接用带参数的 URL 免勾选：
`https://github.com/settings/tokens/new?scopes=repo,admin:public_key&description=zhiliao-key`

**为什么不用 deploy key**：`POST /repos/{owner}/{repo}/keys` 同样需要写权限，
而且 deploy key 只对单个仓库有效。账号级公钥一次配好，以后所有仓库都能推。

**中文用户名的连带问题**：Windows 版 `ssh.exe` 读不懂 MSYS 路径。
Git Bash 里 `HOME=/c/Users/詺`，ssh 会去建 `/c/Users/詺/.ssh` 并报
`Could not create directory '/c/Users/\324\232/.ssh' (Permission denied)` ——
即使手动 `mkdir -p ~/.ssh` 成功，ssh 也读不到里面的 key。
解法是把密钥放 ASCII 目录（`C:/Users/Public/zhiliao_ssh/`），并用
`git config core.sshCommand` 显式传 `-i` 与 `UserKnownHostsFile`，
写进 `.git/config`（不进版本控制，clone 到别的机器不受影响）。

**判断方法**：网络问题要分四组对照 —— 域名 × 端口 × 直连/代理 × 时间跨度。
只看「GitHub 连不上」会得出错误结论：`api.github.com` 通不代表 `github.com` 通，
443 不通不代表 22 不通。

---

## 60. ★★ 验收脚本默认「用户没置顶/收藏过」—— 用户一置顶，测试就假红

**现象**：`verify_all.py` 从 38/38 变成 32/38，`smoke_desktop.py` 从 58/58 变成 55/58，
失败全集中在置顶和收藏：`置顶成功`、`★ 置顶后排到列表第一位`、`POST /star 生效`……
而接口代码一行没改。

**真因**：`/pin` 和 `/star` 是**切换**语义（不传 body 就取反），而脚本拿
`items[0]`（列表第一条）当测试目标。用户把 id=266 置顶并收藏之后，
它就成了 `items[0]`：

```
脚本 post /pin {}  →  切换  →  把用户的置顶取消了  →  断言 is_pinned is True 失败
脚本 post /star {} →  切换  →  把用户的收藏取消了  →  断言 is_starred is True 失败
```

**更糟的是它顺手改了用户的标记**，只是 `user_flags` 兜底又把它们还原了。

**改法（两条一起）**：
1. 选目标时排除已有标记：`pool = [i for i in items if not i["is_pinned"] and not i["is_starred"]]`
2. 断言用**显式值**而不是切换：`post(..., {"is_pinned": True})`，
   再 `{"is_pinned": False}`；「排在最前」的断言改成「第一条必须已置顶」，
   不再假设全局只有一条置顶。

**这一课比看起来重要**：假红和假绿是一对孪生问题。
假绿让 bug 溜走，假红让人开始不信任测试、最后绕过它。
**凡是「依赖环境当前状态」的断言，都要么显式设定状态，要么先排除干扰状态。**

---

## 61. 「粘贴网址就能加源」：探测必须和正式抓取共用同一条链路

新增来源的入口（来源管理 → ＋ 添加来源）要在用户只填一个网址的情况下，
判断这个页面能不能抓、该用什么选择器。三个必须守住的点：

**1. 出网必须走 `Fetcher`**。robots 校验、同域 2 秒限速、UA、重试都在那里面。
探测自己 new 一个 `httpx.Client` 就等于开了个后门（项目铁律第 4 条）。
所以 `POST /sources/probe` 里是 `async with Fetcher(settings)` + `FetchContext`。

**2. 预览必须由**正式解析器**算**，不能另写一套。探测复用的是
`HtmlListAdapter._parse_node`，所以「探测看到 8 条」和「入库 8 条」是同源的。
另写一套的后果是探测全绿、正式抓取全空 —— 本项目最贵的一类 bug。

**3. `item_url_regex` 必须按绝对 URL 生成**。
这是实测踩到的：用原始 href（`../info/1031/8891.htm`）反推规则会得到
`\.\./info/\d+/\d+\.htm`，而适配器是拿 `urljoin` 之后的
`http://teach.dlut.edu.cn/info/1031/8891.htm` 去匹配 → **一条都匹配不上**，
静默抓 0 条。改成从 `urlparse(url).path` 生成 `/info/\d+/\d+\.htm` 才对。

**判据也不能只看「有 3 个带链接的兄弟节点」**：详情页侧栏导航
（研究生院综合办 / 研究生工作处 / 研究生培养处）正好 3 条，会被当成列表。
真实列表至少满足一条：`条数 ≥ 4 且带日期`，或 `条数 ≥ 8`。
实测把这条门槛加上之后，`detail_gs_empty.html` 从「识别出 3 条列表」变成
「没能认出通知列表」——这才是诚实的回答。

**离线校验**：`tests/test_probe.py`（33 项）对 10 个真实页面存档跑通探测，
其中有 5 项把生成的配置交给**真正的 `HtmlListAdapter`**（`transport=file`）跑一遍，
断言必须出条目 —— 专门防上面第 2、3 条。

---

## 62. 接自定义大模型：三个坑都是「界面上看不出来」的

**坑 1：本机地址被系统代理拦成 502。**
Windows 上常配着系统代理（学校网尤其常见）。`httpx` 默认 `trust_env=True`，
会把 `http://127.0.0.1:11434`（本地 Ollama）也交给代理，代理连不上，返回
`502 upstream connect failed`。用户看到的是「我的 Ollama 明明在跑，知了说连不上」，
根本想不到是代理。
修法：`ai_client.is_local_host()` 判 host（localhost / 127.0.0.1 / 10. / 192.168. /
172.16-31 / .local），命中则 `trust_env=False` 直连。
**这个坑只有在「测一个本机地址」时才暴露** —— 拿 deepseek 那种公网地址测永远发现不了。

**坑 2：`thinking` 和 `response_format` 不是所有服务商都认。**
智谱认 `thinking`，别家见到直接 400；`response_format` 多数认，少数网关不认。
不能一律发、也不能一律不发（关了 thinking 会让 GLM 把额度耗在思维链上，docs/02 #52）。
做法分两层：智谱系（按域名判）才发 `thinking`；真被 400 了再看报错**有没有点名**
那个字段，点名了才摘掉重试一次。
**不能见 400 就摘** —— 否则「模型名拼错」这种错误会被摘掉两个参数后变成另一个
更难懂的 400。`_drop_unsupported()` 只在报错提到该字段、或明说
unsupported / unknown / extra 时才动手（4 个单测锁住这个边界）。

**坑 3：打码值被当成新 key 存回去。**
接口只回 `sk-a****mnop`。前端编辑别的字段（改个名字）时把这个打码串原样提交，
如果后端直接 `row.api_key = payload.api_key`，key 就变成了 `sk-****mnop` 这个字符串本身，
下一次提问立刻 401 —— 而且用户改的明明是名字，会觉得「这软件自己把 key 弄坏了」。
规则：**含 `****` 或为空 = 没改，保留原值**。空字符串也保留（防止手滑清空输入框）。

另外两条是设计决定，不是坑：
- **配置存库不存 .env**：改 .env 要重启进程，等于「界面上换模型」这个功能不存在。
- **进程级缓存只存自定义 profile，env 那份每次现算**：`settings` 是 lru_cache 单例，
  测试里会 monkeypatch 它的 key；如果连 env 配置也缓存成 dataclass 副本，
  改过的 settings 就再也影响不到 `AIClient`（表现为「设了 key 还说未配置」）。
  这条是跑全量测试时才发现的（2 个用例变红）。

**验收**：`tests/test_llm_profiles.py`（33 项）+ `scripts/probe_llm_model.py`
（真窗口走一遍：按钮 → 弹窗 → 填错地址测连 → 保存启用 → 删除回落）。
测连失败时**必须回显最终请求地址**，否则用户没法自查是哪一项填错了。

---

## 63. 接学校 AI 网关：三处「看着通了其实没通」

用户给了真实凭据（学校统一 AI 网关），一接就撞出三个问题。共同点是
**界面上都不报错**，只看指标全是绿的。

**坑 1：文档给的地址是 Anthropic 写法，我拼成了 `/v1/messages/chat/completions`。**
网关文档写 `http://xxx/v1/messages`，而它同时提供 OpenAI 兼容接口。
`normalize_endpoint` 的老规则是「不是 `/chat/completions` 结尾就往后拼」，
于是拼出一个 `/v1/messages/chat/completions` —— 404，但报错只说
`Invalid URL (POST /v1/messages/chat/completions)`，看着像 key 不对。
修法：认出 `/messages`、`/completions`、`/responses` 这些**别的协议的完整地址**，
先摘掉协议尾，再按版本号拼。加了 5 个参数化用例 + 一条幂等性用例
（规整两次结果必须一样，否则用户把界面上显示的地址再存一遍就坏了）。
**顺序很关键**：先判 `/chat/completions` 再判 `/completions`，否则正确的地址会被摘掉半截。

**坑 2：`/api/health` 报的是 .env 的模型，不是正在用的那个。**
切到自定义模型后，health 仍然说 `enabled:false, model:glm-4.7-flash`
（因为这个隔离实例没配 `AI_API_KEY`），而实际已经在跑网关的 GLM-4.6。
用户看到这条只会得出一个结论：「切换没生效」。改 `get_active(settings)` 之后
health 报 `{enabled, model, name, source, endpoint}` 五项，和真实调用一致。
**假报告比没报告更坏** —— 排障时会把方向带反（这是本项目第 N 次栽在同一件事上）。

**坑 3：验收脚本写死了 `glm-4.7-flash`，一换模型就假红。**
`verify_all` 的「问答标注生成方」、`smoke_desktop` 的「简报/问答标注生成方」
都把期望值硬编码成 `("local", "glm-4.7-flash")`。切到 GLM-4.6-W8A8 后
三条全红，但功能是好的。改法：先问 `/health` 拿生效模型，再比。
这和 #60 的「假红与假绿孪生」是同一个根：**断言里不应该出现实现细节的常量**。

**顺带加的能力**：`POST /api/llm/models` + 桌面版「拉取模型列表」。
这个网关一次列 25 个模型，id 是 `GLM-4.6-W8A8` `DeepSeek-V3.1-W8A8`
这种带量化后缀的写法 —— 让人手打就是让人猜，猜错只会得到 404 model not found。
现在点一下弹出带搜索的列表，选中即填。

**选型实测**（同一批真实通知、同一份 prompt）：
| 模型 | 单条耗时 | 三行摘要合规 |
|---|---|---|
| GLM-4.6-W8A8 | 6~7s | 2/2 |
| DeepSeek-V3.1-W8A8 | 3~4s | 1/2（有一条 46 字超限） |
| Qwen3.5-122B-A10B | 32s，且有一次 150s 超时 | 1/2 |
所以默认选 GLM-4.6-W8A8。**Qwen3-8B / MiniMax 会在 content 里直接吐 `<think>` 标签**，
不适合当摘要模型（除非显式关思考）。

**验收**：`probe_llm_model.py` 新增第 5b 步 —— 错误地址必须回显
`请求：.../v1/models`；给了真 key 时要求弹出选择窗口、条数 ≥5、
点「用这个」后「模型名」**必须等于列表首项**（只断言非空是假绿，
填进去的可能还是上一次的残留值，第一版就踩了这个）。

## 64. ★ 打包成安装包：六个坑，其中三个「编译通过、装完是坏的」

**坑 1：`.nsi` 不带 BOM，中文全变乱码，编译一句警告都没有。**
NSIS 3 的 `Unicode true` 靠文件头 BOM 判断脚本编码。不带 BOM 就按 ANSI 读，
装出来的向导标题、按钮、许可协议全是乱码 —— 而 `makensis` 返回 0。
`build.py` 里 `ensure_bom()` 专门干这件事，别删。

**坑 2：中文文件名的 exe 在 Git Bash 里直接执行会被杀掉。**
`./知了-Setup-1.0.0.exe /S` → 进程收到 SIGTERM，零输出，看着像安装包坏了。
和「Windows 下别用中文文件名执行 .py」是同一类问题。改走
`cmd //c "setup.exe /S"`、并且**文件名也用 ASCII**（`zhiliao-setup.exe`）就正常。

**坑 3：`Uninstall.exe /S` 退出码 0 ≠ 删完了。**
NSIS 卸载器启动时先把**自己复制到 `%TEMP%\~nsu.tmp\Au_.exe` 再重新执行**，
原进程立刻返回 0。所以「退出码 0 但 4078 个文件一个没少」是正常现象。
自动化必须**轮询文件数**，别拿退出码当结论 —— 和「定时任务注册 ≠ 触发」同构。

**坑 4：在别的目录里调用 `Uninstall.exe` 的绝对路径，会返回 1 且什么都不删。**
实测矩阵（同一个包、同一次安装）：

| 调用方式 | 退出码 | 结果 |
|---|---|---|
| `cd 安装目录 && Uninstall.exe /S` | 0 | 4078 → 1 个文件 |
| 别的目录，绝对路径 + `cmd /c` | 1 | 一个没删 |
| 别的目录，绝对路径 + 直接 `CreateProcess`（不传 cwd） | 1 | 一个没删 |
| 别的目录，绝对路径 + 显式 `cwd=安装目录` | 0 | 正常 |

结论：**卸载器必须带着「安装目录」这个工作目录去跑**。所以门禁脚本里
`run_uninstaller()` 显式给 `cwd=str(target)`；同时保留重试（另有一次是
`%TEMP%\~nsu1.tmp\Un.exe` 残留撞锁，重试一次就好）。
排查顺序要记牢：先确认 cwd，再怀疑卸载脚本本身 —— 我第一次直接怀疑
「Uninstall section 写错了」，差点去改一个没坏的脚本。

**坑 5：截图截不到文字，是窗口 DC 的锅，不是文字没渲染。**
`BitBlt(GetWindowDC(hwnd))` 在 Win10/11 上只能拿到背景层，文字层是空的 ——
第一次截安装向导，对话框里只剩一个「?」图标和两个空白按钮，差点去改 NSIS。
换成**屏幕 DC**（`GetDC(0)`）立刻就对了。附带：DIB 是 BGRA，
按 RGBA 写 PNG 会红蓝互换（橙色问号显示成蓝色）。

**坑 6：装到 `C:/Program Files` 必然坏。**
知了启动第一件事是建库、写 265 条通知、记已读状态。装在 Program Files 里
普通用户没写权限，装完双击就是「后端起不来」。
所以 `RequestExecutionLevel user` + 默认 `%LOCALAPPDATA%\Programs\ZhiLiao`：
不弹 UAC，DB 可写。这也是 VS Code / Teams 用户级安装的做法。

**顺带两个决定**：
- **安装包里没有 AI 密钥**。发给评委的包，`backend/.env` 的 `AI_API_KEY` 与
  `llm_profiles` 表的 key 全部清空（`make_payload.py` 里显式做，不是忘了拷）。
  开箱能看 265 条真实通知（每条 3 行摘要都在），AI 功能让用户自己在界面里填。
- **静默卸载默认保留数据**。没人看着的时候绝不删用户数据；交互式卸载会问，
  选保留就挪到 `%LOCALAPPDATA%\ZhiLiaoData`，重装时 `.onInit` 检测到就问要不要恢复
  （实测 md5 一致）。

**验收口径**（三层，缺一层都可能是假绿）：
| 层 | 脚本 | 实测 |
|---|---|---|
| 载荷能不能跑 | `verify_installer.py <装好的目录> --gui` | 25/25 |
| 装/卸/重装/恢复 | `verify_install_cycle.py <setup.exe>` | 24/24 · 15s |
| 向导中文与图标 | 整屏截图 `logs/_diag/shot_desktop.py` | 无乱码 |
载荷那层的前提是**端口独占 + 按 PID 认窗口**（#49 的老规矩）：
只断言「有窗口」会匹配到 Tk 那个标题叫 `tk` 的隐藏根窗口，必须断言标题含「知了」。
装/卸那层要**从零开始**：先把上一轮留下的 `%LOCALAPPDATA%\ZhiLiaoData` 挪走，
否则「数据已保留」是假绿（那个文件本来就在那儿，跟本次卸载没关系）。

## 65. ★★ 「测试连接」一直卡在「正在测试…」—— 报错回调自己报错了

**症状**：点「测试连接」，按钮变灰、状态文字停在「正在测试…」，一直不回来。
关窗口再重开也一样，用户只能以为网关挂了。

**根因是一行写得「看起来很对」的代码**：

```python
except Exception as e:
    self._post(lambda: set_status(f"测试失败：{_http_detail(e)}", False))   # ✗
```

CPython 在 except 块结束时执行 `del e`（PEP 3110）。这个 lambda 要等主线程
40ms 后才由 `_ui_pump` 取出来执行，那时 `e` 已经没了：

```
NameError: cannot access free variable 'e' where it is not associated with a value
```

而 `_ui_pump` 的兜底是 `except Exception: traceback.print_exc()` ——
**异常被吞进 stderr，界面一动不动**。所以「失败提示」永远不出现，
按钮永远停在 disabled。全文件当时有 **9 处**这个写法（模型对话框 7 处、
AI 问答 1 处、设置页自检 1 处），只修一处没用。

**为什么三个指标全绿也发现不了**（这是本条最值钱的部分）：

| 看起来在测的东西 | 实际测到的是什么 |
|---|---|
| 探针「不通的地址要点出失败」 | 后端把失败包装成 **HTTP 200 + `ok:false`**，前端走的是**成功分支**，根本进不到 except |
| 后端单测「/llm/test 返回 ok=False」 | 只覆盖路由层，前端回调一行都没跑 |
| 手动点一下「应该会报错吧」 | 用户看到的是「正在测试…」，会认为**是自己网络慢** |

我第一版探针就是这么写的，**用旧代码跑也是 PASS** —— 因为它压根没进 except。
真正的复现条件是「**请求本身抛异常**」，只有两种做法：
① 把 `m.API` 指到死端口（前端连不上后端）；② 让后端超过前端超时。
变异测试要用①，2.3 秒必现。

**修复（两层）**：

```python
except Exception as e:
    msg = _http_detail(e)                          # ① 先算成字符串
    later(lambda m=msg: finish(f"测试失败：{m}", False))   # ② 默认参数绑定

win.after(25000, guard)     # ③ 看门狗：到点没收场就自己收场
```

看门狗是第二道保险，不是替代品：只要有任何一条路径没走到回调
（线程卡住、后端 hang、异常在别处被吞），到点仍然恢复按钮并给一句话。
**「不依赖回调一定执行」是界面不锁死的唯一可靠做法。**

**顺带修的两个真问题**：

1. **`httpx.ReadTimeout` 的 `str()` 是空字符串**。界面显示
   「失败：AI 调用最终失败：」后面一片空白，用户完全不知道发生了什么
   （实测 `probe_llm_model` 5d 黑洞网关就是这块空白）。`check_ai` 里补兜底文案。
2. **测试连接的超时必须短**。默认 `ai_timeout=60` + 网络错误再试 1 次 = 121 秒，
   而前端 90 秒就断了 —— 用户等 1 分半等来的是「卡住」不是「连不上」。
   现在 `check_ai(..., timeout=15.0, retry=False)`，前端 20 秒，看门狗 25 秒。

**防复发**：`tests/test_desktop_callbacks.py` 用 AST 扫全文件，
禁止「except 变量被延迟 lambda 引用」，并自带正反用例（旧写法必须被抓到、
`lambda m=msg` 不能误报、`lambda e:` 形参遮蔽不误伤）。
这条守卫上线时立刻抓出了我手动 grep 漏掉的第 9 处。

**验收数字**（真窗口，隔离库，便携包自带 Python 3.13.14）：

| 场景 | 旧代码 | 现在 |
|---|---|---|
| 后端不可达（except 分支） | 15.2s 无任何提示，stderr 抛 NameError | **2.3s** 报错，按钮恢复 |
| 网关只收连接不响应 | 同上 | **15.4s** 出结果 |
| 连接被拒（200+ok:false） | 2.4s 正常提示 | 2.4s 正常提示 |
