import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/app_state.dart';
import '../../data/models.dart';
import '../../widgets/common.dart';

/// 来源管理：添加 / 删除 / 启停 / 单独抓取。
///
/// 「添加来源」的落点就是后端 sources/*.yaml 的同一份配置结构，
/// 所以 App 里加的和手写 yaml 加的是同一种东西，不会两套逻辑。
class SourcesPage extends StatelessWidget {
  const SourcesPage({super.key});

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();

    return RefreshIndicator(
      onRefresh: () => context.read<AppState>().loadSources(),
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.only(bottom: 90),
        children: [
          const SectionTitle(text: '已接入的来源'),
          if (state.sources.isEmpty)
            const Padding(
              padding: EdgeInsets.only(top: 40),
              child: EmptyView(
                icon: Icons.rss_feed_outlined,
                title: '还没有来源',
                subtitle: '点右下角添加学校站点，或在后端 sources/ 里写好 yaml 后点「从 yaml 同步」',
              ),
            )
          else
            for (final s in state.sources) _SourceTile(source: s),
          const SectionTitle(text: '各渠道的接入方式'),
          const _ChannelGuide(),
        ],
      ),
    );
  }
}

class _SourceTile extends StatelessWidget {
  const _SourceTile({required this.source});
  final AppSource source;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final ok = source.lastStatus == 'ok';
    final statusColor = switch (source.lastStatus) {
      'ok' => const Color(0xFF2E9E5B),
      'blocked' => const Color(0xFFF5A524),
      _ => scheme.outline,
    };

    return Card(
      elevation: 0,
      color: scheme.surface,
      margin: const EdgeInsets.fromLTRB(12, 5, 12, 5),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 12, 8, 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    source.name,
                    style: Theme.of(context)
                        .textTheme
                        .titleSmall
                        ?.copyWith(fontWeight: FontWeight.w600),
                  ),
                ),
                Switch(
                  value: source.enabled,
                  onChanged: (_) => context.read<AppState>().toggleSource(source),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Wrap(
              spacing: 6,
              runSpacing: 4,
              children: [
                TagChip(label: source.adapter, color: scheme.primary),
                TagChip(label: source.categoryHint, color: scheme.outline),
                if (source.school.isNotEmpty)
                  TagChip(label: source.school, color: scheme.outline),
                TagChip(
                  label: switch (source.lastStatus) {
                    'ok' => '正常',
                    'blocked' => '被 robots 拦截',
                    'error' => '抓取失败',
                    _ => '未抓取',
                  },
                  color: statusColor,
                ),
                if (source.itemCount > 0)
                  TagChip(label: '${source.itemCount} 条', color: scheme.outline),
              ],
            ),
            if (source.url.isNotEmpty) ...[
              const SizedBox(height: 6),
              Text(
                source.url,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: 11, color: scheme.outline),
              ),
            ],
            if (source.lastError != null) ...[
              const SizedBox(height: 6),
              Text(
                source.lastError!,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: 11, color: scheme.error),
              ),
            ],
            Row(
              children: [
                TextButton.icon(
                  onPressed: () async {
                    final messenger = ScaffoldMessenger.of(context);
                    final msg = await context.read<AppState>().fetchSource(source);
                    messenger.showSnackBar(SnackBar(content: Text(msg ?? '抓取完成')));
                  },
                  icon: const Icon(Icons.refresh, size: 16),
                  label: const Text('抓取'),
                ),
                TextButton.icon(
                  onPressed: () => _confirmDelete(context, source),
                  icon: const Icon(Icons.delete_outline, size: 16),
                  label: const Text('删除'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _confirmDelete(BuildContext context, AppSource source) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('删除来源'),
        content: Text('删除「${source.name}」？已经抓到的通知会保留，不会被一起删掉。'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('删除')),
        ],
      ),
    );
    if (ok == true && context.mounted) {
      await context.read<AppState>().deleteSource(source);
    }
  }
}

