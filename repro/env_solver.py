from __future__ import annotations

import re
from pathlib import Path

from . import RELEASE_WORKFLOW_PATH

SYNC_KEY_RE = re.compile(r"\bSYNC_[A-Z0-9_]+\b")
SENTINEL_PREFIX = "__VM_REPRO_SENTINEL_"
SENTINEL_SUFFIX = "__"

JS_KEYWORDS = frozenset(
    {
        "async",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "function",
        "if",
        "import",
        "in",
        "instanceof",
        "let",
        "new",
        "null",
        "of",
        "return",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "with",
        "yield",
    }
)

JS_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
    | (?P<comment>//[^\r\n]*|/\*[\s\S]*?\*/)
    | (?P<dq>"(?:[^"\\]|\\.)*")
    | (?P<sq>'(?:[^'\\]|\\.)*')
    | (?P<num>\b(?:0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b)
    | (?P<ident>[A-Za-z_$][A-Za-z0-9_$]*)
    | (?P<punct>===|!==|==|!=|<=|>=|=>|\?\?|\?\.|&&|\|\||\.\.\.|[{}()\[\].;,?:+\-*/%&|^!~<>=])
    """,
    re.VERBOSE,
)

IDENT_PATTERN = r"[A-Za-z_$][A-Za-z0-9_$]*"
ANY_STRING_PATTERN = r"""(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
TEXT_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".json", ".html", ".css"})


class EnvSolveError(RuntimeError):
    """Raised when SYNC_* keys cannot be discovered or unambiguously aligned."""


def discover_sync_keys(
    source_dir: Path, workflow_relpath: str = RELEASE_WORKFLOW_PATH
) -> list[str]:
    workflow_path = source_dir / workflow_relpath
    if not workflow_path.is_file():
        raise EnvSolveError(f"missing upstream release workflow: {workflow_relpath}")
    text = workflow_path.read_text(encoding="utf-8")
    return sorted(set(SYNC_KEY_RE.findall(text)))


def make_sentinel_env(keys: list[str]) -> dict[str, str]:
    return {key: f"{SENTINEL_PREFIX}{key}{SENTINEL_SUFFIX}" for key in keys}


def _unescape_js_string(raw: str) -> str:
    out: list[str] = []
    idx = 0
    length = len(raw)
    simple = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
        "v": "\v",
        "0": "\0",
        "\\": "\\",
        '"': '"',
        "'": "'",
        "/": "/",
    }
    while idx < length:
        ch = raw[idx]
        if ch != "\\":
            out.append(ch)
            idx += 1
            continue
        idx += 1
        if idx >= length:
            raise EnvSolveError("unterminated escape in bundle string literal")
        esc = raw[idx]
        idx += 1
        if esc in simple:
            out.append(simple[esc])
        elif esc == "x" and idx + 2 <= length:
            out.append(chr(int(raw[idx : idx + 2], 16)))
            idx += 2
        elif esc == "u":
            if idx < length and raw[idx] == "{":
                end = raw.find("}", idx + 1)
                if end == -1:
                    raise EnvSolveError("invalid unicode escape in bundle string literal")
                out.append(chr(int(raw[idx + 1 : end], 16)))
                idx = end + 1
            elif idx + 4 <= length:
                out.append(chr(int(raw[idx : idx + 4], 16)))
                idx += 4
            else:
                raise EnvSolveError("truncated unicode escape in bundle string literal")
        elif esc in ("\n", "\r"):
            if esc == "\r" and idx < length and raw[idx] == "\n":
                idx += 1
        else:
            out.append(esc)
    return "".join(out)


def _find_enclosing_string(text: str, start: int, end: int) -> tuple[int, int]:
    """Find the exact [quote_start, quote_end) bounds of the JS string containing [start, end)."""
    pos = start - 1
    quote_char: str | None = None
    quote_start = -1
    while pos >= 0:
        ch = text[pos]
        if ch in ('"', "'"):
            backslashes = 0
            scan = pos - 1
            while scan >= 0 and text[scan] == "\\":
                backslashes += 1
                scan -= 1
            if backslashes % 2 == 0:
                quote_char = ch
                quote_start = pos
                break
        if ch in ("\n", "\r"):
            break
        pos -= 1

    if quote_char is None:
        raise EnvSolveError("sentinel was not emitted inside a quoted string literal")

    pos = end
    length = len(text)
    while pos < length:
        ch = text[pos]
        if ch == "\\":
            pos += 2
            continue
        if ch == quote_char:
            return quote_start, pos + 1
        if ch in ("\n", "\r"):
            break
        pos += 1

    raise EnvSolveError("unterminated string literal around sentinel")


