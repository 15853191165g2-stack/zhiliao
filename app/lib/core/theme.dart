import 'package:flutter/material.dart';

/// 简洁清爽的蓝白配色，红=紧急（沿用国内习惯，红色代表需要立刻处理）。

const seedColor = Color(0xFF2F6BFF);

const importanceColors = <String, Color>{
  'urgent': Color(0xFFE5484D), // 紧急
  'important': Color(0xFFF5A524), // 重要
  'normal': Color(0xFF8B93A7), // 一般
};

ThemeData buildLightTheme() {
  final scheme = ColorScheme.fromSeed(seedColor: seedColor);
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: const Color(0xFFF6F7FB),
    appBarTheme: const AppBarTheme(
      centerTitle: false,
      backgroundColor: Color(0xFFF6F7FB),
      elevation: 0,
      scrolledUnderElevation: 0.5,
    ),
    navigationBarTheme: const NavigationBarThemeData(height: 62),
    dividerTheme: const DividerThemeData(space: 1, thickness: 0.5),
    inputDecorationTheme: const InputDecorationTheme(isDense: true),
  );
}

ThemeData buildDarkTheme() {
  final scheme = ColorScheme.fromSeed(
    seedColor: seedColor,
    brightness: Brightness.dark,
  );
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: const Color(0xFF11131A),
    appBarTheme: const AppBarTheme(
      centerTitle: false,
      backgroundColor: Color(0xFF11131A),
      elevation: 0,
      scrolledUnderElevation: 0.5,
    ),
    navigationBarTheme: const NavigationBarThemeData(height: 62),
    dividerTheme: const DividerThemeData(space: 1, thickness: 0.5),
    inputDecorationTheme: const InputDecorationTheme(isDense: true),
  );
}

/// 分类对应的图标，信息流左侧标签用。
IconData categoryIcon(String category) {
  switch (category) {
    case '教务':
      return Icons.school_outlined;
    case '考试':
      return Icons.assignment_outlined;
    case '奖助学金':
      return Icons.card_giftcard_outlined;
    case '活动':
      return Icons.celebration_outlined;
    case '讲座':
      return Icons.mic_none_outlined;
    case '就业':
      return Icons.work_outline;
    case '后勤':
      return Icons.home_repair_service_outlined;
    default:
      return Icons.circle_outlined;
  }
}

Color importanceColor(String importance) =>
    importanceColors[importance] ?? importanceColors['normal']!;
