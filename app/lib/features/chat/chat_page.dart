import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/app_state.dart';
import '../../data/models.dart';
import '../../data/repository.dart';
import '../../widgets/common.dart';

/// AI 对话。问的是「你自己学校最近的通知」，不是通用知识。
///
/// 每个回答都会附上引用的原始通知，方便你自己核对——不让你盲信模型。
///
/// 顶部有一条「模型：xxx」——因为接了自定义大模型之后，「现在到底跑的是谁」
/// 每次都得能回答。切换入口就在这里，不埋进设置页。
class ChatPage extends StatefulWidget {
  const ChatPage({super.key});

  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> {
  final _input = TextEditingController();
  final _scroll = ScrollController();

  LLMList? _llm;
  String? _llmError;

  static const _suggestions = <String>[
    '这周有哪些截止的事项？',
    '奖学金申请要交什么材料？',
    '最近有什么讲座或活动？',
    '选课什么时候开始？',
  ];

  @override
  void initState() {
    super.initState();
    // 放到首帧之后：initState 里做网络请求会拖慢第一帧，
    // 而且后端没起来时抛出的异常没人接。
    WidgetsBinding.instance.addPostFrameCallback((_) => _loadLLM());
  }

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _loadLLM() async {
    try {
      final data = await context.read<AppState>().repo.llmProfiles();
      if (!mounted) return;
      setState(() {
        _llm = data;
        _llmError = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _llmError = '$e');
    }
  }

  Future<void> _openModelSheet() async {
    final repo = context.read<AppState>().repo;
    final changed = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => _ModelSheet(repo: repo),
    );
    if (changed == true) await _loadLLM();
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

    final modelText = _llm == null
        ? (_llmError != null ? '模型：读取失败，点右侧重试' : '模型：读取中…')
        : '模型：${_llm!.effectiveModel.isEmpty ? '未配置' : _llm!.effectiveModel}';

    return Column(
      children: [
        InkWell(
          onTap: _llmError != null ? _loadLLM : _openModelSheet,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 6, 8, 6),
            child: Row(
              children: [
                Icon(Icons.memory, size: 15, color: scheme.outline),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    modelText,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: 12, color: scheme.outline),
                  ),
                ),
                Text(
                  _llmError != null ? '重试' : '切换模型',
                  style: TextStyle(fontSize: 12, color: scheme.primary),
                ),
                Icon(Icons.chevron_right, size: 16, color: scheme.primary),
              ],
            ),
          ),
        ),
        const Divider(height: 1),
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

// --------------------------------------------------------------------------- //
// 模型选择
// --------------------------------------------------------------------------- //

/// 内置默认 + 所有自定义模型。选中即生效（服务端是全局单例，不是按对话区分）。
class _ModelSheet extends StatefulWidget {
  const _ModelSheet({required this.repo});

  final Repository repo;

  @override
  State<_ModelSheet> createState() => _ModelSheetState();
}