MULTI_CHAR_PUNCT = (
    "===",
    "!==",
    "...",
    "==",
    "!=",
    "<=",
    ">=",
    "=>",
    "??",
    "?.",
    "&&",
    "||",
)
SINGLE_CHAR_PUNCT = frozenset("{}()[].;,?:+-*%&|^!~<>=")


def _left_tokens(text: str, str_start: int, max_tokens: int = 40) -> list[tuple[str, str]]:
    """Walk strictly outward to the left from str_start (known to be outside strings)."""
    rev_tokens: list[tuple[str, str]] = []
    pos = str_start

    while pos > 0 and len(rev_tokens) < max_tokens:
        ch = text[pos - 1]
        if ch in " \t\r\n":
            pos -= 1
            continue

        if ch == "/":
            break

        if ch in ('"', "'"):
            quote = ch
            scan = pos - 2
            found_open = -1
            while scan >= 0:
                c = text[scan]
                if c in ("\n", "\r"):
                    break
                if c == quote:
                    bs = 0
                    k = scan - 1
                    while k >= 0 and text[k] == "\\":
                        bs += 1
                        k -= 1
                    if bs % 2 == 0:
                        found_open = scan
                        break
                scan -= 1
            if found_open == -1:
                break
            kind = "dq" if quote == '"' else "sq"
            rev_tokens.append((kind, text[found_open:pos]))
            pos = found_open
            continue

        if ch.isalnum() or ch in "_$":
            scan = pos - 1
            while scan > 0 and (text[scan - 1].isalnum() or text[scan - 1] in "_$"):
                scan -= 1
            word = text[scan:pos]
            kind = "num" if word[0].isdigit() else "ident"
            rev_tokens.append((kind, word))
            pos = scan
            continue

        matched_multi = None
        for op in MULTI_CHAR_PUNCT:
            op_len = len(op)
            if pos >= op_len and text[pos - op_len : pos] == op:
                matched_multi = op
                break
        if matched_multi is not None:
            rev_tokens.append(("punct", matched_multi))
            pos -= len(matched_multi)
            continue

        if ch in SINGLE_CHAR_PUNCT:
            rev_tokens.append(("punct", ch))
            pos -= 1
            continue

        break

    rev_tokens.reverse()
    return rev_tokens


def _right_tokens(text: str, str_end: int, max_tokens: int = 40) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = str_end
    limit = min(len(text), str_end + 600)
    while pos < limit and len(tokens) < max_tokens:
        match = JS_TOKEN_RE.match(text, pos, limit)
        if match is None:
            break
        kind = match.lastgroup
        assert kind is not None
        if kind not in ("ws", "comment"):
            if kind == "punct" and match.group() == "/":
                break
            tokens.append((kind, match.group()))
        pos = match.end()
    return tokens


def _contains_any_sentinel(token_text: str, sentinels: set[str]) -> bool:
    return any(s in token_text for s in sentinels)


