import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../core/app_state.dart';
import '../../core/theme.dart';
import '../../data/models.dart';
import '../../widgets/common.dart';
import '../../widgets/key_info_card.dart';

/// 通知详情：原文 + AI 摘要 + 关键信息卡片。
///
/// 顺序刻意是「AI 摘要 → 关键信息 → 原文」：
/// 用户九成时候看完前两块就够了，原文是备查。
class DetailPage extends StatefulWidget {
  const DetailPage({super.key, required this.item});

  final AppNotification item;

  @override
  State<DetailPage> createState() => _DetailPageState();
}

class _DetailPageState extends State<DetailPage> {
  late AppNotification _item;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _item = widget.item;
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  Future<void> _load() async {
    final state = context.read<AppState>();
    final detail = await state.openDetail(_item);
    if (!mounted) return;
    setState(() {
      if (detail != null) _item = detail;
      _loading = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final state = context.watch<AppState>();
    // 列表里那条可能被收藏/已读操作更新过，优先用最新的那份
    final matches = state.items.where((n) => n.id == _item.id);
    final live = matches.isEmpty ? null : matches.first;
    final item = live ?? _item;

    return Scaffold(
      appBar: AppBar(
        title: Text(item.category, style: const TextStyle(fontSize: 16)),
        actions: [
          IconButton(
            tooltip: item.isStarred ? '取消收藏' : '收藏',
            icon: Icon(item.isStarred ? Icons.star : Icons.star_border),
            onPressed: () => context.read<AppState>().toggleStar(item),
          ),
          PopupMenuButton<String>(
            onSelected: (v) async {
              switch (v) {
                case 'open':
                  await _openOrigin(item);
                case 'resummarize':
                  await context.read<AppState>().resummarize(item);
                  if (context.mounted) {
                    ScaffoldMessenger.of(context).showSnackBar(
                      const SnackBar(content: Text('已重新生成摘要')),
                    );
                  }
                case 'read':
                  await context.read<AppState>().toggleRead(item);
              }
            },
            itemBuilder: (ctx) => [
              const PopupMenuItem(value: 'open', child: Text('在浏览器打开原文')),
              const PopupMenuItem(value: 'resummarize', child: Text('重新生成 AI 摘要')),
              PopupMenuItem(
                value: 'read',
                child: Text(item.isRead ? '标为未读' : '标为已读'),
              ),
            ],
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.only(bottom: 32),
        children: [
          if (_loading) const LinearProgressIndicator(minHeight: 2),

          // ---- 标题与元信息 ----
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 6),
            child: Text(
              item.title,
              style: Theme.of(context)
                  .textTheme
                  .titleLarge
                  ?.copyWith(fontWeight: FontWeight.w600, height: 1.35),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 10),
            child: Wrap(
              spacing: 8,
              runSpacing: 6,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                TagChip(
                  label: item.importanceLabel,
                  color: importanceColor(item.importance),
                  icon: Icons.priority_high,
                ),
                TagChip(label: item.category, icon: categoryIcon(item.category)),
                if (item.sourceName != null) TagChip(label: item.sourceName!, color: scheme.outline),
                Text(
                  fullTime(item.publishedAt),
                  style: TextStyle(fontSize: 11.5, color: scheme.outline),
                ),
              ],
            ),
          ),

          // ---- AI 3 行摘要 ----
          if (item.summaryLines.isNotEmpty) _SummaryCard(item: item),
          if (item.aiStatus == 'failed')
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              child: MaterialBanner(
                content: const Text('这条的 AI 摘要生成失败了，原文仍然完整保留。'),
                actions: [
                  TextButton(
                    onPressed: () => context.read<AppState>().resummarize(item),
                    child: const Text('重试'),
                  ),
                ],
              ),
            ),

          // ---- 关键信息 ----
          KeyInfoCard(
            info: item.keyInfo,
            onCopyAll: () => _copyKeyInfo(item),
            onAddToCalendar: () => _notYet('加入日历', '阶段 5 接入 add_2_calendar 后，这里会把截止时间写进系统日历并设提醒。'),
          ),

          // ---- 原文 ----
          SectionTitle(text: '原文'),
          Card(
            elevation: 0,
            color: scheme.surface,
            margin: const EdgeInsets.fromLTRB(12, 0, 12, 12),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: _buildContent(context, item),
            ),
          ),

          if (item.url != null)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: OutlinedButton.icon(
                onPressed: () => _openOrigin(item),
                icon: const Icon(Icons.open_in_new, size: 18),
                label: const Text('在浏览器打开原文'),
              ),
            ),

          if (item.tags.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 18, 16, 0),
              child: Wrap(
                spacing: 6,
                runSpacing: 6,
                children: [for (final t in item.tags) TagChip(label: '#$t', color: scheme.outline)],
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildContent(BuildContext context, AppNotification item) {
    final text = (item.contentText ?? '').trim();
    if (text.isNotEmpty) {
      return SelectableText(text, style: const TextStyle(fontSize: 14.5, height: 1.75));
    }
    final excerpt = (item.excerpt ?? '').trim();
    if (excerpt.isNotEmpty) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SelectableText(excerpt, style: const TextStyle(fontSize: 14.5, height: 1.75)),
          const SizedBox(height: 10),
          Text(
            '（只抓到摘要。在来源配置里打开 follow_detail: true 可以抓完整正文。）',
            style: TextStyle(
              fontSize: 12,
              color: Theme.of(context).colorScheme.outline,
            ),
          ),
        ],
      );
    }
    return Row(
      children: [
        Icon(Icons.warning_amber_outlined, size: 18, color: Theme.of(context).colorScheme.outline),
        const SizedBox(width: 8),
        const Expanded(child: Text('这条通知没有抓到正文，请点上面的按钮打开原文。')),
      ],
    );
  }

