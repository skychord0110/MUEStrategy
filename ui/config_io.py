"""config.yaml の真偽値だけを、コメントを壊さずに書き換える。

【yaml.safe_dump を使わない理由】
runner/config.yaml と autotrade/config.yaml は、閾値の根拠・実績・安全設計を
コメントで大量に持っている（合計100行以上）。yaml.safe_load → safe_dump で往復すると
コメントも並び順も全部消える。ここは「該当行だけを差し替える」方式にする。

読み取りは yaml.safe_load（構造を正しく解釈するため）、
書き込みは行単位の置換（コメントを保つため）という非対称な作りになっている。
"""
import os
import re

import yaml

_BOOL_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][\w]*)\s*:\s*"
                        r"(?P<val>true|false)\b(?P<rest>.*)$")
_NUM_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][\w]*)\s*:\s*"
                       r"(?P<val>-?\d[\d_]*(?:\.\d+)?)(?P<rest>.*)$")
_KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][\w]*)\s*:\s*(?P<rest>.*)$")


class ConfigEditError(Exception):
    pass


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _read_lines(path: str):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read().splitlines(keepends=True)


def _write_lines(path: str, lines):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)


def _find_key(lines, key: str, indent: int = None, start: int = 0, end: int = None):
    """`key:` の行番号を返す。indent を指定すると字下げが一致する行だけを見る。"""
    end = len(lines) if end is None else end
    for i in range(start, end):
        m = _KEY_LINE.match(lines[i].rstrip("\r\n"))
        if not m or m.group("key") != key:
            continue
        if indent is not None and len(m.group("indent")) != indent:
            continue
        return i
    return None


def _block_end(lines, start: int, indent: int):
    """start行のキーが持つブロック（より深い字下げの範囲）の終端を返す。"""
    for i in range(start + 1, len(lines)):
        line = lines[i].rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cur = len(line) - len(line.lstrip())
        if cur <= indent:
            return i
    return len(lines)


def _replace_value_at(lines, i: int, new_text: str, regex, what: str):
    """値の部分だけを差し替える（行末コメントと字下げはそのまま）。

    改行はCRLF/LFのどちらもあり得るので、元の行の改行をそのまま戻す。
    rstrip("\\n") だけだと CRLF の \\r が rest 側に紛れ込んで行が壊れる。
    """
    raw = lines[i]
    body = raw.rstrip("\r\n")
    nl = raw[len(body):]
    m = regex.match(body)
    if not m:
        raise ConfigEditError(f"{i + 1}行目が{what}の行ではありません: {raw!r}")
    lines[i] = f"{m.group('indent')}{m.group('key')}: {new_text}{m.group('rest')}{nl}"


def _set_bool_at(lines, i: int, value: bool):
    _replace_value_at(lines, i, "true" if value else "false", _BOOL_LINE, " true/false")


def set_nested_enabled(path: str, section: str, value: bool) -> None:
    """`section:` ブロックの中の `enabled:` を書き換える。

    例: strategies > afternoon_reversal > enabled
        section="afternoon_reversal" で足りる（キー名がファイル内で一意なため）。
    """
    lines = _read_lines(path)
    i = _find_key(lines, section)
    if i is None:
        raise ConfigEditError(f"{os.path.basename(path)} に {section}: が見つかりません")
    indent = len(_KEY_LINE.match(lines[i].rstrip("\r\n")).group("indent"))
    j = _find_key(lines, "enabled", start=i + 1, end=_block_end(lines, i, indent))
    if j is None:
        raise ConfigEditError(f"{section}: の中に enabled: が見つかりません")
    _set_bool_at(lines, j, value)
    _write_lines(path, lines)


def set_top_bool(path: str, key: str, value: bool) -> None:
    """字下げなし（トップレベル）の `key: true/false` を書き換える。"""
    lines = _read_lines(path)
    i = _find_key(lines, key, indent=0)
    if i is None:
        raise ConfigEditError(f"{os.path.basename(path)} にトップレベルの {key}: がありません")
    _set_bool_at(lines, i, value)
    _write_lines(path, lines)


def set_child_bool(path: str, parent: str, key: str, value: bool) -> None:
    """`parent:` ブロック直下の `key: true/false` を書き換える。

    autotrade/config.yaml の strategies > afternoon_reversal のように、
    値が真偽値そのもの（enabled: を挟まない）場合に使う。
    """
    lines = _read_lines(path)
    p = _find_key(lines, parent, indent=0)
    if p is None:
        raise ConfigEditError(f"{os.path.basename(path)} に {parent}: がありません")
    i = _find_key(lines, key, start=p + 1, end=_block_end(lines, p, 0))
    if i is None:
        raise ConfigEditError(f"{parent}: の中に {key}: がありません")
    _set_bool_at(lines, i, value)
    _write_lines(path, lines)


def set_child_number(path: str, parent: str, key: str, value) -> None:
    """`parent:` ブロック直下の数値を書き換える（例: capital > max_use_amount）。

    intは桁区切りなしの整数として書く（YAMLの `1_000` 表記は使わない）。
    """
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(int(value)) if isinstance(value, int) else repr(float(value))
    lines = _read_lines(path)
    p = _find_key(lines, parent, indent=0)
    if p is None:
        raise ConfigEditError(f"{os.path.basename(path)} に {parent}: がありません")
    i = _find_key(lines, key, start=p + 1, end=_block_end(lines, p, 0))
    if i is None:
        raise ConfigEditError(f"{parent}: の中に {key}: がありません")
    _replace_value_at(lines, i, text, _NUM_LINE, "数値")
    _write_lines(path, lines)


def digest(paths) -> str:
    """複数ファイルの内容のハッシュ。「起動時から変わったか」の判定に使う。"""
    import hashlib
    h = hashlib.sha256()
    for p in paths:
        h.update(p.encode("utf-8"))
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()


def file_digest(path: str) -> str:
    return digest([path])
