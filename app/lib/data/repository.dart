import '../core/api_client.dart';
import 'models.dart';

class PageResult {
  PageResult({this.items = const [], this.total = 0, this.page = 1, this.pageSize = 20});

  final List<AppNotification> items;
  final int total;
  final int page;
  final int pageSize;

  factory PageResult.fromJson(Map<String, dynamic>? j) {
    if (j == null) return PageResult();
    final raw = j['items'];
    return PageResult(
      items: (raw is List)
          ? raw
              .whereType<Map>()
              .map((e) => AppNotification.fromJson(Map<String, dynamic>.from(e)))
              .toList()
          : const [],
      total: (j['total'] as num?)?.toInt() ?? 0,
      page: (j['page'] as num?)?.toInt() ?? 1,
      pageSize: (j['page_size'] as num?)?.toInt() ?? 20,
    );
  }
}

/// 所有后端调用的唯一出口。页面不直接碰 ApiClient。
class Repository {
  Repository(this._api);

  final ApiClient _api;

  String get baseUrl => _api.baseUrl;
  set baseUrl(String value) => _api.baseUrl = value;

  Future<Map<String, dynamic>> health({bool deep = false}) async {
    final data = await _api.get('/api/health', query: {'deep': deep});
    return data is Map ? Map<String, dynamic>.from(data) : <String, dynamic>{};
  }

  // ---- 信息流 ----
  Future<PageResult> fetchFeed({
    int page = 1,
    int pageSize = 20,
    int? sourceId,
    String? category,
    String? importance,
    String? q,
    bool unreadOnly = false,
    bool starredOnly = false,
  }) async {
    final data = await _api.get('/api/notifications', query: {
      'page': page,
      'page_size': pageSize,
      'source_id': sourceId,
      'category': category,
      'importance': importance,
      'q': q,
      'unread_only': unreadOnly,
      'starred_only': starredOnly,
    });
    return PageResult.fromJson(data is Map ? Map<String, dynamic>.from(data) : null);
  }

  Future<AppNotification> fetchDetail(int id) async {
    final data = await _api.get('/api/notifications/$id');
    return AppNotification.fromJson(Map<String, dynamic>.from(data as Map));
  }

  Future<AppNotification> markRead(int id, {bool read = true}) async {
    final data = await _api.post('/api/notifications/$id/read', body: {'is_read': read});
    return AppNotification.fromJson(Map<String, dynamic>.from(data as Map));
  }

  Future<AppNotification> toggleStar(int id, {bool? starred}) async {
    final data = await _api.post('/api/notifications/$id/star', body: {'is_starred': starred});
    return AppNotification.fromJson(Map<String, dynamic>.from(data as Map));
  }

  Future<AppNotification> resummarize(int id) async {
    final data = await _api.post('/api/notifications/$id/summarize');
    return AppNotification.fromJson(Map<String, dynamic>.from(data as Map));
  }

  // ---- 来源 ----
  Future<List<AppSource>> fetchSources() async {
    final data = await _api.get('/api/sources');
    if (data is! List) return const [];
    return data
        .whereType<Map>()
        .map((e) => AppSource.fromJson(Map<String, dynamic>.from(e)))
        .toList();
  }

  Future<AppSource> createSource({
    required String name,
    required String url,
    String adapter = 'html_list',
    String school = '',
    String categoryHint = '其他',
    Map<String, dynamic> config = const {},
  }) async {
    final data = await _api.post('/api/sources', body: {
      'name': name,
      'url': url,
      'adapter': adapter,
      'school': school,
      'category_hint': categoryHint,
      'enabled': true,
      'config': config,
    });
    return AppSource.fromJson(Map<String, dynamic>.from(data as Map));
  }

  Future<void> setSourceEnabled(int id, bool enabled) async {
    await _api.patch('/api/sources/$id', body: {'enabled': enabled});
  }

  Future<void> deleteSource(int id) => _api.delete('/api/sources/$id');

  Future<List<Map<String, dynamic>>> fetchAdapters() async {
    final data = await _api.get('/api/sources/adapters');
    if (data is! List) return const [];
    return data
        .whereType<Map>()
        .map((e) => Map<String, dynamic>.from(e))
        .toList();
  }

  Future<IngestResult> reloadFromYaml() async {
    final data = await _api.post('/api/sources/reload');
    return IngestResult.fromJson(Map<String, dynamic>.from(data as Map));
  }

  // ---- 抓取 ----
  Future<IngestResult> fetchOneSource(int id) async {
    final data = await _api.post('/api/sources/$id/fetch');
    return IngestResult.fromJson(Map<String, dynamic>.from(data as Map));
  }

  Future<IngestResult> runIngest({bool? summarize}) async {
    final data = await _api.post('/api/ingest/run', query: {
      if (summarize != null) 'summarize': summarize,
    });
    return IngestResult.fromJson(Map<String, dynamic>.from(data as Map));
  }

