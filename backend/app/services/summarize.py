"""摘要服务：把一条通知变成「3 行摘要 + 关键信息 + 重要程度」。

两种模式自动切换：
  - 有 AI_API_KEY → 调 GLM，prompt 模板来自 app/prompts/summarize.v1.md
  - 没有 API Key / 调用失败 → LocalSummarizer 本地规则兜底

**无论哪种模式，出口都是同一个 `AISummary` 结构**，所以下游代码不用关心用了哪种。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from app.core.config import Settings, get_settings
from app.core.timeutil import normalize_text, parse_dt
from app.schemas import AISummary, KeyInfo
from app.services.ai_client import AIClient, AIError
from app.services.prompts import PromptLibrary, get_prompt_library

logger = logging.getLogger(__name__)

# 免费档有并发/频率限制（实测返回 429 / code 1302「账户已达速率限制」、
# code 1305「当前访问量过大」）。这类失败与内容无关，退避重试通常能过。
_RATE_LIMIT_WAIT = 5.0
_RATE_LIMIT_RETRIES = 4
_RATE_LIMIT_HINTS = ("429", "1302", "1305", "访问量过大", "速率限制", "rate limit", "too many")


class _RateLimited(RuntimeError):
    """AI 侧限流。区别于内容类错误 —— 只有它值得重试。"""


def _is_rate_limited(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(h.lower() in msg for h in _RATE_LIMIT_HINTS)

# ---- 本地兜底用的关键词 ----
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "考试": ("考试", "考场", "补考", "重修", "四六级", "考研", "监考", "准考证", "成绩"),
    "奖助学金": ("奖学金", "助学金", "国家奖", "励志", "补助", "资助", "助学贷款", "评优", "评奖"),
    "教务": ("选课", "退课", "教学", "培养方案", "学籍", "注册", "毕业", "论文", "答辩", "教务"),
    "讲座": ("讲座", "报告会", "学术报告", "论坛", "沙龙", "seminar"),
    "活动": ("活动", "比赛", "竞赛", "招募", "社团", "晚会", "运动会", "征集", "报名参加"),
    "就业": ("招聘", "就业", "双选会", "宣讲会", "实习", "岗位", "简历", "offer"),
    "后勤": ("宿舍", "食堂", "水电", "维修", "网络", "一卡通", "校车", "停电", "停水", "后勤"),
}

URGENT_WORDS = ("紧急", "立即", "今天截止", "今晚", "务必于", "逾期不", "最后一天", "即将截止", "抓紧")
IMPORTANT_WORDS = ("截止", "务必", "请注意", "重要", "需提交", "须提交", "前完成", "前提交")

_DEADLINE_PATTERNS = (
    r"(?:截止|截至|deadline)[^\n。；;]{0,20}?((?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}日?(?:\s*\d{1,2}:\d{2})?)",
    r"((?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}日?(?:\s*\d{1,2}:\d{2})?)\s*(?:前|之前)\s*(?:完成|提交|提交材料|报名|申请|截止)",
    r"于\s*((?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}日?(?:\s*\d{1,2}:\d{2})?)\s*(?:前|之前)",
)

# 地点：三档从精确到宽松
_LOCATION_PATTERNS = (
    # ① 「（逸夫楼305）」这类括号地点最靠谱
    r"[(（]([\u4e00-\u9fa5]{2,12}(?:楼|馆|厅|室)[A-Za-z0-9\-]{0,6})[)）]",
    # ② 显式「地点：XXX」
    r"(?:地点|地址|地点为)\s*[:：]\s*([^\n，。；]{2,24})",
    # ③ 兜底：以楼/馆/厅/教室/报告厅结尾的短语（放宽到 14 字，
    #    否则「凌水校区学生服务大厅」会被截成「水校区学生服务大厅」）
    r"([\u4e00-\u9fa5]{2,14}(?:楼|馆|厅|教室|报告厅|服务大厅)[A-Za-z0-9\-]{0,6})",
)

_LOCATION_LEAD_RE = re.compile(r"[到至在于]\s*")

_CONTACT_RE = r"(?:联系人|咨询电话|联系电话|联系方式)\s*[:：]?\s*([^\n。；;]{2,80})"
_CONTACT_STRIP_RE = r"^(?:联系人|咨询电话|联系电话|联系方式)\s*[:：]?\s*"
_PHONE_RE = r"(?:\d{3,4}-\d{7,8}|1[3-9]\d{9})"
# ⚠️ 邮箱**必须**限成 ASCII：Python 的 `\w` 默认匹配 Unicode 词字符，**包含汉字**。
# 早期写成 `[\w.\-+]+@…` 时，「登记表请发送至邮箱dgzz@dlut.edu.cn」会被整个当成邮箱
# （match.start()==0，本地部分是「登记表请发送至邮箱dgzz」），前缀清洗直接失效。
_EMAIL_RE = r"[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+"
# 引导词清洗后可能残留全角标点（「李老师，」里的「，」），统一按「只保留中文人名」收敛。
_NON_NAME_CHAR_RE = re.compile(r"[^\u4e00-\u9fa5·]")

# 材料抽取：先认「材料包括：xxx」这种明确写法，实在没有再退到宽松匹配。
# 只写宽松版会把「将材料提交至教务处（行政楼208）」里的地点当成材料，必须分两档。
_MATERIAL_ANCHORED_RE = r"材料(?:包括|如下|有|为)?\s*[:：]\s*([^\n。；;]{2,80})"
_MATERIAL_LOOSE_RE = r"(?:携带|上交|附上|(?:需|须)提供)\s*[:：]?\s*([^\n。；;]{2,60})"
# 宽松匹配的清洗：过滤掉明显不是材料的东西
_MATERIAL_NOISE_RE = re.compile(r"^(?:至|到|在|于|教务处|学院)|(?:办公室|教学楼|行政楼|学生证)$")
# 宽松匹配会从**散文**里切出一堆句子碎片。实测《新生家长指南》抽出过
# 「的是」「的重要材料」「务必带好」「此外」，研究生入学须知抽出过
# 「籍贯栏必须具体到**省」这种**说明性句子**。材料名应该是个名词短语，
# 不该含标点/星号/「必须/根据」这类措辞。三道一起挡：
#   ① 虚词/动词**开头**（的/请/如…）
#   ② 句中含标点、引号、星号（说明性句子的特征）
#   ③ 含说明性措辞，或以「等」这类收尾词结尾
# ⚠️ 「请/需/应」只能作为**开头**判据：实测「申**请**表」里的「请」在词中间，
#    一旦放进「句中包含」的集合，所有申请表都会被误杀。
_MATERIAL_JUNK_RE = re.compile(
    r"^(?:的|了|是|和|及|等|也|还|此|该|其|并|或|与|如|可|请|务|以|为|需|应)"
    r"|[，,。；;*\"'“”＝=]"
    r"|必须|不能|根据|自行|地址|如下|应当"
    r"|(?:等|等等|同时|另外|此外)$"
)
# 材料后面常跟着「去哪儿办」，要把这段尾巴切掉。
# 实测：大工新生医保通知写的是「可携带参保相关证件到 大连市医保中心…办理」，
# 不切尾巴就会抽出「参保相关证件到」这种半截词。
_MATERIAL_TAIL_RE = re.compile(
    r"(?:到|至|在|于)\s*[\u4e00-\u9fa5A-Za-z0-9]{2,}(?:厅|楼|室|馆|处|中心|校区|窗口|办事处|学院|平台|系统).*$"
)
_MATERIAL_VERB_TAIL_RE = re.compile(r"(?:办理|领取|提交|递交|下载|填写|激活|打印).*$")

# 对象：**必须**以人群名词结尾，否则会把标题本身当成"适用对象"。
# 实测踩过：标题「2026级新生医疗保险投保指南」被 `[^\n]{0,20}` 整段吞进去当成对象。
_AUDIENCE_RE = re.compile(
    r"((?:20\d{2}\s*级|大[一二三四五]|全体|全校|应届|在校|各[\u4e00-\u9fa5]{1,8}(?:学院|专业|班级))"
    r"[\u4e00-\u9fa5]{0,8}?"
    r"(?:本科生|专科生|研究生|硕士生|博士生|新生|毕业生|同学|学生|教职工))"
)

_FALLBACK_LINE = "原文信息较少，建议点开看全文"

#: 正文没抓到时的第 2 行：说清「为什么只有标题」。
#: 以前这里和第 3 行填的是同一句 _FALLBACK_LINE，于是一张卡片上
#: 「关键信息」和「该怎么办」是复读——形式上三行齐全，语义上少了一行。
#: 实测 2026-10-23：49 条 local-no-content 里有 31 条是这种复读。
_NO_CONTENT_LINE = "只抓到标题，正文（图片/附件/登录页）没能提取"

#: 第 3 行永远是「你能做什么」：不能是形容词，也不能复读第 2 行。
_ACTION_LINE = "点开原文确认具体日期和要求"

#: 正文短于这个字数就**不调 AI**，直接用本地规则出摘要。
#: 为什么：模型手上只有标题时，会把「关于放假安排的通知」编成
#: 「9月25日至10月7日放假调休」这种**看起来很像真的**具体事实。
#: 实测 2026-10-18 抓到的大工校级通知（7 条都是公众号链接，robots 禁止抓正文），
#: 摘要里凭空出现了调休日期和上课安排 —— 这是会误导用户的。
#: 宁可给「原文信息较少，建议点开看全文」，也不给一条编的。
_MIN_AI_CONTENT_CHARS = 30

# 「正文其实是提示页」的识别标记。比较前会把正文里的空白全去掉 ——
# 博达把提示拆成多个 <p>/<div>，normalize_text 之后常带换行。
# 只放**强特征**：「请先登录」这种单独出现太常见（通知里教你怎么登录），不列。
_ERROR_PAGE_MARKERS = (
    "无权访问",
    "您访问的页面不存在",
    "页面不存在",
    "访问受限",
    "403Forbidden",
    "404NotFound",
    "AccessDenied",
    "系统提示抱歉",
)

# 正文超过这个长度就不当提示页看了 —— 两千字的通知不可能整篇都是提示语。
_ERROR_PAGE_MAX_CHARS = 150


def _looks_like_error_page(text: str) -> bool:
    """正文是不是「无权限 / 404 / 请登录」提示页。

    2026-10-19 实测：14 条港澳台项目通知的详情页返回博达的
    「系统提示 抱歉 可能是由下列问题导致的：您无权访问此页面」（29 字，HTTP 200）。
    字数刚好卡在 30 字阈值下面，所以没进 AI；但本地规则把
    「您无权访问此页面」当成第二行摘要展示出来了 —— 用户看到的是系统提示，不是通知。
    """
    t = re.sub(r"\s+", "", text or "")
    if not t or len(t) > _ERROR_PAGE_MAX_CHARS:
        return False
    return any(m in t for m in _ERROR_PAGE_MARKERS)


# 「像人名」的判定：2-4 个汉字，且不以「东西类」后缀结尾。
# 用来挡住「登记表」「申请表」「教学系统」这类被误当成联系人的前缀。
_NOT_A_NAME_SUFFIX = ("表", "单", "箱", "站", "统", "页", "件", "号", "科", "处", "室", "部")


def _looks_like_person_name(text: str) -> bool:
    if not text or not (2 <= len(text) <= 4):
        return False
    if not re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", text):
        return False
    return not text.endswith(_NOT_A_NAME_SUFFIX)


@dataclass(slots=True)
class SummarizeResult:
    summary: AISummary
    model: str
    error: str | None = None


class SummarizeService:
    def __init__(
        self,
        settings: Settings | None = None,
        ai: AIClient | None = None,
        prompts: PromptLibrary | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.ai = ai or AIClient(self.settings)
        self.prompts = prompts or get_prompt_library()

    # ------------------------------------------------------------------ #
    async def summarize(
        self,
        *,
        title: str,
        content: str | None,
        source_name: str = "",
        school: str = "",
        published_at: str | None = None,
        category_hint: str = "其他",
    ) -> SummarizeResult:
        text = normalize_text(content) or ""
        if len(text) > self.settings.ai_max_input_chars:
            text = text[: self.settings.ai_max_input_chars] + "…"

        # ★ 正文其实是「无权访问 / 404 / 请登录」提示页 —— 也算没抓到正文。
        #   这类页面 HTTP 200、字数可能刚好压过 30 字阈值，于是：
        #     · 本地规则会把「您无权访问此页面」当成第二行摘要展示给用户；
        #     · 若真喂给模型，它会拿这句系统提示当素材继续编。
        blocked = _looks_like_error_page(text)
        if blocked:
            logger.info("详情页是登录/权限提示页，按无正文处理：%s", (title or "")[:40])
            text = ""

        # ★ 正文没抓到就别让 AI 上手（见 _MIN_AI_CONTENT_CHARS 的说明）。
        #   走到这里的常见原因：公众号链接被 robots 拦、博达返回「您无权访问此页面」、
        #   详情页 404。这时模型只能看着标题猜，猜出来的具体日期最危险。
        if len(text.strip()) < _MIN_AI_CONTENT_CHARS:
            logger.info(
                "正文仅 %d 字，跳过 AI 改用本地规则：%s",
                len(text.strip()), (title or "")[:40],
            )
            return SummarizeResult(
                summary=self._local_summarize(
                    title=title, content=text, category_hint=category_hint, blocked=blocked
                ),
                model="local-no-content",
            )

        if self.ai.enabled:
            last_err: Exception | None = None
            # 免费档限流很频繁（实测 1302「账户已达速率限制」/ 1305「当前访问量过大」），
            # 所以按**指数退避**多试几次再降级 —— 一次退避 5s 往往不够。
            for attempt in range(_RATE_LIMIT_RETRIES):
                try:
                    return await self._ai_once(
                        title=title,
                        text=text,
                        source_name=source_name,
                        school=school,
                        published_at=published_at,
                        category_hint=category_hint,
                    )
                except _RateLimited as e:
                    last_err = e
                    wait = _RATE_LIMIT_WAIT * (2**attempt)
                    logger.warning(
                        "AI 限流（第 %d/%d 次），%.1fs 后重试：%s",
                        attempt + 1,
                        _RATE_LIMIT_RETRIES,
                        wait,
                        str(e)[:100],
                    )
                    await asyncio.sleep(wait)
                except (AIError, Exception) as e:  # noqa: BLE001
                    last_err = e
                    break
            logger.warning("AI 摘要失败，降级到本地规则：%s", last_err)
            assert last_err is not None
            return self._fallback(title, text, category_hint, last_err)

        return SummarizeResult(
            summary=self._local_summarize(title=title, content=text, category_hint=category_hint),
            model="local",
        )

    async def _ai_once(
        self,
        *,
        title: str,
        text: str,
        source_name: str,
        school: str,
        published_at: str | None,
        category_hint: str,
    ) -> SummarizeResult:
        try:
            summary = await self._ai_summarize(
                title=title,
                content=text,
                source_name=source_name,
                school=school,
                published_at=published_at or "",
                category_hint=category_hint,
            )
        except (AIError, Exception) as e:  # noqa: BLE001
            # 把「限流」从其它错误里挑出来，交给上层决定是否重试
            if _is_rate_limited(e):
                raise _RateLimited(str(e)) from e
            raise
        return SummarizeResult(summary=summary, model=self.ai.model)
    def _fallback(
        self, title: str, text: str, category_hint: str, error: Exception
    ) -> SummarizeResult:
        return SummarizeResult(
            summary=self._local_summarize(title=title, content=text, category_hint=category_hint),
            model="local-fallback",
            error=str(error)[:300],
        )

    # ------------------------------------------------------------------ #
    async def _ai_summarize(
        self,
        *,
        title: str,
        content: str,
        source_name: str,
        school: str,
        published_at: str,
        category_hint: str,
    ) -> AISummary:
        rendered = self.prompts.render(
            "summarize",
            "v1",
            title=title,
            source_name=source_name,
            school=school,
            published_at=published_at or "未知",
            category_hint=category_hint,
            content=content or "（正文为空，仅标题可用）",
        )
        system, user = self.prompts.split_system_user(rendered)
        # 2000 而非 1500：GLM-4.7-Flash 是推理模型，即使关掉 thinking 也要留余量，
        # 否则 3 行摘要 + key_info 会被从中截断，报「模型未返回合法 JSON」。
        payload = await self.ai.chat_json(system, user, max_tokens=2000)
        summary = AISummary.model_validate(payload)

        # 模型给的是原文表述，这里再解析成真实时间，供「一键加日历」用
        if summary.key_info.deadline and not summary.key_info.deadline_ts:
            summary.key_info.deadline_ts = parse_dt(summary.key_info.deadline)

        if not summary.summary_lines:
            raise AIError("模型未产出 summary_lines")
        return summary

    # ------------------------------------------------------------------ #
    # 本地规则兜底：没网 / 没 Key 也能给出可用的结构化结果
    # ------------------------------------------------------------------ #
    def _local_summarize(
        self,
        *,
        title: str,
        content: str,
        category_hint: str,
        blocked: bool = False,
    ) -> AISummary:
        if blocked:
            # 详情页根本打不开：一个字都不猜，只把「为什么只有标题」讲清楚。
            # 第 1 行仍用标题 —— 标题本身是抓取到的原文信息，可信。
            return AISummary(
                summary_lines=[
                    (title or "").strip()[:110] or _FALLBACK_LINE,
                    "原文页面需要登录或无权限，本条只抓到标题",
                    "点开原文查看；校内页面通常需在校园网内登录后访问",
                ],
                key_info=KeyInfo(),
                importance="normal",
                category=self._guess_category(title, "", category_hint),
                tags=["需登录"],
                confidence=0.15,
            )

        blob = f"{title}\n{content}"

        deadline_raw, deadline_ts = self._find_deadline(blob)
        key_info = KeyInfo(
            deadline=deadline_raw,
            deadline_ts=deadline_ts,
            audience=self._find_audience(blob, title),
            location=self._find_location(blob),
            materials=self._find_materials(blob),
            contact=self._find_contact(blob),
        )

        importance = "normal"
        if any(w in blob for w in URGENT_WORDS):
            importance = "urgent"
        elif any(w in blob for w in IMPORTANT_WORDS):
            importance = "important"

        category = self._guess_category(title, content, category_hint)
        lines = self._build_lines(title, content, key_info)

        return AISummary(
            summary_lines=lines,
            key_info=key_info,
            importance=importance,  # type: ignore[arg-type]
            category=category,  # type: ignore[arg-type]
            # 标签里不放 audience：本地规则出的对象经常只是「2026级新生」这种片段，
            # 当标签展示反而更乱；让 AI 模式去补标签
            tags=[t for t in (category,) if t][:4],
            confidence=0.35,  # 本地规则，置信度标低，前端可提示「建议查看原文」
        )

    @staticmethod
    def _find_audience(blob: str, title: str) -> str | None:
        """抽适用对象。

        两条硬约束，都是踩过坑加的：
          1. 正则必须以人群名词结尾，不能是"任意 20 字"
          2. 抽出来的东西如果是标题的一部分，直接丢掉（那是标题不是对象）
        """
        norm_title = re.sub(r"\s+", "", title or "")
        for m in _AUDIENCE_RE.finditer(blob):
            value = normalize_text(m.group(1))
            if not value:
                continue
            compact = re.sub(r"\s+", "", value)
            if compact and compact in norm_title:
                continue
            return value[:40]
        return None

    @staticmethod
    def _find_location(text: str) -> str | None:
        for pattern in _LOCATION_PATTERNS:
            m = re.search(pattern, text)
            if not m:
                continue
            value = normalize_text(m.group(1))
            # 第三档容易把「办公室」这种泛称抓进来，长度太短或纯泛称就跳过
            if value and len(value) >= 3 and not value.endswith(("校", "学", "的")):
                return SummarizeService._clean_location(value)[:40]
        return None

    @staticmethod
    def _clean_location(value: str) -> str:
        """把「服务专员每周二到凌水校区学生服务大厅」收敛成地点本身。

        第三档兜底正则是「…以楼/馆/厅结尾的最长中文串」，会连同前面的动词短语
        一起吃进来（实测抓到「服务专员每周二到凌水校区学生服务大厅」）。
        这里取最后一个地点引导词（到/至/在/于）之后的片段，前提是它仍像地点。
        """
        lead = None
        for lead in _LOCATION_LEAD_RE.finditer(value):
            pass
        if lead is not None:
            tail = value[lead.end():].strip()
            if len(tail) >= 3:
                return tail
        return value

    @staticmethod
    def _find_deadline(text: str) -> tuple[str | None, datetime | None]:
        for pattern in _DEADLINE_PATTERNS:
            m = re.search(pattern, text)
            if not m:
                continue
            raw = normalize_text(m.group(1))
            if not raw:
                continue
            return raw, parse_dt(raw)
        return None, None

    def _find_materials(self, text: str) -> list[str]:
        """两档抽取：明确写法优先，兜底再宽松匹配并清洗噪声。"""
        for pattern in (_MATERIAL_ANCHORED_RE, _MATERIAL_LOOSE_RE):
            found = self._split_materials(text, pattern)
            if found:
                return found
        return []

    def _split_materials(self, text: str, pattern: str) -> list[str]:
        found: list[str] = []
        for m in re.finditer(pattern, text):
            chunk = m.group(1)
            # 去掉「的材料包括：」这类引导语
            for sep in ("：", ":"):
                if sep in chunk:
                    chunk = chunk.split(sep, 1)[1]
                    break
            for p in re.split(r"[、,，和及]\s*", chunk):
                p = self._clean_material(p)
                if not p or p in found:
                    continue
                if _MATERIAL_NOISE_RE.search(p):
                    continue
                found.append(p)
            if len(found) >= 6:
                break
        return found[:6]

    @staticmethod
    def _clean_material(raw: str) -> str | None:
        """把「去哪儿办」的尾巴切掉，只留材料本身。

        原文：「可携带参保相关证件到 大连市医保中心高新园区办事处 经办窗口办理」
        期望：「参保相关证件」而不是「参保相关证件到」
        """
        p = normalize_text(raw).strip("。；; 、,，")
        p = _MATERIAL_TAIL_RE.sub("", p)
        p = _MATERIAL_VERB_TAIL_RE.sub("", p)
        p = p.strip("。；; 、,，到至在于 ")
        if not (2 <= len(p) <= 20):
            return None
        if _MATERIAL_JUNK_RE.search(p):
            return None
        return p

    @staticmethod
    def _find_contact(text: str) -> str | None:
        """抽联系方式。

        优先级：①「联系人/联系电话:」引导语**后面那一段** → ②全文就近找邮箱/电话。
        两档都拿不到就返回 None —— **绝不能把整篇正文当联系方式**。
        （踩过：5850 字的《新生家长指南》整篇被塞进了 contact 字段。）

        原文：「…发送至邮箱dgzz@dlut.edu.cn」
        期望：「dgzz@dlut.edu.cn」而不是「请发送邮件至dgzz@dlut.edu.cn」
        """
        m = re.search(_CONTACT_RE, text)
        if m:
            value = re.sub(_CONTACT_STRIP_RE, "", normalize_text(m.group(1))).rstrip("。；; ")
            if value:
                got = SummarizeService._contact_from(value, whole=False)
                if got:
                    return got
        return SummarizeService._contact_from(text, whole=True)

    @staticmethod
    def _contact_from(value: str, *, whole: bool) -> str | None:
        """从片段里挑联系方式。

        whole=True 表示 value 是**整篇正文**：这时只在号码前的一小段窗口里
        找人名，且没有号码就直接返回 None（不允许把正文回退成联系方式）。
        whole=False 表示 value 是引导语片段，没号码时允许回退成清洗后的人名。
        """
        email = re.search(_EMAIL_RE, value)
        phone = re.search(_PHONE_RE, value)
        hit = email or phone
        if hit is None:
            if whole:
                return None
            cleaned = value.strip()
            return cleaned[:100] or None

        before = value[: hit.start()]
        # 整篇正文时只看号码前 24 字，避免把整本书当成「人名」
        window = before[-24:] if whole else before
        # 去掉「请发送邮件至」「邮箱」「电话」这类引导词
        name = re.sub(
            r"(请|发送|邮件|至|到|邮箱|Email|E-mail|电话|联系|咨询|联系方式|联系人"
            r"|[:：]|、|\s)+",
            "",
            window,
        )
        # 再把残留的全角标点/数字等一律抹掉，只留中文（人名）。
        # 实测踩过：前缀「李老师，」因为全角逗号没被清掉，过不了「像人名」判定，
        # 结果「李老师 010-62781234」被降级成只剩号码。
        name = _NON_NAME_CHAR_RE.sub("", name)
        # 只有「干净的人名」才和号码拼一起。
        # 实测踩过：「登记表请发送至邮箱xxx」里的「登记表」会被当成联系人姓名，
        # 结果输出「登记表 dgzz@dlut.edu.cn」。
        if _looks_like_person_name(name):
            return f"{name} {hit.group(0)}"[:100]
        return hit.group(0)

    @classmethod
    def _guess_category(cls, title: str, content: str, hint: str) -> str:
        """猜分类。判定顺序（按可信度从高到低）：

          1. 标题命中 → 直接用。标题是人工写的，最可信
          2. 正文命中且达到阈值（≥3 次）→ 用正文结果
          3. 都没命中 → 用来源配置里的 category_hint（也是人工判断）
          4. 最后才回到「其他」

        为什么正文要有阈值：实测新生医保通知的正文里顺带出现了一次「就业」、
        一次「实习」，旧逻辑就把整条通知判成了「就业」——一次顺带提及
        不该改变整条通知的分类。
        """
        title_scores = {
            cat: sum(title.count(w) for w in words) for cat, words in CATEGORY_KEYWORDS.items()
        }
        best_title, title_hits = max(title_scores.items(), key=lambda kv: kv[1])
        if title_hits > 0:
            return best_title

        body_scores = {
            cat: sum(content.count(w) for w in words)
            for cat, words in CATEGORY_KEYWORDS.items()
        }
        best_body, body_hits = max(body_scores.items(), key=lambda kv: kv[1])
        if body_hits >= 3:
            return best_body

        if hint in CATEGORY_KEYWORDS and hint != "其他":
            return hint
        return "其他"

    @staticmethod
    def _split_sentences(text: str, limit: int = 40) -> list[str]:
        cleaned = normalize_text(text)
        parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", cleaned)
        return [p.strip() for p in parts if p and len(p.strip()) >= 6][:limit]

    def _build_lines(self, title: str, content: str, key_info: KeyInfo) -> list[str]:
        """保证返回**正好 3 行**——前端的列表卡片按 3 行排版。"""
        sentences = self._split_sentences(content)
        lines: list[str] = []

        # 第 1 行：这是什么通知
        head = title.strip()
        if sentences and len(head) < 20:
            head = f"{head}——{sentences[0][:60]}"
        lines.append(head[:110] or _FALLBACK_LINE)

        # 第 2 行：时间 / 地点 / 对象
        meta: list[str] = []
        if key_info.deadline:
            meta.append(f"截止 {key_info.deadline}")
        if key_info.location:
            meta.append(f"地点 {key_info.location}")
        if key_info.audience:
            meta.append(f"对象 {key_info.audience}")
        if meta:
            lines.append("；".join(meta)[:110])
        elif len(sentences) > 1:
            lines.append(sentences[1][:110])
        else:
            # 正文没抓到（纯图片 / PDF / 需登录 / robots 拦）。
            # 这里只解释「为什么没有关键信息」，不再把兜底话术塞进来——
            # 那句话该出现在第 3 行，出现在这里就成了复读。
            lines.append(_NO_CONTENT_LINE)

        # 第 3 行：要你做什么
        todo: list[str] = []
        if key_info.materials:
            todo.append("需提交：" + "、".join(key_info.materials[:4]))
        if key_info.contact:
            todo.append(f"联系人 {key_info.contact}")
        if not todo and len(sentences) > 2:
            todo.append(sentences[2][:110])
        if not todo:
            # 永远给一个"你能做什么"，绝不重复第 2 行
            todo.append(_ACTION_LINE)
        lines.append("；".join(todo)[:110])

        # 结构性保证：三行各司其职。第 2、3 行撞车，等于白写了一行。
        # 这里用兜底纠正而不是 assert —— 这是后台跑批的路径，
        # 一条格式异常的通知不该让整轮抓取崩掉。真正的防线是 tests/ 里那条用例。
        if lines[1] == lines[2]:
            lines[2] = _ACTION_LINE

        return lines[:3]
