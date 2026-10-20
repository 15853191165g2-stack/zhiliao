import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../core/api_client.dart' show ApiException;
import '../data/models.dart';
import '../data/repository.dart';

/// 手机端通知：**轮询 + 系统本地通知**。
///
/// ⚠️ 诚实说明（这一条很重要，别在 UI 上含糊过去）
///
/// 这是**轮询**，不是真正的推送。App 被系统杀掉、或者被冻结到后台之后，
/// 定时器就停了，新通知也就不会再来 —— 直到用户再次打开 App。
/// 要做到「App 没开也能收到」，必须走 FCM 或厂商推送（小米/华为/OPPO/vivo），
/// 而那需要：开发者账号 + 应用上架审核 + 服务端推送凭据。
/// 另外国内 Android 普遍不带 GMS，FCM 基本不可用。
///
/// 所以本阶段的承诺边界是：**App 在前台或刚被切到后台的一段时间内不漏消息**，
/// 并且重启 App 后会把错过的消息补上（靠持久化游标 + 一次补偿拉取）。
///
/// 生命周期外的这部分，UI 上会明确写出来，不假装成「推送」。
class PushService {
  PushService(this._repo);

  final Repository _repo;

  // ---- 持久化键 ----
  static const kCursor = 'push_cursor';
  static const kCursorReady = 'push_cursor_ready';
  static const kNotifyEnabled = 'push_notify_enabled';
  static const kIntervalMinutes = 'push_interval_minutes';
  static const kLastError = 'push_last_error';

  static const defaultIntervalMinutes = 5;

  static const _channelId = 'quanzhi_notify';
  static const _channelName = '知了 · 校园通知';
  static const _channelDesc = '校内各渠道的新通知与重点提炼';

  final FlutterLocalNotificationsPlugin _plugin =
      FlutterLocalNotificationsPlugin();

  Timer? _timer;
  bool _busy = false;
  bool _disposed = false;

  /// 通知插件是否真的可用。不可用时**不报错**，退化成「只在 App 内提醒」，
  /// 这样在没配好原生依赖的环境里 UI 依然能跑（链路不依赖它）。
  bool _pluginReady = false;
  bool _permissionDenied = false;

  /// 拉到新消息时回调。AppState 用它刷新角标/列表。
  ///
  /// 约定：**只在真的有新消息时触发**，没有新消息时保持沉默
  ///（成功但无新消息 ≠ 有消息）。失败走 [onError]。
  void Function(List<PushPendingItem> items)? onItems;

  /// 出错回调。UI 要能看到「已连续失败 N 次」，不能装作没事。
  void Function(String message)? onError;

  bool get pluginReady => _pluginReady;
  bool get permissionDenied => _permissionDenied;
  bool get running => _timer?.isActive ?? false;

  // ================================================================ 初始化
  /// 初始化通知插件 + 游标。**必须在开始轮询之前调用**。
  Future<void> init() async {
    await _initPlugin();
    await _ensureCursor();
  }

  Future<void> _initPlugin() async {
    try {
      const android = AndroidInitializationSettings('@mipmap/ic_launcher');
      const ios = DarwinInitializationSettings(
        // 权限统一在 requestPermission() 里申请，这里不要重复弹窗。
        requestAlertPermission: false,
        requestBadgePermission: false,
        requestSoundPermission: false,
      );
      await _plugin.initialize(
        const InitializationSettings(android: android, iOS: ios),
      );
      _pluginReady = true;
    } catch (e) {
      // 常见原因：原生依赖还没配（没有 android/ 目录、没加 POST_NOTIFICATIONS
      // 权限、模拟器不支持等）。这时候**不能**让整个 App 起不来。
      _pluginReady = false;
      debugPrint('[push] 通知插件初始化失败，退化为应用内提醒：$e');
    }
  }

  /// 申请通知权限（Android 13+ / iOS 都需要显式申请）。
  ///
  /// 返回 false 表示用户拒绝了 —— 设置页要把这个如实显示出来，
  /// 而不是勾着「已开启」却什么都收不到。
  Future<bool> requestPermission() async {
    if (!_pluginReady) return false;
    try {
      if (Platform.isAndroid) {
        final impl = _plugin.resolvePlatformSpecificImplementation<
            AndroidFlutterLocalNotificationsPlugin>();
        final ok = await impl?.requestNotificationsPermission();
        _permissionDenied = ok == false;
        return ok ?? true;
      }
      if (Platform.isIOS) {
        final impl = _plugin.resolvePlatformSpecificImplementation<
            IOSFlutterLocalNotificationsPlugin>();
        final ok = await impl?.requestPermissions(alert: true, badge: true, sound: true);
        _permissionDenied = ok == false;
        return ok ?? true;
      }
      return true;
    } catch (e) {
      debugPrint('[push] 申请权限失败：$e');
      return false;
    }
  }

