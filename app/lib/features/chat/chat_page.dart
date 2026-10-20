import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/app_state.dart';
import '../../data/models.dart';
import '../../widgets/common.dart';

/// AI 对话。问的是「你自己学校最近的通知」，不是通用知识。
///
/// 每个回答都会附上引用的原始通知，方便你自己核对——不让你盲信模型。
class ChatPage extends StatefulWidget {
  const ChatPage({super.key});

  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> {
  final _input = TextEditingController();
  final _scroll = ScrollController();

  static const _suggestions = <String>[
    '这周有哪些截止的事项？',
    '奖学金申请要交什么材料？',
    '最近有什么讲座或活动？',
    '选课什么时候开始？',
  ];

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _send([String? preset]) async {
    final text = (preset ?? _input.text).trim();
    if (text.isEmpty) return;
    _input.clear();
    final state = context.read<AppState>();
    await state.ask(text);
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 80));
    if (_scroll.hasClients) {
      _scroll.animateTo(
        _scroll.position.maxScrollExtent,
        duration: const Duration(milliseconds: 260),
        curve: Curves.easeOut,
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();
    final scheme = Theme.of(context).colorScheme;

    return Column(
      children: [
        Expanded(
          child: state.chat.isEmpty
              ? _Intro(onPick: _send)
              : ListView.builder(
                  controller: _scroll,
                  padding: const EdgeInsets.symmetric(vertical: 12),
                  itemCount: state.chat.length + (state.asking ? 1 : 0),
                  itemBuilder: (context, index) {
                    if (index >= state.chat.length) {
                      return const Padding(
                        padding: EdgeInsets.all(16),
                        child: Row(
                          children: [
                            SizedBox(
                              width: 16,
                              height: 16,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            ),
                            SizedBox(width: 12),
                            Text('正在翻通知…', style: TextStyle(fontSize: 13)),
                          ],
                        ),
                      );
                    }
                    return _Bubble(message: state.chat[index]);
                  },
                ),
        ),
        const Divider(height: 1),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                IconButton(
                  tooltip: '清空对话',
                  onPressed: state.chat.isEmpty ? null : () => context.read<AppState>().clearChat(),
                  icon: const Icon(Icons.delete_outline),
                ),
                Expanded(
                  child: TextField(
                    controller: _input,
                    minLines: 1,
                    maxLines: 4,
                    textInputAction: TextInputAction.send,
                    onSubmitted: (_) => _send(),
                    decoration: InputDecoration(
                      hintText: '问点什么，比如「这周有哪些截止的事项」',
                      border: OutlineInputBorder(borderRadius: BorderRadius.circular(22)),
                      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                      filled: true,
                      fillColor: scheme.surface,
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                IconButton.filled(
                  onPressed: state.asking ? null : () => _send(),
                  icon: const Icon(Icons.arrow_upward),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _Intro extends StatelessWidget {
  const _Intro({required this.onPick});
  final ValueChanged<String> onPick;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 32, 20, 20),
      children: [
        Icon(Icons.auto_awesome, size: 44, color: scheme.primary),
        const SizedBox(height: 14),
        Text(
          '问我你学校的通知',
          style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 8),
        Text(
          '我只依据抓到的通知回答，每条结论都会附上原始通知让你核对。'
          '通知里没写的事，我会直接说没写。',
          style: TextStyle(fontSize: 13.5, height: 1.6, color: scheme.outline),
        ),
        const SizedBox(height: 22),
        for (final q in _ChatPageState._suggestions)
          Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: OutlinedButton(
              style: OutlinedButton.styleFrom(
                alignment: Alignment.centerLeft,
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
              ),
              onPressed: () => onPick(q),
              child: Row(
                children: [
                  Icon(Icons.help_outline, size: 16, color: scheme.primary),
                  const SizedBox(width: 10),
                  Expanded(child: Text(q, style: const TextStyle(fontSize: 13.5))),
                ],
              ),
            ),
          ),
      ],
    );
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble({required this.message});
  final ChatMessage message;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final isUser = message.isUser;

    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 6, 16, 6),
      child: Column(
        crossAxisAlignment: isUser ? CrossAxisAlignment.end : CrossAxisAlignment.start,
        children: [
          Container(
            constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.82),
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 11),
            decoration: BoxDecoration(
              color: isUser ? scheme.primary : scheme.surface,
              borderRadius: BorderRadius.only(
                topLeft: const Radius.circular(14),
                topRight: const Radius.circular(14),
                bottomLeft: Radius.circular(isUser ? 14 : 4),
                bottomRight: Radius.circular(isUser ? 4 : 14),
              ),
            ),
            child: SelectableText(
              message.text,
              style: TextStyle(
                fontSize: 14,
                height: 1.6,
                color: isUser ? scheme.onPrimary : scheme.onSurface,
              ),
            ),
          ),
          if (message.citations.isNotEmpty) ...[
            const SizedBox(height: 10),
            SizedBox(
              width: double.infinity,
              child: Card(
                elevation: 0,
                color: scheme.surface,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                  side: BorderSide(color: scheme.outlineVariant),
                ),
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        '依据的 ${message.citations.length} 条通知',
                        style: TextStyle(
                          fontSize: 11.5,
                          color: scheme.outline,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const SizedBox(height: 6),
                      for (var i = 0; i < message.citations.length && i < 6; i++)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 4),
                          child: Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                '[${i + 1}] ',
                                style: TextStyle(fontSize: 12, color: scheme.primary),
                              ),
                              Expanded(
                                child: Text(
                                  message.citations[i].title,
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(fontSize: 12, height: 1.4),
                                ),
                              ),
                              if (message.citations[i].keyInfo?.deadline != null)
                                Text(
                                  message.citations[i].keyInfo!.deadline!,
                                  style: TextStyle(fontSize: 11, color: scheme.outline),
                                ),
                            ],
                          ),
                        ),
                    ],
                  ),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}
