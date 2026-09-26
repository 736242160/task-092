#!/usr/bin/env python3
"""xref.py — 跨行引用解析器（纯 Python 标准库，单文件）

语法设计（自定）及理由
----------------------
* 定义：``@def 名称 = 内容``，行内定义，内容到逻辑行末尾。
  理由：单行头 + 行尾体，无需块结构/缩进规则，对任意文本行健壮。
* 引用：``@{名称}``，可出现在普通文本或定义体内。
  理由：花括号给出明确边界，避免裸 ``@name`` 与邮箱等文本冲突，
  也允许引用紧贴前后文字（如 ``前缀@{x}后缀``）。
* 跨行拼接：物理行以反斜杠 ``\\`` 结尾时与下一行拼接为一个逻辑行
  （去掉反斜杠、不额外插入空格）。定义内容因此可延伸到下一行。
  理由：显式续行符比"缩进推断"对自由文本更可靠，且行号仍可回溯。
* 注释：``#`` 到行尾（字符串外）。字符串：单/双引号，支持 ``\\`` 转义。
  字符串与注释中的 ``@def`` / ``@{...}`` 一律豁免（不解析）。
* 名称：``[A-Za-z_][A-Za-z0-9_]*``。

判定规则
--------
* 引用在定义之后：正常；在定义之前：合法，标记 forward=True（前向引用）。
* 引用未定义的名称：报 undef-ref，含引用位置（行、列）。
* 环：对定义间引用图求强连通分量（Tarjan），分量大小 >1 或自环即环，
  报告环上全部名称。
* 重复定义：报 dup-def，以首次定义为准（后续定义不改变解析结果）。
* 定义/引用语法错误：报 def-syntax / ref-syntax，含行号。

已知取舍（限制）
----------------
* 续行是物理行级预处理：字符串字面量末尾的 ``\\`` 同样触发拼接。
* 不支持跨行字符串；``@def`` 出现在另一定义体内时按普通文本处理。
* 列号是拼接后逻辑行内的列（1 起始）。

用法
----
    python3 xref.py 文件路径          # 解析文件并打印报告
    python3 xref.py 文件路径 --json   # 机器可读 JSON 输出
    python3 xref.py                   # 运行内置示例
    import xref; xref.analyze(lines)  # 作为库调用，lines 为文本行列表
"""

from __future__ import annotations

import json
import re
import sys

NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------- 预处理

def join_continuations(lines):
    """把以反斜杠结尾的物理行拼接成逻辑行。

    返回 [(逻辑行文本, 起始行号)]，行号为 1 起始的物理行号。
    """
    logical = []
    buf = None
    start = 0
    for lineno, raw in enumerate(lines, 1):
        line = raw.rstrip("\n").rstrip("\r")
        if buf is None:
            buf, start = line, lineno
        else:
            buf += line
        if buf.endswith("\\"):
            buf = buf[:-1]
            continue
        logical.append((buf, start))
        buf = None
    if buf is not None:  # 文件以续行符结尾
        logical.append((buf, start))
    return logical


# ---------------------------------------------------------------- 扫描

def _err(line, col, etype, message):
    return {"type": etype, "line": line, "col": col, "message": message}


def _skip_ws(text, i):
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def _is_boundary(text, i):
    return i >= len(text) or not (text[i].isalnum() or text[i] == "_")


def _parse_reference(text, lineno, at, in_def, references, errors):
    """解析 @{名称}，at 为 '@' 的下标。返回扫描继续的下标。"""
    col = at + 1
    end = text.find("}", at + 2)
    if end == -1:
        errors.append(_err(lineno, col, "ref-syntax", "引用 '@{' 未闭合"))
        return len(text)
    name = text[at + 2:end].strip()
    if not NAME_RE.fullmatch(name):
        errors.append(_err(lineno, col, "ref-syntax", "非法引用名 %r" % name))
    else:
        references.append({"name": name, "in_def": in_def,
                           "line": lineno, "col": col, "forward": False})
    return end + 1


def _scan_body(text, lineno, start, in_def, references, errors):
    """扫描定义体（或普通行片段）中的引用，字符串/注释豁免。"""
    i, n = start, len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        elif ch == "@" and i + 1 < n and text[i + 1] == "{":
            i = _parse_reference(text, lineno, i, in_def, references, errors)
            continue
        i += 1


def _parse_definition(text, lineno, at, definitions, references, errors):
    """解析 @def 名称 = 内容，at 为 '@' 的下标。返回扫描继续的下标。"""
    col = at + 1
    i = _skip_ws(text, at + 4)
    m = NAME_RE.match(text, i)
    if not m:
        errors.append(_err(lineno, col, "def-syntax", "@def 后缺少合法名称"))
        return len(text)
    name = m.group(0)
    i = _skip_ws(text, m.end())
    if i >= len(text) or text[i] != "=":
        errors.append(_err(lineno, col, "def-syntax",
                           "定义 %r 缺少 '='" % name))
        return len(text)
    body_start = i + 1
    if name in definitions:
        errors.append(_err(lineno, col, "dup-def",
                           "名称 %r 重复定义（首次定义在第 %d 行）"
                           % (name, definitions[name]["line"])))
    else:
        definitions[name] = {"name": name, "line": lineno, "col": col,
                             "body": text[body_start:].strip()}
    _scan_body(text, lineno, body_start, name, references, errors)
    return len(text)


