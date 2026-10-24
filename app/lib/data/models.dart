/// 数据模型。字段与后端 `app/schemas.py` 一一对应。
///
/// 解析一律走安全转换：后端字段缺失/为 null 时给默认值，
/// 不要因为一个可选字段就把整页弄崩。

int? _asInt(dynamic v) {
  if (v == null) return null;
  if (v is int) return v;
  if (v is num) return v.toInt();
  return int.tryParse('$v');
}

String? _asString(dynamic v) => v == null ? null : '$v';

DateTime? _asDate(dynamic v) {
  final s = _asString(v);
  if (s == null || s.isEmpty) return null;
  return DateTime.tryParse(s)?.toLocal();
}

List<String> _asStringList(dynamic v) {
  if (v is List) return v.map((e) => '$e').where((e) => e.isNotEmpty).toList();
  return const [];
}

double _asDouble(dynamic v, [double fallback = 0]) {
  if (v is num) return v.toDouble();
  return double.tryParse('$v') ?? fallback;
}

// --------------------------------------------------------------------------- //
class KeyInfo {
  KeyInfo({
    this.deadline,
    this.deadlineTs,
    this.audience,
    this.location,
    this.materials = const [],
    this.contact,
    this.fee,
  });

  final String? deadline;
  final DateTime? deadlineTs;
  final String? audience;
  final String? location;
  final List<String> materials;
  final String? contact;
  final String? fee;

  bool get isEmpty =>
      deadline == null &&
      audience == null &&
      location == null &&
      materials.isEmpty &&
      contact == null &&
      fee == null;

  factory KeyInfo.fromJson(Map<String, dynamic>? j) {
    if (j == null) return KeyInfo();
    return KeyInfo(
      deadline: _asString(j['deadline']),
      deadlineTs: _asDate(j['deadline_ts']),
      audience: _asString(j['audience']),
      location: _asString(j['location']),
      materials: _asStringList(j['materials']),
      contact: _asString(j['contact']),
      fee: _asString(j['fee']),
    );
  }

  Map<String, dynamic> toJson() => {
        'deadline': deadline,
        'deadline_ts': deadlineTs?.toIso8601String(),
        'audience': audience,
        'location': location,
        'materials': materials,
        'contact': contact,
        'fee': fee,
      };
}

// --------------------------------------------------------------------------- //
class AppNotification {
  AppNotification({
    required this.id,
    required this.title,
    this.sourceId,
    this.sourceName,
    this.school,
    this.url,
    this.author,
    this.publishedAt,
    this.excerpt,
    this.summaryLines = const [],
    this.keyInfo,
    this.importance = 'normal',
    this.importanceLabel = '一般',
    this.category = '其他',
    this.tags = const [],
    this.aiStatus = 'pending',
    this.aiModel,
    this.contentHtml,
    this.contentText,
    this.isRead = false,
    this.isStarred = false,
  });

  final int id;
  final String title;
  final int? sourceId;
  final String? sourceName;
  final String? school;
  final String? url;
  final String? author;
  final DateTime? publishedAt;
  final String? excerpt;
  final List<String> summaryLines;
  final KeyInfo? keyInfo;
  final String importance;
  final String importanceLabel;
  final String category;
  final List<String> tags;
  final String aiStatus;
  final String? aiModel;
  final String? contentHtml;
  final String? contentText;
  final bool isRead;
  final bool isStarred;

  bool get isUrgent => importance == 'urgent';
  bool get aiReady => aiStatus == 'done';

  AppNotification copyWith({bool? isRead, bool? isStarred}) => AppNotification(
        id: id,
        title: title,
        sourceId: sourceId,
        sourceName: sourceName,
        school: school,
        url: url,
        author: author,
        publishedAt: publishedAt,
        excerpt: excerpt,
        summaryLines: summaryLines,
        keyInfo: keyInfo,
        importance: importance,
        importanceLabel: importanceLabel,
        category: category,
        tags: tags,
        aiStatus: aiStatus,
        aiModel: aiModel,
        contentHtml: contentHtml,
        contentText: contentText,
        isRead: isRead ?? this.isRead,
        isStarred: isStarred ?? this.isStarred,
      );

