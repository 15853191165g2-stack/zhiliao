import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/app_state.dart';
import '../../widgets/common.dart';

/// 设置：后端地址、刷新时间、推送、朗读语速、外观。
class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key});

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late final TextEditingController _urlCtrl;
  late final TextEditingController _hoursCtrl;
  bool _testing = false;
  String? _healthText;

  @override
  void initState() {
    super.initState();
    final state = context.read<AppState>();
    _urlCtrl = TextEditingController(text: state.baseUrl);
    _hoursCtrl = TextEditingController(text: state.refreshHours);
  }

  @override
  void dispose() {
    _urlCtrl.dispose();
    _hoursCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final scheme = Theme.of(context).colorScheme;

    return ListView(
      padding: const EdgeInsets.only(bottom: 40),
      children: [
        const SectionTitle(text: '后端连接'),
        Card(
          elevation: 0,
          color: scheme.surface,
          margin: const EdgeInsets.symmetric(horizontal: 12),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                TextField(
                  controller: _urlCtrl,
                  decoration: const InputDecoration(
                    labelText: '后端地址',
                    border: OutlineInputBorder(),
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  'Android 模拟器用 http://10.0.2.2:8000（10.0.2.2 就是宿主机的 127.0.0.1）。\n'
                  '真机调试改成电脑的局域网 IP，例如 http://192.168.1.5:8000，'
                  '并确保手机和电脑在同一个 Wi-Fi 下。',
                  style: TextStyle(fontSize: 11.5, color: scheme.outline, height: 1.55),
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: FilledButton.tonal(
                        onPressed: _testing
                            ? null
                            : () async {
                                setState(() => _testing = true);
                                await state.setBaseUrl(_urlCtrl.text);
                                try {
                                  final h = await state.repo.health(deep: false);
                                  final counts = h['counts'] as Map? ?? {};
                                  final ai = h['ai'] as Map? ?? {};
                                  setState(() {
                                    _healthText =
                                        '连接成功 ✅\n'
                                        '来源 ${counts['sources']} 个，通知 ${counts['notifications']} 条，'
                                        '待摘要 ${counts['ai_pending']} 条\n'
                                        'AI：${ai['enabled'] == true ? "已接入 ${ai['model']}" : "未配置，走本地规则摘要"}\n'
                                        '刷新计划：${h['cron']}';
                                  });
                                } catch (e) {
                                  setState(() => _healthText = '连接失败 ❌\n$e');
                                } finally {
                                  setState(() => _testing = false);
                                }
                              },
                        child: Text(_testing ? '检测中…' : '保存并测试连接'),
                      ),
                    ),
                    const SizedBox(width: 10),
                    OutlinedButton(
                      onPressed: () async {
                        await state.setBaseUrl(AppState.defaultBaseUrl);
                        _urlCtrl.text = AppState.defaultBaseUrl;
                      },
                      child: const Text('恢复默认'),
                    ),
                  ],
                ),
                if (_healthText != null) ...[
                  const SizedBox(height: 12),
                  Container(
                    width: double.infinity,
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: scheme.surfaceContainerHighest,
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: SelectableText(
                      _healthText!,
                      style: const TextStyle(fontSize: 12, height: 1.6),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),

        const SectionTitle(text: '自动刷新时间'),
        Card(
          elevation: 0,
          color: scheme.surface,
          margin: const EdgeInsets.symmetric(horizontal: 12),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                TextField(
                  controller: _hoursCtrl,
                  decoration: const InputDecoration(
                    labelText: '刷新时点（24 小时制，逗号分隔）',
                    hintText: '8,18',
                    border: OutlineInputBorder(),
                  ),
                  onSubmitted: (v) => state.setRefreshHours(v),
                ),
                const SizedBox(height: 10),
                Wrap(
                  spacing: 8,
                  children: [
                    for (final preset in const ['8,18', '7,12,18,22', '9,15,21'])
                      ActionChip(
                        label: Text(preset),
                        onPressed: () {
                          _hoursCtrl.text = preset;
                          state.setRefreshHours(preset);
                        },
                      ),
                  ],
                ),
                const SizedBox(height: 10),
                Text(
                  '默认每天 8:00 和 18:00 各抓一次。这个设置在服务端生效'
                  '（改 backend/.env 的 REFRESH_CRON_HOURS，或在后端接口里调整）。'
                  'App 里的下拉刷新是立即抓一次，两边不冲突。',
                  style: TextStyle(fontSize: 11.5, color: scheme.outline, height: 1.55),
                ),
              ],
            ),
          ),
        ),

        const SectionTitle(text: '通知与朗读'),
        Card(
          elevation: 0,
          color: scheme.surface,
          margin: const EdgeInsets.symmetric(horizontal: 12),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          child: Column(
            children: [
              SwitchListTile(
                value: state.pushEnabled,
                onChanged: state.setPushEnabled,
                title: const Text('新通知推送提醒', style: TextStyle(fontSize: 14)),
                subtitle: Text(
                  '阶段 3 接入 FCM/APNs 后生效',
                  style: TextStyle(fontSize: 11.5, color: scheme.outline),
                ),
              ),
              const Divider(height: 1),
              ListTile(
                title: const Text('朗读语速', style: TextStyle(fontSize: 14)),
                subtitle: Slider(
                  value: state.speechRate,
                  min: 0.5,
                  max: 2.0,
                  divisions: 6,
                  label: '${state.speechRate.toStringAsFixed(1)}x',
                  onChanged: state.setSpeechRate,
                ),
                trailing: Text('${state.speechRate.toStringAsFixed(1)}x'),
              ),
            ],
          ),
        ),

        const SectionTitle(text: '外观'),
        Card(
          elevation: 0,
          color: scheme.surface,
          margin: const EdgeInsets.symmetric(horizontal: 12),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          child: Column(
            children: [
              RadioListTile<ThemeMode>(
                value: ThemeMode.system,
                groupValue: state.themeMode,
                onChanged: (v) => state.setThemeMode(v ?? ThemeMode.system),
                title: const Text('跟随系统', style: TextStyle(fontSize: 14)),
              ),
              RadioListTile<ThemeMode>(
                value: ThemeMode.light,
                groupValue: state.themeMode,
                onChanged: (v) => state.setThemeMode(v ?? ThemeMode.light),
                title: const Text('浅色', style: TextStyle(fontSize: 14)),
              ),
              RadioListTile<ThemeMode>(
                value: ThemeMode.dark,
                groupValue: state.themeMode,
                onChanged: (v) => state.setThemeMode(v ?? ThemeMode.dark),
                title: const Text('深色', style: TextStyle(fontSize: 14)),
              ),
            ],
          ),
        ),

        const SectionTitle(text: '关于'),
        Card(
          elevation: 0,
          color: scheme.surface,
          margin: const EdgeInsets.symmetric(horizontal: 12),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text('知了 v0.1.0', style: TextStyle(fontSize: 14)),
                const SizedBox(height: 8),
                Text(
                  'Flutter 客户端 · FastAPI 后端 · 智谱 GLM-4.7-Flash\n'
                  '抓取遵守 robots 协议与频率限制；学校登录凭据只存手机本地，不上传服务端。',
                  style: TextStyle(fontSize: 12, color: scheme.outline, height: 1.6),
                ),
                const SizedBox(height: 12),
                Text(
                  '当前进度：阶段 1（项目骨架 + 端到端垂直切片）已完成。\n'
                  '阶段 2-6：站点抓取与 AI 摘要 · 定时刷新与推送 · 公众号/小程序/学校 APP 接入 · '
                  '朗读/日历/问答 · 打包 APK。',
                  style: TextStyle(fontSize: 12, color: scheme.outline, height: 1.6),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}
