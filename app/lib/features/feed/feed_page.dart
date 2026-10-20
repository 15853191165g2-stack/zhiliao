import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/app_state.dart';
import '../../widgets/common.dart';
import '../../widgets/notification_card.dart';
import '../detail/detail_page.dart';

/// 首页信息流：所有来源的通知按时间倒序，支持筛选、搜索、收藏、已读。
class FeedPage extends StatefulWidget {
  const FeedPage({super.key});

  @override
  State<FeedPage> createState() => _FeedPageState();
}

class _FeedPageState extends State<FeedPage> {
  final _controller = ScrollController();

  @override
  void initState() {
    super.initState();
    _controller.addListener(_onScroll);
  }

  @override
  void dispose() {
    _controller.removeListener(_onScroll);
    _controller.dispose();
    super.dispose();
  }

  void _onScroll() {
    if (_controller.position.pixels >= _controller.position.maxScrollExtent - 320) {
      context.read<AppState>().loadMore();
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();

    return Column(
      children: [
        if (state.offline) const OfflineBanner(),
        _FilterBar(state: state),
        const Divider(height: 1),
        Expanded(
          child: RefreshIndicator(
            onRefresh: () => context.read<AppState>().refreshAll(),
            child: _buildBody(state),
          ),
        ),
      ],
    );
  }

  Widget _buildBody(AppState state) {
    if (state.loading && state.items.isEmpty) {
      return const LoadingView(message: '正在拉取校园通知…');
    }
    if (state.items.isEmpty) {
      return ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        children: [
          SizedBox(height: MediaQuery.of(context).size.height * 0.16),
          EmptyView(
            icon: state.hasFilter ? Icons.filter_alt_off_outlined : Icons.inbox_outlined,
            title: state.hasFilter ? '没有符合条件的通知' : '还没有抓到通知',
            subtitle: state.hasFilter
                ? '换个筛选条件试试，或者清空筛选'
                : '到「来源管理」添加学校站点，然后下拉刷新',
            action: state.hasFilter
                ? FilledButton.tonal(
                    onPressed: () => context.read<AppState>().clearFilters(),
                    child: const Text('清空筛选'),
                  )
                : null,
          ),
        ],
      );
    }

    return ListView.builder(
      controller: _controller,
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.only(top: 6, bottom: 24),
      itemCount: state.items.length + 1,
      itemBuilder: (context, index) {
        if (index == state.items.length) return _buildFooter(state);
        final item = state.items[index];
        return NotificationCard(
          item: item,
          onTap: () => Navigator.of(context).push(
            MaterialPageRoute(builder: (_) => DetailPage(item: item)),
          ),
          onStar: () => context.read<AppState>().toggleStar(item),
        );
      },
    );
  }

  Widget _buildFooter(AppState state) {
    if (state.loadingMore) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 20),
        child: Center(child: CircularProgressIndicator(strokeWidth: 2.4)),
      );
    }
    if (!state.hasMore) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 24),
        child: Center(
          child: Text(
            '— 已经到底了（共 ${state.total} 条）—',
            style: TextStyle(
              fontSize: 12,
              color: Theme.of(context).colorScheme.outline,
            ),
          ),
        ),
      );
    }
    return const SizedBox(height: 40);
  }
}

class _FilterBar extends StatelessWidget {
  const _FilterBar({required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final s = context.read<AppState>();

    return Container(
      color: Theme.of(context).scaffoldBackgroundColor,
      padding: const EdgeInsets.only(top: 8, bottom: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // 分类
          SizedBox(
            height: 34,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 12),
              children: [
                _Chip(
                  label: '全部',
                  selected: state.filterCategory.isEmpty,
                  onTap: () => s.setCategory(null),
                ),
                for (final c in kCategories)
                  _Chip(
                    label: c,
                    selected: state.filterCategory == c,
                    onTap: () => s.setCategory(state.filterCategory == c ? null : c),
                  ),
              ],
            ),
          ),
          const SizedBox(height: 6),
          // 重要程度 + 未读 + 收藏
          SizedBox(
            height: 32,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 12),
              children: [
                for (final opt in kImportanceOptions)
                  _Chip(
                    label: opt['label']!,
                    dense: true,
                    selected: state.filterImportance == opt['value'],
                    onTap: () => s.setImportance(
                      state.filterImportance == opt['value'] ? null : opt['value'],
                    ),
                  ),
                const SizedBox(width: 8),
                _Chip(
                  label: '未读',
                  dense: true,
                  icon: Icons.mark_email_unread_outlined,
                  selected: state.unreadOnly,
                  onTap: () => s.setUnreadOnly(!state.unreadOnly),
                ),
                _Chip(
                  label: '收藏',
                  dense: true,
                  icon: Icons.star_border,
                  selected: state.starredOnly,
                  onTap: () => s.setStarredOnly(!state.starredOnly),
                ),
                if (state.query.isNotEmpty)
                  _Chip(
                    label: '搜索：${state.query}',
                    dense: true,
                    icon: Icons.search,
                    selected: true,
                    onTap: () => s.applySearch(''),
                  ),
              ],
            ),
          ),
          if (state.lastRefreshSummary != null)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 6, 16, 0),
              child: Text(
                state.lastRefreshSummary!,
                style: TextStyle(fontSize: 11.5, color: scheme.outline),
              ),
            ),
        ],
      ),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip({
    required this.label,
    required this.selected,
    required this.onTap,
    this.icon,
    this.dense = false,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;
  final IconData? icon;
  final bool dense;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.only(right: 6),
      child: GestureDetector(
        onTap: onTap,
        child: Container(
          padding: EdgeInsets.symmetric(horizontal: dense ? 10 : 13, vertical: dense ? 5 : 7),
          decoration: BoxDecoration(
            color: selected ? scheme.primary : scheme.surface,
            borderRadius: BorderRadius.circular(20),
            border: Border.all(
              color: selected ? scheme.primary : scheme.outlineVariant,
            ),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (icon != null) ...[
                Icon(
                  icon,
                  size: 13,
                  color: selected ? scheme.onPrimary : scheme.outline,
                ),
                const SizedBox(width: 4),
              ],
              Text(
                label,
                style: TextStyle(
                  fontSize: dense ? 12 : 13,
                  color: selected ? scheme.onPrimary : scheme.onSurfaceVariant,
                  fontWeight: selected ? FontWeight.w600 : FontWeight.normal,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