def _scan_line(text, lineno, definitions, references, errors):
    i, n = 0, len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        elif ch == "@":
            if text.startswith("def", i + 1) and _is_boundary(text, i + 4):
                i = _parse_definition(text, lineno, i,
                                      definitions, references, errors)
                continue
            if i + 1 < n and text[i + 1] == "{":
                i = _parse_reference(text, lineno, i, None,
                                     references, errors)
                continue
        i += 1


# ---------------------------------------------------------------- 环检测

def find_cycles(graph):
    """Tarjan 强连通分量；返回成环分量的名称列表（每个分量已排序）。"""
    index_of, lowlink, on_stack = {}, {}, set()
    stack, sccs = [], []
    counter = [0]

    def visit(start):
        index_of[start] = lowlink[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        work = [(start, iter(sorted(graph.get(start, ()))))]
        while work:
            node, it = work[-1]
            descended = False
            for succ in it:
                if succ not in index_of:
                    index_of[succ] = lowlink[succ] = counter[0]
                    counter[0] += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, iter(sorted(graph.get(succ, ())))))
                    descended = True
                    break
                if succ in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[succ])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == node:
                        break
                sccs.append(sorted(scc))

    for node in sorted(graph):
        if node not in index_of:
            visit(node)
    return sorted(
        s for s in sccs
        if len(s) > 1 or (len(s) == 1 and s[0] in graph.get(s[0], ()))
    )


# ---------------------------------------------------------------- 主流程

def analyze(lines):
    """解析文本行列表，返回引用关系与错误报告（dict）。"""
    logical = join_continuations(lines)
    definitions, references, errors = {}, [], []
    for text, lineno in logical:
        _scan_line(text, lineno, definitions, references, errors)

    relations = {name: [] for name in definitions}
    for ref in references:
        target = definitions.get(ref["name"])
        if target is None:
            errors.append(_err(ref["line"], ref["col"], "undef-ref",
                               "引用了未定义的名称 %r" % ref["name"]))
        else:
            ref["forward"] = (target["line"], target["col"]) > \
                             (ref["line"], ref["col"])
        if ref["in_def"] in relations:
            relations[ref["in_def"]].append(ref["name"])

    graph = {name: sorted({r for r in refs if r in definitions})
             for name, refs in relations.items()}
    cycles = find_cycles(graph)
    for cyc in cycles:
        errors.append(_err(None, None, "cycle",
                           "引用成环: " + " -> ".join(cyc + [cyc[0]])))

    errors.sort(key=lambda e: (e["line"] is None, e["line"] or 0,
                               e["col"] or 0))
    return {
        "definitions": definitions,
        "references": references,
        "relations": relations,
        "cycles": cycles,
        "errors": errors,
    }


# ---------------------------------------------------------------- 报告

def format_report(result):
    out = ["== 定义 =="]
    if result["definitions"]:
        for name, d in result["definitions"].items():
            out.append("  %s (第 %d 行): %s" % (name, d["line"], d["body"]))
    else:
        out.append("  （无）")
    out.append("== 引用关系 ==")
    if result["relations"]:
        for name, refs in result["relations"].items():
            out.append("  %s -> %s" % (name, ", ".join(refs) or "（无引用）"))
    else:
        out.append("  （无）")
    out.append("== 引用清单 ==")
    if result["references"]:
        for r in result["references"]:
            where = "定义 %s 内" % r["in_def"] if r["in_def"] else "顶层文本"
            tag = " [前向引用]" if r["forward"] else ""
            out.append("  第 %d 行第 %d 列: @{%s}（%s）%s"
                       % (r["line"], r["col"], r["name"], where, tag))
    else:
        out.append("  （无）")
    out.append("== 错误报告 ==")
    if result["errors"]:
        for e in result["errors"]:
            pos = "第 %s 行第 %s 列" % (e["line"], e["col"]) \
                if e["line"] is not None else "（全局）"
            out.append("  [%s] %s: %s" % (e["type"], pos, e["message"]))
    else:
        out.append("  （无错误）")
    return "\n".join(out)


DEMO = """\
@def a = 苹果与@{b}
@def b = 香蕉 \\
和@{c}
@def c = 橙子@{a}
@def d = 前向引用@{e}示例
文本中的@{undefined}引用
@def e = 末尾定义
@def a = 重复定义
@def = 缺少名称
@def g 缺少等号
字符串 "@{not_a_ref}" 与注释里的标记 # @{also_not} @def x = 1
@def h = 自引用@{h}
"""


def main(argv):
    args = [a for a in argv[1:] if a != "--json"]
    as_json = "--json" in argv[1:]
    if args:
        with open(args[0], encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = DEMO.splitlines(keepends=True)
    result = analyze(lines)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
