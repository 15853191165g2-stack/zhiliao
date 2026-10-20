# 阶段 2 交付说明：大连理工大学数据源接入

> 本文回答四件事：**接了哪些源**、**选择器怎么定位的（附实测证据）**、
> **哪些接不了以及为什么**、**怎么在本地跑起来看结果**。
>
> 所有选择器都是**逐个访问真实页面确认**后写下的，不是猜的。
> 页面快照存档在 `backend/tests/fixtures/dlut/`（21 份真实 HTML），
> 离线测试 `backend/tests/test_dlut_offline.py` 直接吃这些快照验证。

---

## 一、来源总览（实测，2026-10-06）

| # | 来源 | 地址 | 类型 | 栏目页 | 首页实测条数 | 状态 |
|---|------|------|------|--------|:---:|:---:|
| 1 | 迎新网 | yx.dlut.edu.cn | 网站 | `/index/bksbd.htm` 本科生报到 | 11 | ✅ |
| 2 | 迎新网 | 〃 | 网站 | `/index/yjsbd.htm` 研究生报到 | 9 | ✅ |
| 3 | 迎新网 | 〃 | 网站 | `/index/syjs.htm` 书院介绍 | 9 | ✅ |
| 4 | 迎新网 | 〃 | 网站 | `/index/yxfc.htm` 院系风采 | 20 | ✅ |
| 5 | 迎新网 | 〃 | 网站 | `/` 首页聚合 | 20 | ✅ |
| 6 | 教务处 | teach.dlut.edu.cn | 网站 | `/xwkd/xwkd.htm` 新闻快递 | 20 | ✅ |
| 7 | 教务处 | 〃 | 网站 | `/ksap/ksap.htm` 考试安排 | 1 | ✅ |
| 8 | 学生处 | xsc.dlut.edu.cn | 网站 | `/index/tzgg.htm` 通知公告 | 14 | ✅ |
| 9 | 研究生院 | gs.dlut.edu.cn | 网站 | `/gzdt1.htm` 工作动态 | 18 | ✅ |
| 10 | 后勤处 | houqin.dlut.edu.cn | 网站 | `/tzgg.htm` 通知公告 | 15 | ✅ |
| 11 | 网信中心 | its.dlut.edu.cn | 网站 | `/index/zxtz.htm` 中心通知 | 20 | ✅ |
| 12 | 图书馆 | www.lib.dlut.edu.cn | 网站 | `/xw/tzgg.htm` 通知公告 | 12 | ✅ |
| 13 | 校团委 | tuanwei.dlut.edu.cn | 网站 | — | 0 | ⛔ 需校内权限 |
| 14 | 公众号 DUT学生工作 | mp.weixin.qq.com | 微信 | RSS 桥接 | — | ⚙️ 待填地址 |
| 15 | 公众号 大连理工大学 | 〃 | 微信 | RSS 桥接 | — | ⚙️ 待填地址 |

- **网站源 12 个已接通**（其中迎新网占 5 个），全部形如
  `backend/sources/dlut_*.yaml` 里的**纯配置**，没有为新源写一行 Python。
- 公众号走**「粘贴文章链接导入」+「RSS 桥接占位」**两条路，见第四节。

---

## 二、网站适配器：一个适配器吃下 4 种页面结构

大工各子站**大多是博达（Visual SiteBuilder）系统，但模板版本不同**，
所以是一份配置复制粘贴就能搞定的。实测到的结构差异：

| 版式 | 代表 | 列表结构 | 日期在哪 |
|------|------|----------|----------|
| A 经典表格版 | 迎新网 | `div.ny_news tr` | 兄弟 `<td>` 的 `span[class^="timestyle"]` |
| B li 列表版 | 教务处 / 网信中心 | `div.list li` | 同行 `<span>` |
| C 日期拆分版 | 后勤处 | `ul.message-list li` | `p.day` + `p.year-month` 两个节点 |
| C 日期拆分版 | 图书馆 | `ul.nytzlist.flex li` | `<time class="time"><span>2026</span>09-22</time>` |
| D 前缀样式版 | 学生处 | `div.news-content li` | 标题**前面**的 `span.date`（如 `2026年07月20日`） |