class _ModelSheetState extends State<_ModelSheet> {
  LLMList? _data;
  String? _error;
  bool _busy = false;
  bool _changed = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final data = await widget.repo.llmProfiles();
      if (!mounted) return;
      setState(() {
        _data = data;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = '$e');
    }
  }

  Future<void> _run(Future<void> Function() action, String failPrefix) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await action();
      _changed = true;
      await _load();
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$failPrefix：$e')));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _add() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (_) => _ProfileEditor(repo: widget.repo),
    );
    if (ok == true) {
      _changed = true;
      await _load();
    }
  }

  Future<void> _confirmDelete(LLMProfile p) async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('删除模型配置'),
        content: Text('确定删除「${p.name}」？删掉之后这个 Key 也要重新填。'),
        actions: [
          TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('取消')),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('删除', style: TextStyle(color: Colors.red)),
          ),
        ],
      ),
    );
    if (yes == true) {
      await _run(() => widget.repo.deleteLLMProfile(p.id), '删除失败');
    }
  }

  String _subtitleOf(LLMProfile p) {
    final parts = <String>[];
    if (p.endpoint.isNotEmpty) parts.add(p.endpoint);
    if (p.lastTestOk == true) {
      parts.add('上次测试通过');
    } else if (p.lastTestOk == false) {
      parts.add(p.lastTestError.isEmpty ? '上次测试失败' : '上次失败：${p.lastTestError}');
    }
    if (parts.isEmpty) parts.add('还没测过');
    return parts.join('\n');
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final data = _data;
    final maxH = MediaQuery.of(context).size.height * 0.72;

    return SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: maxH),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 8, 4),
              child: Row(
                children: [
                  Expanded(
                    child: Text('模型', style: Theme.of(context).textTheme.titleMedium),
                  ),
                  TextButton(
                    onPressed: () => Navigator.of(context).pop(_changed),
                    child: const Text('完成'),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
              child: Text(
                '默认用智谱 GLM-4.7-Flash。也可以接任何 OpenAI 兼容协议的服务。'
                'API Key 只存在你这台机器的后端库里。',
                style: TextStyle(fontSize: 12, height: 1.5, color: scheme.outline),
              ),
            ),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
                child: Text('读取失败：$_error', style: const TextStyle(fontSize: 12, color: Colors.red)),
              )
            else if (data == null)
              const Padding(
                padding: EdgeInsets.all(24),
                child: SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2)),
              )
            else
              Flexible(
                child: ListView(
                  shrinkWrap: true,
                  children: [
                    ListTile(
                      leading: Icon(
                        data.usingEnv ? Icons.check_circle : Icons.radio_button_unchecked,
                        color: data.usingEnv ? scheme.primary : scheme.outline,
                      ),
                      title: Text(
                        data.envModel.isEmpty ? data.envName : '${data.envName} · ${data.envModel}',
                        style: const TextStyle(fontSize: 14),
                      ),
                      subtitle: Text(
                        data.envEnabled ? '内置默认配置（.env）' : '未配置 Key，只能本地兜底',
                        style: const TextStyle(fontSize: 11.5),
                      ),
                      trailing: data.usingEnv ? Text('在用', style: TextStyle(fontSize: 12, color: scheme.primary)) : null,
                      onTap: data.usingEnv ? null : () => _run(widget.repo.deactivateLLM, '切换失败'),
                    ),
                    const Divider(height: 1),
                    for (final p in data.items)
                      ListTile(
                        leading: Icon(
                          p.isActive ? Icons.check_circle : Icons.radio_button_unchecked,
                          color: p.isActive ? scheme.primary : scheme.outline,
                        ),
                        title: Text(
                          p.model.isEmpty ? p.name : '${p.name} · ${p.model}',
                          style: const TextStyle(fontSize: 14),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                        subtitle: Text(_subtitleOf(p), style: const TextStyle(fontSize: 11.5)),
                        isThreeLine: _subtitleOf(p).contains('\n'),
                        trailing: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            if (p.isActive)
                              Text('在用', style: TextStyle(fontSize: 12, color: scheme.primary))
                            else
                              IconButton(
                                tooltip: '启用',
                                icon: const Icon(Icons.power_settings_new, size: 18),
                                onPressed: _busy
                                    ? null
                                    : () => _run(() => widget.repo.activateLLMProfile(p.id), '启用失败'),
                              ),
                            IconButton(
                              tooltip: '删除',
                              icon: const Icon(Icons.delete_outline, size: 18),
                              onPressed: _busy ? null : () => _confirmDelete(p),
                            ),
                          ],
                        ),
                        onTap: p.isActive
                            ? null
                            : () => _run(() => widget.repo.activateLLMProfile(p.id), '启用失败'),
                      ),
                  ],
                ),
              ),
            const Divider(height: 1),
            ListTile(
              leading: Icon(Icons.add, color: scheme.primary),
              title: Text('添加自定义模型', style: TextStyle(fontSize: 14, color: scheme.primary)),
              onTap: _busy ? null : _add,
            ),
          ],
        ),
      ),
    );
  }
}

/// 添加一份自定义模型。先去「测试连接」试通再保存 —— 地址填错只会还你 401/404，
/// 光看报错分不清是哪一项错，所以必须把最终请求地址亮出来。
class _ProfileEditor extends StatefulWidget {
  const _ProfileEditor({required this.repo});

  final Repository repo;

  @override
  State<_ProfileEditor> createState() => _ProfileEditorState();
}

class _ProfileEditorState extends State<_ProfileEditor> {
  final _name = TextEditingController();
  final _model = TextEditingController();
  final _url = TextEditingController();
  final _key = TextEditingController();

  bool _thinking = false;
  bool _jsonMode = true;
  bool _busy = false;
  bool _loadingModels = false;
  LLMTestResult? _result;

  @override
  void dispose() {
    _name.dispose();
    _model.dispose();
    _url.dispose();
    _key.dispose();
    super.dispose();
  }

  /// 问服务商有哪些模型，挑一个填进「模型名」。
  ///
  /// 学校网关一次列 25 个模型（`GLM-4.6-W8A8` 这种带量化后缀的 id），
  /// 手打就是猜；猜错了只会得到 404 model not found，分不清是名字错还是地址错。
  Future<void> _fetchModels() async {
    if (_url.text.trim().isEmpty || _key.text.trim().isEmpty) {
      setState(() => _result = LLMTestResult(detail: '拉取模型列表要先填接入地址和 API Key'));
      return;
    }
    setState(() {
      _loadingModels = true;
      _result = null;
    });
    try {
      final res = await widget.repo.llmModels(
        baseUrl: _url.text.trim(),
        apiKey: _key.text.trim(),
      );
      if (!mounted) return;
      if (res.items.isEmpty) {
        setState(() => _result = LLMTestResult(
              detail: res.detail.isEmpty ? '没拿到模型列表' : res.detail,
              endpoint: res.endpoint,
            ));
      } else {
        await _showModels(res.items);
      }
    } catch (e) {
      if (mounted) {
        setState(() => _result = LLMTestResult(detail: '拉取失败：$e'));
      }
    } finally {
      if (mounted) setState(() => _loadingModels = false);
    }
  }

