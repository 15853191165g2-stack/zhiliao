import 'package:flutter/material.dart';

import '../core/theme.dart';
import '../data/models.dart';
import 'common.dart';

/// 信息流里的通知卡片。
///
/// 排版优先级刻意做成「3 行摘要 > 标题 > 元信息」——
/// 用户扫一眼卡片就能知道要不要点进去看原文。
class NotificationCard extends StatelessWidget {
  const NotificationCard({
    super.key,
    required this.item,
    this.onTap,
    this.onStar,
  });

  final AppNotification item;
  final VoidCallback? onTap;
  final VoidCallback? onStar;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final impColor = importanceColor(item.importance);
    final lines = item.summaryLines;
    final horizontal = const EdgeInsets.symmetric(horizontal: 16);
    final isRead = item.isRead;

    return Card(
      elevation: 0,
      color: scheme.surface,
      margin: const EdgeInsets.fromLTRB(12, 6, 12, 6),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 12, 8, 12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // 顶部：重要程度 + 分类 + 时间 + 收藏
              Row(
                children: [
                  Container(
                    width: 6,
                    height: 6,
                    decoration: BoxDecoration(color: impColor, shape: BoxShape.circle),
                  ),
                  const SizedBox(width: 6),
                  Text(
                    item.importanceLabel,
                    style: TextStyle(fontSize: 11, color: impColor, fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(width: 8),
                  TagChip(label: item.category, icon: categoryIcon(item.category)),
                  if (!item.isRead) ...[
                    const SizedBox(width: 6),
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                      decoration: BoxDecoration(
                        color: scheme.primary,
                        borderRadius: BorderRadius.circular(4),
                      ),
                      child: Text(
                        '未读',
                        style: TextStyle(fontSize: 10, color: scheme.onPrimary),
                      ),
                    ),
                  ],
                  const Spacer(),
                  Text(
                    relativeTime(item.publishedAt),
                    style: TextStyle(fontSize: 11, color: scheme.outline),
                  ),
                  IconButton(
                    visualDensity: VisualDensity.compact,
                    iconSize: 18,
                    onPressed: onStar,
                    tooltip: item.isStarred ? '取消收藏' : '收藏',
                    icon: Icon(
                      item.isStarred ? Icons.star : Icons.star_border,
                      color: item.isStarred ? const Color(0xFFF5A524) : scheme.outline,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 6),

              // 标题
              Padding(
                padding: const EdgeInsets.only(right: 6),
                child: Text(
                  item.title,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.titleSmall?.copyWith(
                        fontWeight: FontWeight.w600,
                        height: 1.35,
                        color: isRead ? scheme.onSurfaceVariant : scheme.onSurface,
                      ),
                ),
              ),

              // 3 行 AI 摘要：这是知了的核心价值，所以要占最显眼的位置
              if (lines.isNotEmpty) ...[
                const SizedBox(height: 8),
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.fromLTRB(10, 8, 10, 8),
                  decoration: BoxDecoration(
                    color: scheme.primary.withAlpha(14),
                    borderRadius: BorderRadius.circular(8),
                    border: Border(left: BorderSide(color: scheme.primary, width: 2.5)),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Icon(Icons.auto_awesome, size: 12, color: scheme.primary),
                          const SizedBox(width: 4),
                          Text(
                            'AI 摘要',
                            style: TextStyle(
                              fontSize: 10.5,
                              color: scheme.primary,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          if (item.aiModel != null && item.aiModel != 'demo') ...[
                            const SizedBox(width: 6),
                            Text(
                              item.aiModel!,
                              style: TextStyle(fontSize: 10, color: scheme.outline),
                            ),
                          ],
                        ],
                      ),
                      const SizedBox(height: 5),
                      for (final line in lines.take(3))
                        Padding(
                          padding: const EdgeInsets.only(bottom: 2),
                          child: Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text('· ', style: TextStyle(fontSize: 12.5, color: scheme.primary)),
                              Expanded(
                                child: Text(
                                  line,
                                  style: const TextStyle(fontSize: 12.5, height: 1.45),
                                ),
                              ),
                            ],
                          ),
                        ),
                    ],
                  ),
                ),
              ] else ...[
                const SizedBox(height: 6),
                Text(
                  item.aiStatus == 'failed' ? 'AI 摘要生成失败，可进详情页重试' : 'AI 摘要生成中…',
                  style: TextStyle(fontSize: 12, color: scheme.outline),
                ),
              ],

              // 底部：截止时间 + 来源
              const SizedBox(height: 8),
              Padding(
                padding: const EdgeInsets.only(right: 6),
                child: Row(
                  children: [
                    if (item.keyInfo?.deadline != null) ...[
                      Icon(Icons.schedule, size: 13, color: impColor),
                      const SizedBox(width: 4),
                      Flexible(
                        child: Text(
                          '截止 ${item.keyInfo!.deadline}',
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(fontSize: 11.5, color: impColor),
                        ),
                      ),
                      const SizedBox(width: 10),
                    ],
                    Expanded(
                      child: Text(
                        item.sourceName ?? '',
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(fontSize: 11, color: scheme.outline),
                      ),
                    ),
                    Padding(padding: horizontal, child: const SizedBox.shrink()),
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