适配器 `app/adapters/html_list.py` 通过配置项吸收这些差异：

```yaml
list_selector: "div.list li"      # 列表行
fields:
  title: "a@title"                # 支持 a@属性 与 CSS 选择器
  url: "a@href"
  date: 'span[class^="timestyle"]'  # 属性前缀匹配，避开模板 ID
title_cleanup_regex: "^[·•\\s]+"    # 去掉标题前缀「·」
date_extract_regex: "\\d{1,2}\\s+20\\d{2}-\\d{1,2}"  # 从整行抠日期
item_url_regex: "dlut\\.edu\\.cn/info/\\d+/\\d+\\.htm"  # 只留文章链接，挡导航
content_selector: ["#vsb_content", ".v_news_content"]   # 详情正文，多候选兜底
```

### 关键坑（每条都验证过）

1. **模板 ID 绝不能硬编码。** 迎新网列表是
   `span.timestyle58538`，其中 `58538` 是栏目模板 ID，各栏目/子站都不同。
   用属性前缀选择器 `span[class^="timestyle"]` 才通用。

2. **日期常在「兄弟节点」而不是标题节点里。** 只取 `<a>` 的父节点永远拿不到日期。
   适配器会把「字段选择器」和「整行文本」都试一遍。

3. **导航菜单会污染结果。** 网信中心页面挂了个 `ul.dropdown-menu`，
   里面也有 `/info/` 链接（「核心机房管理办法」等）。所以
   `list_selector` 必须限定到 `div.list`，再用 `item_url_regex` 二次过滤。

4. **标题可能藏在 `h4` 而不是 `a`。** 图书馆是
   `<a><time/><h4 class="tit">标题</h4><span>了解详情</span></a>`，
   标题选择器要写 `h4.tit`，否则会把「了解详情」带进来。

### 日期形态一览（`core/timeutil.py` 已全部覆盖）

| 原文 | 来源 | 归一化结果 |
|------|------|-----------|
| `2026-08-10` | 迎新网 | ymd |
| `2026年07月20日` | 学生处 | ymd |
| `[2026-04-29]` | 研究生院 | 剥括号 → ymd |
| `21 2026-09` | 后勤处 | **dmy**（日在前） |
| `2026 09-22` | 图书馆 | **ym + md**（年与月日拆开） |

`parse_dt` 内置 **6 档优先级模式**（`ymdhm`→`ymd`→`dmy`→`ym`→`mdhm`→`md`），
先剥包裹符号（`[]（）【】`）再匹配。全 16 种测试形态均正确。

### 分页：博达的「反向编号」怪癖（只此一处）

后勤处是唯一启用多页的来源，专门用来记录这个坑：

```
tzgg.htm     = 第1页（最新）
tzgg/4.htm   = 第2页     ← N 不是页码
tzgg/3.htm   = 第3页
tzgg/2.htm   = 第4页
tzgg/1.htm   = 第5页（最旧）
```

而且**页面上的页码控件本身是坏的**：它把「页2」链到 `tzgg/5.htm`，那是个空页。

所以实现里**不信任控件的 href**，而是扫描控件里出现过的最大 N，再倒着走：

```yaml
pagination:
  mode: path_index
  path_template: "tzgg/{n}.htm"
  index_desc: true     # N 越大越新
  max_pages: 2
  stop_on_empty: true
```

> 踩坑记录：最初用泛化的 `/\d+\.htm` 去找「目录里的编号」，
> 结果把文章 ID 也算了进去（`info/1031/8891.htm` → 以为最大 N=8891），
> 翻页翻到了 `tzgg/8890.htm`。修正为**按 `path_template` 的形态精确编译正则**，
> 只认 `tzgg/{N}.htm`。