  factory AppNotification.fromJson(Map<String, dynamic> j) {
    final ki = j['key_info'];
    return AppNotification(
      id: _asInt(j['id']) ?? 0,
      title: _asString(j['title']) ?? '(无标题)',
      sourceId: _asInt(j['source_id']),
      sourceName: _asString(j['source_name']),
      school: _asString(j['school']),
      url: _asString(j['url']),
      author: _asString(j['author']),
      publishedAt: _asDate(j['published_at']),
      excerpt: _asString(j['excerpt']),
      summaryLines: _asStringList(j['summary_lines']),
      keyInfo: ki is Map<String, dynamic> ? KeyInfo.fromJson(ki) : null,
      importance: _asString(j['importance']) ?? 'normal',
      importanceLabel: _asString(j['importance_label']) ?? '一般',
      category: _asString(j['category']) ?? '其他',
      tags: _asStringList(j['tags']),
      aiStatus: _asString(j['ai_status']) ?? 'pending',
      aiModel: _asString(j['ai_model']),
      contentHtml: _asString(j['content_html']),
      contentText: _asString(j['content_text']),
      isRead: j['is_read'] == true,
      isStarred: j['is_starred'] == true,
    );
  }
}

// --------------------------------------------------------------------------- //
class AppSource {
  AppSource({
    required this.id,
    required this.name,
    this.school = '',
    this.adapter = 'html_list',
    this.url = '',
    this.categoryHint = '其他',
    this.enabled = true,
    this.config = const {},
    this.lastStatus = 'pending',
    this.lastError,
    this.lastFetchedAt,
    this.itemCount = 0,
  });

  final int id;
  final String name;
  final String school;
  final String adapter;
  final String url;
  final String categoryHint;
  final bool enabled;
  final Map<String, dynamic> config;
  final String lastStatus;
  final String? lastError;
  final DateTime? lastFetchedAt;
  final int itemCount;

  factory AppSource.fromJson(Map<String, dynamic> j) => AppSource(
        id: _asInt(j['id']) ?? 0,
        name: _asString(j['name']) ?? '(未命名来源)',
        school: _asString(j['school']) ?? '',
        adapter: _asString(j['adapter']) ?? 'html_list',
        url: _asString(j['url']) ?? '',
        categoryHint: _asString(j['category_hint']) ?? '其他',
        enabled: j['enabled'] != false,
        config: (j['config'] is Map) ? Map<String, dynamic>.from(j['config'] as Map) : const {},
        lastStatus: _asString(j['last_status']) ?? 'pending',
        lastError: _asString(j['last_error']),
        lastFetchedAt: _asDate(j['last_fetched_at']),
        itemCount: _asInt(j['item_count']) ?? 0,
      );
}

// --------------------------------------------------------------------------- //
class BriefSection {
  BriefSection({required this.title, this.items = const [], this.notificationIds = const []});

  final String title;
  final List<String> items;
  final List<int> notificationIds;

  factory BriefSection.fromJson(Map<String, dynamic> j) => BriefSection(
        title: _asString(j['title']) ?? '',
        items: _asStringList(j['items']),
        notificationIds:
            (j['notification_ids'] is List) ? (j['notification_ids'] as List).map((e) => _asInt(e) ?? 0).toList() : const [],
      );
}

class Brief {
  Brief({
    required this.date,
    this.headline = '',
    this.greeting = '',
    this.counts = const {},
    this.sections = const [],
    this.speechText = '',
    this.generatedBy = 'local',
  });

  final String date;
  final String headline;
  final String greeting;
  final Map<String, int> counts;
  final List<BriefSection> sections;
  final String speechText;
  final String generatedBy;

  factory Brief.fromJson(Map<String, dynamic> j) {
    final counts = <String, int>{};
    final raw = j['counts'];
    if (raw is Map) {
      raw.forEach((k, v) => counts['$k'] = _asInt(v) ?? 0);
    }
    return Brief(
      date: _asString(j['date']) ?? '',
      headline: _asString(j['headline']) ?? '',
      greeting: _asString(j['greeting']) ?? '',
      counts: counts,
      sections: (j['sections'] is List)
          ? (j['sections'] as List)
              .whereType<Map>()
              .map((e) => BriefSection.fromJson(Map<String, dynamic>.from(e)))
              .toList()
          : const [],
      speechText: _asString(j['speech_text']) ?? '',
      generatedBy: _asString(j['generated_by']) ?? 'local',
    );
  }
}