class _ChannelGuide extends StatelessWidget {
  const _ChannelGuide();

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    const rows = <List<String>>[
      ['学校官网 / 教务处 / 学院 / 图书馆', 'html_list 适配器', '改 yaml 里的选择器即可，无需写代码'],
      ['学校自带的 RSS', 'rss 适配器', '直接填 feed 地址'],
      ['微信公众号文章', '链接导入 / 第三方 RSS 桥接', '微信没有公开接口，只能用这两种方式'],
      ['微信小程序通知', '分享链接导入', '小程序通知没有可访问的数据源'],
      ['学校 APP 通知（Android）', '通知监听权限', '阶段 4 实现，需手动授权「通知使用权」'],
      ['学校 APP 通知（iOS）', '分享扩展 / 截图 OCR', 'iOS 沙盒禁止读取其他 App 的通知，做不到直接监听'],
    ];

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12),
      child: Card(
        elevation: 0,
        color: scheme.surface,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              for (final r in rows)
                Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        r[0],
                        style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        '${r[1]} · ${r[2]}',
                        style: TextStyle(fontSize: 11.5, color: scheme.outline, height: 1.45),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// 添加来源对话框。抽成独立函数方便从 FAB 调用。
Future<void> showAddSourceSheet(BuildContext context) async {
  final state = context.read<AppState>();
  final nameCtrl = TextEditingController();
  final urlCtrl = TextEditingController();
  final schoolCtrl = TextEditingController();
  String adapter = 'html_list';
  String category = '教务';
  final listSelectorCtrl = TextEditingController(text: 'ul.news-list li');
  final titleCtrl = TextEditingController(text: 'a');
  final dateCtrl = TextEditingController(text: 'span.date');
  final contentCtrl = TextEditingController(text: 'div.v_news_content');

  await showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    builder: (ctx) {
      var busy = false;
      return StatefulBuilder(
        builder: (ctx, setSheetState) => Padding(
          padding: EdgeInsets.only(
            left: 20,
            right: 20,
            top: 20,
            bottom: MediaQuery.of(ctx).viewInsets.bottom + 20,
          ),
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('添加来源', style: Theme.of(ctx).textTheme.titleMedium),
                const SizedBox(height: 14),
                TextField(
                  controller: nameCtrl,
                  decoration: const InputDecoration(
                    labelText: '来源名称',
                    hintText: '例如：XX大学教务处通知',
                    border: OutlineInputBorder(),
                  ),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: schoolCtrl,
                  decoration: const InputDecoration(
                    labelText: '学校（可选）',
                    border: OutlineInputBorder(),
                  ),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: urlCtrl,
                  decoration: const InputDecoration(
                    labelText: '通知列表页地址 / RSS 地址',
                    hintText: 'https://jwc.xxx.edu.cn/tzgg/index.htm',
                    border: OutlineInputBorder(),
                  ),
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: DropdownButtonFormField<String>(
                        value: adapter,
                        decoration: const InputDecoration(
                          labelText: '适配器',
                          border: OutlineInputBorder(),
                        ),
                        items: (state.adapters.isEmpty
                                ? [
                                    {'kind': 'html_list', 'doc': ''},
                                    {'kind': 'rss', 'doc': ''},
                                  ]
                                : state.adapters)
                            .map(
                              (a) => DropdownMenuItem(
                                value: '${a['kind']}',
                                child: Text('${a['kind']}'),
                              ),
                            )
                            .toList(),
                        onChanged: (v) => setSheetState(() => adapter = v ?? 'html_list'),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: DropdownButtonFormField<String>(
                        value: category,
                        decoration: const InputDecoration(
                          labelText: '类别',
                          border: OutlineInputBorder(),
                        ),
                        items: kCategories
                            .map((c) => DropdownMenuItem(value: c, child: Text(c)))
                            .toList(),
                        onChanged: (v) => setSheetState(() => category = v ?? '其他'),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                if (adapter == 'html_list') ...[
                  TextField(
                    controller: listSelectorCtrl,
                    decoration: const InputDecoration(
                      labelText: '列表项选择器（list_selector）',
                      border: OutlineInputBorder(),
                    ),
                  ),
                  const SizedBox(height: 12),
                  Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: titleCtrl,
                          decoration: const InputDecoration(
                            labelText: '标题选择器',
                            border: OutlineInputBorder(),
                          ),
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: TextField(
                          controller: dateCtrl,
                          decoration: const InputDecoration(
                            labelText: '日期选择器',
                            border: OutlineInputBorder(),
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: contentCtrl,
                    decoration: const InputDecoration(
                      labelText: '详情页正文选择器（可选）',
                      border: OutlineInputBorder(),
                    ),
                  ),
                  const SizedBox(height: 10),
                  Text(
                    '提示：浏览器 F12 右键列表项 → 检查，就能看到该用哪个选择器。'
                    '填好先保存，再点该来源的「抓取」验证；报错会显示在来源卡片下方。',
                    style: TextStyle(
                      fontSize: 11.5,
                      color: Theme.of(ctx).colorScheme.outline,
                      height: 1.5,
                    ),
                  ),
                ],
                const SizedBox(height: 20),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton(
                    onPressed: busy
                        ? null
                        : () async {
                            if (nameCtrl.text.trim().isEmpty || urlCtrl.text.trim().isEmpty) {
                              ScaffoldMessenger.of(ctx).showSnackBar(
                                const SnackBar(content: Text('来源名称和地址都得填')),
                              );
                              return;
                            }
                            setSheetState(() => busy = true);
                            final config = adapter == 'html_list'
                                ? {
                                    'list_selector': listSelectorCtrl.text.trim(),
                                    'fields': {
                                      'title': titleCtrl.text.trim(),
                                      'url': '${titleCtrl.text.trim()}@href',
                                      'date': dateCtrl.text.trim(),
                                    },
                                    'follow_detail': contentCtrl.text.trim().isNotEmpty,
                                    'detail': {'content_selector': contentCtrl.text.trim()},
                                  }
                                : <String, dynamic>{};
                            final err = await state.addSource(
                              name: nameCtrl.text.trim(),
                              url: urlCtrl.text.trim(),
                              adapter: adapter,
                              school: schoolCtrl.text.trim(),
                              category: category,
                              config: config,
                            );
                            setSheetState(() => busy = false);
                            if (!ctx.mounted) return;
                            if (err == null) {
                              Navigator.pop(ctx);
                              ScaffoldMessenger.of(context).showSnackBar(
                                const SnackBar(content: Text('来源已添加，点「抓取」验证一下')),
                              );
                            } else {
                              ScaffoldMessenger.of(ctx).showSnackBar(SnackBar(content: Text(err)));
                            }
                          },
                    child: Text(busy ? '保存中…' : '保存'),
                  ),
                ),
              ],
            ),
          ),
        ),
      );
    },
  );
}