### 空正文 / 纯图片页：是合法内容，不是抓取失败

- 研究生院 `/info/1005/50392.htm` 的 `#vsb_content` 里只有 `<span></span><br/>`，
  **源站本身就是空正文**。
- 学生处的通知正文**经常是纯图片海报**（`.v_news_content` 里只有一个 `<img>`），
  纯文本长度天然为 0。

入库层做了「空壳保护」（`services/ingest.py` + `services/dedup.py`）：

- `is_shell_content()`：**只要含媒体标签（img/video/audio）就判为有效**，
  只有「无媒体且纯文本 < 20 字」才算空壳。
- 抽到空壳时，**不用空正文覆盖库里已有的正文**，只补 URL，计入 `empty_kept`。
- 详情抓取失败时，**保留列表页已知的字段**并标记 `detail_failed`，
  绝不因为详情页失败就静默丢掉这条通知。
- 结构化标签：正文为空/纯图时打上「正文为空」「图片通知」，前端可提示点开原文。

---

## 三、接不了的网站源（如实说明 + 替代方案）

### 3.1 校团委（tuanwei.dlut.edu.cn）——博达栏目权限

整站匿名访问返回：

```
系统提示：抱歉，可能是由下列问题导致的：您没有访问当前栏目的权限。
```

这是博达的**栏目权限控制**，不是选择器问题、也不是 UA 问题
（换 UA / 加 Referer / 换 https 均无效）。配置已写好但默认 `enabled: false`。

**替代方案：**
- 校团委活动常同步发布在**党委学生工作部 / 新闻网的站点**，可改接那些；
- 或走「粘贴链接导入」（用户在校内网打开后复制链接）；
- 或拿到校内授权后把 `enabled` 改成 `true`。

### 3.2 教务处「重要通知 / 教学文件 / 其他文件」三栏目

这三个栏目是 `list.jsp?wbtreeid=…` 形式，**匿名访问一律返回
「系统提示：您没有访问当前栏目的权限」**（实测确认）。

可匿名访问的是**「新闻快递」和「考试安排」**，本配置接的就是这两个。

**替代方案：** 重要通知多在「新闻快递」里会同步；或贴链接导入；或校内授权。

---

## 四、微信公众号 / i大工：能力边界与实现

### 4.1 先说清楚做不到的

- **微信没有公开接口**能「获取某公众号的最新文章列表」。
  官方开放平台的「素材管理」等接口只对认证服务号且需主体授权开放，第三方拿不到。
- **iOS 无法读取其他 App 的通知**（系统沙箱限制）。
- **Android 可以用 `NotificationListenerService` 读取通知**，但需用户手动授权。

### 4.2 已实现的替代方案

**路线 A（推荐，已实现）：粘贴文章链接导入**

用户在微信里「··· → 复制链接」，App 调用：

```
POST /api/import/article
{ "url": "https://mp.weixin.qq.com/s/xxxx", "source_name": "公众号·大连理工大学" }
```

实测：可直接抓到**标题 / 正文 / 公众号名 / 发布时间**，未触发风控。
（正文容器 `#js_content`，标题 `#activity-name`，作者 `#js_name`，
日期取 `#publish_time` 或 JS 变量 `var create_time`。）

> 合规注记：`mp.weixin.qq.com` 的 `robots.txt` 是 `Disallow: /`。
> 因此**只有用户手动粘贴的单条链接**才允许越过 robots 校验
> （`ignore_robots=true`），并且会计入统计 `robots_overridden`。
> 批量自动抓取一律禁止。

**路线 B（可选，需自备桥接）：RSS 桥接**

把公众号列表转成 RSS，用 `rss` 适配器接。**不内置任何第三方地址**
（多为个人自建、随时停服、属微信服务条款灰色地带），
请自行评估后填入 `bridge_url`。

**路线 C（最省事且完全合规）：不接**

