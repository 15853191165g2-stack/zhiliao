# 知了 · Flutter 客户端

## 跑起来（三步）

```bash
cd app

# ① 生成 android/ 与 ios/ 平台目录
#    仓库里只提交了 lib/ 和 pubspec.yaml，平台目录用官方命令生成，避免手写漏文件
flutter create --platforms=android,ios --org cn.edu.quanzhi --project-name quanzhi .

# ② 拉依赖
flutter pub get

# ③ 跑
flutter run                # 需要已连接 Android 设备或启动了模拟器
```

### 后端地址怎么填

| 场景 | 地址 |
|---|---|
| Android 模拟器 | `http://10.0.2.2:8000`（默认值，10.0.2.2 = 宿主机 127.0.0.1） |
| 真机（同一 Wi-Fi） | `http://192.168.x.x:8000`，在 App 的「设置 → 后端地址」里改 |
| iOS 模拟器 | `http://127.0.0.1:8000` |

**后端没启动也能跑**：App 会自动进入「离线演示模式」，用内置样例数据渲染全部界面，
顶部会显示醒目的橙色提示条。这样 UI 可以脱离后端单独调。

## 没有 Flutter SDK 时的自检

```bash
python scripts/check_dart.py
```

检查相对 import 是否都存在、括号是否配平、有无重复类名，
以及阶段 3 推送链路的**接线检查**（`PushService` / `PushPendingItem` /
`fetchPushPending` 有没有被真正用到，而不是写了没接）。
它**不能**替代 `flutter analyze`，只是没装 SDK 时的第一道防线。

## 目录

```
lib/
├── main.dart                    入口 + 底部导航（通知/简报/问AI/来源/设置）
│                                并在 AppLifecycleState.resumed 时补拉一次推送
├── core/
│   ├── theme.dart               浅色/深色主题、重要程度配色、分类图标
│   ├── api_client.dart          HTTP 封装，统一解析 {code,message,data} 响应包
│   ├── app_state.dart           唯一的全局状态（ChangeNotifier）+ 离线兜底逻辑
│   └── push_service.dart        ★ 阶段 3：轮询 /api/push/pending + 系统本地通知
├── data/
│   ├── models.dart              与后端 schemas.py 一一对应的模型（含 PushPendingItem）
│   ├── repository.dart          所有接口调用的唯一出口
│   └── demo_data.dart           离线演示数据
├── widgets/                     通用组件（通知卡片 / 关键信息卡片 / 空态 / 离线条）
└── features/
    ├── feed/                    首页信息流（筛选、搜索、下拉刷新、上拉加载）
    ├── detail/                  详情：AI 摘要 → 关键信息 → 原文
    ├── brief/                   今日简报
    ├── chat/                    AI 对话（带引用来源）
    ├── sources/                 来源管理 + 添加来源表单
    └── settings/                后端地址 / 刷新时点 / 推送 / 语速 / 外观
```

## 阶段 3：手机端通知（需要手动补两处原生配置）

生成完 `android/` 目录后，通知要真正弹出来还必须做两件事，
**这两处我没法在本机验证**（没有 Flutter SDK，也生成不了平台目录）：

**① `android/app/src/main/AndroidManifest.xml` 加权限**
（Android 13+ 必须要，不加的话 `requestNotificationsPermission()` 会直接返回 false）

```xml
<uses-permission android:name="android.permission.POST_NOTIFICATIONS"/>
<!-- 轮询本身需要联网权限（Flutter 模板默认已有 INTERNET） -->
<uses-permission android:name="android.permission.INTERNET"/>
```

**② 通知图标**：`push_service.dart` 里用的是 `@mipmap/ic_launcher`。
Flutter 模板默认有 `ic_launcher`，可以直接跑；
要换成专属的小图标（通知栏图标应该是**白色单色**的），
在 `res/drawable/` 放一个 `ic_stat_quanzhi.png` 并把那行改成 `'ic_stat_quanzhi'`。

