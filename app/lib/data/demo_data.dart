import 'models.dart';

/// 离线演示数据。
///
/// 后端没启动时（或者手机还没连上电脑），App 用这份数据把界面渲染出来，
/// 让人能先看到 UI 和交互，而不是面对一个空白报错页。
/// 数据内容与后端 `backend/tests/fixtures/` 里的样例保持一致。

final demoNotifications = <AppNotification>[
  AppNotification(
    id: -1,
    title: '关于开展2025-2026学年国家奖学金评审工作的通知',
    sourceName: '示例大学教务处通知',
    school: '示例大学',
    author: '学生资助管理中心',
    publishedAt: DateTime(2026, 10, 5, 9, 30),
    url: 'https://jwc.example.edu.cn/tzgg/1001.html',
    importance: 'urgent',
    importanceLabel: '紧急',
    category: '奖助学金',
    aiStatus: 'done',
    aiModel: 'demo',
    tags: const ['奖学金', '材料提交'],
    excerpt: '请符合条件的同学于2026年10月15日17:00前将申请材料提交至所在学院学生工作办公室（逸夫楼305）。',
    summaryLines: const [
      '面向 2023级、2024级本科生启动国家奖学金评审，各学院推荐名额已下达。',
      '截止 2026年10月15日17:00；地点 逸夫楼305；对象 2023级、2024级全日制本科生',
      '需提交：国家奖学金申请表、上一学年成绩单原件、获奖证明复印件；联系人 李老师 010-62781234',
    ],
    keyInfo: KeyInfo(
      deadline: '2026年10月15日17:00',
      deadlineTs: DateTime(2026, 10, 15, 17),
      audience: '2023级、2024级全日制本科生',
      location: '逸夫楼305',
      materials: const ['国家奖学金申请表', '上一学年成绩单原件', '获奖证明复印件', '个人事迹材料（1500字以内）'],
      contact: '李老师，联系电话：010-62781234，邮箱：xszz@example.edu.cn',
    ),
    contentText: '各学院、各位同学：根据学校学生资助管理中心工作安排，现启动2025-2026学年国家奖学金评审工作……',
  ),
  AppNotification(
    id: -2,
    title: '2026年秋季学期本科生选课工作安排',
    sourceName: '示例大学教务处通知',
    school: '示例大学',
    publishedAt: DateTime(2026, 10, 4, 16, 20),
    url: 'https://jwc.example.edu.cn/tzgg/1002.html',
    importance: 'important',
    importanceLabel: '重要',
    category: '教务',
    aiStatus: 'done',
    aiModel: 'demo',
    tags: const ['选课'],
    summaryLines: const [
      '秋季学期选课分两轮，第一轮 10月20日12:00 开放。',
      '截止 10月27日12:00；对象 全体在校本科生',
      '通识选修课每人限选 2 门，系统问题联系教务处信息科 010-62789999',
    ],
    keyInfo: KeyInfo(
      deadline: '2026年10月27日12:00',
      deadlineTs: DateTime(2026, 10, 27, 12),
      audience: '全体在校本科生',
      location: '行政楼208',
      contact: '010-62789999',
    ),
    contentText: '2026年秋季学期选课将于2026年10月20日12:00开放第一轮选课，10月27日12:00关闭选课系统进行抽签……',
  ),
  AppNotification(
    id: -3,
    title: '关于举办“人工智能与未来教育”学术讲座的通知',
    sourceName: '示例大学教务处通知',
    school: '示例大学',
    publishedAt: DateTime(2026, 10, 3, 10, 0),
    url: 'https://jwc.example.edu.cn/tzgg/1003.html',
    importance: 'normal',
    importanceLabel: '一般',
    category: '讲座',
    aiStatus: 'done',
    aiModel: 'demo',
    summaryLines: const [
      '教育学院邀请校外专家做“人工智能与未来教育”主题讲座。',
      '时间 10月12日 14:00；地点 逸夫楼报告厅',
      '无需报名，先到先坐',
    ],
    keyInfo: KeyInfo(
      deadline: '2026年10月12日14:00',
      deadlineTs: DateTime(2026, 10, 12, 14),
      location: '逸夫楼报告厅',
    ),
    contentText: '本次讲座将围绕人工智能技术对教育形态的影响展开……',
  ),
  AppNotification(
    id: -4,
    title: '关于图书馆国庆假期开放时间调整的通知',
    sourceName: '示例大学图书馆通知',
    school: '示例大学',
    author: '图书馆办公室',
    publishedAt: DateTime(2026, 10, 1, 8, 0),
    url: 'https://lib.example.edu.cn/notice/1001.html',
    importance: 'normal',
    importanceLabel: '一般',
    category: '后勤',
    aiStatus: 'done',
    aiModel: 'demo',
    summaryLines: const [
      '国庆假期图书馆开放时间调整：10月1日至3日闭馆。',
      '10月4日起恢复 8:00-22:00 开放',
      '闭馆期间还书期限自动顺延，24 小时自助还书机正常使用',
    ],
    keyInfo: KeyInfo(
      deadline: '2026年10月8日',
      deadlineTs: DateTime(2026, 10, 8),
      contact: '010-62780000',
    ),
    contentText: '各位读者：国庆假期期间，图书馆开放时间调整如下：10月1日至10月3日闭馆……',
  ),
];

final demoSources = <AppSource>[
  AppSource(id: -1, name: '示例大学教务处通知', school: '示例大学', adapter: 'html_list', url: 'https://jwc.example.edu.cn/tzgg/index.htm', categoryHint: '教务', lastStatus: 'ok', itemCount: 4),
  AppSource(id: -2, name: '示例大学图书馆通知', school: '示例大学', adapter: 'rss', url: 'https://lib.example.edu.cn/rss.xml', categoryHint: '后勤', lastStatus: 'ok', itemCount: 2),
];

final demoBrief = Brief(
  date: '2026-10-06',
  greeting: '早上好，今天的通知已经帮你整理好了',
  headline: '共 4 条新通知，1 条紧急，2 条即将截止',
  generatedBy: 'demo',
  counts: const {'total': 4, 'urgent': 1, 'due_soon': 2, 'important': 1, 'unread': 3},
  sections: [
    BriefSection(title: '⚡ 紧急', items: const ['[示例大学教务处通知] 关于开展2025-2026学年国家奖学金评审工作的通知'], notificationIds: const [-1]),
    BriefSection(title: '⏰ 即将截止', items: const ['[示例大学教务处通知] 2026年秋季学期本科生选课工作安排（截止 10月27日12:00）']),
    BriefSection(title: '📌 重要', items: const ['[示例大学教务处通知] 关于开展2025-2026学年国家奖学金评审工作的通知']),
    BriefSection(title: '📰 其他更新', items: const ['[示例大学图书馆通知] 关于图书馆国庆假期开放时间调整的通知', '[示例大学教务处通知] 关于举办“人工智能与未来教育”学术讲座的通知']),
  ],
  speechText:
      '2026年10月6日 通知简报。共 4 条新通知，1 条紧急，2 条即将截止。紧急事项：国家奖学金评审工作通知；重要提醒：秋季学期选课安排。',
);