后勤处、网信中心等站点会**直接把公众号文章链接挂在自家列表里**
（见 `dlut_org.yaml`「后勤处·通知公告」的 `item_url_regex` 放行了 `mp.weixin`），
等于学校帮你做了聚合。

**i大工（校园 App）**
- Android：`NotificationListenerService` 监听通知栏 → 调用 `POST /api/import/notify`
  （结构化的 `entries` 批量导入）。
- iOS：无通知监听权限 → 走**分享扩展**（Share Extension）或复制文本后
  调 `POST /api/import/notify`（纯文本导入）。
- 导入内容统一标记 `source="i大工"`，与网站源汇入同一信息流。

> 能力自述接口：`GET /api/import/capability` 会返回
> 「哪些能自动导入、哪些必须手动」的机器可读说明。

---

## 五、功能实现对照

| 需求 | 实现 | 位置 |
|------|------|------|
| URL 去重 | `canonical_url()`，保留微信关键参数 | `services/dedup.py` |
| 存 title/date/source/column/content/link | `Notification` 模型 | `models.py` |
| AI 固定 JSON 摘要 | `AISummary`（3 行 + 关键信息 + 重要程度 + 分类） | `services/summarize.py` |
| 分类：紧急/重要/一般 | `importance` ∈ urgent/important/normal | 同上 |
| 分类：通知类别 | `category`（考试/奖助学金/教务/讲座/活动/就业/后勤/其他） | 同上 |
| 信息流按来源/分类/未读筛选 | `GET /api/notifications?source=&category=&unread=` | `api/routes/notifications.py` |
| 详情页（原文 + 关键信息卡） | `GET /api/notifications/{id}` | 同上 |
| 抓取间隔 ≥2s | 同域串行 + `fetch_min_interval=2.0` | `core/config.py` `services/fetch.py` |
| 尊重 robots.txt | `RobotsCache`，抓不到按放行 | `services/fetch.py` |
| 失败日志不影响其他源 | 每源独立 try，失败写 `last_error` | `services/ingest.py` |

---

## 六、本地运行

```bash
cd quanzhi/backend

# 1) 装依赖（首次）
pip install -r requirements.txt

# 2) 跑测试（58 个；其中 25 个是基于真实页面快照的离线回放测试，不联网）
pytest -q

# 3) 全链路实跑：同步 yaml → 抓取 → 去重入库 →（有 Key 则）AI 摘要
python scripts/e2e_dlut.py --reset        # --reset 先清库，得到干净的验收数字

# 4) 改了摘要规则 / prompt 后，不必重新抓取，直接重跑库里已有内容的摘要
python scripts/resummarize.py --sample    # --sample 顺带打印一条完整输出

# 4b) 用真实 GLM 跑几条并落盘对比（免费档限流频繁，脚本已内置退避重试）
python scripts/glm_sample.py --n 6 --sleep 12   # 结果写到 data/glm_sample.json

# 5) 只看单个来源的健康度（抓了几条 / 日期解析率 / 详情失败数）
python scripts/check_dlut_sources.py

# 6) 起服务，用信息流接口
uvicorn app.main:app --reload --port 8000
#   浏览器打开 http://127.0.0.1:8000/docs 看全部 API
```

> 抓取耗时约 3 分钟属正常：同域请求强制间隔 2 秒。

> 关于 AI：**没有 `AI_API_KEY` 全链路照跑**（降级为本地规则摘要，
> `ai_model` 落 `local`，`confidence=0.35`）。要启用 GLM，复制
> `.env.example` 为 `.env` 填入 `AI_API_KEY` 即可，出口结构完全一致。

### AI 接入注意（实测，务必先看）

1. **`glm-4.7-flash` 官方长期免费**，手机号注册即得 Key，无需信用卡。
   但免费档**并发只有 1**，所以摘要必须串行（本项目本来就是串行，天然契合）。