// --------------------------------------------------------------------------- //
class ChatAnswer {
  ChatAnswer({required this.answer, this.citations = const [], this.generatedBy = 'local'});

  final String answer;
  final List<AppNotification> citations;
  final String generatedBy;

  factory ChatAnswer.fromJson(Map<String, dynamic> j) => ChatAnswer(
        answer: _asString(j['answer']) ?? '',
        citations: (j['citations'] is List)
            ? (j['citations'] as List)
                .whereType<Map>()
                .map((e) => AppNotification.fromJson(Map<String, dynamic>.from(e)))
                .toList()
            : const [],
        generatedBy: _asString(j['generated_by']) ?? 'local',
      );
}

class ChatMessage {
  ChatMessage({required this.text, required this.isUser, this.citations = const []});

  final String text;
  final bool isUser;
  final List<AppNotification> citations;
}

class IngestResult {
  IngestResult({
    this.totalItems = 0,
    this.newItems = 0,
    this.updatedItems = 0,
    this.summarized = 0,
    this.durationMs = 0,
  });

  final int totalItems;
  final int newItems;
  final int updatedItems;
  final int summarized;
  final double durationMs;

  factory IngestResult.fromJson(Map<String, dynamic> j) => IngestResult(
        totalItems: _asInt(j['total_items']) ?? 0,
        newItems: _asInt(j['new_items']) ?? 0,
        updatedItems: _asInt(j['updated_items']) ?? 0,
        summarized: _asInt(j['summarized']) ?? 0,
        durationMs: _asDouble(j['duration_ms']),
      );
}

// --------------------------------------------------------------------------- //
/// 推送队列里的一条。对应后端 `PushMessage.as_dict()`。
///
/// 注意 `lines` 是**已经切好的 3 行摘要**，客户端直接渲染，
/// 不要在这里自己再切一次（切法不一致就会出现「手机 3 行、电脑 2 行」）。
class PushPendingItem {
  PushPendingItem({
    required this.id,
    required this.title,
    this.lines = const [],
    this.importance = 'normal',
    this.importanceLabel = '一般',
    this.category = '',
    this.sourceName = '',
    this.url,
    this.publishedAt,
    this.text = '',
  });

  final int id;
  final String title;
  final List<String> lines;
  final String importance;
  final String importanceLabel;
  final String category;
  final String sourceName;
  final String? url;
  final DateTime? publishedAt;

  /// 朗读/长文本用的合并正文。
  final String text;

  /// 系统通知的正文：3 行摘要拼起来。缺失时退回标题，保证不出现空通知。
  String get body => lines.isEmpty ? title : lines.join('\n');

  factory PushPendingItem.fromJson(Map<String, dynamic> j) => PushPendingItem(
        id: _asInt(j['id']) ?? 0,
        title: _asString(j['title']) ?? '(无标题)',
        lines: _asStringList(j['lines']),
        importance: _asString(j['importance']) ?? 'normal',
        importanceLabel: _asString(j['importance_label']) ?? '一般',
        category: _asString(j['category']) ?? '',
        sourceName: _asString(j['source_name']) ?? '',
        url: _asString(j['url']),
        publishedAt: _asDate(j['published_at']),
        text: _asString(j['text']) ?? '',
      );
}

/// `GET /api/push/pending` 的返回。
///
/// 初始游标取的是**队列最老那条的 id - 1**（`sinceId: 0` 拿第一条），
/// 不是 [maxId] —— 用 maxId 会把队列里已有的通知一起跳过（见 push_service.dart
/// 里 `_ensureCursor` 的说明）。[maxId] 只在队列为空时用来兜底。
/// 历史通知不会进队列，那由服务端的启动基线负责。
class PushPendingResult {
  PushPendingResult({
    this.items = const [],
    this.total = 0,
    this.maxId = 0,
    this.nextSinceId = 0,
    this.quietNow = false,
  });

  final List<PushPendingItem> items;
  final int total;
  final int maxId;
  final int nextSinceId;
  final bool quietNow;

  factory PushPendingResult.fromJson(Map<String, dynamic>? j) {
    if (j == null) return PushPendingResult();
    final raw = j['items'];
    return PushPendingResult(
      items: (raw is List)
          ? raw
              .whereType<Map>()
              .map((e) => PushPendingItem.fromJson(Map<String, dynamic>.from(e)))
              .toList()
          : const [],
      total: _asInt(j['total']) ?? 0,
      maxId: _asInt(j['max_id']) ?? 0,
      nextSinceId: _asInt(j['next_since_id']) ?? 0,
      quietNow: j['quiet_now'] == true,
    );
  }
}

