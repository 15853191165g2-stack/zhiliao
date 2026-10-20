import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../data/demo_data.dart';
import '../data/models.dart';
import '../data/repository.dart';
import 'api_client.dart';
import 'push_service.dart';

const kCategories = <String>['教务', '考试', '奖助学金', '活动', '讲座', '就业', '后勤', '其他'];
const kImportanceOptions = <Map<String, String>>[
  {'value': '', 'label': '全部'},
  {'value': 'urgent', 'label': '紧急'},
  {'value': 'important', 'label': '重要'},
  {'value': 'normal', 'label': '一般'},
];

/// 全局状态。只有一个 ChangeNotifier，页面通过 Provider 订阅。
///
/// 关键行为：**后端连不上时自动进入离线演示模式**，用内置样例数据渲染界面，
/// 而不是白屏或报错——这样 UI 可以脱离后端单独调。
class AppState extends ChangeNotifier {
  AppState() {
    _api = ApiClient(defaultBaseUrl);
    _repo = Repository(_api);
    push = PushService(_repo);
    push.onItems = _onPushItems;
    push.onError = (m) {
      pushError = m;
      notifyListeners();
    };
  }

  /// Android 模拟器访问宿主机的地址；真机改成电脑局域网 IP。
  static const defaultBaseUrl = 'http://10.0.2.2:8000';
  static const pageSize = 20;

  late final ApiClient _api;
  late final Repository _repo;
  Repository get repo => _repo;

  // ------------------------------------------------------------------ 推送
  /// 手机端通知（轮询 + 系统通知栏）。
  ///
  /// ⚠️ 它是**轮询**，不是真正的推送：App 被系统杀掉后就不再收。
  /// 详见 PushService 顶部那段说明，以及设置页里的边界提示。
  late final PushService push;

  /// 最近一次拉到的推送消息（给界面做角标/提示用）。
  List<PushPendingItem> latestPush = const [];

  /// 推送链路的最近一次错误。非空时设置页要如实显示。
  String? pushError;

  int pushIntervalMinutes = PushService.defaultIntervalMinutes;

  // ------------------------------------------------------------------ 设置
  String baseUrl = defaultBaseUrl;
  ThemeMode themeMode = ThemeMode.system;
  double speechRate = 1.0;
  String refreshHours = '8,18';
  bool pushEnabled = true;

  // ------------------------------------------------------------------ 信息流
  List<AppNotification> items = const [];
  int total = 0;
  int page = 1;
  bool loading = false;
  bool loadingMore = false;
  bool refreshing = false;
  bool offline = false;
  String? error;
  String? lastRefreshSummary;

  bool get hasMore => items.length < total;

  // 筛选条件
  int? filterSourceId;
  String filterCategory = '';
  String filterImportance = '';
  String query = '';
  bool unreadOnly = false;
  bool starredOnly = false;

  bool get hasFilter =>
      filterSourceId != null ||
      filterCategory.isNotEmpty ||
      filterImportance.isNotEmpty ||
      query.isNotEmpty ||
      unreadOnly ||
      starredOnly;

  // ------------------------------------------------------------------ 其他数据
  List<AppSource> sources = const [];
  Brief? brief;
  List<ChatMessage> chat = const [];
  bool asking = false;
  List<Map<String, dynamic>> adapters = const [];

  int unreadCount = 0;

  // ================================================================== 启动
  Future<void> bootstrap() async {
    await _loadPrefs();
    await Future.wait<void>([refreshFeed(), loadSources(), loadBrief()]);
    await startPush();
  }

  @override
  void dispose() {
    // 必须停掉定时器，否则热重载/退出后还会继续拉取
    push.dispose();
    super.dispose();
  }