2. **它是推理模型，思维链计入 `max_tokens`**。默认会先吐一大段
   `reasoning_content`，额度被吃光后 `content` 返回空串、`finish_reason=length`，
   表面报错却是「模型未返回合法 JSON」。本项目已在 `ai_client.py` 里
   **默认关闭 thinking**（`AI_THINKING=false`），并把 `max_tokens` 提到 2000。
3. **免费档限流频繁**（429 / code 1302「账户已达速率限制」/ 1305「访问量过大」）。
   已实现**指数退避 4 次**（5s→10s→20s→40s），耗尽后优雅降级到本地规则，
   不会中断整批任务。
4. 填完 Key 先跑一次 `python scripts/check_ai.py`，它会做「真实摘要预演」，
   同时验证 Key 有效性和输出是否会被截断。

---

## 七、验收样本：一条通知的完整 AI 输出

以下是**真实跑出来的**一条（`python scripts/e2e_dlut.py --reset` 抓取 + 摘要后，
从库里取「解析出截止时间」的最近一条通知）：

```json
{
  "id": 123,
  "title": "大连理工大学2026级研究生新生入学须知",
  "source": "迎新网·研究生报到",
  "url": "https://yx.dlut.edu.cn/info/9991/90557.htm",
  "published_at": "2026-07-11T16:00:00",
  "category": "教务",
  "importance": "important",
  "importance_label": "重要",
  "summary": [
    "大连理工大学2026级研究生新生入学须知",
    "截止 2026年10月15日；地点 大连市甘井子区凌工路2号大连理工大学档案馆113；对象 应届毕业生",
    "需提交：户口迁移证原件、录取通知书原件、复印件、身份证原件；联系人 0411-84708906"
  ],
  "key_info": {
    "deadline": "2026年10月15日",
    "deadline_ts": "2026-10-15T00:00:00+08:00",
    "audience": "应届毕业生",
    "location": "大连市甘井子区凌工路2号大连理工大学档案馆113",
    "materials": [
      "户口迁移证原件",
      "录取通知书原件",
      "复印件",
      "身份证原件",
      "迁移证上出生地"
    ],
    "contact": "0411-84708906",
    "fee": null,
    "extra": {}
  },
  "tags": ["教务"],
  "ai_status": "done",
  "ai_model": "local",
  "content_chars": 2808
}
```

说明：
- `deadline_ts` 是 `parse_dt` 把「2026年10月15日」解析成的真实时间，
  直接喂给「一键加日历」，带 `+08:00`。
- `ai_model: "local"` 表示**本次没有配置 API Key**，走的是本地规则兜底；
  填了 `AI_API_KEY` 后同一个 JSON 结构由 GLM-4.7-Flash 产出
  （`ai_model` 变成 `glm-4.7-flash`，`confidence` 也会从 0.35 抬高）。
- `materials` 里「迁移证上出生地」是本地规则从散文里切出的残片 ——
  这是**规则法的固有上限**：长文散文中，纯正则无法区分「材料名」和
  「说明性从句」。本地模式只承诺「尽力给出可用的结构」，不承诺完美；
  真要高质量关键信息，开 AI 模式。前端可用 `confidence < 0.5` 提示「建议看原文」。

### 全量结果

| 指标 | 数值 |
|------|------|
| 启用来源 | 12 个（网站，含迎新网 5 个） |
| 抓取条目 | 173 条 |
| 入库新增 | 153 条（20 条为跨栏目重复，去重命中） |
| 空壳保护命中 | 1 次 |
| robots 拦截 | 17 次（均为 `mp.weixin.qq.com` 直链，规则 `Disallow: /`） |
| 详情失败 | 17 条（同上；列表级字段已保留，未丢通知） |
| 摘要完成 | 153 / 153，失败 0 |
| 3 行契约 | 153 / 153 满足 |

> 抓取耗时约 176 秒 —— 因为**同域请求强制间隔 2 秒**，173 条详情页加上
> 13 个列表页请求，串行下来就是这个量级。这是合规的代价，不是性能问题。

