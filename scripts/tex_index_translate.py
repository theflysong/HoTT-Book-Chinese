#!/usr/bin/env python3
# -*- coding: utf-8 -*-

r"""
tex_index_translate.py

用途
- 扫描当前仓库内所有 .tex 文件，提取 \index|\indexfoot|\indexdef 的条目，
  将分层（以 ! 分隔）的英文段落作为键写入 index_terms.json，值初始化为空串。
- 识别 \indexsee 的第一个参数（键）并纳入提取/替换。
 - 识别 \indexsee 的两个参数：
     - 第一个参数按常规替换为 `pinyin@中文`；
     - 第二个参数仅替换为中文显示名（不含 `key@` 前缀）。
- 读取用户在 index_terms.json 中填写的中文翻译，对已翻译的键，
  将对应的索引命令段落从 `英文` 改写为 `pinyin@中文` 并回写 .tex 文件。

特点
- 保留已存在的 index_terms.json 中的翻译，不会覆盖非空值。
- 仅对有翻译的段落进行替换，其他段落保持不变，可分多次翻译/应用。
- 尽量解析带可选参数的命令形式（如 \index[general]{...}），并支持平衡花括号。
- 解析分段内的 encap（形如 `key|textbf`、`key|(defstyle`、页码区间 `|(` / `|)`），
    提取键时忽略 encap，应用替换时原样保留 encap。

混合文本处理
- 若 JSON 中的中文翻译包含中英混合（如："英文a中文1英文b"），
    则生成的 pinyin 仅对中文部分进行转写，英文和数字原样保留，
    例如：pinyin -> "英文aPinYin1英文b"（默认每音节首字母大写且无分隔）。

    维护操作
    - 可用 prune 子命令清理 JSON 中“已存在 display（sort@display）”的键：
        - 默认仅移除“只以 sort@display 形式出现、从未以纯 sort 出现”的键；
        - 若加 --any，则移除所有曾以 sort@display 出现过的键（更激进）。
        - 支持 --dry-run 仅查看将移除哪些键而不写回。
        - 若加 --unused，则移除所有在源码中已不再出现的键（既无纯 sort 也无 sort@display）。

用法（PowerShell）
  # 第一次提取（生成或合并 index_terms.json）
  python scripts/tex_index_translate.py extract

  # 应用翻译，将有值的键替换为 pinyin@中文
  python scripts/tex_index_translate.py apply

可选参数
  --root <path>            指定根目录（默认当前脚本所在仓库根）
  --json <file>            指定 JSON 文件路径（默认 index_terms.json）
  --pinyin-style <style>   pinyin 风格：normal|tone|tone3（默认 normal）
    --pinyin-case <case>     pinyin 大小写：lower|upper|capitalized（默认 capitalized）
    --pinyin-sep <sep>       pinyin 连接符（默认无分隔 ""；例如 "-" 或 " ")
  --backup-suffix <sfx>    备份后缀（默认 .bak）
    --include-key-at         提取时包含“sort@display”形式的段落（默认忽略）

注意
- pinyin 由中文翻译自动生成，默认无声调（Style.NORMAL）、每音节首字母大写且无分隔（即 "PinYin"）。
    如需其他形式，可使用 --pinyin-case 与 --pinyin-sep 调整。
- 若段落本身已有 `sort@display` 形式，程序按 `sort` 作为英文键进行匹配并替换为
  `pinyin@中文`；若无匹配翻译，保留原样。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    # pypinyin is used to generate pinyin from Chinese translations
    from pypinyin import Style, lazy_pinyin
except Exception:  # pragma: no cover - optional import checked at runtime
    Style = None  # type: ignore
    lazy_pinyin = None  # type: ignore


INDEX_CMDS = {"index", "indexfoot", "indexdef", "indexsee"}


def iter_tex_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.tex"):
        # 忽略常见输出/临时目录
        parts = {part.lower() for part in p.parts}
        if any(x in parts for x in {"build", "out", "dist", "_build", ".git", "snapshots"}):
            continue
        yield p


def _find_commands_positions(text: str) -> List[Tuple[str, int, int]]:
    r"""
    返回所有 \index|\indexfoot|\indexdef|\indexsee 命令在文本中的 (cmd, start_index, end_index) 位置，
    其中 [start_index, end_index) 覆盖整个命令，包括可选参数和花括号内容。
    对于 \indexsee，end 会覆盖到第二个必选参数的右花括号之后。
    """
    results: List[Tuple[str, int, int]] = []
    i = 0
    n = len(text)

    def is_word_char(c: str) -> bool:
        return c.isalpha()

    while i < n:
        if text[i] == "\\":
            # 读取命令名
            j = i + 1
            while j < n and is_word_char(text[j]):
                j += 1
            cmd = text[i + 1 : j]
            if cmd in INDEX_CMDS:
                k = j
                # 跳过空白
                while k < n and text[k].isspace():
                    k += 1
                # 可选参数 [..]
                if k < n and text[k] == "[":
                    depth = 1
                    k += 1
                    while k < n and depth > 0:
                        if text[k] == "\\":
                            k += 2  # 跳过转义
                            continue
                        if text[k] == "[":
                            depth += 1
                        elif text[k] == "]":
                            depth -= 1
                        k += 1
                    while k < n and text[k].isspace():
                        k += 1
                # 必选参数 {...}
                if k < n and text[k] == "{":
                    depth = 1
                    k += 1
                    while k < n and depth > 0:
                        ch = text[k]
                        if ch == "\\":
                            # 跳过控制序列或转义的单字符
                            k += 1
                            if k < n:
                                k += 1
                            continue
                        if ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                        k += 1
                    # 对于 \indexsee 解析第二个 {...}
                    if cmd == "indexsee":
                        while k < n and text[k].isspace():
                            k += 1
                        if k < n and text[k] == "{":
                            depth = 1
                            k += 1
                            while k < n and depth > 0:
                                ch = text[k]
                                if ch == "\\":
                                    k += 1
                                    if k < n:
                                        k += 1
                                    continue
                                if ch == "{":
                                    depth += 1
                                elif ch == "}":
                                    depth -= 1
                                k += 1
                    results.append((cmd, i, k))
                    i = k
                    continue
            i = j
        else:
            i += 1
    return results


def _split_hierarchy(arg: str) -> List[str]:
    # 按 ! 分层，但忽略形如 \! 的转义（极少见），这里采用简单切分后再合并。
    parts: List[str] = []
    buf = []
    escaped = False
    for ch in arg:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            buf.append(ch)
            continue
        if ch == "!":
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf).strip())
    return [p for p in parts if p != ""]


def _segment_sort_and_display(segment: str) -> Tuple[str, Optional[str]]:
    # 处理 sort@display 形式；若无 @ 则 display 为 None
    if "@" in segment:
        sort, display = segment.split("@", 1)
        return sort.strip(), display.strip()
    return segment.strip(), None


def _split_encap(segment: str) -> Tuple[str, Optional[str]]:
    """
    拆分 encap（以 '|' 标记的部分），返回 (main, encap)；若无 '|' 则 encap 为 None。
    例如：
      'dependent|(defstyle' -> ('dependent', '(defstyle)')
      'key|textbf'         -> ('key', 'textbf')
    注意：这里不解析 encap 的含义，只作为原样字符串保留，以便应用翻译时复原。
    """
    # 寻找未转义的第一个 '|'
    esc = False
    for i, ch in enumerate(segment):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == "|":
            return segment[:i], segment[i + 1 :]
    return segment, None


def _is_english_key(s: str) -> bool:
    # 将包含 ASCII 字母/数字/空格/连字符/下划线/斜杠/逗号/点 的当作“英文键”
    # 目的是避免把纯中文或明显命令片段加入键集合
    return bool(re.fullmatch(r"[\w\-\s/.,:'()]+", s))


def extract_terms_from_text(text: str, include_key_at: bool = False) -> List[str]:
    keys: List[str] = []
    for _cmd, start, end in _find_commands_positions(text):
        # 获取 {...} 内容
        brace_open = text.find("{", start)
        if brace_open == -1 or brace_open >= end:
            continue
        # 提取第一个参数
        depth = 1
        k = brace_open + 1
        while k < end and depth > 0:
            ch = text[k]
            if ch == "\\":
                k += 1
                if k < end:
                    k += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            k += 1
        arg1_end = k - 1  # '}' 的位置
        arg1 = text[brace_open + 1 : arg1_end]

        def process_arg_content(arg_content: str) -> None:
            for seg in _split_hierarchy(arg_content):
                main, _encap = _split_encap(seg)
                sort, _display = _segment_sort_and_display(main)
                # 默认忽略已有 display（sort@display）的段落，除非显式包含
                if _display is not None and not include_key_at:
                    continue
                if sort and _is_english_key(sort):
                    keys.append(sort)

        process_arg_content(arg1)

        # 若为 indexsee，则尝试读取第二个参数
        if _cmd == "indexsee":
            k2 = arg1_end + 1
            while k2 < end and text[k2].isspace():
                k2 += 1
            if k2 < end and text[k2] == "{":
                depth = 1
                k2 += 1
                start2 = k2
                while k2 < end and depth > 0:
                    ch = text[k2]
                    if ch == "\\":
                        k2 += 1
                        if k2 < end:
                            k2 += 1
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    k2 += 1
                arg2_end = k2 - 1
                arg2 = text[start2:arg2_end]
                process_arg_content(arg2)
    return keys


def scan_usage_in_text(text: str) -> Dict[str, Tuple[bool, bool]]:
    r"""
    扫描文本内每个 sort 键的使用形态：
    返回 { sort: (seen_plain, seen_display) }
    - seen_plain: 以不含 @ 的形式出现过
    - seen_display: 以 sort@display 形式出现过
    识别 \indexsee 的两个参数，并处理分段内的 encap。
    """
    usage: Dict[str, Tuple[bool, bool]] = {}
    for _cmd, start, end in _find_commands_positions(text):
        brace_open = text.find("{", start)
        if brace_open == -1 or brace_open >= end:
            continue
        # 解析第一个参数
        depth = 1
        k = brace_open + 1
        while k < end and depth > 0:
            ch = text[k]
            if ch == "\\":
                k += 1
                if k < end:
                    k += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            k += 1
        arg1_end = k - 1
        arg1 = text[brace_open + 1 : arg1_end]

        def process_arg(arg_content: str) -> None:
            for seg in _split_hierarchy(arg_content):
                main, _encap = _split_encap(seg)
                sort, display = _segment_sort_and_display(main)
                if not sort:
                    continue
                seen_plain, seen_disp = usage.get(sort, (False, False))
                if display is None:
                    seen_plain = True
                else:
                    seen_disp = True
                usage[sort] = (seen_plain, seen_disp)

        process_arg(arg1)

        if _cmd == "indexsee":
            k2 = arg1_end + 1
            while k2 < end and text[k2].isspace():
                k2 += 1
            if k2 < end and text[k2] == "{":
                depth = 1
                k2 += 1
                start2 = k2
                while k2 < end and depth > 0:
                    ch = text[k2]
                    if ch == "\\":
                        k2 += 1
                        if k2 < end:
                            k2 += 1
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    k2 += 1
                arg2_end = k2 - 1
                arg2 = text[start2:arg2_end]
                process_arg(arg2)
    return usage


def merge_usage_maps(maps: Iterable[Dict[str, Tuple[bool, bool]]]) -> Dict[str, Tuple[bool, bool]]:
    merged: Dict[str, Tuple[bool, bool]] = {}
    for m in maps:
        for k, (p, d) in m.items():
            mp, md = merged.get(k, (False, False))
            merged[k] = (mp or p, md or d)
    return merged


def apply_translations_to_text(
    text: str,
    mapping: Dict[str, str],
    pinyin_style: str = "normal",
    pinyin_case: str = "capitalized",
    pinyin_sep: str = "",
) -> Tuple[str, int]:
    if lazy_pinyin is None:
        raise RuntimeError("pypinyin 未安装，请先安装后再执行 apply。")
    style = {
        "normal": Style.NORMAL,
        "tone": Style.TONE,
        "tone3": Style.TONE3,
    }.get(pinyin_style, Style.NORMAL)

    def _apply_case(syllable: str) -> str:
        if pinyin_case == "upper":
            return syllable.upper()
        if pinyin_case == "capitalized":
            return syllable[:1].upper() + syllable[1:]
        return syllable.lower()

    CHINESE_RE = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff\uF900-\uFAFF]+)")

    def to_pinyin(text_mixed: str) -> str:
        # 将中文子串转换为拼音，非中文子串原样保留，实现 “英文a中文1英文b -> 英文aPinYin1英文b”
        parts: List[str] = []
        for token in CHINESE_RE.split(text_mixed):
            if not token:
                continue
            if CHINESE_RE.fullmatch(token):
                py_list = lazy_pinyin(token, style=style)
                py_list = [_apply_case(x) for x in py_list]
                parts.append(pinyin_sep.join(py_list))
            else:
                parts.append(token)
        return "".join(parts)

    pieces: List[str] = []
    last = 0
    changes = 0
    for cmd, start, end in _find_commands_positions(text):
        pieces.append(text[last:start])
        block = text[start:end]
        # 找到第一个 { 和最后一个 }
        brace_open = block.find("{")
        if brace_open == -1 or not block.endswith("}"):
            pieces.append(block)
            last = end
            continue
        if cmd != "indexsee":
            head = block[: brace_open + 1]
            arg = block[brace_open + 1 : -1]

            segs = _split_hierarchy(arg)
            new_segs: List[str] = []
            seg_changed = False
            for seg in segs:
                main, encap = _split_encap(seg)
                sort, display = _segment_sort_and_display(main)
                trans = mapping.get(sort, "").strip()
                if trans:
                    pin = to_pinyin(trans)
                    new_seg = f"{pin}@{trans}"
                    if encap is not None:
                        new_seg = new_seg + "|" + encap
                    new_segs.append(new_seg)
                    seg_changed = True
                else:
                    new_segs.append(seg)
            if seg_changed:
                changes += 1
            new_arg = "!".join(new_segs)
            pieces.append(head + new_arg + "}")
        else:
            # 针对 \indexsee：处理两个参数并保留中间空白
            # 定位第一个参数闭合
            depth = 1
            k = brace_open + 1
            while k < len(block) and depth > 0:
                ch = block[k]
                if ch == "\\":
                    k += 1
                    if k < len(block):
                        k += 1
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                k += 1
            arg1_close = k - 1
            arg1 = block[brace_open + 1 : arg1_close]
            # 定位第二个参数
            k2 = arg1_close + 1
            while k2 < len(block) and block[k2].isspace():
                k2 += 1
            if k2 < len(block) and block[k2] == "{":
                brace2_open = k2
                depth = 1
                k2 += 1
                while k2 < len(block) and depth > 0:
                    ch = block[k2]
                    if ch == "\\":
                        k2 += 1
                        if k2 < len(block):
                            k2 += 1
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    k2 += 1
                arg2_close = k2 - 1
                arg2 = block[brace2_open + 1 : arg2_close]
            else:
                # 没有第二个参数（极少见），按单参数处理回退
                brace2_open = -1
                arg2_close = -1
                arg2 = ""

            def translate_arg(arg_text: str, display_only: bool = False) -> Tuple[str, bool]:
                segs = _split_hierarchy(arg_text)
                new_segs: List[str] = []
                seg_changed_local = False
                for seg in segs:
                    main, encap = _split_encap(seg)
                    sort, display = _segment_sort_and_display(main)
                    trans = mapping.get(sort, "").strip()
                    if trans:
                        if display_only:
                            new_seg = f"{trans}"
                        else:
                            pin = to_pinyin(trans)
                            new_seg = f"{pin}@{trans}"
                        if encap is not None:
                            new_seg = new_seg + "|" + encap
                        new_segs.append(new_seg)
                        seg_changed_local = True
                    else:
                        new_segs.append(seg)
                return "!".join(new_segs), seg_changed_local

            new_arg1, ch1 = translate_arg(arg1, display_only=False)
            if brace2_open != -1:
                # 对第二参数，仅使用显示名（value），不含 key@
                new_arg2, ch2 = translate_arg(arg2, display_only=True)
                if ch1 or ch2:
                    changes += 1
                # 重建 block：开头到第一个 '{' 之前 + '{' + new_arg1 + '}' + 中间 + '{' + new_arg2 + '}' + 结尾
                new_block = (
                    block[: brace_open + 1]
                    + new_arg1
                    + block[arg1_close : brace2_open + 1]
                    + new_arg2
                    + block[arg2_close:]
                )
                pieces.append(new_block)
            else:
                if ch1:
                    changes += 1
                pieces.append(block[: brace_open + 1] + new_arg1 + block[arg1_close:])
        last = end
    pieces.append(text[last:])
    return "".join(pieces), changes


def load_json(path: Path) -> Dict[str, str]:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if isinstance(data, dict):
                    # 仅保留 str->str
                    return {str(k): str(v) for k, v in data.items()}
            except json.JSONDecodeError:
                pass
    return {}


def save_json(path: Path, mapping: Dict[str, str]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def cmd_extract(root: Path, json_path: Path, include_key_at: bool = False) -> None:
    existing = load_json(json_path)
    keys: List[str] = []
    for tex in iter_tex_files(root):
        try:
            txt = tex.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            txt = tex.read_text(encoding="latin-1")
        keys.extend(extract_terms_from_text(txt, include_key_at=include_key_at))
    unique_keys = set(keys)
    # 合并：已有翻译不覆盖，缺失键补为空串
    merged = dict(existing)
    for k in sorted(unique_keys):
        if k not in merged:
            merged[k] = ""
    save_json(json_path, merged)
    print(f"提取完成：共发现 {len(unique_keys)} 个键；已合并至 {json_path}")


def cmd_prune(
    root: Path,
    json_path: Path,
    any_display: bool = False,
    remove_unused: bool = False,
    dry_run: bool = False,
) -> None:
    mapping = load_json(json_path)
    if not mapping:
        print(f"未找到或无法解析 {json_path}，无需清理。")
        return
    usage_maps = []
    for tex in iter_tex_files(root):
        try:
            txt = tex.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            txt = tex.read_text(encoding="latin-1")
        usage_maps.append(scan_usage_in_text(txt))
    usage = merge_usage_maps(usage_maps)

    to_remove: List[str] = []
    for k in list(mapping.keys()):
        seen = usage.get(k, (False, False))
        seen_plain, seen_disp = seen
        # 未使用键（既未纯 sort，也未 display）
        if remove_unused and not (seen_plain or seen_disp):
            to_remove.append(k)
            continue
        # display 相关移除逻辑
        if any_display:
            if seen_disp:
                to_remove.append(k)
        else:
            if seen_disp and not seen_plain:
                to_remove.append(k)

    if not to_remove:
        print("没有可移除的键。")
        return

    print(f"将移除 {len(to_remove)} 个键：")
    # 为避免过长，最多打印前 50 个
    preview = to_remove[:50]
    for k in preview:
        print(f"  - {k}")
    if len(to_remove) > len(preview):
        print(f"  ... 其余 {len(to_remove) - len(preview)} 个省略")

    if dry_run:
        print("dry-run 模式：未写回任何更改。")
        return

    for k in to_remove:
        mapping.pop(k, None)
    save_json(json_path, mapping)
    print(f"已从 {json_path} 中移除 {len(to_remove)} 个键。")


def cmd_apply(
    root: Path,
    json_path: Path,
    pinyin_style: str,
    backup_suffix: str,
    pinyin_case: str,
    pinyin_sep: str,
) -> None:
    mapping = load_json(json_path)
    if not mapping:
        print(f"未找到或无法解析 {json_path}，请先执行 extract。")
        sys.exit(1)
    total_changes = 0
    changed_files = 0
    for tex in iter_tex_files(root):
        try:
            original = tex.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            original = tex.read_text(encoding="latin-1")

        new_text, changes = apply_translations_to_text(
            original,
            mapping,
            pinyin_style=pinyin_style,
            pinyin_case=pinyin_case,
            pinyin_sep=pinyin_sep,
        )
        if changes > 0 and new_text != original:
            # 写备份
            backup_path = tex.with_suffix(tex.suffix + backup_suffix)
            backup_path.write_text(original, encoding="utf-8")
            tex.write_text(new_text, encoding="utf-8")
            total_changes += changes
            changed_files += 1
            print(f"已更新 {tex} ：替换 {changes} 处索引条目（备份 -> {backup_path.name}）")
    if changed_files == 0:
        print("没有需要更新的文件（可能尚未填写任何翻译）。")
    else:
        print(f"完成：共 {changed_files} 个文件、{total_changes} 处索引条目已更新。")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="提取与应用 LaTeX 索引翻译")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=str, default=str(Path(__file__).resolve().parents[1]), help="仓库根目录")
    common.add_argument("--json", type=str, default="index_terms.json", help="JSON 文件路径（相对或绝对）")

    p_extract = sub.add_parser("extract", parents=[common], help="提取所有英文键至 JSON（保留已有翻译）")
    p_extract.add_argument("--include-key-at", action="store_true", help="包含 sort@display 段落的英文键（默认忽略）")
    p_apply = sub.add_parser("apply", parents=[common], help="根据 JSON 翻译回写 pinyin@中文")
    p_apply.add_argument("--pinyin-style", type=str, choices=["normal", "tone", "tone3"], default="normal")
    p_apply.add_argument("--backup-suffix", type=str, default=".bak")
    p_apply.add_argument("--pinyin-case", type=str, choices=["lower", "upper", "capitalized"], default="capitalized")
    p_apply.add_argument("--pinyin-sep", type=str, default="")

    p_prune = sub.add_parser("prune", parents=[common], help="从 JSON 中清除已存在 display 的键或移除未使用的键")
    p_prune.add_argument("--any", action="store_true", help="移除所有出现过 sort@display 的键（不论是否也出现纯 sort）")
    p_prune.add_argument("--unused", action="store_true", help="移除在源码中已不再出现的键（既无纯 sort 也无 sort@display）")
    p_prune.add_argument("--dry-run", action="store_true", help="只预览将移除的键，不写回文件")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    json_path = Path(args.json)
    if not json_path.is_absolute():
        json_path = (root / json_path).resolve()

    if args.cmd == "extract":
        cmd_extract(root, json_path, include_key_at=args.include_key_at)
        return 0
    elif args.cmd == "apply":
        if lazy_pinyin is None:
            print("缺少依赖 pypinyin，请先安装：pip install pypinyin")
            return 2
        cmd_apply(
            root,
            json_path,
            pinyin_style=args.pinyin_style,
            backup_suffix=args.backup_suffix,
            pinyin_case=args.pinyin_case,
            pinyin_sep=args.pinyin_sep,
        )
        return 0
    elif args.cmd == "prune":
        cmd_prune(root, json_path, any_display=args.any, remove_unused=args.unused, dry_run=args.dry_run)
        return 0
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