iOS 侧需要在 `ios/Runner/Info.plist` 里保留 `UIBackgroundModes` 的 `fetch`/`processing`
（如果要尝试后台补拉），并让 `requestPermissions(alert:badge:sound:)` 弹出授权框。

> ⚠️ **边界（不要在 UI 上含糊过去）**：这是**轮询**，不是真正的推送。
> App 被系统杀掉后定时器就停了，新通知要等再次打开 App 才补上。
> 真正要做到「App 没开也能收到」必须走 FCM 或厂商推送 ——
> FCM 在国内无 GMS 手机上收不到，厂商推送要开发者账号 + 上架审核，个人拿不到。
> 所以阶段 3 把「推送」做成**可插拔通道**（后端 `register_channel`），
> 将来有凭据时加一个 `fcm` / `getui` 通道，业务代码一行都不用改。
> 详细取舍见仓库根目录 `README.md` 的「诚实的边界说明」。

## 设计上的几个取舍

1. **只有一个 ChangeNotifier**。这个 App 的状态不复杂，引入 Riverpod/Bloc 是过度设计。
   等到阶段 4/5 加入后台通知监听和 TTS 播放状态时，如果 `AppState` 超过 500 行再拆。
2. **离线兜底写在状态层，不写在页面里**。每个页面只管「有数据就渲染」，
   是真是假由 `AppState.offline` 统一标记——避免每个页面各自写一套 try/catch 空态。
3. **不自己渲染 HTML**。详情页展示的是后端抽好的 `content_text`，不引 `flutter_html`。
   学校通知页面的 HTML 里塞满了表格样式，渲染出来比纯文本更难读。
4. **卡片排版优先级：3 行摘要 > 标题 > 元信息**。用户扫一眼就该知道要不要点进去。
5. **推送用持久化游标，不用 ack**。后端的 `notified_at` 是**全局**的：
   手机 ack 了，电脑就再也拿不到这条。所以每台设备在本地存自己的
   `since_id`，各拉各的，互不抢消息。
6. **第一次启动不弹历史，但也不漏掉待推送的**。历史通知由**服务端的启动基线**
   负责挡住（库里已有的会被标成「无需推送」）；客户端游标只需要从
   **队列里最老那条之前**开始拉。
   ⚠️ 一开始游标是对齐到「当前最大 id」的，那是错的 ——
   队列里待推送的那几条**正好**都满足 `id <= maxId`，会被整体跳过：
   不弹、不报错、日志干净。换台手机或清了 App 数据就会遇到。
   详见 `docs/02-gotchas.md` 第 50 条，复现脚本 `D:\QuanZhi\scripts\probe_push_cursor.py`。
   桌面版同样是这个坑，两端现在保持一致。
7. **先推进游标，再弹通知**。反过来的话，弹通知过程中抛异常会导致
   游标没动，下一轮同一条再弹一次。宁可极少数情况下漏一条，也不要循环重弹。
8. **推进游标只在「真拉到了东西」时发生**。不要拿 `maxId` 兜底 ——
   抓取刚跑完、AI 还在串行摘要时，新通知的 `ai_status` 还是 `pending`，
   进不了队列，此时 `items` 为空而 `maxId` 已经很大；若拿它当游标，
   等这批摘要好之后游标早走过去了，**它们永远不会被推送**。
   这三条约束都有静态断言守着，见 `scripts/check_dart.py` 的「游标安全自检」。

## 阶段 5 要加的依赖（已在 pubspec 里注释预留）

```yaml
flutter_tts: ^4.0.2                # 朗读通知与简报，语速取 AppState.speechRate
add_2_calendar: ^3.0.1             # 「加入日历并提醒我」按钮接它
flutter_secure_storage: ^9.2.2     # 学校登录 Cookie / 账密，只存本地
```

（`flutter_local_notifications` 已在阶段 3 提前加进 `dependencies`。）
