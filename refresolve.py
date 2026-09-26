#!/usr/bin/env python3
"""refresolve.py — 跨行引用解析器（纯 Python 标准库，单文件）

语法（自定）：
  定义   行首（允许前导空白）的 `@name: 内容`，name 为 [A-Za-z_][A-Za-z0-9_]*
  引用   任意位置的 `@name`；`\\@` 表示字面 @
  续行   定义内容以 `\\` 结尾时延伸到下一行，各行拼接为一个空格
  注释   字符串外的 `#` 到行尾，其中的 @ 不算引用
  字符串 `"..."` 或 `'...'`（支持 \\ 转义），其中的 @ 不算引用

规则：
  * 引用可出现在定义之后（正常）或之前（前向引用，合法但标注 forward）
  * 引用未定义的名字            -> 错误 undefined-ref（报引用位置）
  * 引用成环（含自环）          -> 错误 cycle（报环上所有名字及定义行）
  * 同名重复定义                -> 错误 duplicate-def（报两处行号，保留先定义者）
  * 行首 @ 但不合定义语法       -> 错误 def-syntax（报行号）
  * 文件末尾仍有续行符          -> 警告 eof-continuation

用法：
  python3 refresolve.py 文件路径        # 解析文件
  python3 refresolve.py -               # 从 stdin 读
  python3 refresolve.py --demo          # 运行内置示例
  python3 refresolve.py --json ...      # JSON 输出
退出码：有错误（不含警告）为 1，否则为 0。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DEF_RE = re.compile(r"^\s*@([A-Za-z_][A-Za-z0-9_]*)\s*:")


@dataclass
class Occurrence:
    """一次引用出现。owner 为它所属的定义名（顶层引用为 None）。"""
    name: str
    line: int
    col: int
    owner: str | None


@dataclass
class Definition:
    name: str
    line: int
    col: int
    body: str
    refs: list[Occurrence] = field(default_factory=list)


@dataclass
class Error:
    kind: str          # undefined-ref / cycle / duplicate-def / def-syntax / eof-continuation
    line: int
    col: int
    message: str


def mask_line(line: str) -> str:
    """把字符串与注释内容替换为空格（保留列位置），返回掩码后的行。"""
    out = list(line)
    i, n = 0, len(line)
    quote = None
    while i < n:
        ch = line[i]
        if quote:
            out[i] = " "
            if ch == "\\" and i + 1 < n:      # 字符串内转义：连下一字符一起掩掉
                out[i + 1] = " "
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out[i] = " "
        elif ch == "#":
            for j in range(i, n):
                out[j] = " "
            break
        i += 1
    return "".join(out)


def find_refs(segment: str, line_no: int, col_offset: int, owner: str | None) -> list[Occurrence]:
    """在（已掩码的）文本片段中找引用。`\\@` 与 @ 后非标识符的不算。"""
    refs = []
    i = 0
    while i < len(segment):
        if segment[i] == "@" and (i == 0 or segment[i - 1] != "\\"):
            m = IDENT_RE.match(segment, i + 1)
            if m:
                refs.append(Occurrence(m.group(0), line_no, col_offset + i + 1, owner))
                i = m.end()
                continue
        i += 1
    return refs


def parse(lines: list[str]) -> tuple[dict[str, Definition], list[Occurrence], list[Error]]:
    defs: dict[str, Definition] = {}
    top_refs: list[Occurrence] = []
    errors: list[Error] = []
    i, n = 0, len(lines)
    while i < n:
        masked = mask_line(lines[i])
        stripped = masked.strip()
        m = DEF_RE.match(masked)
        if m:
            name = m.group(1)
            def_line, def_col = i + 1, m.start(1) + 1
            # 收集续行：本行冒号之后的内容以 \ 结尾则拼下一行
            parts: list[str] = []
            refs: list[Occurrence] = []
            seg, seg_line, seg_off = masked[m.end():], i + 1, m.end()
            while True:
                body = seg.rstrip()
                cont = body.endswith("\\")
                if cont:
                    body = body[:-1]
                parts.append(body.strip())
                refs.extend(find_refs(seg[:len(seg.rstrip()) - (1 if cont else 0)] if cont else seg,
                                      seg_line, seg_off, name))
                if not cont:
                    break
                i += 1
                if i >= n:
                    errors.append(Error("eof-continuation", seg_line, len(lines[seg_line - 1]),
                                        f"定义 @{name} 在文件末尾仍有续行符 '\\'，续行被截断"))
                    break
                seg, seg_line, seg_off = mask_line(lines[i]), i + 1, 0
            body_text = " ".join(p for p in parts if p)
            if name in defs:
                errors.append(Error("duplicate-def", def_line, def_col,
                                    f"重复定义 @{name}（首次定义在第 {defs[name].line} 行），保留先定义者"))
            else:
                defs[name] = Definition(name, def_line, def_col, body_text, refs)
        elif stripped.startswith("@"):
            errors.append(Error("def-syntax", i + 1, masked.index("@") + 1,
                                "定义语法错误：应为 `@name: 内容`（name 为标识符，冒号不可缺）"))
        else:
            top_refs.extend(find_refs(masked, i + 1, 0, None))
        i += 1
    return defs, top_refs, errors


def find_cycles(defs: dict[str, Definition]) -> list[list[str]]:
    """Tarjan SCC：大小>1 的强连通分量或自环即引用环。"""
    graph = {name: sorted({r.name for r in d.refs if r.name in defs})
             for name, d in defs.items()}
    index, low, on_stack, stack, sccs = {}, {}, set(), [], []
    counter = 0
    for root in graph:
        if root in index:
            continue
        index[root] = low[root] = counter; counter += 1
        stack.append(root); on_stack.add(root)
        work = [(root, iter(graph[root]))]
        while work:
            node, it = work[-1]
            descended = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter; counter += 1
                    stack.append(nxt); on_stack.add(nxt)
                    work.append((nxt, iter(graph[nxt])))
                    descended = True
                    break
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                scc = []
                while True:
                    w = stack.pop(); on_stack.discard(w); scc.append(w)
                    if w == node:
                        break
                if len(scc) > 1 or node in graph[node]:
                    sccs.append(sorted(scc))
    return sccs


def resolve(lines: list[str]) -> dict:
    defs, top_refs, errors = parse(lines)

    for cycle in find_cycles(defs):
        where = ", ".join(f"@{n}(第{defs[n].line}行)" for n in cycle)
        errors.append(Error("cycle", defs[cycle[0]].line, defs[cycle[0]].col,
                            f"引用环: {' -> '.join(cycle)}（{where}）"))

    def ref_info(oc: Occurrence) -> dict:
        target = defs.get(oc.name)
        info = {"name": oc.name, "line": oc.line, "col": oc.col,
                "defined": target is not None,
                "forward": bool(target and target.line > oc.line)}
        if target is None:
            errors.append(Error("undefined-ref", oc.line, oc.col,
                                f"引用了未定义的 @{oc.name}"))
        return info

    result = {
        "definitions": [
            {"name": d.name, "line": d.line, "col": d.col, "body": d.body,
             "refs": [ref_info(r) for r in d.refs]}
            for d in defs.values()
        ],
        "top_level_refs": [ref_info(r) for r in top_refs],
        "cycles": find_cycles(defs),
        "errors": [],  # 下面统一排序后填充
    }
    errors.sort(key=lambda e: (e.line, e.col))
    result["errors"] = [{"kind": e.kind, "line": e.line, "col": e.col, "message": e.message}
                        for e in errors]
    return result


def render_text(result: dict) -> str:
    out = ["== 定义与引用关系 =="]
    if not result["definitions"]:
        out.append("  （无定义）")
    for d in result["definitions"]:
        out.append(f"@{d['name']}  定义于 第{d['line']}行  内容: {d['body']!r}")
        for r in d["refs"]:
            tag = "未定义" if not r["defined"] else ("前向引用" if r["forward"] else "正常")
            out.append(f"    引用 @{r['name']} (第{r['line']}行:{r['col']}列)  [{tag}]")
    if result["top_level_refs"]:
        out.append("== 顶层引用（不在任何定义内） ==")
        for r in result["top_level_refs"]:
            tag = "未定义" if not r["defined"] else ("前向引用" if r["forward"] else "正常")
            out.append(f"  @{r['name']} (第{r['line']}行:{r['col']}列)  [{tag}]")
    out.append("== 错误与警告 ==")
    if not result["errors"]:
        out.append("  （无）")
    for e in result["errors"]:
        level = "警告" if e["kind"] == "eof-continuation" else "错误"
        out.append(f"  [{level}:{e['kind']}] 第{e['line']}行:{e['col']}列  {e['message']}")
    return "\n".join(out)


DEMO = """\
# 这是注释，@fake 不算引用
@a: 甲引用 @b，还有 "@not_a_ref" 在字符串里
@b: 乙引用 @c，并且跨行 \\
    延伸到下一行还引用 @d
@c: 丙回引 @a            # a<->b? 不，a->b->c->a 成环
@d: 丁的内容
顶层前向引用 @later 是合法的
@later: 后来的定义
@dup: 第一个
@dup: 第二个（重复定义）
@broken def            # 行首 @ 但缺冒号 -> 语法错误
这里引用了 @missing    # 未定义引用
字面 \\@literal 不算引用
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="跨行引用解析器")
    ap.add_argument("path", nargs="?", help="输入文件，'-' 表示 stdin")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args(argv)

    if args.demo:
        text = DEMO
    elif args.path == "-" or args.path is None:
        text = sys.stdin.read()
    else:
        with open(args.path, encoding="utf-8") as f:
            text = f.read()

    result = resolve(text.splitlines())
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_text(result))
    return 1 if any(e["kind"] != "eof-continuation" for e in result["errors"]) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
