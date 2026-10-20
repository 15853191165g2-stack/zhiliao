import 'package:flutter/material.dart';

import '../data/models.dart';
import 'common.dart';

/// 关键信息卡片：截止时间 / 对象 / 地点 / 材料 / 联系人。
///
/// 这是「不用点开原文就知道要干什么」的核心组件。
/// 「加入日历」按钮在阶段 5 接入真实日历后才会真正写系统日历，
/// 现在点了会明确告诉你还没接，不做假动作。
class KeyInfoCard extends StatelessWidget {
  const KeyInfoCard({
    super.key,
    required this.info,
    this.onAddToCalendar,
    this.onCopyAll,
  });

  final KeyInfo? info;
  final VoidCallback? onAddToCalendar;
  final VoidCallback? onCopyAll;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final ki = info;

    if (ki == null || ki.isEmpty) {
      return Card(
        elevation: 0,
        color: scheme.surface,
        margin: const EdgeInsets.fromLTRB(12, 6, 12, 6),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Row(
            children: [
              Icon(Icons.info_outline, size: 18, color: scheme.outline),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  '这条通知里没有可提取的结构化信息（没有明确的时间、地点、材料要求）。',
                  style: TextStyle(fontSize: 13, color: scheme.outline, height: 1.5),
                ),
              ),
            ],
          ),
        ),
      );
    }

    return Card(
      elevation: 0,
      color: scheme.surface,
      margin: const EdgeInsets.fromLTRB(12, 6, 12, 6),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.fact_check_outlined, size: 18, color: scheme.primary),
                const SizedBox(width: 8),
                Text(
                  '关键信息',
                  style: Theme.of(context)
                      .textTheme
                      .titleSmall
                      ?.copyWith(fontWeight: FontWeight.w600),
                ),
                const Spacer(),
                if (onCopyAll != null)
                  IconButton(
                    visualDensity: VisualDensity.compact,
                    iconSize: 18,
                    tooltip: '复制全部关键信息',
                    onPressed: onCopyAll,
                    icon: const Icon(Icons.copy_all_outlined),
                  ),
              ],
            ),
            const Divider(height: 18),

            if (ki.deadline != null)
              InfoRow(icon: Icons.event_busy, label: '截止', value: ki.deadline!),
            if (ki.audience != null)
              InfoRow(icon: Icons.groups_outlined, label: '对象', value: ki.audience!),
            if (ki.location != null)
              InfoRow(icon: Icons.place_outlined, label: '地点', value: ki.location!),
            if (ki.fee != null) InfoRow(icon: Icons.payments_outlined, label: '费用', value: ki.fee!),
            if (ki.contact != null)
              InfoRow(icon: Icons.contact_phone_outlined, label: '联系', value: ki.contact!),

            if (ki.materials.isNotEmpty) ...[
              const SizedBox(height: 4),
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Padding(
                    padding: const EdgeInsets.only(top: 6),
                    child: Icon(Icons.checklist_outlined, size: 16, color: scheme.primary),
                  ),
                  const SizedBox(width: 10),
                  SizedBox(
                    width: 56,
                    child: Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Text('材料', style: TextStyle(fontSize: 13, color: scheme.outline)),
                    ),
                  ),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        for (final m in ki.materials)
                          Padding(
                            padding: const EdgeInsets.symmetric(vertical: 3),
                            child: Row(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Padding(
                                  padding: const EdgeInsets.only(top: 6),
                                  child: Container(
                                    width: 5,
                                    height: 5,
                                    decoration: BoxDecoration(
                                      color: scheme.primary,
                                      shape: BoxShape.circle,
                                    ),
                                  ),
                                ),
                                const SizedBox(width: 8),
                                Expanded(
                                  child: SelectableText(
                                    m,
                                    style: const TextStyle(fontSize: 13.5, height: 1.4),
                                  ),
                                ),
                              ],
                            ),
                          ),
                      ],
                    ),
                  ),
                ],
              ),
            ],

            if (ki.deadlineTs != null) ...[
              const SizedBox(height: 14),
              SizedBox(
                width: double.infinity,
                child: OutlinedButton.icon(
                  onPressed: onAddToCalendar,
                  icon: const Icon(Icons.calendar_month_outlined, size: 18),
                  label: const Text('加入日历并提醒我'),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