// --------------------------------------------------------------------------- //
// 自定义大模型（AI 问答页的模型选择器）
// --------------------------------------------------------------------------- //

/// 一份自定义模型接入配置。
///
/// 注意 [apiKeyMasked]：后端**永远不回明文 key**。编辑时把这个打码串原样传回去，
/// 后端据此判断「用户没动 key」，保留库里的原值。
class LLMProfile {
  LLMProfile({
    required this.id,
    required this.name,
    this.baseUrl = '',
    this.model = '',
    this.apiKeyMasked = '',
    this.hasKey = false,
    this.thinking = false,
    this.jsonMode = true,
    this.isActive = false,
    this.endpoint = '',
    this.lastTestOk,
    this.lastTestError = '',
  });

  final int id;
  final String name;
  final String baseUrl;
  final String model;
  final String apiKeyMasked;
  final bool hasKey;
  final bool thinking;
  final bool jsonMode;
  final bool isActive;

  /// 后端拼好的最终请求地址，用来告诉用户「你填的域名最后打到哪」。
  final String endpoint;
  final bool? lastTestOk;
  final String lastTestError;

  factory LLMProfile.fromJson(Map<String, dynamic> j) => LLMProfile(
        id: _asInt(j['id']) ?? 0,
        name: _asString(j['name']) ?? '',
        baseUrl: _asString(j['base_url']) ?? '',
        model: _asString(j['model']) ?? '',
        apiKeyMasked: _asString(j['api_key_masked']) ?? '',
        hasKey: j['has_key'] == true,
        thinking: j['thinking'] == true,
        jsonMode: j['json_mode'] != false,
        isActive: j['is_active'] == true,
        endpoint: _asString(j['endpoint']) ?? '',
        lastTestOk: j['last_test_ok'] is bool ? j['last_test_ok'] as bool : null,
        lastTestError: _asString(j['last_test_error']) ?? '',
      );
}

/// 模型列表 + 当前生效的是谁。
class LLMList {
  LLMList({
    this.items = const [],
    this.envName = '默认',
    this.envModel = '',
    this.envEnabled = false,
    this.activeId,
    this.effective = '',
    this.effectiveModel = '',
    this.effectiveSource = 'env',
  });

  final List<LLMProfile> items;
  final String envName;
  final String envModel;
  final bool envEnabled;
  final int? activeId;
  final String effective;
  final String effectiveModel;
  final String effectiveSource;

  /// 没有自定义配置在激活态 = 走 .env 里那份。
  bool get usingEnv => activeId == null;

  factory LLMList.fromJson(Map<String, dynamic> j) {
    final env = j['env'] is Map
        ? Map<String, dynamic>.from(j['env'] as Map)
        : <String, dynamic>{};
    final raw = j['items'];
    return LLMList(
      items: (raw is List)
          ? raw
              .whereType<Map>()
              .map((e) => LLMProfile.fromJson(Map<String, dynamic>.from(e)))
              .toList()
          : const [],
      envName: _asString(env['name']) ?? '默认',
      envModel: _asString(env['model']) ?? '',
      envEnabled: env['enabled'] == true,
      activeId: _asInt(j['active_id']),
      effective: _asString(j['effective']) ?? '',
      effectiveModel: _asString(j['effective_model']) ?? '',
      effectiveSource: _asString(j['effective_source']) ?? 'env',
    );
  }
}

/// 「测试连接」的结果。
class LLMTestResult {
  LLMTestResult({
    this.ok = false,
    this.detail = '',
    this.endpoint = '',
    this.model = '',
    this.durationMs = 0,
  });

  final bool ok;
  final String detail;
  final String endpoint;
  final String model;
  final double durationMs;

  factory LLMTestResult.fromJson(Map<String, dynamic> j) => LLMTestResult(
        ok: j['ok'] == true,
        detail: _asString(j['detail']) ?? '',
        endpoint: _asString(j['endpoint']) ?? '',
        model: _asString(j['model']) ?? '',
        durationMs: (j['duration_ms'] as num?)?.toDouble() ?? 0,
      );
}