  Future<void> _showModels(List<String> items) async {
    final picked = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('选择模型（共 ${items.length} 个）'),
        content: SizedBox(
          width: 320,
          height: 360,
          child: ListView.builder(
            shrinkWrap: true,
            itemCount: items.length,
            itemBuilder: (_, i) => ListTile(
              dense: true,
              title: Text(items[i], style: const TextStyle(fontSize: 13)),
              onTap: () => Navigator.of(ctx).pop(items[i]),
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(ctx).pop(), child: const Text('取消')),
        ],
      ),
    );
    if (picked != null && mounted) {
      setState(() {
        _model.text = picked;
        if (_name.text.trim().isEmpty) _name.text = picked;
      });
    }
  }

  Future<void> _test() async {
    if (_model.text.trim().isEmpty || _key.text.trim().isEmpty) {
      setState(() => _result = LLMTestResult(detail: '测试连接至少要填模型名和 API Key'));
      return;
    }
    setState(() {
      _busy = true;
      _result = null;
    });
    try {
      final r = await widget.repo.testLLM(
        baseUrl: _url.text.trim(),
        apiKey: _key.text.trim(),
        model: _model.text.trim(),
        thinking: _thinking,
        jsonMode: _jsonMode,
      );
      if (mounted) setState(() => _result = r);
    } catch (e) {
      if (mounted) setState(() => _result = LLMTestResult(detail: '$e'));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _save() async {
    if (_name.text.trim().isEmpty ||
        _url.text.trim().isEmpty ||
        _model.text.trim().isEmpty ||
        _key.text.trim().isEmpty) {
      setState(() => _result = LLMTestResult(detail: '配置名、接入地址、模型名、API Key 四项都要填'));
      return;
    }
    setState(() => _busy = true);
    try {
      await widget.repo.createLLMProfile(
        name: _name.text.trim(),
        baseUrl: _url.text.trim(),
        model: _model.text.trim(),
        apiKey: _key.text.trim(),
        thinking: _thinking,
        jsonMode: _jsonMode,
      );
      if (mounted) Navigator.of(context).pop(true);
    } catch (e) {
      if (mounted) {
        setState(() {
          _busy = false;
          _result = LLMTestResult(detail: '保存失败：$e');
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final r = _result;

    return AlertDialog(
      title: const Text('添加自定义模型'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              controller: _name,
              decoration: const InputDecoration(labelText: '配置名', hintText: '我的 DeepSeek'),
            ),
            TextField(
              controller: _url,
              keyboardType: TextInputType.url,
              decoration: const InputDecoration(
                labelText: '接入地址',
                hintText: 'https://api.deepseek.com',
                helperText: '只填域名也行，会自动补 /v1/chat/completions',
                helperMaxLines: 2,
              ),
            ),
            Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _model,
                    decoration: const InputDecoration(labelText: '模型名', hintText: 'deepseek-chat'),
                  ),
                ),
                const SizedBox(width: 4),
                TextButton(
                  onPressed: _busy || _loadingModels ? null : _fetchModels,
                  child: Text(_loadingModels ? '拉取中' : '拉取列表'),
                ),
              ],
            ),
            TextField(
              controller: _key,
              obscureText: true,
              decoration: const InputDecoration(labelText: 'API Key', hintText: 'sk-...'),
            ),
            const SizedBox(height: 8),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              dense: true,
              value: _thinking,
              onChanged: (v) => setState(() => _thinking = v),
              title: const Text('关闭思维链', style: TextStyle(fontSize: 13)),
              subtitle: const Text('只有智谱系认这个开关，别家会报 400', style: TextStyle(fontSize: 11)),
            ),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              dense: true,
              value: _jsonMode,
              onChanged: (v) => setState(() => _jsonMode = v),
              title: const Text('要求返回 JSON', style: TextStyle(fontSize: 13)),
              subtitle: const Text('个别模型不支持；不支持会自动去掉重试', style: TextStyle(fontSize: 11)),
            ),
            if (r != null)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  r.ok
                      ? '连接成功（${r.durationMs.toStringAsFixed(0)} ms）\n实际请求：${r.endpoint}'
                      : '${r.detail}${r.endpoint.isEmpty ? '' : '\n实际请求：${r.endpoint}'}',
                  style: TextStyle(fontSize: 12, height: 1.4, color: r.ok ? Colors.green : Colors.red),
                ),
              ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: _busy ? null : _test,
          child: Text('测试连接', style: TextStyle(color: scheme.primary)),
        ),
        TextButton(
          onPressed: _busy ? null : () => Navigator.of(context).pop(false),
          child: const Text('取消'),
        ),
        FilledButton(
          onPressed: _busy ? null : _save,
          child: const Text('保存并启用'),
        ),
      ],
    );
  }
}