  /// 首次启动给游标定一个起点：**队列里最老那条之前**。
  ///
  /// 队列为空时才退化成「对齐到 [PushPendingResult.maxId]」（= 什么都不弹）。
  ///
  /// ⚠️ 不要直接对齐 maxId（这是本来的写法，已修）：那意味着 `id <= maxId`
  /// 一律不拉，而队列里那些待推送的通知**正好**满足 `id <= maxId` ——
  /// 于是它们永远不弹、也不报错。触发场景很日常：换了台手机、清了 App 数据。
  /// 电脑版踩过同一个坑，`scripts/probe_push_cursor.py` 可直接复现。
  ///
  /// 为什么不担心「一次性刷屏」（当初对齐 maxId 的动机）：能进队列的通知
  /// 要同时满足三个条件，条数天然有限 ——
  /// ① 不在启动基线里（库里的历史通知在服务端 lifespan 就被标掉了）；
  /// ② 落在 48 小时时间窗内；③ 摘要已完成（`ai_status == done`）。
  /// 真正会糊满屏的是①没做的情况，那归基线管，不该让游标来兜。
  Future<void> _ensureCursor() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      if (prefs.getBool(kCursorReady) == true) return;

      // ⚠️ 必须带 sinceId: 0 —— 不传 sinceId 时后端按**发布时间倒序**返回，
      // items.first 是最新的那条，拿它当起点会跳过所有更老的队列条目，
      // 等于把同一个缺口换个地方再犯一次。
      final r = await _repo.fetchPushPending(sinceId: 0, limit: 1);
      final start = r.items.isNotEmpty ? (r.items.first.id - 1) : r.maxId;
      await prefs.setInt(kCursor, start < 0 ? 0 : start);
      await prefs.setBool(kCursorReady, true);
      debugPrint('[push] 游标初始化完成 cursor=$start'
          '（队列待推送 ${r.total} 条；历史通知不推）');
    } on ApiException catch (e) {
      // 后端连不上就先不建游标，下一次轮询再试。
      // 这里**不能**把 cursor_ready 置 true —— 否则会带着 0 游标开始推历史。
      _report('初始化游标失败：${e.message}');
    }
  }

  /// 用户手动「重置游标」：下次拉取从队列最老那条开始。重来一次不会丢 48 小时内的新通知。
  Future<void> resetCursor() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(kCursorReady, false);
    await prefs.remove(kCursor);
    await _ensureCursor();
  }

  // ================================================================ 轮询
  /// 开始定时轮询。`minutes <= 0` 时用默认值。
  Future<void> start({int? minutes}) async {
    if (_disposed) return;
    await stop();
    final m = (minutes ?? await intervalMinutes).clamp(1, 720);
    _timer = Timer.periodic(Duration(minutes: m), (_) => poll());
    // 启动时立刻拉一次，别让用户干等一个周期。
    unawaited(poll());
  }

  Future<void> stop() async {
    _timer?.cancel();
    _timer = null;
  }

  void dispose() {
    _disposed = true;
    _timer?.cancel();
    _timer = null;
  }

  /// 拉一次新消息并弹通知。
  ///
  /// 重入保护：上一轮还没跑完就直接跳过，避免慢网络下堆积。
  Future<void> poll() async {
    if (_busy || _disposed) return;
    _busy = true;
    try {
      final prefs = await SharedPreferences.getInstance();
      // 游标还没建好就先去建，**不要**用 0 当游标 —— 那会把历史全推出来。
      if (prefs.getBool(kCursorReady) != true) {
        await _ensureCursor();
        if (prefs.getBool(kCursorReady) != true) return;
      }

      final since = prefs.getInt(kCursor) ?? 0;
      final enabled = prefs.getBool(kNotifyEnabled) ?? true;

      final r = await _repo.fetchPushPending(sinceId: since, limit: 50);

      // ★ 先把游标推进，再弹通知。
      //   顺序反过来的话：弹通知过程中抛异常 → 游标没动 → 下一轮同一条再弹一次。
      //   宁可（极少数情况下）漏一条，也不要循环重弹同一条。
      //
      // ⚠️ 只在「这一批真拉到了东西」时推进，**不要**用 maxId 兜底（已修）。
      //   maxId 是**全局最大通知 id**，和「队列里有什么」不是一回事：
      //   抓取刚跑完、AI 还在串行摘要时，新通知的 ai_status 还是 pending，
      //   进不了队列（后端过滤 `ai_status == done`）—— 此时 items 为空
      //   而 maxId 已经很大。若拿 maxId 当游标，等这批摘要好之后，
      //   游标早就走过去了，**它们永远不会被推送**。
      final cursor = r.nextSinceId > since ? r.nextSinceId : since;
      if (cursor > since) await prefs.setInt(kCursor, cursor);
      await prefs.remove(kLastError);

      if (r.items.isEmpty) return;

      // 免打扰时段：仍然推进游标（消息不会丢，列表里能看到），只是不弹系统通知。
      if (enabled && !r.quietNow) {
        await _notifyAll(r.items);
      } else if (r.quietNow) {
        debugPrint('[push] 免打扰时段，${r.items.length} 条只入列表不弹窗');
      }

      onItems?.call(r.items);
    } on ApiException catch (e) {
      _report(e.message);
    } catch (e) {
      _report('$e');
    } finally {
      _busy = false;
    }
  }

  Future<void> _notifyAll(List<PushPendingItem> items) async {
    // 一批最多弹 3 条，其余汇总成一条 —— 一次弹十几条会淹没通知栏。
    const head = 3;
    for (final it in items.take(head)) {
      await _notify(it);
    }
    if (items.length > head) {
      final rest = items.length - head;
      await _notify(PushPendingItem(
        id: -1,
        title: '还有 $rest 条新通知',
        lines: const ['打开知了查看全部'],
        importance: 'normal',
        importanceLabel: '一般',
        sourceName: '知了',
      ));
    }
  }

  Future<void> _notify(PushPendingItem it) async {
    if (!_pluginReady) return;
    final urgent = it.importance == 'urgent';
    final important = it.importance == 'important';

    final android = AndroidNotificationDetails(
      _channelId,
      _channelName,
      channelDescription: _channelDesc,
      importance: urgent
          ? Importance.max
          : (important ? Importance.high : Importance.defaultImportance),
      priority: urgent
          ? Priority.high
          : (important ? Priority.high : Priority.defaultPriority),
      // 3 行摘要用 BigText，保证展开后能看到完整三行。
      styleInformation: BigTextStyleInformation(it.body),
      groupKey: 'quanzhi',
    );
    const ios = DarwinNotificationDetails(
      presentAlert: true,
      presentBadge: true,
      presentSound: true,
    );

    try {
      // id 用通知自身的 id，同一条重复到达会覆盖而不是堆叠。
      await _plugin.show(
        it.id < 0 ? 999999 : it.id,
        '${it.sourceName.isEmpty ? '知了' : it.sourceName} · ${it.importanceLabel}',
        it.body,
        NotificationDetails(android: android, iOS: ios),
      );
    } catch (e) {
      // 弹窗失败不该影响其它条目
      debugPrint('[push] 弹出通知失败（id=${it.id}）：$e');
    }
  }

  // ================================================================ 设置
  Future<int> get intervalMinutes async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getInt(kIntervalMinutes) ?? defaultIntervalMinutes;
  }

  Future<void> setIntervalMinutes(int minutes) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setInt(kIntervalMinutes, minutes.clamp(1, 720));
    if (running) await start(minutes: minutes);
  }

  Future<bool> get notifyEnabled async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getBool(kNotifyEnabled) ?? true;
  }

  Future<void> setNotifyEnabled(bool value) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(kNotifyEnabled, value);
  }

  Future<String?> get lastError async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString(kLastError);
  }

  void _report(String message) {
    debugPrint('[push] $message');
    // 写错误也要容错 —— 这里已经在异常路径上了。
    // 注意别写成 `.catchError((_) => SharedPreferences.getInstance())`：
    // then 的返回值是 Future<bool>，catchError 回调返回 Future<SharedPreferences>
    // 类型对不上，静态检查就会报错。老老实实包一层 async 闭包。
    unawaited(() async {
      try {
        final p = await SharedPreferences.getInstance();
        await p.setString(kLastError, message);
      } catch (_) {
        // 连错误都存不下就算了，别再往上抛
      }
    }());
    onError?.call(message);
  }
}