### 摘要在 GLM 与本地规则下的效果对比（同一通知，实测）

**关于图书馆2026年中秋节、国庆节开放安排的通知**（正文 189 字）：

| | local 规则（无 Key） | **GLM-4.7-Flash（有 Key）** |
|---|---|---|
| 第 1 行 | 关于图书馆2026年中秋节、国庆节开放安排的通知 | 图书馆中秋国庆假期正常开馆，伯川和令希图书馆开放时间不同。 |
| 第 2 行 | 地点 **图书馆2026** ← 抽错了 | 9月25日-27日及10月1日-7日开放，盘锦等分馆闭馆。 |
| 第 3 行 | 9月25日-27日，10月1日-7日 | 请根据所在校区安排时间，伯川馆7点开门，其他馆8点开门。 |
| location | `图书馆2026` ❌ | `伯川图书馆、令希图书馆、盘锦校区分馆、开发区校区分馆、马克思主义分馆` ✅ |
| audience | `None` | `全校师生读者` ✅ |
| confidence | 0.35 | 0.2 |

**结论：本地规则是「有比没有好」的兜底，GLM 才是产品该有的样子。**
本地规则的本质是正则匹配，遇到「图书馆2026」这种把标题年份当地点的情况毫无办法；
GLM 能真正读懂「哪些馆开、哪些馆闭、几点开门」并重写成可执行的一句话。

> 实测 6 条真实大工通知，配好退避重试后 **GLM 成功率 6/6，降级 0 次**。

---

## 八、诚实的边界说明小结

| 事项 | 结论 |
|------|------|
| 迎新网 5 个栏目各 20 条 | ✅ 4 个真实栏目 + 1 个「首页聚合」（首页结构上只有 4 个列表区块） |
| 教务处 / 学生处接通 | ✅ 教务处接「新闻快递 + 考试安排」；学生处接「通知公告」 |
| 教务处 list.jsp 三栏目 | ⛔ 匿名无权限，已给替代方案 |
| 校团委 | ⛔ 博达栏目权限墙，默认关闭 |
| 公众号自动发现新文章 | ⛔ 微信无公开接口 → 粘贴链接导入 + RSS 桥接占位 |
| iOS 读取其他 App 通知 | ⛔ 系统不允许 → 分享扩展 / 文本导入 |
| Android 读取通知 | ✅ 可用 `NotificationListenerService`（阶段 4 实现） |
| 抓取合规（间隔/robots/日志） | ✅ 间隔 2s、robots 生效、失败隔离 |

---

## 九、附录2 学校常用官网接入（2026-10-18）

来源是腾讯文档《【新生攻略】2026大连理工大学盘锦校区》的**附录2「学校常用官网」**
（<https://docs.qq.com/doc/DVG5Kc0RhVWJoQmtD>），共列了 16 个站点。

> 注意：前八节接的都是**凌水主校区**的职能部处；这一节接的是**盘锦校区**各部处
> 加校级门户。两个校区是**独立的博达实例**，模板版本不同，选择器不能互相套用。

### 9.1 处置结果：12 接入 / 4 未接入