  Future<void> _openOrigin(AppNotification item) async {
    final url = item.url;
    if (url == null || url.isEmpty) {
      _notYet('打开原文', '这条通知没有原始链接（例如通知栏捕获或手动导入的内容）。');
      return;
    }
    final uri = Uri.tryParse(url);
    if (uri == null) return;
    final ok = await launchUrl(uri, mode: LaunchMode.externalApplication);
    if (!ok && mounted) _notYet('打开原文', '打不开链接：$url');
  }

  Future<void> _copyKeyInfo(AppNotification item) async {
    final ki = item.keyInfo;
    if (ki == null) return;
    final buffer = StringBuffer()..writeln(item.title);
    if (ki.deadline != null) buffer.writeln('截止：${ki.deadline}');
    if (ki.audience != null) buffer.writeln('对象：${ki.audience}');
    if (ki.location != null) buffer.writeln('地点：${ki.location}');
    if (ki.materials.isNotEmpty) buffer.writeln('材料：${ki.materials.join('、')}');
    if (ki.contact != null) buffer.writeln('联系：${ki.contact}');
    if (item.url != null) buffer.writeln(item.url);
    await Clipboard.setData(ClipboardData(text: buffer.toString()));
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('关键信息已复制，可以直接粘到备忘录')),
      );
    }
  }

  void _notYet(String title, String detail) {
    if (!mounted) return;
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: Text(detail),
        actions: [TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('知道了'))],
      ),
    );
  }
}

class _SummaryCard extends StatelessWidget {
  const _SummaryCard({required this.item});

  final AppNotification item;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      elevation: 0,
      color: scheme.primary.withAlpha(14),
      margin: const EdgeInsets.fromLTRB(12, 6, 12, 6),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(14),
        side: BorderSide(color: scheme.primary.withAlpha(60)),
      ),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.auto_awesome, size: 16, color: scheme.primary),
                const SizedBox(width: 6),
                Text(
                  'AI 摘要',
                  style: TextStyle(
                    fontWeight: FontWeight.w600,
                    color: scheme.primary,
                    fontSize: 13.5,
                  ),
                ),
                const Spacer(),
                if (item.aiModel != null)
                  Text(
                    item.aiModel!,
                    style: TextStyle(fontSize: 10.5, color: scheme.outline),
                  ),
              ],
            ),
            const SizedBox(height: 10),
            for (var i = 0; i < item.summaryLines.length; i++)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Container(
                      width: 18,
                      height: 18,
                      alignment: Alignment.center,
                      margin: const EdgeInsets.only(top: 1),
                      decoration: BoxDecoration(
                        color: scheme.primary.withAlpha(38),
                        borderRadius: BorderRadius.circular(5),
                      ),
                      child: Text(
                        '${i + 1}',
                        style: TextStyle(
                          fontSize: 10.5,
                          fontWeight: FontWeight.w700,
                          color: scheme.primary,
                        ),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: SelectableText(
                        item.summaryLines[i],
                        style: const TextStyle(fontSize: 14, height: 1.55),
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}