  Future<List<Map<String, dynamic>>> fetchLogs({int limit = 20}) async {
    final data = await _api.get('/api/ingest/logs', query: {'limit': limit});
    if (data is! List) return const [];
    return data.whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
  }

  // ---- 简报与问答 ----
  Future<Brief> fetchBrief({int hours = 36}) async {
    final data = await _api.get('/api/brief/today', query: {'hours': hours});
    return Brief.fromJson(Map<String, dynamic>.from(data as Map));
  }

  Future<ChatAnswer> ask(String question, {int days = 30, int topK = 12}) async {
    final data = await _api.post('/api/chat', body: {
      'question': question,
      'days': days,
      'top_k': topK,
    });
    return ChatAnswer.fromJson(Map<String, dynamic>.from(data as Map));
  }

  // ---- 自定义大模型 ----
  /// 列出「内置默认 + 所有自定义配置」，以及当前生效的是谁。
  Future<LLMList> llmProfiles() async {
    final data = await _api.get('/api/llm/profiles');
    return LLMList.fromJson(Map<String, dynamic>.from(data as Map));
  }

  Future<void> createLLMProfile({
    required String name,
    required String baseUrl,
    required String model,
    required String apiKey,
    bool thinking = false,
    bool jsonMode = true,
    bool activate = true,
  }) async {
    await _api.post('/api/llm/profiles', query: {'activate': activate}, body: {
      'name': name,
      'base_url': baseUrl,
      'model': model,
      'api_key': apiKey,
      'thinking': thinking,
      'json_mode': jsonMode,
    });
  }

  Future<void> activateLLMProfile(int id) => _api.post('/api/llm/profiles/$id/activate');

  /// 切回 .env 里那份默认配置。
  Future<void> deactivateLLM() => _api.post('/api/llm/profiles/deactivate');

  Future<void> deleteLLMProfile(int id) => _api.delete('/api/llm/profiles/$id');

  /// 测一次连通性。**不保存也能测** —— 先试通再存，列表里不会堆一堆错的。
  ///
  /// 编辑已有配置时传 [profileId] 且 [apiKey] 留空：后端会用库里的原 key，
  /// 这样「只改地址、没动 key」也能测。
  Future<LLMTestResult> testLLM({
    String baseUrl = '',
    String apiKey = '',
    String model = '',
    bool? thinking,
    bool? jsonMode,
    int? profileId,
  }) async {
    final data = await _api.post('/api/llm/test', body: {
      'base_url': baseUrl,
      'api_key': apiKey,
      'model': model,
      if (thinking != null) 'thinking': thinking,
      if (jsonMode != null) 'json_mode': jsonMode,
      if (profileId != null) 'profile_id': profileId,
    });
    return LLMTestResult.fromJson(Map<String, dynamic>.from(data as Map));
  }

  // ---- 推送 ----
  /// 拉取「比游标新」的通知。
  ///
  /// `sinceId` 传 null 表示不带给游标 —— **只在首次初始化游标时这么用**，
  /// 那时拿回来的 `maxId` 要存起来当游标，且这一批**不要展示**。
  Future<PushPendingResult> fetchPushPending({int? sinceId, int limit = 20}) async {
    final data = await _api.get('/api/push/pending', query: {
      'limit': limit,
      if (sinceId != null) 'since_id': sinceId,
    });
    return PushPendingResult.fromJson(
        data is Map ? Map<String, dynamic>.from(data) : null);
  }

  /// 回执：把服务端的 `notified_at` 标掉（外部通道去重用）。
  ///
  /// ⚠️ 多设备场景**不要**用 ack —— 一台设备 ack 了，另一台就再也收不到。
  /// 多设备请用游标。这里保留 ack 只是为了给「单设备 + 想让别处也收到 webhook」
  /// 这类场景一个开关。
  Future<int> ackPush(List<int> ids) async {
    if (ids.isEmpty) return 0;
    final data = await _api.post('/api/push/ack', body: {'ids': ids});
    if (data is Map) return (data['acked'] as num?)?.toInt() ?? 0;
    return 0;
  }

  /// 通道配置与状态。设置页要如实显示「FCM 未验证」这类信息。
  Future<Map<String, dynamic>> fetchPushConfig() async {
    final data = await _api.get('/api/push/config');
    return data is Map ? Map<String, dynamic>.from(data) : <String, dynamic>{};
  }

  Future<List<Map<String, dynamic>>> fetchPushChannels() async {
    final data = await _api.get('/api/push/channels');
    if (data is! List) return const [];
    return data
        .whereType<Map>()
        .map((e) => Map<String, dynamic>.from(e))
        .toList();
  }
}