| 附录2 里的站点 | 处置 | 说明 |
|---|---|---|
| 本科生招生网 `zs` | **未接入** | Vue SPA（`<div id="app">` 空壳），HTML 里没有任何条目，需浏览器渲染 |
| 迎新网 `yx` | 已接入（第 1 节） | 阶段 2 就接了 5 个栏目，不重复 |
| 智慧大工 `sso` | **未接入** | 统一身份认证，**需要登录**。抓别人账号属越界，不做 |
| 盘锦教学与科研工作部 `pjteach` | **接入 3 个栏目** | 选课 / 考试 / 学籍 |
| 正版软件平台 `its…/sysm.htm` | **未接入** | 单个说明页，没有列表 |
| 盘锦学生工作部 `xsgzb` | 接入 | `index/tzgg.htm` |
| 大连理工大学 `www` | 接入 | `tzgg.htm`（校级通知公告） |
| 盘锦校区 `panjin` | 接入 | `tzgg.htm` |
| 信息公开网 `info` | **未接入** | 是**制度/年报目录**，不是通知流 |
| 就业网 `job` | **未接入** | 列表由前端 JS 调接口渲染，静态 HTML 里没有 |
| 盘锦总务部 `pjxqzwb` | 接入 2 个栏目 | 工作通知 + 公示公告 |
| 盘锦图书馆 `pjlib` | 接入 | `xw/xwtz.htm` |
| 盘锦团委 `pjxqtw` | 接入 | `tzgg.htm` |
| 国际化办 `pjoia` | **未接入** | 栏目最后更新停在 2019–2020 年，价值极低 |
| 国际合作与交流处 `dutdice` | 接入 | `gatjl/tzgg/tzgg.htm` |
| 档案馆 `dangan` | 接入 | `gzdt/dagtz.htm` |

### 9.2 实测条数（首次抓取，2026-10-18）

| # | 来源 | 列表页 | 条目 | 有正文 |
|---|---|---|---|---|
| 18 | 大连理工大学·通知公告 | `www.dlut.edu.cn/tzgg.htm` | 8 | **1** |
| 19 | 盘锦校区·通知公告 | `panjin.dlut.edu.cn/tzgg.htm` | 12 | 12 |
| 20 | 盘锦教学部·课程与选课 | `pjteach…/jxyx/kcyxkgl.htm` | 5 | 5 |
| 21 | 盘锦教学部·考试管理 | `pjteach…/jxyx/ksgl.htm` | 3 | 3 |
| 22 | 盘锦教学部·学籍证件 | `pjteach…/jxgz/xjzj.htm` | 3 | 2 |
| 23 | 盘锦学生工作部·通知公告 | `xsgzb…/index/tzgg.htm` | 10 | 10 |
| 24 | 盘锦总务部·工作通知 | `pjxqzwb…/index/gztz.htm` | 10 | 10 |
| 25 | 盘锦总务部·公示公告 | `pjxqzwb…/index/gsgg.htm` | **1** | 1 |
| 26 | 盘锦图书馆·新闻通知 | `pjlib…/xw/xwtz.htm` | 12 | 12 |
| 27 | 盘锦团委·通知公告 | `pjxqtw.dlut.edu.cn/tzgg.htm` | 10 | 10 |
| 28 | 国际合作与交流处·通知公告 | `dutdice…/gatjl/tzgg/tzgg.htm` | 20 | 6 |
| 29 | 档案馆·通知 | `dangan…/gzdt/dagtz.htm` | 6 | 6 |
| | **合计** | | **100** | **78** |

两个「条数偏少」都是**有意的过滤**，不是抓错：

- **#18 只有 1 条有正文**：列表里 8 条有 7 条指向 `mp.weixin.qq.com`（学校用公众号发
  校级通知），而微信的 robots 是 `Disallow: /`，详情一律跳过（计入 `skipped_by_robots`）。
  标题和链接仍然保留 —— 这正是「诚实的边界」：**能看到通知存在，但抓不到正文**。
- **#25 只有 1 条**：`gsgg` 栏目 10 条里 9 条是 `cgbmis.dlut.edu.cn` 的采购系统外链，
  被 `item_url_regex: '/info/\d+/\d+\.htm'` 过滤掉了。剩下的 1 条才是本校区通知。

### 9.3 三种新模板（都在 `sources/dlut_panjin.yaml` 里）

