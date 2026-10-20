import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

/// 后端返回的统一响应包：{code, message, data}
class ApiException implements Exception {
  ApiException(this.message, {this.statusCode});
  final String message;
  final int? statusCode;

  @override
  String toString() => message;
}

class ApiClient {
  ApiClient(this.baseUrl, {http.Client? client}) : _client = client ?? http.Client();

  String baseUrl;
  final http.Client _client;
  static const _timeout = Duration(seconds: 60);

  Uri _uri(String path, Map<String, dynamic>? query) {
    final base = Uri.parse(baseUrl.endsWith('/') ? baseUrl : '$baseUrl/');
    final uri = base.resolve(path.startsWith('/') ? path.substring(1) : path);
    if (query == null || query.isEmpty) return uri;
    final params = <String, String>{};
    query.forEach((k, v) {
      if (v == null) return;
      if (v is bool) {
        params[k] = v ? 'true' : 'false'; // FastAPI 的 bool 查询参数只认这两个词
      } else if (v is String && v.isEmpty) {
        return;
      } else {
        params[k] = '$v';
      }
    });
    return uri.replace(queryParameters: {...uri.queryParameters, ...params});
  }

  Future<dynamic> get(String path, {Map<String, dynamic>? query}) async {
    final uri = _uri(path, query);
    try {
      final resp = await _client.get(uri).timeout(_timeout);
      return _decode(resp);
    } on TimeoutException {
      throw ApiException('请求超时（$uri）');
    } on ApiException {
      rethrow;
    } catch (e) {
      throw ApiException('无法连接后端：$e');
    }
  }

  Future<dynamic> post(String path, {Map<String, dynamic>? body, Map<String, dynamic>? query}) async {
    final uri = _uri(path, query);
    try {
      final resp = await _client
          .post(
            uri,
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body ?? const {}),
          )
          .timeout(_timeout);
      return _decode(resp);
    } on TimeoutException {
      throw ApiException('请求超时（$uri）');
    } on ApiException {
      rethrow;
    } catch (e) {
      throw ApiException('无法连接后端：$e');
    }
  }

  Future<dynamic> patch(String path, {Map<String, dynamic>? body}) async {
    final uri = _uri(path, null);
    try {
      final resp = await _client
          .patch(
            uri,
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode(body ?? const {}),
          )
          .timeout(_timeout);
      return _decode(resp);
    } on ApiException {
      rethrow;
    } catch (e) {
      throw ApiException('请求失败：$e');
    }
  }

  Future<dynamic> delete(String path, {Map<String, dynamic>? query}) async {
    final uri = _uri(path, query);
    try {
      final resp = await _client.delete(uri).timeout(_timeout);
      return _decode(resp);
    } catch (e) {
      throw ApiException('请求失败：$e');
    }
  }

  dynamic _decode(http.Response resp) {
    if (resp.statusCode >= 400) {
      throw ApiException(
        'HTTP ${resp.statusCode}：${_snippet(resp.body)}',
        statusCode: resp.statusCode,
      );
    }
    final text = utf8.decode(resp.bodyBytes);
    if (text.isEmpty) return null;
    final decoded = jsonDecode(text);
    if (decoded is Map<String, dynamic> && decoded.containsKey('code')) {
      final code = decoded['code'];
      if (code != null && code != 0) {
        throw ApiException('${decoded['message'] ?? '后端返回错误'}');
      }
      return decoded['data'];
    }
    return decoded;
  }

  static String _snippet(String body) {
    final t = body.replaceAll('\n', ' ').trim();
    return t.length > 160 ? '${t.substring(0, 160)}…' : t;
  }

  void close() => _client.close();
}
