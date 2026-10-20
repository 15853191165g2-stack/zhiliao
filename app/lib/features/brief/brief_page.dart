import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/app_state.dart';
import '../../data/models.dart';
import '../../widgets/common.dart';

/// 今日简报：把今天的通知压成 30 秒能读完的一段话。
/// 顶部的朗读按钮是给 TTS 留的位置（阶段 5 接 flutter_tts）。
class BriefPage extends StatelessWidget {
  const BriefPage({super.key});

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final brief = state.brief;

    if (brief == null) {
      return const LoadingView(message: '正在生成今日简报…');
    }

    final scheme = Theme.of(context).colorScheme;

    return RefreshIndicator(
      onRefresh: () => context.read<AppState>().loadBrief(),
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.only(bottom: 28),
        children: [
          // ---- 头部 ----
          Container(
            margin: const EdgeInsets.fromLTRB(12, 12, 12, 0),
            padding: const EdgeInsets.fromLTRB(18, 18, 18, 16),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(16),
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [scheme.primary, scheme.primary.withAlpha(200)],
              ),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  brief.date,
                  style: TextStyle(fontSize: 12, color: scheme.onPrimary.withAlpha(210)),
                ),
                const SizedBox(height: 8),
                Text(
                  brief.greeting,
                  style: TextStyle(fontSize: 13, color: scheme.onPrimary.withAlpha(230)),
                ),
                const SizedBox(height: 10),
                Text(
                  brief.headline,
                  style: TextStyle(
                    fontSize: 19,
                    height: 1.4,
                    fontWeight: FontWeight.w600,
                    color: scheme.onPrimary,
                  ),
                ),
                const SizedBox(height: 14),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton.icon(
                        style: OutlinedButton.styleFrom(
                          foregroundColor: scheme.onPrimary,
                          side: BorderSide(color: scheme.onPrimary.withAlpha(120)),
                        ),
                        onPressed: () => _speak(context, brief),
                        icon: const Icon(Icons.volume_up_outlined, size: 18),
                        label: const Text('朗读简报'),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: OutlinedButton.icon(
                        style: OutlinedButton.styleFrom(
                          foregroundColor: scheme.onPrimary,
                          side: BorderSide(color: scheme.onPrimary.withAlpha(120)),
                        ),
                        onPressed: () {
                          ScaffoldMessenger.of(context).showSnackBar(
                            SnackBar(
                              content: Text('生成方：${brief.generatedBy}'),
                            ),
                          );
                        },
                        icon: const Icon(Icons.info_outline, size: 18),
                        label: Text(brief.generatedBy == 'local' ? '本地规则' : 'AI 生成'),
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),

          // ---- 统计 ----
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
            child: Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                _Stat(label: '新通知', value: brief.counts['total'] ?? 0, color: scheme.primary),
                _Stat(
                  label: '紧急',
                  value: brief.counts['urgent'] ?? 0,
                  color: const Color(0xFFE5484D),
                ),
                _Stat(
                  label: '即将截止',
                  value: brief.counts['due_soon'] ?? 0,
                  color: const Color(0xFFF5A524),
                ),
                _Stat(label: '未读', value: brief.counts['unread'] ?? 0, color: scheme.outline),
              ],
            ),
          ),

          if (brief.sections.isEmpty)
            const Padding(
              padding: EdgeInsets.only(top: 40),
              child: EmptyView(
                icon: Icons.beach_access_outlined,
                title: '今天没有新通知',
                subtitle: '安心安排自己的事情',
              ),
            )
          else
            for (final section in brief.sections) _SectionCard(section: section),

          // ---- 朗读全文（给 TTS 用的文本，让用户能看到会读什么）----
          if (brief.speechText.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 6, 12, 0),
              child: Card(
                elevation: 0,
                color: scheme.surface,
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
                child: ExpansionTile(
                  leading: Icon(Icons.record_voice_over_outlined, color: scheme.primary),
                  title: const Text('朗读稿预览', style: TextStyle(fontSize: 14)),
                  subtitle: const Text('点开看看会读些什么', style: TextStyle(fontSize: 12)),
                  childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
                  children: [
                    SelectableText(
                      brief.speechText,
                      style: const TextStyle(fontSize: 14, height: 1.7),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }

  void _speak(BuildContext context, Brief brief) {
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('朗读'),
        content: Text(
          '阶段 5 接入 flutter_tts 后，这里会用系统语音朗读下方这段内容，'
          '语速可在「设置」里调整。\n\n'
          '当前设置的语速：${context.read<AppState>().speechRate.toStringAsFixed(1)}x\n\n'
          '${brief.speechText}',
        ),
        actions: [TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('知道了'))],
      ),
    );
  }
}

class _Stat extends StatelessWidget {
  const _Stat({required this.label, required this.value, required this.color});

  final String label;
  final int value;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
      decoration: BoxDecoration(
        color: color.withAlpha(24),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            '$value',
            style: TextStyle(fontSize: 17, fontWeight: FontWeight.w700, color: color),
          ),
          const SizedBox(width: 6),
          Text(label, style: TextStyle(fontSize: 12, color: color)),
        ],
      ),
    );
  }
}

class _SectionCard extends StatelessWidget {
  const _SectionCard({required this.section});

  final BriefSection section;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      elevation: 0,
      color: scheme.surface,
      margin: const EdgeInsets.fromLTRB(12, 10, 12, 0),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              section.title,
              style: Theme.of(context)
                  .textTheme
                  .titleSmall
                  ?.copyWith(fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 8),
            for (final line in section.items)
              Padding(
                padding: const EdgeInsets.only(bottom: 6),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Padding(
                      padding: const EdgeInsets.only(top: 7),
                      child: Container(
                        width: 5,
                        height: 5,
                        decoration: BoxDecoration(color: scheme.primary, shape: BoxShape.circle),
                      ),
                    ),
                    const SizedBox(width: 9),
                    Expanded(
                      child: Text(line, style: const TextStyle(fontSize: 13.5, height: 1.5)),
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