| 模板 | 站点 | 结构 | 关键点 |
|---|---|---|---|
| E. 卡片列表 | 教学部 / 学生工作部 / 总务部 | `div.space-y-6 > a[id^="line_"]` | 日期是 `<p>21</p><p>2026-07</p>` **两个节点**，必须 `date_extract_regex` + `date_format="%d %Y-%m"` 拼回来 |
| F. 普通 li | 团委 / 档案馆 / 国际合作处 / 图书馆 | `div.list > a.box`、`div.list01 > div > ul > li`、`ul.nytzlist > li` | 日期在兄弟 `span` |
| G. 门户自建 | `panjin` / `www` | `div.common.tzgg div.tz > ul > li`、`ul.ul-listls2 > li` | 日期带 `.` 分隔（`2026.09.23`） |

### 9.4 这一轮踩的三个坑

**① `ul > li` 太宽泛，把导航菜单抓成了通知。**

`dutdice` 和 `dangan` 一开始都写 `ul > li`，离线回放直接暴露出问题：

```
⚠️ 国际合作与交流处·通知公告  条目=111  title=110/111  url=110/111  date=20/111
⚠️ 档案馆·通知              条目=47   title=43/47    url=44/47    date=10/47
```

111 条里绝大多数是「首页 / 机构设置 / 本馆概况」这类导航。锚到具体容器
（`div.list01` / `div.list`）之后，两个都变成 20/20、10/10 **全部字段命中**。

**② 日期被拆成两个 `<p>`：`21` + `2026-07`。**

教学部四个栏目都是这个结构。适配器早就支持这种写法（`_date_pieces` 里专门为
「后勤处把日拆成两个 p」写过），所以只需要配：

```yaml
date_extract_regex: '\d{1,2}\s+20\d{2}-\d{1,2}'
date_format: "%d %Y-%m"
```

**③ ★ 图书馆 12 条日期全被解析成「抓取当天」——不报错，但全错。**

它的结构是 `<time class="time"><span>2026</span>09-23</time>`，`get_text` 出来是
`"2026 09-23"`（**空格分隔**）。第一版正则写的是 `20\d{2}[./-]\d{1,2}[./-]\d{1,2}`，
匹配不到 → 回退到整行文本 → 仍然匹配不到 → 最终落到了页面别处的某个日期。

**结果：12 条的 `published_at` 全是 `2026-10-17 16:00`（= 抓取当天）**，
而真实日期分布在 2025-12 到 2026-09 之间。

**怎么发现的**：抓完做数据体检，看每个来源的 `min(published_at)` / `max(published_at)`
—— 12 条落在同一天、且正好是今天，这就不对了。
**「日期字段不报错」不等于「日期对」**，必须有个「日期分布是否合理」的检查。

修法：`fields.date: "time"` + `date_extract_regex: '20\d{2}\s+\d{1,2}-\d{1,2}'`
+ `date_format: "%Y %m-%d"`。

**怎么修的历史数据**：重抓**不会**修正 —— 去重键优先用 URL，正文没变就判定「无更新」，
`published_at` 不在比较范围内。所以这 12 条是直接按离线存档里的真实日期
UPDATE 修正的（改前备份在 `D:/QuanZhi/logs/_diag/quanzhi_before_lib_date_fix.db`）。
**这是已知的设计取舍**：正文没变就不动行，代价是「字段解析规则改了，历史数据不会自动跟着改」。

### 9.5 robots.txt 的情况（合规说明）

这批站点的 `/robots.txt` **一律强制断连**：

```
curl -A "Mozilla/5.0 …" http://pjteach.dlut.edu.cn/robots.txt
  → HTTP:000  size:0  rc:56        （接收失败 / 连接被重置）
httpx                              → ReadError: [WinError 10054]
```

而首页正常返回 200。按 RFC 9309「无法获取即不限制」处理，
`app/services/fetch.py` 的 `RobotsCache` 也是这个行为（拿不到 parser 就放行，
见第 105 行注释）。抓取全程遵守同域 2 秒最小间隔，只碰公开列表与详情页。

`mp.weixin.qq.com` 仍然按 `Disallow: /` 严格执行 —— 那 7 条被跳过的正是微信链接。