  Future<void> _loadPrefs() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      baseUrl = prefs.getString('baseUrl') ?? defaultBaseUrl;
      final mode = prefs.getString('themeMode') ?? 'system';
      themeMode = switch (mode) {
        'light' => ThemeMode.light,
        'dark' => ThemeMode.dark,
        _ => ThemeMode.system,
      };
      speechRate = prefs.getDouble('speechRate') ?? 1.0;
      refreshHours = prefs.getString('refreshHours') ?? '8,18';
      pushEnabled = prefs.getBool('pushEnabled') ?? true;
      pushIntervalMinutes =
          prefs.getInt(PushService.kIntervalMinutes) ?? PushService.defaultIntervalMinutes;
      _api.baseUrl = baseUrl;
    } catch (_) {
      // 读不到设置就用默认值，不阻塞启动
    }
  }

  Future<void> _savePref(String key, Object value) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      if (value is String) await prefs.setString(key, value);
      if (value is double) await prefs.setDouble(key, value);
      if (value is bool) await prefs.setBool(key, value);
    } catch (_) {}
  }

  // ================================================================== 信息流
  Future<void> refreshFeed() async {
    loading = true;
    error = null;
    notifyListeners();
    try {
      final r = await _repo.fetchFeed(
        page: 1,
        pageSize: pageSize,
        sourceId: filterSourceId,
        category: _nz(filterCategory),
        importance: _nz(filterImportance),
        q: _nz(query),
        unreadOnly: unreadOnly,
        starredOnly: starredOnly,
      );
      items = r.items;
      total = r.total;
      page = 1;
      offline = false;
      _recountUnread(r.items);
    } on ApiException catch (e) {
      offline = true;
      error = e.message;
      final demo = _demoFiltered();
      items = demo;
      total = demo.length;
      page = 1;
    } finally {
      loading = false;
      notifyListeners();
    }
  }

  Future<void> loadMore() async {
    if (loadingMore || !hasMore || offline) return;
    loadingMore = true;
    notifyListeners();
    try {
      final r = await _repo.fetchFeed(
        page: page + 1,
        pageSize: pageSize,
        sourceId: filterSourceId,
        category: _nz(filterCategory),
        importance: _nz(filterImportance),
        q: _nz(query),
        unreadOnly: unreadOnly,
        starredOnly: starredOnly,
      );
      items = [...items, ...r.items];
      page += 1;
      total = r.total;
    } on ApiException catch (_) {
      // 加载更多失败就静默停住，不打断阅读
    } finally {
      loadingMore = false;
      notifyListeners();
    }
  }

  void _recountUnread(List<AppNotification> list) {
    unreadCount = list.where((n) => !n.isRead).length;
  }

  String? _nz(String v) => v.trim().isEmpty ? null : v.trim();

  List<AppNotification> _demoFiltered() {
    return demoNotifications.where((n) {
      if (filterSourceId != null) return false; // 离线数据没有真实 id，按来源名过滤意义不大
      if (filterCategory.isNotEmpty && n.category != filterCategory) return false;
      if (filterImportance.isNotEmpty && n.importance != filterImportance) return false;
      if (unreadOnly && n.isRead) return false;
      if (starredOnly && !n.isStarred) return false;
      if (query.isNotEmpty &&
          !n.title.contains(query) &&
          !(n.contentText ?? '').contains(query) &&
          !n.summaryLines.any((l) => l.contains(query))) {
        return false;
      }
      return true;
    }).toList();
  }

  Future<void> setCategory(String? category) async {
    filterCategory = category ?? '';
    await refreshFeed();
  }

  Future<void> setImportance(String? importance) async {
    filterImportance = importance ?? '';
    await refreshFeed();
  }

  Future<void> setSourceFilter(int? sourceId) async {
    filterSourceId = sourceId;
    await refreshFeed();
  }

  Future<void> setUnreadOnly(bool value) async {
    unreadOnly = value;
    await refreshFeed();
  }

  Future<void> setStarredOnly(bool value) async {
    starredOnly = value;
    await refreshFeed();
  }

  Future<void> applySearch(String value) async {
    query = value.trim();
    await refreshFeed();
  }

  Future<void> clearFilters() async {
    filterSourceId = null;
    filterCategory = '';
    filterImportance = '';
    query = '';
    unreadOnly = false;
    starredOnly = false;
    await refreshFeed();
  }

  // ------------------------------------------------------------------ 单条操作
  Future<AppNotification?> openDetail(AppNotification item) async {
    if (offline) {
      _patchLocal(item.id, isRead: true);
      return item.copyWith(isRead: true);
    }
    try {
      final detail = await _repo.fetchDetail(item.id);
      if (!detail.isRead) {
        final updated = await _repo.markRead(item.id, read: true);
        _patchLocal(item.id, isRead: true);
        return detail.copyWith(isRead: updated.isRead);
      }
      _patchLocal(item.id, isRead: true);
      return detail;
    } on ApiException catch (e) {
      error = e.message;
      notifyListeners();
      return item;
    }
  }

  Future<void> toggleStar(AppNotification item) async {
    final next = !item.isStarred;
    _patchLocal(item.id, isStarred: next);
    notifyListeners();
    if (offline) return;
    try {
      await _repo.toggleStar(item.id, starred: next);
    } on ApiException catch (e) {
      _patchLocal(item.id, isStarred: !next); // 失败回滚
      error = e.message;
      notifyListeners();
    }
  }

  Future<void> toggleRead(AppNotification item) async {
    final next = !item.isRead;
    _patchLocal(item.id, isRead: next);
    notifyListeners();
    if (offline) return;
    try {
      await _repo.markRead(item.id, read: next);
    } on ApiException catch (e) {
      _patchLocal(item.id, isRead: !next);
      error = e.message;
      notifyListeners();
    }
  }

  Future<void> resummarize(AppNotification item) async {
    if (offline) return;
    try {
      final updated = await _repo.resummarize(item.id);
      _replaceLocal(updated);
      notifyListeners();
    } on ApiException catch (e) {
      error = e.message;
      notifyListeners();
    }
  }

  void _patchLocal(int id, {bool? isRead, bool? isStarred}) {
    items = items
        .map((n) => n.id == id ? n.copyWith(isRead: isRead, isStarred: isStarred) : n)
        .toList();
    _recountUnread(items);
  }

  void _replaceLocal(AppNotification updated) {
    items = items.map((n) => n.id == updated.id ? updated : n).toList();
  }

  // ================================================================== 抓取
  Future<void> refreshAll() async {
    refreshing = true;
    lastRefreshSummary = null;
    notifyListeners();
    try {
      final r = await _repo.runIngest();
      lastRefreshSummary =
          '抓取 ${r.totalItems} 条，新增 ${r.newItems} 条，摘要 ${r.summarized} 条';
      offline = false;
      await Future.wait<void>([refreshFeed(), loadBrief(), loadSources()]);
    } on ApiException catch (e) {
      offline = true;
      error = e.message;
      lastRefreshSummary = '后端连不上，已切到离线演示数据';
    } finally {
      refreshing = false;
      notifyListeners();
    }
  }

  Future<void> loadSources() async {
    try {
      sources = await _repo.fetchSources();
      adapters = await _repo.fetchAdapters();
      offline = false;
    } on ApiException catch (_) {
      sources = demoSources;
      adapters = const [
        {'kind': 'html_list', 'doc': '配置驱动的 HTML 列表抓取（学校官网 / 教务处 / 学院 / 图书馆）'},
        {'kind': 'rss', 'doc': 'RSS / Atom 订阅源（含第三方 RSS 桥接的公众号）'},
      ];
    }
    notifyListeners();
  }

  Future<String?> addSource({
    required String name,
    required String url,
    required String adapter,
    String school = '',
    String category = '其他',
    Map<String, dynamic> config = const {},
  }) async {
    try {
      await _repo.createSource(
        name: name,
        url: url,
        adapter: adapter,
        school: school,
        categoryHint: category,
        config: config,
      );
      await loadSources();
      return null;
    } on ApiException catch (e) {
      return e.message;
    }
  }

  Future<void> toggleSource(AppSource s) async {
    try {
      await _repo.setSourceEnabled(s.id, !s.enabled);
      await loadSources();
    } on ApiException catch (e) {
      error = e.message;
      notifyListeners();
    }
  }

  Future<void> deleteSource(AppSource s) async {
    try {
      await _repo.deleteSource(s.id);
      await loadSources();
    } on ApiException catch (e) {
      error = e.message;
      notifyListeners();
    }
  }

  Future<String?> fetchSource(AppSource s) async {
    try {
      final r = await _repo.fetchOneSource(s.id);
      await Future.wait<void>([loadSources(), refreshFeed()]);
      return '${s.name}：抓取 ${r.totalItems} 条，新增 ${r.newItems} 条';
    } on ApiException catch (e) {
      return e.message;
    }
  }

  // ================================================================== 简报
  Future<void> loadBrief() async {
    try {
      brief = await _repo.fetchBrief();
      offline = false;
    } on ApiException catch (_) {
      brief = demoBrief;
    }
    notifyListeners();
  }

  // ================================================================== 对话
  Future<void> ask(String question) async {
    if (question.trim().isEmpty || asking) return;
    chat = [...chat, ChatMessage(text: question.trim(), isUser: true)];
    asking = true;
    notifyListeners();
    try {
      final answer = await _repo.ask(question.trim());
      chat = [
        ...chat,
        ChatMessage(text: answer.answer, isUser: false, citations: answer.citations),
      ];
      offline = false;
    } on ApiException catch (e) {
      chat = [
        ...chat,
        ChatMessage(
          text: '后端暂时连不上（${e.message}）。\n\n'
              '排查：① 后端是否已启动；② 设置页里的后端地址是否正确'
              '（模拟器用 10.0.2.2，真机用电脑局域网 IP）。',
          isUser: false,
        ),
      ];
      offline = true;
    } finally {
      asking = false;
      notifyListeners();
    }
  }

  void clearChat() {
    chat = const [];
    notifyListeners();
  }

  // ================================================================== 推送
  /// 启动推送链路。**幂等**：重复调用只是重新开始轮询。
  ///
  /// 失败不抛异常、不阻塞启动 —— 通知是附加能力，
  /// 后端连不上或用户拒绝权限时，信息流本身必须照常可用。
  Future<void> startPush() async {
    try {
      await push.init();
      final granted = await push.requestPermission();
      if (!granted) {
        pushError = '系统通知权限未授予 —— 只能应用内提醒，通知栏不会弹。';
      }
      if (pushEnabled) {
        await push.start(minutes: pushIntervalMinutes);
        if (granted) pushError = null;
      }
      notifyListeners();
    } catch (e) {
      pushError = '$e';
      notifyListeners();
    }
  }

  /// 回到前台时立刻补偿拉一次（后台期间系统可能冻结过定时器）。
  Future<void> pollPushNow() => push.poll();

  Future<void> _onPushItems(List<PushPendingItem> items) async {
    latestPush = items;
    pushError = null;
    notifyListeners();
    // 有新消息就顺手刷新列表与未读角标：用户从通知栏点进来时，
    // 看到的应该是已经包含这条的列表，而不是先愣一下再刷出来。
    await refreshFeed();
  }

  /// 手动「重新对齐游标」：不重弹历史。设置页给一个入口，出问题时能自救。
  Future<void> resetPushCursor() async {
    await push.resetCursor();
    notifyListeners();
  }

  Future<void> setPushInterval(int minutes) async {
    pushIntervalMinutes = minutes;
    await push.setIntervalMinutes(minutes);
    notifyListeners();
  }

  // ================================================================== 设置
  Future<void> setBaseUrl(String value) async {
    baseUrl = value.trim().isEmpty ? defaultBaseUrl : value.trim();
    _api.baseUrl = baseUrl;
    await _savePref('baseUrl', baseUrl);
    await refreshFeed();
  }

  Future<void> setThemeMode(ThemeMode mode) async {
    themeMode = mode;
    await _savePref('themeMode', mode.name);
    notifyListeners();
  }

  Future<void> setSpeechRate(double rate) async {
    speechRate = rate;
    await _savePref('speechRate', rate);
    notifyListeners();
  }

  Future<void> setRefreshHours(String value) async {
    refreshHours = value;
    await _savePref('refreshHours', value);
    notifyListeners();
  }

  Future<void> setPushEnabled(bool value) async {
    pushEnabled = value;
    await _savePref('pushEnabled', value);
    await push.setNotifyEnabled(value);
    // 关掉就真的停掉轮询，不是只在界面上留个摆设 ——
    // 否则用户关了通知却还是弹，是最招人烦的那类 bug。
    if (value) {
      await push.start(minutes: pushIntervalMinutes);
    } else {
      await push.stop();
    }
    notifyListeners();
  }
}
