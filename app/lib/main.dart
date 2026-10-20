import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'core/app_state.dart';
import 'core/theme.dart';
import 'features/brief/brief_page.dart';
import 'features/chat/chat_page.dart';
import 'features/feed/feed_page.dart';
import 'features/settings/settings_page.dart';
import 'features/sources/sources_page.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const QuanZhiApp());
}

class QuanZhiApp extends StatelessWidget {
  const QuanZhiApp({super.key});

  @override
  Widget build(BuildContext context) {
    return ChangeNotifierProvider(
      create: (_) => AppState()..bootstrap(),
      child: Consumer<AppState>(
        builder: (context, state, _) {
          return MaterialApp(
            title: '知了',
            debugShowCheckedModeBanner: false,
            theme: buildLightTheme(),
            darkTheme: buildDarkTheme(),
            themeMode: state.themeMode,
            home: const HomeShell(),
          );
        },
      ),
    );
  }
}

class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> with WidgetsBindingObserver {
  int _index = 0;

  static const _pages = <Widget>[
    FeedPage(),
    BriefPage(),
    ChatPage(),
    SourcesPage(),
    SettingsPage(),
  ];

  static const _titles = <String>['通知', '今日简报', '问 AI', '来源管理', '设置'];

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // 回到前台立刻补拉一次。
    // 为什么需要：后台期间系统可能把进程冻结，定时器就不再走了 ——
    // 回来时如果不补拉，这段时间的通知要等下一个周期才出现。
    //（更根本的限制是「App 被杀掉就收不到」，那不是补拉能解决的，
    //  设置页里已经如实写明。）
    if (state == AppLifecycleState.resumed) {
      context.read<AppState>().pollPushNow();
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    return Scaffold(
      appBar: AppBar(
        title: Text(_titles[_index]),
        actions: _index == 0
            ? [
                if (state.offline)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: Chip(
                      label: const Text('离线演示'),
                      visualDensity: VisualDensity.compact,
                      side: BorderSide.none,
                      backgroundColor: Theme.of(context).colorScheme.errorContainer,
                    ),
                  ),
                IconButton(
                  tooltip: '搜索',
                  icon: const Icon(Icons.search),
                  onPressed: () => _showSearch(context, state),
                ),
              ]
            : null,
      ),
      body: IndexedStack(index: _index, children: _pages),
      floatingActionButton: _index == 3
          ? FloatingActionButton.extended(
              onPressed: () => showAddSourceSheet(context),
              icon: const Icon(Icons.add),
              label: const Text('添加来源'),
            )
          : null,
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.dynamic_feed_outlined), selectedIcon: Icon(Icons.dynamic_feed), label: '通知'),
          NavigationDestination(icon: Icon(Icons.article_outlined), selectedIcon: Icon(Icons.article), label: '简报'),
          NavigationDestination(icon: Icon(Icons.chat_bubble_outline), selectedIcon: Icon(Icons.chat_bubble), label: '问 AI'),
          NavigationDestination(icon: Icon(Icons.rss_feed_outlined), selectedIcon: Icon(Icons.rss_feed), label: '来源'),
          NavigationDestination(icon: Icon(Icons.settings_outlined), selectedIcon: Icon(Icons.settings), label: '设置'),
        ],
      ),
    );
  }

  Future<void> _showSearch(BuildContext context, AppState state) async {
    final controller = TextEditingController(text: state.query);
    final result = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      builder: (ctx) => Padding(
        padding: EdgeInsets.only(
          left: 16,
          right: 16,
          top: 16,
          bottom: MediaQuery.of(ctx).viewInsets.bottom + 16,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            TextField(
              controller: controller,
              autofocus: true,
              decoration: const InputDecoration(
                labelText: '搜索通知标题和正文',
                prefixIcon: Icon(Icons.search),
                border: OutlineInputBorder(),
              ),
              onSubmitted: (v) => Navigator.pop(ctx, v),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: OutlinedButton(
                    onPressed: () => Navigator.pop(ctx, ''),
                    child: const Text('清除条件'),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: FilledButton(
                    onPressed: () => Navigator.pop(ctx, controller.text),
                    child: const Text('搜索'),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
    if (result != null && context.mounted) {
      await state.applySearch(result);
    }
  }
}
