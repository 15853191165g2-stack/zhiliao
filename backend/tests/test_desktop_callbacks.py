"""桌面壳的异步回调静态守卫（不启动 Tk，纯 AST）。

守的是一条**会让界面静默锁死**的写法：

    except Exception as e:
        self._post(lambda: self._chat_error(e))     # ✗ e 出 except 块即失效

CPython 在 except 块结束时会 `del e`（PEP 3110）。而这个 lambda 要等主线程
40ms 后才执行，那时访问 e 抛：

    NameError: cannot access free variable 'e' where it is not associated with a value

它被 `_ui_pump` 的 `except Exception: traceback.print_exc()` 吞掉 —— 不崩溃、
不报错、界面上什么都不会发生。表现就是**按钮永久禁用、状态文字停在
「正在测试…」**，用户只能关窗口（2026-10-24 实测踩到，就是「测试连接」）。

正确写法（在 except 块内先算成值，再用默认参数绑定）：

    except Exception as e:
        msg = _http_detail(e)
        later(lambda m=msg: finish(f"测试失败：{m}", False))

为什么不写成 `lambda: f"...{msg}"` 也行：msg 是普通局部变量，不会被 del。
但默认参数绑定最不容易被后人改坏，所以统一用它。
"""
from __future__ import annotations

import ast
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[2] / "知了桌面版.py"


def _lambdas_reading_except_var(tree: ast.AST) -> list[tuple[int, int, str]]:
    """返回 (except 行号, lambda 行号, 变量名)。"""
    hits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not node.name:
            continue
        name = node.name
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Lambda):
                continue
            # lambda 自己的形参同名的话是遮蔽，安全（事件回调常见 `lambda e:`）
            if any(a.arg == name for a in sub.args.args):
                continue
            if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(sub)):
                hits.append((node.lineno, sub.lineno, name))
    return hits


def test_desktop_file_exists() -> None:
    assert DESKTOP.exists(), f"找不到桌面壳：{DESKTOP}"


def test_no_lambda_over_except_variable() -> None:
    """except 变量不许被延迟执行的 lambda 引用 —— 否则按钮会静默锁死。"""
    tree = ast.parse(DESKTOP.read_text(encoding="utf-8"))
    bad = _lambdas_reading_except_var(tree)
    detail = "\n".join(
        f"  except@L{el} -> lambda@L{ll} 引用了 `{name}`" for el, ll, name in bad
    )
    assert not bad, (
        "发现「except 变量被 lambda 延迟引用」的写法，运行时会抛 NameError，\n"
        f"而且界面完全看不出来（按钮卡在禁用状态）：\n{detail}\n"
        "改法：在 except 块内先取成字符串，再 later(lambda m=msg: ...)。"
    )


def test_eq_detects_the_known_bad_pattern() -> None:
    """反向验证：把旧写法喂进来必须能被抓出来，否则这条守卫是假绿的。"""
    src = (
        "try:\n"
        "    pass\n"
        "except Exception as e:\n"
        "    self._post(lambda: self._chat_error(e))\n"
    )
    hits = _lambdas_reading_except_var(ast.parse(src))
    assert len(hits) == 1, hits
    assert hits[0][2] == "e"


def test_eq_allows_bound_default_pattern() -> None:
    """修复后的写法不能被误报，否则守卫会逼人绕过。"""
    src = (
        "try:\n"
        "    pass\n"
        "except Exception as e:\n"
        "    msg = str(e)\n"
        "    later(lambda m=msg: finish(m))\n"
    )
    assert _lambdas_reading_except_var(ast.parse(src)) == []


def test_eq_ignores_shadowing_lambda_param() -> None:
    """`lambda e: ...` 里 e 是形参，与 except 变量无关，不该误伤。"""
    src = (
        "try:\n"
        "    pass\n"
        "except Exception as e:\n"
        "    cv.bind('<Configure>', lambda e: cv.itemconfigure(cw, width=e.width))\n"
    )
    assert _lambdas_reading_except_var(ast.parse(src)) == []
