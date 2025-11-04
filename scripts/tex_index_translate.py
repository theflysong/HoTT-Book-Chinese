#!/usr/bin/env python3
# -*- coding: utf-8 -*-

r"""
tex_index_translate.py

用途
- 扫描当前仓库内所有 .tex 文件，提取 \index|\indexfoot|\indexdef 的条目，
  将分层（以 ! 分隔）的英文段落作为键写入 index_terms.json，值初始化为空串。
- 读取用户在 index_terms.json 中填写的中文翻译，对已翻译的键，
  将对应的索引命令段落从 `英文` 改写为 `pinyin@中文` 并回写 .tex 文件。

特点
- 保留已存在的 index_terms.json 中的翻译，不会覆盖非空值。
- 仅对有翻译的段落进行替换，其他段落保持不变，可分多次翻译/应用。
- 尽量解析带可选参数的命令形式（如 \index[general]{...}），并支持平衡花括号。

混合文本处理
- 若 JSON 中的中文翻译包含中英混合（如："英文a中文1英文b"），
    则生成的 pinyin 仅对中文部分进行转写，英文和数字原样保留，
    例如：pinyin -> "英文aPinYin1英文b"（默认每音节首字母大写且无分隔）。

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


INDEX_CMDS = {"index", "indexfoot", "indexdef"}


def iter_tex_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.tex"):
        # 忽略常见输出/临时目录
        parts = {part.lower() for part in p.parts}
        if any(x in parts for x in {"build", "out", "dist", "_build", ".git", "snapshots"}):
            continue
        yield p


def _find_commands_positions(text: str) -> List[Tuple[str, int, int]]:
    """
    返回所有 \index|\indexfoot|\indexdef 命令在文本中的 (cmd, start_index, end_index) 位置，
    其中 [start_index, end_index) 覆盖整个命令，包括可选参数和花括号内容。
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


def _is_english_key(s: str) -> bool:
    # 将包含 ASCII 字母/数字/空格/连字符/下划线/斜杠/逗号/点 的当作“英文键”
    # 目的是避免把纯中文或明显命令片段加入键集合
    return bool(re.fullmatch(r"[\w\-\s/.,:'()]+", s))


def extract_terms_from_text(text: str) -> List[str]:
    keys: List[str] = []
    for _cmd, start, end in _find_commands_positions(text):
        # 获取 {...} 内容
        brace_open = text.find("{", start)
        if brace_open == -1 or brace_open >= end:
            continue
        arg = text[brace_open + 1 : end - 1]
        for seg in _split_hierarchy(arg):
            sort, _display = _segment_sort_and_display(seg)
            if sort and _is_english_key(sort):
                keys.append(sort)
    return keys


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
        head = block[: brace_open + 1]
        arg = block[brace_open + 1 : -1]

        segs = _split_hierarchy(arg)
        new_segs: List[str] = []
        seg_changed = False
        for seg in segs:
            sort, display = _segment_sort_and_display(seg)
            trans = mapping.get(sort, "").strip()
            if trans:
                pin = to_pinyin(trans)
                new_segs.append(f"{pin}@{trans}")
                seg_changed = True
            else:
                new_segs.append(seg)
        if seg_changed:
            changes += 1
        new_arg = "!".join(new_segs)
        pieces.append(head + new_arg + "}")
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


def cmd_extract(root: Path, json_path: Path) -> None:
    existing = load_json(json_path)
    keys: List[str] = []
    for tex in iter_tex_files(root):
        try:
            txt = tex.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            txt = tex.read_text(encoding="latin-1")
        keys.extend(extract_terms_from_text(txt))
    unique_keys = set(keys)
    # 合并：已有翻译不覆盖，缺失键补为空串
    merged = dict(existing)
    for k in sorted(unique_keys):
        if k not in merged:
            merged[k] = ""
    save_json(json_path, merged)
    print(f"提取完成：共发现 {len(unique_keys)} 个键；已合并至 {json_path}")


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
    p_apply = sub.add_parser("apply", parents=[common], help="根据 JSON 翻译回写 pinyin@中文")
    p_apply.add_argument("--pinyin-style", type=str, choices=["normal", "tone", "tone3"], default="normal")
    p_apply.add_argument("--backup-suffix", type=str, default=".bak")
    p_apply.add_argument("--pinyin-case", type=str, choices=["lower", "upper", "capitalized"], default="capitalized")
    p_apply.add_argument("--pinyin-sep", type=str, default="")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    json_path = Path(args.json)
    if not json_path.is_absolute():
        json_path = (root / json_path).resolve()

    if args.cmd == "extract":
        cmd_extract(root, json_path)
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
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