def _compile_alignment_regex(
    left: list[tuple[str, str]],
    target_literal: str,
    right: list[tuple[str, str]],
    *,
    sentinel: str,
    all_sentinels: set[str],
) -> re.Pattern[str]:
    inner = target_literal[1:-1]
    parts = inner.split(sentinel)
    if len(parts) != 2:
        raise EnvSolveError("unexpected multiple sentinels inside single string literal")
    prefix_raw, suffix_raw = parts
    target_pattern = (
        rf'(?:"{re.escape(prefix_raw)}(?P<dq>(?:[^"\\]|\\.)*){re.escape(suffix_raw)}"'
        rf"|'{re.escape(prefix_raw)}(?P<sq>(?:[^'\\]|\\.)*){re.escape(suffix_raw)}')"
    )

    seq: list[tuple[str, str]] = [*left, ("target", target_literal), *right]
    pieces: list[str] = []

    for idx, (kind, tok) in enumerate(seq):
        if kind == "target":
            pieces.append(target_pattern)
        elif kind in ("dq", "sq"):
            if _contains_any_sentinel(tok, all_sentinels):
                pieces.append(ANY_STRING_PATTERN)
            else:
                pieces.append(re.escape(tok))
        elif kind == "ident":
            prev_tok = seq[idx - 1][1] if idx > 0 else ""
            next_tok = seq[idx + 1][1] if idx + 1 < len(seq) else ""
            is_fixed = (
                tok in JS_KEYWORDS
                or prev_tok in (".", "?.")
                or (prev_tok in ("{", ",") and next_tok == ":")
            )
            pieces.append(re.escape(tok) if is_fixed else IDENT_PATTERN)
        else:
            pieces.append(re.escape(tok))

    return re.compile(r"\s*".join(pieces))


def _solve_occurrence(
    sentinel_text: str,
    amo_text: str,
    match_start: int,
    match_end: int,
    *,
    sentinel: str,
    key: str,
    all_sentinels: set[str],
) -> str | None:
    str_start, str_end = _find_enclosing_string(sentinel_text, match_start, match_end)
    target_literal = sentinel_text[str_start:str_end]
    full_left = _left_tokens(sentinel_text, str_start)
    full_right = _right_tokens(sentinel_text, str_end)

    last_distinct: set[str] = set()
    for radius in (8, 16, 32):
        left = full_left[-radius:] if full_left else []
        right = full_right[:radius] if full_right else []
        pattern = _compile_alignment_regex(
            left,
            target_literal,
            right,
            sentinel=sentinel,
            all_sentinels=all_sentinels,
        )
        found: set[str] = set()
        for amo_match in pattern.finditer(amo_text):
            raw_val = amo_match.group("dq")
            if raw_val is None:
                raw_val = amo_match.group("sq")
            assert raw_val is not None
            found.add(_unescape_js_string(raw_val))

        if len(found) == 1:
            return next(iter(found))
        last_distinct = found
        if not found:
            break

    if len(last_distinct) > 1:
        raise EnvSolveError(f"ambiguous build environment key in AMO bundle: {key}")
    return None


def solve_sync_env(
    keys: list[str],
    sentinel_dist: Path,
    amo_dist: Path,
) -> dict[str, str]:
    """Recover SYNC_* values by structurally aligning sentinel build outputs with AMO outputs."""
    if not keys:
        return {}

    sentinel_map = make_sentinel_env(keys)
    all_sentinels = set(sentinel_map.values())
    recovered: dict[str, str] = {}

    text_files: list[tuple[Path, str]] = []
    for path in sorted(sentinel_dist.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(s in content for s in all_sentinels):
            text_files.append((path.relative_to(sentinel_dist), content))

    amo_cache: dict[Path, str] = {}

    for key, sentinel in sentinel_map.items():
        key_values: set[str] = set()
        saw_sentinel = False

        for rel_path, sentinel_text in text_files:
            if sentinel not in sentinel_text:
                continue
            saw_sentinel = True
            if rel_path not in amo_cache:
                amo_file = amo_dist / rel_path
                if not amo_file.is_file():
                    raise EnvSolveError(
                        f"sentinel output file missing from AMO artifact: {rel_path.as_posix()}"
                    )
                amo_cache[rel_path] = amo_file.read_text(encoding="utf-8")
            amo_text = amo_cache[rel_path]

            for occ in re.finditer(re.escape(sentinel), sentinel_text):
                val = _solve_occurrence(
                    sentinel_text,
                    amo_text,
                    occ.start(),
                    occ.end(),
                    sentinel=sentinel,
                    key=key,
                    all_sentinels=all_sentinels,
                )
                if val is not None:
                    key_values.add(val)

        if len(key_values) > 1:
            raise EnvSolveError(f"inconsistent values recovered across occurrences for {key}")
        if len(key_values) == 1:
            recovered[key] = next(iter(key_values))
        elif saw_sentinel:
            # Key was dead-code-eliminated in AMO (empty/unset secret in upstream release)
            recovered[key] = ""

    return recovered
