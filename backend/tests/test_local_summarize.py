"""本地规则摘要的抽取回归测试。

为什么值得单独测：**没有 AI_API_KEY 时这就是默认行为**，
而它的输出直接决定用户看到的「关键信息卡片」准不准。
这里的断言全部对准真实大工原文里踩过的坑。
"""

from __future__ import annotations

import pytest

from app.services.summarize import SummarizeService


@pytest.fixture()
def svc() -> SummarizeService:
    return SummarizeService()


# 真实原文片段（迎新网 · 2026级新生医疗保险投保指南）
YX_CONTENT = (
    "为做好在校大学生的医疗保障服务，现启动2026-2027年度学生医疗保险参保缴费工作。"
    "无法自助办理电子医保凭证的港澳台及外籍大学生，可携带参保相关证件到"
    "大连市医保中心高新园区办事处经办窗口办理电子医保凭证激活业务。"
    "1.入学当年9月1日至12月31日为集中参保期。"
    "《盘锦校区学生大连参保登记表》以“盘锦校区学生大连参保－姓名－学号”命名，"
    "发送至邮箱dgzz@dlut.edu.cn。"
    "线下服务：人保财险服务专员每周二到凌水校区学生服务大厅驻校办公。"
    "学生服务中心定期处理参保变更登记和新的参保登记申请，"
    "如需咨询可联系学生服务中心王老师，联系电话0411-84708676。"
)


def test_audience_is_not_the_title(svc: SummarizeService):
    """标题里带「2026级」时，不能把标题整句当成适用对象。"""
    title = "2026级新生医疗保险投保指南"
    res = svc._local_summarize(title=title, content=YX_CONTENT, category_hint="其他")
    audience = res.key_info.audience
    if audience is not None:
        assert "投保指南" not in audience, f"对象抽成了标题：{audience}"
        assert len(audience) <= 20


def test_location_not_truncated(svc: SummarizeService):
    res = svc._local_summarize(
        title="2026级新生医疗保险投保指南", content=YX_CONTENT, category_hint="其他"
    )
    loc = res.key_info.location
    assert loc is not None
    # 踩过的坑：`{2,8}` 把「凌水校区学生服务大厅」截成「水校区学生服务大厅」
    assert "凌水" in loc or "医保" in loc, f"地点被截断：{loc}"
    assert not loc.startswith("水校区")


def test_material_tail_is_stripped(svc: SummarizeService):
    """「携带参保相关证件到 大连市医保中心…办理」不能抽成「参保相关证件到」。"""
    res = svc._local_summarize(
        title="2026级新生医疗保险投保指南", content=YX_CONTENT, category_hint="其他"
    )
    for m in res.key_info.materials:
        assert not m.endswith(("到", "至", "在", "于")), f"材料带了尾巴：{m}"
        assert "办事处" not in m and "窗口" not in m


def test_contact_prefers_person_plus_phone(svc: SummarizeService):
    res = svc._local_summarize(
        title="2026级新生医疗保险投保指南", content=YX_CONTENT, category_hint="其他"
    )
    contact = res.key_info.contact
    assert contact is not None
    assert "0411-84708676" in contact, f"应优先取带姓名的联系电话：{contact}"
    assert not contact.startswith("请发送")


def test_contact_email_only_when_no_person(svc: SummarizeService):
    text = "登记表请发送至邮箱dgzz@dlut.edu.cn。"
    res = svc._local_summarize(title="参保登记通知", content=text, category_hint="其他")
    assert res.key_info.contact == "dgzz@dlut.edu.cn", res.key_info.contact


def test_category_not_hijacked_by_a_single_body_mention(svc: SummarizeService):
    """正文里顺带出现一次「实习」不该把整条通知判成「就业」。

    实测：新生医保通知的正文含「就业」1 次、「实习」1 次，标题零命中，
    旧逻辑判成「就业」，但来源给的 hint 是「其他」。
    """
    text = YX_CONTENT + "部分同学可结合就业实习安排自行选择参保档次。"
    res = svc._local_summarize(title="2026级新生医疗保险投保指南", content=text, category_hint="其他")
    assert res.category == "其他", f"被正文里的弱信号带偏了：{res.category}"


def test_category_title_weight_beats_body_noise(svc: SummarizeService):
    """标题明确是奖学金，正文里夹了几个「选课」，仍应判「奖助学金」。"""
    text = "选课期间系统繁忙，请错峰操作。选课入口在教学系统。" * 3
    res = svc._local_summarize(title="国家奖学金评审工作通知", content=text, category_hint="其他")
    assert res.category == "奖助学金", res.category


def test_deadline_parsed_to_real_datetime(svc: SummarizeService):
    res = svc._local_summarize(
        title="关于报送材料的通知",
        content="请于2026年10月15日17:00前提交至教务处，逾期不再受理。",
        category_hint="教务",
    )
    assert res.key_info.deadline
    assert res.key_info.deadline_ts is not None, "截止时间必须能解析成真实时间（要写日历）"
    assert res.key_info.deadline_ts.year == 2026


def test_always_exactly_three_summary_lines(svc: SummarizeService):
    """列表卡片按 3 行排版，正文再少也必须凑满 3 行。"""
    for content in ("", "很短。", YX_CONTENT):
        res = svc._local_summarize(title="测试通知标题", content=content, category_hint="其他")
        assert len(res.summary_lines) == 3, content[:20]


def test_empty_content_does_not_crash(svc: SummarizeService):
    res = svc._local_summarize(title="关于某事项的通知", content="", category_hint="其他")
    assert res.key_info.deadline is None
    assert res.key_info.materials == []
    assert res.summary_lines


def test_contact_is_none_when_absent_never_the_whole_article(svc: SummarizeService):
    """没有联系方式时必须返回 None，**不能**把整篇正文塞进 contact。

    踩过的坑：迎新网《2026年新生家长指南》（5850 字）没有「联系人/电话」，
    旧逻辑把整篇正文当成了 contact，前端的关键信息卡被撑爆。
    """
    long_text = "前 言 尊敬的家长：您好！感谢您打开这本《新生家长指南》。" * 30
    assert svc._find_contact(long_text) is None
    res = svc._local_summarize(title="2026年新生家长指南", content=long_text, category_hint="其他")
    assert res.key_info.contact is None


def test_contact_from_label_without_number_falls_back_to_text(svc: SummarizeService):
    """有「联系人：X」引导但没号码时，仍应给出那个人名/部门，而不是整篇。"""
    text = "如有疑问请联系学生工作办公室。" + "正文很长很长。" * 30
    got = svc._find_contact("联系人：学生工作办公室。\n" + text)
    assert got is not None and len(got) <= 40

