#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阿里云RDS MySQL 8.x 快照备份SQL文件快速恢复到自建数据库。

Fast SQL-only restore script for Alibaba Cloud RDS MySQL 8.x snapshot
backup SQL exports.

Supported backup layouts:

1) Flat single-database layout:

  backup_root/
    structure.sql
    table_a/
      structure.sql
      data/
        *.sql

2) Nested multi-database layout:

  backup_root/
    db_name/
      structure.sql
      table_a/
        structure.sql
        data/
          *.sql

Compatible with Python 3.6+ and uses only the Python standard library.
"""

import argparse
import concurrent.futures
import getpass
import logging
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, TextIO, Tuple, Union

STRUCTURE_FILE_NAME = "structure.sql"
DATA_PATH_PREFIX = "data"
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
DEFAULT_DETECT_DB_READ_LIMIT = 8 * 1024 * 1024
STATEMENT_SCAN_BUFFER_LIMIT = 1024 * 1024
DEFAULT_PROCESS_CLEANUP_TIMEOUT = 5.0
REWRITE_OVERLAP_BYTES = 4096
SOURCE_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
SOURCE_SAFE_CHAR_RE = re.compile(r"[A-Za-z0-9_./-]")

__version__ = "v260606"

LOGGER = logging.getLogger("mysql_sql_fast_restore")
ACTIVE_PROCS_LOCK = threading.Lock()
ACTIVE_PROCS = {}  # type: Dict[int, Tuple[subprocess.Popen, str]]

USE_BACKTICK_RE = re.compile(r"(?is)(?:^|[;\r\n])\s*USE\s+`((?:``|[^`])+)`\s*;")
USE_PLAIN_RE = re.compile(r"(?is)(?:^|[;\r\n])\s*USE\s+([A-Za-z0-9_$.-]+)\s*;")
CREATE_DB_BACKTICK_RE = re.compile(
    r"(?is)(?:^|[;\r\n])\s*CREATE\s+(?:DATABASE|SCHEMA)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?`((?:``|[^`])+)`"
)
CREATE_DB_PLAIN_RE = re.compile(
    r"(?is)(?:^|[;\r\n])\s*CREATE\s+(?:DATABASE|SCHEMA)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_$.-]+)"
)
CREATE_DB_REWRITE_BYTES_RE = re.compile(
    br"(?i)(^|\xef\xbb\xbf|[;\r\n])([ \t\r\n]*)CREATE\s+(DATABASE|SCHEMA)\s+"
    br"(?!IF\s+NOT\s+EXISTS\b)"
    br"(?!/\*![0-9]{5}\s+IF\s+NOT\s+EXISTS\s*\*/)"
)
MYSQL_CONDITIONAL_COMMENT_RE = re.compile(r"/\*![0-9]{5}\s+(.*?)\*/", re.DOTALL)


def setup_logging(log_file: Optional[str], verbose: bool, quiet: bool) -> None:
    console_level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    LOGGER.setLevel(logging.DEBUG)
    LOGGER.handlers = []
    LOGGER.propagate = False

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    LOGGER.addHandler(console_handler)

    if log_file:
        path = Path(log_file).expanduser()
        if str(path.parent) and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(path), mode="a", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)


def die(message: str, code: int = 1) -> None:
    LOGGER.error(message)
    sys.exit(code)


def natural_key(value: str) -> List[Union[int, str]]:
    parts = re.split(r"(\d+)", value)
    key = []  # type: List[Union[int, str]]
    for part in parts:
        key.append(int(part) if part.isdigit() else part.lower())
    return key


def sql_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def normalize_password_for_option_file(value: str, source: str) -> str:
    """Normalize password text before writing a generated MySQL option file.

    A few terminals, clipboard tools, or password-file generation paths may add
    trailing NUL bytes. MySQL client option files are text files and cannot
    represent NUL safely, so strip only trailing NUL bytes. Embedded NUL bytes
    remain a hard error because silently removing them could change the actual
    password.
    """
    if "\x00" in value:
        stripped = value.rstrip("\x00")
        if "\x00" not in stripped:
            count = len(value) - len(stripped)
            LOGGER.warning(
                "%s contained %s trailing NUL byte(s); stripped before creating the MySQL option file. "
                "This is usually caused by terminal, clipboard, or password-file artifacts.",
                source,
                count,
            )
            value = stripped
        else:
            positions = [str(index) for index, ch in enumerate(value) if ch == "\x00"]
            raise RuntimeError(
                "password contains embedded NUL byte(s) at character position(s): "
                + ", ".join(positions[:8])
                + (" ..." if len(positions) > 8 else "")
                + ". Use --login-path, provide a pre-created --defaults-extra-file, or change the password."
            )

    forbidden = []  # type: List[str]
    for ch, label in (("\n", "newline"), ("\r", "carriage return")):
        if ch in value:
            forbidden.append(label)
    if forbidden:
        raise RuntimeError(
            "password contains characters that cannot be represented safely in generated MySQL option files: "
            + ", ".join(forbidden)
            + ". Use --login-path, provide a pre-created --defaults-extra-file, or change the password."
        )
    return value

def mysql_option_file_value(value: str) -> str:
    if "\x00" in value:
        raise RuntimeError("internal error: password still contains NUL after normalization")
    if "\n" in value or "\r" in value:
        raise RuntimeError("password contains newline/carriage return; use --login-path or a pre-created --defaults-extra-file")
    # MySQL option-file values can be quoted. Keep comment characters such as
    # '#' and ';' inside the quoted value instead of rejecting valid passwords.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_size(value: str) -> int:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("size must not be empty")
    match = re.match(r"(?i)^([0-9]+)([kmgt]?b?|)$", text)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid size: {value}; use bytes or suffix K/M/G/T, e.g. 8M")
    number = int(match.group(1))
    suffix = match.group(2).lower()
    multiplier = {
        "": 1, "b": 1,
        "k": 1024, "kb": 1024,
        "m": 1024 ** 2, "mb": 1024 ** 2,
        "g": 1024 ** 3, "gb": 1024 ** 3,
        "t": 1024 ** 4, "tb": 1024 ** 4,
    }[suffix]
    return number * multiplier


def decode_sql_bytes(data: bytes) -> str:
    """Decode a scan buffer for metadata detection only.

    UTF-8 is preferred because dumps commonly contain UTF-8 comments and
    identifiers. latin-1 remains a fallback for legacy dumps. The decoded text
    is never used to rewrite SQL payload bytes.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    utf8_text = data.decode("utf-8", errors="replace")
    # A tiny number of replacement characters can happen when the rolling scan
    # buffer starts in the middle of a multibyte character. Keep UTF-8 in that
    # case so non-ASCII identifiers later in the buffer remain readable.
    if utf8_text.count("\ufffd") <= 4:
        return utf8_text
    return data.decode("latin-1", errors="replace")


def format_bytes(num: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(num)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{num} B"


def safe_temp_prefix(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return (value or "restore")[:80]


def expand_mysql_conditional_comments(text: str) -> str:
    return MYSQL_CONDITIONAL_COMMENT_RE.sub(lambda match: match.group(1), text)


CONSTRAINT_START_WORDS = set([
    "CONSTRAINT", "PRIMARY", "UNIQUE", "KEY", "INDEX", "FULLTEXT", "SPATIAL", "FOREIGN", "CHECK"
])


def is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_" or ch == "$"


def starts_word(text: str, index: int, word: str) -> bool:
    end = index + len(word)
    if text[index:end].upper() != word.upper():
        return False
    if index > 0 and is_ident_char(text[index - 1]):
        return False
    if end < len(text) and is_ident_char(text[end]):
        return False
    return True


def skip_ws_and_comments(text: str, index: int) -> int:
    i = index
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text.startswith("--", i) and (i + 2 >= n or text[i + 2].isspace()):
            end = text.find("\n", i + 2)
            i = n if end < 0 else end + 1
            continue
        if text[i] == "#":
            end = text.find("\n", i + 1)
            i = n if end < 0 else end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        break
    return i


def find_matching_paren(text: str, open_index: int) -> int:
    if open_index < 0 or open_index >= len(text) or text[open_index] != "(":
        raise ValueError("open_index does not point to '('")
    depth = 0
    quote = None  # type: Optional[str]
    line_comment = False
    block_comment = False
    i = open_index
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            if quote in ("'", '"'):
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    if nxt == quote:
                        i += 2
                        continue
                    quote = None
                i += 1
                continue
            if quote == "`":
                if ch == "`":
                    if nxt == "`":
                        i += 2
                        continue
                    quote = None
                i += 1
                continue
        if ch == "-" and nxt == "-" and (i + 2 >= n or text[i + 2].isspace()):
            line_comment = True
            i += 2
            continue
        if ch == "#":
            line_comment = True
            i += 1
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unclosed parenthesis")


def split_top_level_commas(text: str) -> List[str]:
    parts = []  # type: List[str]
    start = 0
    depth = 0
    quote = None  # type: Optional[str]
    line_comment = False
    block_comment = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            if quote in ("'", '"'):
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    if nxt == quote:
                        i += 2
                        continue
                    quote = None
                i += 1
                continue
            if quote == "`":
                if ch == "`":
                    if nxt == "`":
                        i += 2
                        continue
                    quote = None
                i += 1
                continue
        if ch == "-" and nxt == "-" and (i + 2 >= n or text[i + 2].isspace()):
            line_comment = True
            i += 2
            continue
        if ch == "#":
            line_comment = True
            i += 1
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def parse_backtick_identifier(text: str, index: int = 0) -> Tuple[str, int]:
    if index >= len(text) or text[index] != "`":
        raise ValueError("expected backtick identifier")
    chars = []  # type: List[str]
    i = index + 1
    while i < len(text):
        ch = text[i]
        if ch == "`":
            if i + 1 < len(text) and text[i + 1] == "`":
                chars.append("`")
                i += 2
                continue
            return "".join(chars), i + 1
        chars.append(ch)
        i += 1
    raise ValueError("unclosed backtick identifier")


def parse_identifier_token(token: str) -> Optional[str]:
    token = token.strip()
    if not token:
        return None
    # Column lists occasionally use qualified names. Compare the final identifier.
    pieces = []  # type: List[str]
    i = 0
    n = len(token)
    while i < n:
        i = skip_ws_and_comments(token, i)
        if i >= n:
            break
        if token[i] == "`":
            try:
                name, i = parse_backtick_identifier(token, i)
            except ValueError:
                return None
            pieces.append(name)
        else:
            match = re.match(r"[A-Za-z0-9_$]+", token[i:])
            if not match:
                break
            pieces.append(match.group(0))
            i += len(match.group(0))
        i = skip_ws_and_comments(token, i)
        if i < n and token[i] == ".":
            i += 1
            continue
        break
    return pieces[-1] if pieces else None


def parse_column_name_from_definition(definition: str) -> Optional[str]:
    text = definition.lstrip()
    if not text:
        return None
    if text.startswith("`"):
        try:
            name, _ = parse_backtick_identifier(text, 0)
            return name
        except ValueError:
            return None
    match = re.match(r"([A-Za-z0-9_$]+)", text)
    if not match:
        return None
    word = match.group(1)
    if word.upper() in CONSTRAINT_START_WORDS:
        return None
    return word


def sql_code_without_string_literals(text: str) -> str:
    out = []  # type: List[str]
    quote = None  # type: Optional[str]
    line_comment = False
    block_comment = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
                out.append("\n")
            else:
                out.append(" ")
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                out.append("  ")
                i += 2
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue
        if quote:
            if quote in ("'", '"'):
                if ch == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if ch == quote:
                    if nxt == quote:
                        out.append("  ")
                        i += 2
                        continue
                    quote = None
                out.append(" ")
                i += 1
                continue
            if quote == "`":
                if ch == "`":
                    if nxt == "`":
                        out.append("  ")
                        i += 2
                        continue
                    quote = None
                out.append(" ")
                i += 1
                continue
        if ch == "-" and nxt == "-" and (i + 2 >= n or text[i + 2].isspace()):
            line_comment = True
            out.append("  ")
            i += 2
            continue
        if ch == "#":
            line_comment = True
            out.append(" ")
            i += 1
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            out.append("  ")
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def column_definition_is_generated(definition: str) -> bool:
    code = sql_code_without_string_literals(definition).upper()
    # MySQL generated-column syntax always contains AS (expr); GENERATED ALWAYS,
    # VIRTUAL, and STORED are optional. String literals and comments are masked
    # before this check to avoid matching COMMENT 'AS (...)'.
    return re.search(r"\bAS\s*\(", code) is not None


def extract_create_table_body(sql_text: str) -> Optional[str]:
    for match in re.finditer(r"(?is)\bCREATE\s+(?:TEMPORARY\s+)?TABLE\b", sql_text):
        i = match.end()
        while i < len(sql_text):
            if sql_text[i] == "(":
                try:
                    close = find_matching_paren(sql_text, i)
                    return sql_text[i + 1:close]
                except ValueError:
                    return None
            i += 1
    return None


def detect_generated_columns_from_structure(path: Path) -> List[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    text = decode_sql_bytes(data)
    body = extract_create_table_body(text)
    if body is None:
        return []
    generated = []  # type: List[str]
    for part in split_top_level_commas(body):
        name = parse_column_name_from_definition(part)
        if name and column_definition_is_generated(part):
            generated.append(name)
    return generated


def default_like(original: str) -> str:
    leading_len = len(original) - len(original.lstrip())
    trailing_len = len(original) - len(original.rstrip())
    leading = original[:leading_len]
    trailing = original[len(original) - trailing_len:] if trailing_len else ""
    return f"{leading}DEFAULT{trailing}"


def find_keyword_outside(text: str, keyword: str, start: int) -> int:
    target = keyword.upper()
    quote = None  # type: Optional[str]
    line_comment = False
    block_comment = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            if quote in ("'", '"'):
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    if nxt == quote:
                        i += 2
                        continue
                    quote = None
                i += 1
                continue
            if quote == "`":
                if ch == "`":
                    if nxt == "`":
                        i += 2
                        continue
                    quote = None
                i += 1
                continue
        if ch == "-" and nxt == "-" and (i + 2 >= n or text[i + 2].isspace()):
            line_comment = True
            i += 2
            continue
        if ch == "#":
            line_comment = True
            i += 1
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if starts_word(text, i, target):
            return i
        i += 1
    return -1


def rewrite_values_rows_to_default(values_text: str, generated_indices: List[int], expected_count: int) -> Tuple[str, int]:
    out = []  # type: List[str]
    pos = 0
    i = 0
    changed_rows = 0
    n = len(values_text)
    while True:
        i = skip_ws_and_comments(values_text, i)
        if i < n and values_text[i] == ",":
            i += 1
            continue
        i = skip_ws_and_comments(values_text, i)
        if i >= n or values_text[i] == ";":
            break
        row_prefix_start = i
        row_open = i
        if starts_word(values_text, i, "ROW"):
            after_row = skip_ws_and_comments(values_text, i + 3)
            if after_row >= n or values_text[after_row] != "(":
                break
            row_open = after_row
        elif values_text[i] != "(":
            break
        try:
            row_close = find_matching_paren(values_text, row_open)
        except ValueError as exc:
            raise RuntimeError(f"cannot parse INSERT VALUES row for generated-column rewrite: {exc}")
        inner = values_text[row_open + 1:row_close]
        items = split_top_level_commas(inner)
        if len(items) != expected_count:
            raise RuntimeError(
                "cannot rewrite generated-column values because INSERT column count does not match row value count: "
                f"columns={expected_count}, values={len(items)}"
            )
        for idx in generated_indices:
            items[idx] = default_like(items[idx])
        out.append(values_text[pos:row_open + 1])
        out.append(",".join(items))
        out.append(")")
        changed_rows += 1
        i = row_close + 1
        pos = i
    out.append(values_text[pos:])
    return "".join(out), changed_rows


def rewrite_insert_generated_defaults(statement: str, generated_columns: Set[str]) -> Tuple[str, int]:
    if not generated_columns:
        return statement, 0
    i = skip_ws_and_comments(statement, 0)
    if not starts_word(statement, i, "INSERT") and not starts_word(statement, i, "REPLACE"):
        return statement, 0
    values_word = "VALUES"
    values_pos = find_keyword_outside(statement, values_word, i)
    if values_pos < 0:
        values_word = "VALUE"
        values_pos = find_keyword_outside(statement, values_word, i)
    if values_pos < 0:
        return statement, 0
    open_pos = -1
    scan = i
    while scan < values_pos:
        if statement[scan] == "(":
            open_pos = scan
            break
        scan += 1
    if open_pos < 0:
        return statement, 0
    try:
        close_pos = find_matching_paren(statement, open_pos)
    except ValueError as exc:
        raise RuntimeError(f"cannot parse INSERT column list for generated-column rewrite: {exc}")
    if close_pos > values_pos:
        return statement, 0
    columns = []  # type: List[Optional[str]]
    for item in split_top_level_commas(statement[open_pos + 1:close_pos]):
        columns.append(parse_identifier_token(item))
    generated_lower = set([name.lower() for name in generated_columns])
    generated_indices = []  # type: List[int]
    for idx, name in enumerate(columns):
        if name is not None and name.lower() in generated_lower:
            generated_indices.append(idx)
    if not generated_indices:
        return statement, 0
    values_word_end = values_pos + len(values_word)
    rewritten_values, changed_rows = rewrite_values_rows_to_default(statement[values_word_end:], generated_indices, len(columns))
    if changed_rows <= 0:
        return statement, 0
    return statement[:values_word_end] + rewritten_values, changed_rows


def iter_sql_statements_from_file(path: Path, chunk_size: int) -> Iterable[str]:
    buffer = ""
    scan_pos = 0
    quote = None  # type: Optional[str]
    line_comment = False
    block_comment = False
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="surrogateescape")
            i = scan_pos
            while i < len(buffer):
                ch = buffer[i]
                nxt = buffer[i + 1] if i + 1 < len(buffer) else ""
                if line_comment:
                    if ch == "\n":
                        line_comment = False
                    i += 1
                    continue
                if block_comment:
                    if ch == "*" and nxt == "/":
                        block_comment = False
                        i += 2
                    else:
                        i += 1
                    continue
                if quote:
                    if quote in ("'", '"'):
                        if ch == "\\":
                            i += 2
                            continue
                        if ch == quote:
                            if nxt == quote:
                                i += 2
                                continue
                            quote = None
                        i += 1
                        continue
                    if quote == "`":
                        if ch == "`":
                            if nxt == "`":
                                i += 2
                                continue
                            quote = None
                        i += 1
                        continue
                if ch == "-" and nxt == "-" and (i + 2 >= len(buffer) or buffer[i + 2].isspace()):
                    line_comment = True
                    i += 2
                    continue
                if ch == "#":
                    line_comment = True
                    i += 1
                    continue
                if ch == "/" and nxt == "*":
                    block_comment = True
                    i += 2
                    continue
                if ch in ("'", '"', "`"):
                    quote = ch
                    i += 1
                    continue
                if ch == ";":
                    statement = buffer[:i + 1]
                    yield statement
                    buffer = buffer[i + 1:]
                    i = 0
                    scan_pos = 0
                    quote = None
                    line_comment = False
                    block_comment = False
                    continue
                i += 1
            scan_pos = i
    if buffer:
        yield buffer


def write_text_preserve(proc: subprocess.Popen, text: str) -> None:
    write_bytes(proc, text.encode("utf-8", errors="surrogateescape"))


class TablePlan(object):
    def __init__(
        self,
        db_name: Optional[str],
        table_name: str,
        table_dir: Path,
        structure_file: Path,
        data_files: List[Path],
        generated_columns: Optional[List[str]] = None,
    ) -> None:
        self.db_name = db_name
        self.table_name = table_name
        self.table_dir = table_dir
        self.structure_file = structure_file
        self.data_files = data_files
        self.generated_columns = generated_columns or []

    def full_name(self) -> str:
        if self.db_name:
            return f"{self.db_name}.{self.table_name}"
        return self.table_name


class DbPlan(object):
    def __init__(
        self,
        db_name: Optional[str],
        db_dir: Path,
        structure_file: Path,
        tables: List[TablePlan],
        layout: str,
        detected_db_name: Optional[str] = None,
    ) -> None:
        self.db_name = db_name
        self.db_dir = db_dir
        self.structure_file = structure_file
        self.tables = tables
        self.layout = layout
        self.detected_db_name = detected_db_name

    def display_name(self) -> str:
        return self.db_name if self.db_name else "<no selected database>"


class AuthManager(object):
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.temp_files = []  # type: List[Path]
        self.password = None  # type: Optional[str]
        self.generated_option_file = None  # type: Optional[Path]

    def __enter__(self) -> "AuthManager":
        if self.args.dry_run:
            return self
        if self.args.defaults_extra_file:
            defaults_file = Path(self.args.defaults_extra_file).expanduser()
            if not defaults_file.is_file():
                raise RuntimeError(f"--defaults-extra-file does not exist or is not a file: {defaults_file}")
            self.args.defaults_extra_file = str(defaults_file)
            return self
        self.password = resolve_password(self.args)
        if self.args.auth_method == "defaults-extra-file" and self.password is not None:
            self.generated_option_file = create_temp_client_option_file(self.password)
            self.temp_files.append(self.generated_option_file)
            LOGGER.debug("created temporary mysql client option file: %s", self.generated_option_file)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            cleanup_temp_files(self.temp_files)
        except Exception:
            # Do not mask the original restore error with a cleanup failure.
            LOGGER.debug("failed to cleanup authentication temporary files", exc_info=True)
        return False

    def option_args(self) -> List[str]:
        result = []  # type: List[str]
        if self.args.defaults_extra_file:
            result.append(f"--defaults-extra-file={self.args.defaults_extra_file}")
        elif self.generated_option_file is not None:
            result.append(f"--defaults-extra-file={self.generated_option_file}")
        if self.args.login_path:
            result.append(f"--login-path={self.args.login_path}")
        return result

    def child_env(self) -> dict:
        """Return a sanitized environment for mysql child processes.

        MYSQL_PWD is intentionally removed. MySQL 8.0 deprecates MYSQL_PWD,
        and environment variables can be exposed through process inspection on
        some Linux systems. Use --ask-password, --password-file,
        --defaults-extra-file, or --login-path instead.
        """
        env = os.environ.copy()
        env.pop("MYSQL_PWD", None)
        return env


def list_dirs(path: Path) -> List[Path]:
    return sorted([p for p in path.iterdir() if p.is_dir()], key=lambda p: natural_key(p.name))


def list_sql_files(path: Path, ignore_non_sql: bool) -> List[Path]:
    if not path.is_dir():
        return []
    sql_files = []  # type: List[Path]
    non_sql_files = []  # type: List[Path]
    for item in sorted(path.iterdir(), key=lambda p: natural_key(p.name)):
        if not item.is_file() or item.stat().st_size <= 0:
            continue
        if item.name.lower().endswith(".sql"):
            sql_files.append(item)
        else:
            non_sql_files.append(item)
    if non_sql_files and not ignore_non_sql:
        raise RuntimeError(
            "data directory contains non-SQL files; CSV import is intentionally removed. "
            f"Use --ignore-non-sql to skip them. First non-SQL file: {non_sql_files[0]}"
        )
    return sql_files


def scan_db_name_from_text(text: str, last_use: Optional[str], first_create: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    text = expand_mysql_conditional_comments(text)
    for match in USE_BACKTICK_RE.finditer(text):
        last_use = match.group(1).replace("``", "`")
    for match in USE_PLAIN_RE.finditer(text):
        last_use = match.group(1)
    if first_create is None:
        match = CREATE_DB_BACKTICK_RE.search(text)
        if match:
            first_create = match.group(1).replace("``", "`")
    if first_create is None:
        match = CREATE_DB_PLAIN_RE.search(text)
        if match:
            first_create = match.group(1)
    return last_use, first_create


def detect_database_from_structure(path: Path, detect_limit: int) -> Optional[str]:
    """Best-effort streaming parser for USE db; or CREATE DATABASE db.

    The scan prefers UTF-8 and falls back safely for metadata detection only.
    The SQL file itself is never rewritten through decoded text. A detect_limit
    of 0 means scan the whole file.
    """
    last_use = None  # type: Optional[str]
    first_create = None  # type: Optional[str]
    scanned_bytes = 0
    raw_buffer = b""
    try:
        with path.open("rb") as fh:
            while True:
                if detect_limit > 0:
                    remaining = detect_limit - scanned_bytes
                    if remaining <= 0:
                        break
                    chunk = fh.read(min(DEFAULT_CHUNK_SIZE, remaining))
                else:
                    chunk = fh.read(DEFAULT_CHUNK_SIZE)
                if not chunk:
                    break
                scanned_bytes += len(chunk)
                raw_buffer += chunk
                if len(raw_buffer) > STATEMENT_SCAN_BUFFER_LIMIT:
                    raw_buffer = raw_buffer[-STATEMENT_SCAN_BUFFER_LIMIT:]
                text = decode_sql_bytes(raw_buffer)
                last_use, first_create = scan_db_name_from_text(text, last_use, first_create)
                if last_use:
                    break
    except OSError:
        return None
    return last_use or first_create


def build_table_plan(
    db_name: Optional[str],
    table_name: str,
    table_dir: Path,
    ignore_non_sql: bool,
    strict_layout: bool,
) -> Optional[TablePlan]:
    table_structure = table_dir / STRUCTURE_FILE_NAME
    if not table_structure.is_file():
        if strict_layout:
            raise RuntimeError(f"missing table structure file: {table_structure}")
        LOGGER.warning("skip directory without table structure.sql: %s", table_dir)
        return None
    data_files = list_sql_files(table_dir / DATA_PATH_PREFIX, ignore_non_sql)
    generated_columns = detect_generated_columns_from_structure(table_structure)
    if generated_columns:
        LOGGER.info("detected generated column(s) in %s: %s", f"{db_name}.{table_name}" if db_name else table_name, ",".join(generated_columns))
    return TablePlan(db_name, table_name, table_dir, table_structure, data_files, generated_columns)


def build_flat_plan(root_dir: Path, args: argparse.Namespace) -> List[DbPlan]:
    root_structure = root_dir / STRUCTURE_FILE_NAME
    if not root_structure.is_file():
        raise RuntimeError(f"flat layout requires root structure file: {root_structure}")
    detected_db_name = detect_database_from_structure(root_structure, args.detect_limit)
    db_name = args.database or detected_db_name
    if not db_name and not args.allow_no_database:
        raise RuntimeError(
            "flat layout was detected, but target database cannot be determined from root structure.sql. "
            "Add -D/--database DB_NAME, or use --allow-no-database only when every data SQL file contains USE or db-qualified table names."
        )
    tables = []  # type: List[TablePlan]
    for table_dir in list_dirs(root_dir):
        table_plan = build_table_plan(db_name, table_dir.name, table_dir, args.ignore_non_sql, args.strict_layout)
        if table_plan is not None:
            tables.append(table_plan)
    return [DbPlan(db_name, root_dir, root_structure, tables, "flat", detected_db_name=detected_db_name)]


def selected_nested_database(args: argparse.Namespace, db_name: str) -> bool:
    if not args.only_database:
        return True
    return db_name in args.only_database


def build_nested_plan(root_dir: Path, args: argparse.Namespace) -> List[DbPlan]:
    db_plans = []  # type: List[DbPlan]
    for db_dir in list_dirs(root_dir):
        db_name = db_dir.name
        if not selected_nested_database(args, db_name):
            continue
        db_structure = db_dir / STRUCTURE_FILE_NAME
        if not db_structure.is_file():
            if args.strict_layout:
                raise RuntimeError(f"missing database structure file: {db_structure}")
            LOGGER.warning("skip directory without database structure.sql: %s", db_dir)
            continue
        tables = []  # type: List[TablePlan]
        for table_dir in list_dirs(db_dir):
            table_plan = build_table_plan(db_name, table_dir.name, table_dir, args.ignore_non_sql, args.strict_layout)
            if table_plan is not None:
                tables.append(table_plan)
        db_plans.append(DbPlan(db_name, db_dir, db_structure, tables, "nested"))
    return db_plans


def build_plan(root_dir: Path, args: argparse.Namespace) -> List[DbPlan]:
    if not root_dir.is_dir():
        raise RuntimeError(f"backupset_directory does not exist or is not a directory: {root_dir}")
    root_structure = root_dir / STRUCTURE_FILE_NAME
    if args.layout == "flat":
        if args.only_database:
            raise RuntimeError("--only-database is only valid for nested multi-database layout; current layout is flat")
        return build_flat_plan(root_dir, args)
    if args.layout == "nested":
        if args.database:
            raise RuntimeError(
                "-D/--database is only for flat single-database layout. For nested multi-database layout, "
                "do not pass -D; use --only-database DB_NAME if you want to restore only one database."
            )
        return build_nested_plan(root_dir, args)
    if root_structure.is_file():
        if args.only_database:
            raise RuntimeError("--only-database is only valid for nested multi-database layout; current layout is flat")
        return build_flat_plan(root_dir, args)
    if args.database:
        raise RuntimeError(
            "-D/--database is only for flat single-database layout. For nested multi-database layout, "
            "do not pass -D; use --only-database DB_NAME if you want to restore only one database."
        )
    return build_nested_plan(root_dir, args)


def plan_stats(db_plans: Iterable[DbPlan]) -> Tuple[int, int, int, int]:
    db_count = 0
    table_count = 0
    data_file_count = 0
    total_bytes = 0
    for db in db_plans:
        db_count += 1
        table_count += len(db.tables)
        for table in db.tables:
            data_file_count += len(table.data_files)
            for path in table.data_files:
                total_bytes += path.stat().st_size
    return db_count, table_count, data_file_count, total_bytes


def read_password_file(path: str) -> str:
    with Path(path).expanduser().open("r", encoding="utf-8") as fh:
        return fh.readline().rstrip("\r\n")


def password_source_count(args: argparse.Namespace) -> int:
    count = 0
    if args.ask_password:
        count += 1
    if args.password_file:
        count += 1
    if args.db_pass is not None:
        count += 1
    return count


def resolve_password(args: argparse.Namespace) -> Optional[str]:
    if args.defaults_extra_file:
        if password_source_count(args):
            LOGGER.warning("password source ignored because --defaults-extra-file was provided")
        return None
    if args.auth_method == "none":
        if password_source_count(args):
            LOGGER.warning("password source ignored because --auth-method none was selected")
        return None
    if args.ask_password:
        return normalize_password_for_option_file(getpass.getpass("MySQL password: "), "--ask-password input")
    if args.password_file:
        return normalize_password_for_option_file(read_password_file(args.password_file), "--password-file input")
    if args.db_pass == "-":
        raise RuntimeError(
            "db_pass='-' no longer reads MYSQL_PWD. MySQL 8.0 deprecates MYSQL_PWD; "
            "use --ask-password, --password-file, --defaults-extra-file, or --login-path instead."
        )
    if args.db_pass is not None:
        return normalize_password_for_option_file(args.db_pass, "DB_PASS argument")
    if args.login_path:
        return None
    raise RuntimeError(
        "password is required. Provide DB_PASS, use --ask-password, use --password-file, "
        "use --login-path, use --defaults-extra-file, or select --auth-method none."
    )


def create_temp_client_option_file(password: str) -> Path:
    password = normalize_password_for_option_file(password, "password")
    fd, path_str = tempfile.mkstemp(prefix=".mysql_sql_fast_restore_", suffix=".cnf")
    try:
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("[client]\n")
            fh.write(f"password={mysql_option_file_value(password)}\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(path_str)
        except OSError:
            pass
        raise
    return Path(path_str)


def mysql_base_args(args: argparse.Namespace, auth: AuthManager, database: Optional[str] = None, binary_mode: bool = False) -> List[str]:
    cmd = [str(args.mysql)]
    cmd.extend(auth.option_args())
    cmd.extend([
        f"--host={args.db_host}",
        f"--port={args.db_port}",
        f"--user={args.db_user}",
        "--protocol=TCP",
        f"--default-character-set={args.default_character_set}",
        f"--max_allowed_packet={args.max_allowed_packet}",
        "--comments",
    ])
    if database:
        cmd.append(f"--database={database}")
    if args.force:
        cmd.append("--force")
    if binary_mode:
        cmd.append("--binary-mode=1")
    if args.compression_algorithms:
        cmd.append(f"--compression-algorithms={args.compression_algorithms}")
    if getattr(args, "mysql_extra_args", None):
        cmd.extend(args.mysql_extra_args)
    return cmd


def redact_mysql_command(cmd: Sequence[str]) -> str:
    redacted = []  # type: List[str]
    for item in cmd:
        if item.startswith("--defaults-extra-file="):
            redacted.append("--defaults-extra-file=<client-option-file>")
        else:
            redacted.append(item)
    return " ".join(redacted)


def cleanup_temp_files(paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            LOGGER.debug("failed to remove temporary file: %s", path, exc_info=True)


def cleanup_manifests(args: argparse.Namespace, paths: Iterable[Path]) -> None:
    if not args.keep_manifests_dir:
        cleanup_temp_files(paths)


def require_source_safe_path(path: Path) -> None:
    text = str(path)
    if not SOURCE_SAFE_PATH_RE.match(text):
        bad_chars = []  # type: List[str]
        seen = set()  # type: Set[str]
        for ch in text:
            if SOURCE_SAFE_CHAR_RE.fullmatch(ch):
                continue
            if ch not in seen:
                seen.add(ch)
                bad_chars.append(repr(ch))
        detail = ", ".join(bad_chars[:8]) + (" ..." if len(bad_chars) > 8 else "")
        raise RuntimeError(
            f"file path contains characters unsafe for mysql source mode: {path}. "
            f"Unsafe character(s): {detail or '<unknown>'}. "
            "source mode only allows letters, digits, underscore, dot, slash, and hyphen because mysql source has limited path quoting. "
            "Move the backup directory to a simpler ASCII path or run with --input-mode stream."
        )


def common_session_init_sql(args: argparse.Namespace, data_mode: bool) -> str:
    lines = []  # type: List[str]
    # Put sql_log_bin first so all following session statements and import work
    # happen in the intended binlog mode.
    if getattr(args, "disable_binlog", False):
        lines.append("SET SESSION sql_log_bin=0;\n")
    lines.append("SET SESSION foreign_key_checks=0;\n")
    lines.append("SET SESSION unique_checks=0;\n")
    if data_mode and not getattr(args, "no_transaction", False):
        lines.append("SET SESSION autocommit=0;\n")
    return "".join(lines)


def write_common_session_init(fh: TextIO, args: argparse.Namespace, data_mode: bool) -> None:
    fh.write(common_session_init_sql(args, data_mode))


def write_common_session_init_to_proc(proc: subprocess.Popen, args: argparse.Namespace, data_mode: bool) -> None:
    write_sql(proc, common_session_init_sql(args, data_mode))


def make_manifest(prefix: str, keep_dir: Optional[str]) -> Path:
    safe_prefix = safe_temp_prefix(prefix)
    if keep_dir:
        directory = Path(keep_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        fd, path_str = tempfile.mkstemp(prefix=safe_prefix, suffix=".sql", dir=str(directory))
    else:
        fd, path_str = tempfile.mkstemp(prefix=safe_prefix, suffix=".sql")
    os.close(fd)
    return Path(path_str)


def database_structure_needs_rewrite(original_path: Path, detect_limit: int) -> bool:
    scanned_bytes = 0
    buffer = b""
    with original_path.open("rb") as src:
        while True:
            if detect_limit > 0:
                remaining = detect_limit - scanned_bytes
                if remaining <= 0:
                    break
                chunk = src.read(min(DEFAULT_CHUNK_SIZE, remaining))
            else:
                chunk = src.read(DEFAULT_CHUNK_SIZE)
            if not chunk:
                break
            scanned_bytes += len(chunk)
            buffer += chunk
            if len(buffer) > STATEMENT_SCAN_BUFFER_LIMIT:
                buffer = buffer[-STATEMENT_SCAN_BUFFER_LIMIT:]
            if CREATE_DB_REWRITE_BYTES_RE.search(buffer):
                return True
    return False


def rewrite_create_database_bytes(data: bytes) -> Tuple[bytes, int]:
    def replace(match: Any) -> bytes:
        return match.group(1) + match.group(2) + b"CREATE " + match.group(3).upper() + b" IF NOT EXISTS "

    return CREATE_DB_REWRITE_BYTES_RE.subn(replace, data)


def rewrite_database_structure_streaming(args: argparse.Namespace, original_path: Path, rewritten_path: Path) -> int:
    """Rewrite CREATE DATABASE/SCHEMA without decoding or re-encoding the SQL payload."""
    count = 0
    pending = b""
    with original_path.open("rb") as src, rewritten_path.open("wb") as dst:
        while True:
            chunk = src.read(DEFAULT_CHUNK_SIZE)
            if not chunk:
                break
            data = pending + chunk
            if len(data) <= REWRITE_OVERLAP_BYTES:
                pending = data
                continue
            process = data[:-REWRITE_OVERLAP_BYTES]
            pending = data[-REWRITE_OVERLAP_BYTES:]
            new_data, replaced = rewrite_create_database_bytes(process)
            count += replaced
            dst.write(new_data)
        if pending:
            new_data, replaced = rewrite_create_database_bytes(pending)
            count += replaced
            dst.write(new_data)
    return count


def make_database_structure_reusable(args: argparse.Namespace, original_path: Path) -> Path:
    """Return a SQL file where CREATE DATABASE/SCHEMA is changed to IF NOT EXISTS."""
    if not database_structure_needs_rewrite(original_path, args.detect_limit):
        return original_path
    if args.dry_run:
        LOGGER.info("would rewrite CREATE DATABASE/SCHEMA to IF NOT EXISTS: %s", original_path)
        return original_path
    rewritten_path = make_manifest("restore_db_structure_reuse_", args.keep_manifests_dir)
    try:
        replacement_count = rewrite_database_structure_streaming(args, original_path, rewritten_path)
    except Exception:
        cleanup_temp_files([rewritten_path])
        raise
    if replacement_count == 0:
        cleanup_temp_files([rewritten_path])
        return original_path
    LOGGER.info("rewrote CREATE DATABASE/SCHEMA to IF NOT EXISTS: %s", original_path)
    return rewritten_path


def prepare_db_structure_file(args: argparse.Namespace, db: DbPlan, temp_files: List[Path]) -> Path:
    if args.existing_database == "reuse" and db.db_name:
        path = make_database_structure_reusable(args, db.structure_file)
        if path != db.structure_file:
            temp_files.append(path)
        return path
    return db.structure_file


def write_drop_database_if_requested(fh: TextIO, args: argparse.Namespace, db: DbPlan) -> None:
    if args.existing_database != "drop":
        return
    if not db.db_name:
        raise RuntimeError("--existing-database drop requires a known database name; use -D/--database")
    fh.write(f"DROP DATABASE IF EXISTS {sql_ident(db.db_name)};\n")


def stream_drop_database_if_requested(proc: subprocess.Popen, args: argparse.Namespace, db: DbPlan) -> None:
    if args.existing_database != "drop":
        return
    if not db.db_name:
        raise RuntimeError("--existing-database drop requires a known database name; use -D/--database")
    write_sql(proc, f"DROP DATABASE IF EXISTS {sql_ident(db.db_name)};\n")


def terminate_process(proc: subprocess.Popen, description: str, timeout: float) -> None:
    if proc.poll() is not None:
        return
    LOGGER.warning("terminating mysql process after local exception: %s", description)
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        LOGGER.warning("mysql process did not terminate within %.1fs; killing: %s", timeout, description)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        LOGGER.error("mysql process still did not exit after kill: %s", description)


def register_active_process(proc: subprocess.Popen, description: str) -> None:
    with ACTIVE_PROCS_LOCK:
        ACTIVE_PROCS[id(proc)] = (proc, description)


def unregister_active_process(proc: subprocess.Popen) -> None:
    with ACTIVE_PROCS_LOCK:
        ACTIVE_PROCS.pop(id(proc), None)


def terminate_active_processes(reason: str, timeout: float) -> None:
    with ACTIVE_PROCS_LOCK:
        processes = list(ACTIVE_PROCS.values())
    if not processes:
        return
    LOGGER.warning("terminating %s active mysql process(es): %s", len(processes), reason)
    for proc, description in processes:
        close_stdin(proc)
        terminate_process(proc, description, timeout)


def close_stdin(proc: subprocess.Popen) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        LOGGER.debug("mysql stdin was already closed while cleaning up", exc_info=True)


def flush_stdin(proc: subprocess.Popen, description: str) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        rc = proc.poll()
        raise RuntimeError(f"mysql process closed stdin while flushing input for {description}; exit_code={rc}") from exc


def run_mysql_from_manifest(args: argparse.Namespace, auth: AuthManager, manifest_path: Path, description: str, data_mode: bool) -> None:
    cmd = mysql_base_args(args, auth, binary_mode=False)
    LOGGER.info("mysql start: %s: %s < %s; data_mode=%s", description, redact_mysql_command(cmd), manifest_path, data_mode)
    if args.dry_run:
        return
    proc = None  # type: Optional[subprocess.Popen]
    try:
        with manifest_path.open("rb") as stdin:
            proc = subprocess.Popen(cmd, stdin=stdin, env=auth.child_env())
            register_active_process(proc, description)
            try:
                rc = proc.wait()
            finally:
                unregister_active_process(proc)
    except Exception:
        if proc is not None:
            terminate_process(proc, description, args.process_cleanup_timeout)
        raise
    if rc != 0:
        raise RuntimeError(f"mysql failed with exit code {rc}: {description}")


def import_structure_source(args: argparse.Namespace, auth: AuthManager, db_plans: List[DbPlan]) -> None:
    if args.dry_run:
        temp_files = []  # type: List[Path]
        try:
            total_items = sum(1 + len(db.tables) for db in db_plans)
            done = 0
            for db in db_plans:
                if args.existing_database == "drop" and db.db_name:
                    LOGGER.info("dry-run drop database before structure: %s", db.db_name)
                db_structure_file = prepare_db_structure_file(args, db, temp_files)
                require_source_safe_path(db_structure_file)
                done += 1
                LOGGER.info("dry-run source structure %s/%s: root/database %s -> %s", done, total_items, db.display_name(), db_structure_file)
                if db.db_name:
                    LOGGER.debug("dry-run selected database after root structure: %s", db.db_name)
                for table in db.tables:
                    require_source_safe_path(table.structure_file)
                    done += 1
                    LOGGER.info("dry-run source structure %s/%s: table %s -> %s", done, total_items, table.full_name(), table.structure_file)
        finally:
            cleanup_manifests(args, temp_files)
        return

    manifest_path = make_manifest("restore_structure_", args.keep_manifests_dir)
    temp_files = []  # type: List[Path]
    try:
        with manifest_path.open("w", encoding="utf-8") as fh:
            fh.write("-- generated by restore_sql_fast.py\n")
            write_common_session_init(fh, args, data_mode=False)
            for db in db_plans:
                write_drop_database_if_requested(fh, args, db)
                db_structure_file = prepare_db_structure_file(args, db, temp_files)
                require_source_safe_path(db_structure_file)
                fh.write(f"SELECT {sql_string('structure root/database ' + db.display_name())} AS restore_progress;\n")
                fh.write(f"source {db_structure_file}\n")
                if db.db_name:
                    fh.write(f"USE {sql_ident(db.db_name)};\n")
                for table in db.tables:
                    require_source_safe_path(table.structure_file)
                    fh.write(f"SELECT {sql_string('structure table ' + table.full_name())} AS restore_progress;\n")
                    fh.write(f"source {table.structure_file}\n")
        run_mysql_from_manifest(args, auth, manifest_path, "all structures", data_mode=False)
    finally:
        cleanup_manifests(args, [manifest_path])
        cleanup_manifests(args, temp_files)


def import_table_data_source(args: argparse.Namespace, auth: AuthManager, table: TablePlan) -> None:
    if not table.data_files:
        LOGGER.debug("no SQL data files for %s", table.full_name())
        return
    if args.generated_column_mode == "default" and table.generated_columns:
        LOGGER.warning(
            "table %s has generated column(s); source mode cannot rewrite source files, so this table will be imported through stream mode",
            table.full_name(),
        )
        import_table_data_stream(args, auth, table)
        return
    if args.dry_run:
        LOGGER.info("dry-run source-mode data table: %s; files=%s; no temporary manifest will be created", table.full_name(), len(table.data_files))
        for idx, path in enumerate(table.data_files, 1):
            require_source_safe_path(path)
            LOGGER.debug("dry-run source data %s [%s/%s]: %s", table.full_name(), idx, len(table.data_files), path)
        return

    manifest_prefix = f"restore_data_{table.full_name().replace('.', '_')}_"
    manifest_path = make_manifest(manifest_prefix, args.keep_manifests_dir)
    try:
        with manifest_path.open("w", encoding="utf-8") as fh:
            fh.write("-- generated by restore_sql_fast.py\n")
            write_common_session_init(fh, args, data_mode=True)
            if table.db_name:
                fh.write(f"USE {sql_ident(table.db_name)};\n")
            total = len(table.data_files)
            for idx, path in enumerate(table.data_files, 1):
                require_source_safe_path(path)
                label = f"data {table.full_name()} [{idx}/{total}] {path.name}"
                fh.write(f"SELECT {sql_string(label)} AS restore_progress;\n")
                fh.write(f"source {path}\n")
                if not args.no_transaction:
                    fh.write("COMMIT;\n")
        run_mysql_from_manifest(args, auth, manifest_path, f"data {table.full_name()}", data_mode=True)
    finally:
        cleanup_manifests(args, [manifest_path])


def write_bytes(proc: subprocess.Popen, data: bytes) -> None:
    if proc.stdin is None:
        raise RuntimeError("mysql process stdin is not available")
    try:
        proc.stdin.write(data)
    except (BrokenPipeError, OSError, ValueError) as exc:
        rc = proc.poll()
        raise RuntimeError(f"mysql process closed stdin while receiving streamed SQL; exit_code={rc}") from exc


def write_sql(proc: subprocess.Popen, sql: str) -> None:
    write_bytes(proc, sql.encode("utf-8"))


def stream_one_file(proc: subprocess.Popen, path: Path, chunk_size: int) -> None:
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            write_bytes(proc, chunk)
    write_sql(proc, "\n")


def stream_one_file_rewrite_generated_defaults(
    proc: subprocess.Popen,
    path: Path,
    chunk_size: int,
    generated_columns: Set[str],
) -> Tuple[int, int]:
    changed_statements = 0
    changed_rows = 0
    for statement in iter_sql_statements_from_file(path, chunk_size):
        rewritten, rows = rewrite_insert_generated_defaults(statement, generated_columns)
        if rows:
            changed_statements += 1
            changed_rows += rows
        write_text_preserve(proc, rewritten)
        if rewritten and not rewritten.endswith("\n"):
            write_sql(proc, "\n")
    return changed_statements, changed_rows


def run_mysql_stream(
    args: argparse.Namespace,
    auth: AuthManager,
    database: Optional[str],
    files: List[Path],
    description: str,
    data_mode: bool,
    generated_columns: Optional[Set[str]] = None,
) -> None:
    cmd = mysql_base_args(args, auth, database=database, binary_mode=True)
    LOGGER.info("mysql start: %s: %s", description, redact_mysql_command(cmd))
    if args.dry_run:
        for idx, path in enumerate(files, 1):
            LOGGER.info("dry-run stream %s/%s: %s", idx, len(files), path)
        return
    proc = None  # type: Optional[subprocess.Popen]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, env=auth.child_env())
        register_active_process(proc, description)
        write_common_session_init_to_proc(proc, args, data_mode=data_mode)
        flush_stdin(proc, description)
        if database:
            write_sql(proc, f"USE {sql_ident(database)};\n")
        total = len(files)
        rewrite_generated = bool(data_mode and generated_columns and args.generated_column_mode == "default")
        if rewrite_generated:
            LOGGER.warning(
                "generated-column rewrite enabled for %s; explicit values for generated columns will be replaced with DEFAULT: %s",
                description,
                ",".join(sorted(generated_columns or [])),
            )
        for idx, path in enumerate(files, 1):
            LOGGER.info("stream %s/%s: %s", idx, total, path)
            if rewrite_generated:
                changed_statements, changed_rows = stream_one_file_rewrite_generated_defaults(
                    proc, path, args.chunk_size, generated_columns or set()
                )
                LOGGER.info(
                    "generated-column rewrite %s: statements=%s, rows=%s",
                    path.name,
                    changed_statements,
                    changed_rows,
                )
            else:
                stream_one_file(proc, path, args.chunk_size)
            if data_mode and not args.no_transaction:
                write_sql(proc, "COMMIT;\n")
        close_stdin(proc)
        rc = proc.wait()
    except Exception:
        if proc is not None:
            close_stdin(proc)
            terminate_process(proc, description, args.process_cleanup_timeout)
        raise
    finally:
        if proc is not None:
            unregister_active_process(proc)
    if rc != 0:
        raise RuntimeError(f"mysql failed with exit code {rc}: {description}")


def import_structure_stream(args: argparse.Namespace, auth: AuthManager, db_plans: List[DbPlan]) -> None:
    cmd = mysql_base_args(args, auth, binary_mode=True)
    LOGGER.info("mysql start: all structures: %s", redact_mysql_command(cmd))
    if args.dry_run:
        for db in db_plans:
            if args.existing_database == "drop" and db.db_name:
                LOGGER.info("dry-run drop database before structure: %s", db.db_name)
            LOGGER.info("dry-run structure root/database: %s", db.structure_file)
            if db.db_name:
                LOGGER.info("dry-run selected database after root structure: %s", db.db_name)
            for table in db.tables:
                LOGGER.info("dry-run structure table: %s", table.structure_file)
        return
    temp_files = []  # type: List[Path]
    proc = None  # type: Optional[subprocess.Popen]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, env=auth.child_env())
        register_active_process(proc, "all structures")
        write_common_session_init_to_proc(proc, args, data_mode=False)
        flush_stdin(proc, "all structures")
        for db in db_plans:
            stream_drop_database_if_requested(proc, args, db)
            db_structure_file = prepare_db_structure_file(args, db, temp_files)
            LOGGER.info("stream structure root/database: %s", db.display_name())
            stream_one_file(proc, db_structure_file, args.chunk_size)
            if db.db_name:
                write_sql(proc, f"USE {sql_ident(db.db_name)};\n")
            for table in db.tables:
                LOGGER.info("stream structure table: %s", table.full_name())
                stream_one_file(proc, table.structure_file, args.chunk_size)
        close_stdin(proc)
        rc = proc.wait()
    except Exception:
        if proc is not None:
            close_stdin(proc)
            terminate_process(proc, "all structures", args.process_cleanup_timeout)
        cleanup_manifests(args, temp_files)
        raise
    finally:
        if proc is not None:
            unregister_active_process(proc)
    cleanup_manifests(args, temp_files)
    if rc != 0:
        raise RuntimeError(f"mysql failed with exit code {rc}: all structures")


def import_table_data_stream(args: argparse.Namespace, auth: AuthManager, table: TablePlan) -> None:
    if not table.data_files:
        LOGGER.info("no SQL data files for %s", table.full_name())
        return
    generated_columns = set(table.generated_columns) if table.generated_columns else None
    run_mysql_stream(
        args,
        auth,
        table.db_name,
        table.data_files,
        f"data {table.full_name()}",
        data_mode=True,
        generated_columns=generated_columns,
    )


def all_tables(db_plans: Iterable[DbPlan]) -> List[TablePlan]:
    tables = []  # type: List[TablePlan]
    for db in db_plans:
        tables.extend(db.tables)
    return tables


def submit_table_data(
    executor: concurrent.futures.ThreadPoolExecutor,
    args: argparse.Namespace,
    auth: AuthManager,
    table: TablePlan,
) -> concurrent.futures.Future:
    if args.input_mode == "source":
        return executor.submit(import_table_data_source, args, auth, table)
    return executor.submit(import_table_data_stream, args, auth, table)


def log_data_progress(completed_tables: int, total_tables: int, completed_files: int, total_files: int, table: TablePlan) -> None:
    table_pct = (completed_tables * 100.0 / total_tables) if total_tables else 100.0
    file_pct = (completed_files * 100.0 / total_files) if total_files else 100.0
    LOGGER.info(
        "data progress: tables=%s/%s %.1f%%, sql_files=%s/%s %.1f%%; done=%s",
        completed_tables,
        total_tables,
        table_pct,
        completed_files,
        total_files,
        file_pct,
        table.full_name(),
    )


def format_table_errors(errors: List[Tuple[TablePlan, BaseException]]) -> str:
    parts = []  # type: List[str]
    for table, exc in errors[:5]:
        parts.append(f"{table.full_name()}: {exc}")
    if len(errors) > 5:
        parts.append(f"... and {len(errors) - 5} more")
    return "; ".join(parts)


def import_all_data(args: argparse.Namespace, auth: AuthManager, db_plans: List[DbPlan]) -> None:
    tables = [table for table in all_tables(db_plans) if table.data_files]
    if not tables:
        LOGGER.info("no SQL data files found")
        return

    total_tables = len(tables)
    total_files = sum(len(table.data_files) for table in tables)
    LOGGER.info(
        "data import plan: tables_with_data=%s, sql_data_files=%s, workers=%s, fail_fast=%s",
        total_tables,
        total_files,
        args.workers,
        args.fail_fast,
    )

    completed_tables = 0
    completed_files = 0
    errors = []  # type: List[Tuple[TablePlan, BaseException]]

    if args.workers <= 1:
        for table in tables:
            try:
                LOGGER.info("start data table %s, files=%s", table.full_name(), len(table.data_files))
                if args.input_mode == "source":
                    import_table_data_source(args, auth, table)
                else:
                    import_table_data_stream(args, auth, table)
                completed_tables += 1
                completed_files += len(table.data_files)
                log_data_progress(completed_tables, total_tables, completed_files, total_files, table)
            except Exception as exc:
                errors.append((table, exc))
                LOGGER.error("failed data %s: %s", table.full_name(), exc)
                if args.fail_fast:
                    terminate_active_processes("fail-fast after table data failure", args.process_cleanup_timeout)
                    raise RuntimeError(f"table data import failed: {format_table_errors(errors)}")
        if errors:
            raise RuntimeError(f"{len(errors)} table data import job(s) failed: {format_table_errors(errors)}")
        return

    LOGGER.info("parallel data import with workers=%s; one worker imports one table at a time", args.workers)
    next_index = 0
    pending = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        while next_index < len(tables) and len(pending) < args.workers:
            table = tables[next_index]
            pending[submit_table_data(executor, args, auth, table)] = table
            next_index += 1

        while pending:
            done, _ = concurrent.futures.wait(list(pending.keys()), return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                table = pending.pop(future)
                try:
                    future.result()
                    completed_tables += 1
                    completed_files += len(table.data_files)
                    log_data_progress(completed_tables, total_tables, completed_files, total_files, table)
                except Exception as exc:
                    errors.append((table, exc))
                    LOGGER.error("failed data %s: %s", table.full_name(), exc)
                    if args.fail_fast:
                        cancelled = 0
                        for pending_future in list(pending.keys()):
                            if pending_future.cancel():
                                cancelled += 1
                        LOGGER.error(
                            "fail-fast: stopped scheduling new table imports after first failure; cancelled=%s, running_or_finished=%s",
                            cancelled,
                            len(pending) - cancelled,
                        )
                        terminate_active_processes("fail-fast after table data failure", args.process_cleanup_timeout)
                        for pending_future, pending_table in list(pending.items()):
                            if pending_future.cancelled():
                                pending.pop(pending_future, None)
                                continue
                            try:
                                pending_future.result()
                                completed_tables += 1
                                completed_files += len(pending_table.data_files)
                                log_data_progress(completed_tables, total_tables, completed_files, total_files, pending_table)
                            except Exception as pending_exc:
                                errors.append((pending_table, pending_exc))
                                LOGGER.error("failed data %s: %s", pending_table.full_name(), pending_exc)
                            finally:
                                pending.pop(pending_future, None)
                        raise RuntimeError(f"{len(errors)} table data import job(s) failed: {format_table_errors(errors)}")

                while next_index < len(tables) and len(pending) < args.workers:
                    if args.fail_fast and errors:
                        break
                    next_table = tables[next_index]
                    pending[submit_table_data(executor, args, auth, next_table)] = next_table
                    next_index += 1

    if errors:
        raise RuntimeError(f"{len(errors)} table data import job(s) failed: {format_table_errors(errors)}")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="阿里云RDS MySQL 8.x 快照备份SQL文件快速恢复到自建数据库。Supports SQL-only flat single-db and nested multi-db directories."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("backupset_directory")
    parser.add_argument("db_host")
    parser.add_argument("db_port")
    parser.add_argument("db_user")
    parser.add_argument(
        "db_pass",
        nargs="?",
        default=None,
        help="database password. Optional with --ask-password, --password-file, --login-path, --defaults-extra-file, or --auth-method none. '-' is rejected; MYSQL_PWD is not supported.",
    )
    parser.add_argument("-D", "--database", default=None,
                        help="target database for flat single-database layout only; not needed for nested multi-database layout")
    parser.add_argument("--only-database", action="append", default=None,
                        help="nested multi-database layout only: import only this database. Can be repeated or comma-separated")
    parser.add_argument("--existing-database", choices=["reuse", "drop", "error"], default="reuse",
                        help="reuse rewrites CREATE DATABASE/SCHEMA to IF NOT EXISTS; drop runs DROP DATABASE IF EXISTS; error keeps the dump unchanged. default: reuse")
    parser.add_argument("--drop-database", action="store_true", help="shortcut for --existing-database drop; destructive")
    parser.add_argument("--layout", choices=["auto", "flat", "nested"], default="auto",
                        help="backup layout. auto: root/structure.sql means flat single-db; otherwise nested multi-db. default: auto")
    parser.add_argument("--allow-no-database", action="store_true",
                        help="flat layout only: allow import without selected database if SQL files contain USE or db-qualified table names")
    parser.add_argument("--strict-layout", action="store_true",
                        help="fail on non-table/non-database directories missing structure.sql instead of skipping them")
    parser.add_argument("--skip-structure", action="store_true", help="skip database/table structure restore and import data only")
    parser.add_argument("--skip-data", action="store_true", help="skip data restore and import structures only")
    parser.add_argument("--workers", type=int, default=1, help="parallel table data import workers; default: 1")
    fail_group = parser.add_mutually_exclusive_group()
    fail_group.add_argument("--fail-fast", dest="fail_fast", action="store_true",
                            help="stop scheduling new table imports after the first table data failure; default")
    fail_group.add_argument("--no-fail-fast", dest="fail_fast", action="store_false",
                            help="continue scheduling remaining tables and report all failures at the end")
    parser.set_defaults(fail_fast=True)
    parser.add_argument("--mysql", default="mysql", help="mysql client path; default: mysql")
    parser.add_argument("--mysql-extra-args", default=None,
                        help="extra mysql client arguments parsed with shlex shell-like quoting; for complex values prefer repeated --mysql-extra-arg")
    parser.add_argument("--mysql-extra-arg", action="append", default=None,
                        help="append one raw mysql client argument; can be repeated, e.g. --mysql-extra-arg=--ssl-mode=REQUIRED")
    parser.add_argument("--input-mode", choices=["source", "stream"], default="source",
                        help="source is usually faster; stream supports whitespace paths and binary-mode. default: source")
    parser.add_argument("--generated-column-mode", choices=["default", "off"], default="default",
                        help="default rewrites explicit values for generated columns to DEFAULT during data import; off leaves SQL files unchanged. default: default")
    parser.add_argument("--max-allowed-packet", default="1G",
                        help="mysql client max_allowed_packet; server must be configured too; default: 1G")
    parser.add_argument("--default-character-set", default="utf8mb4", help="mysql client character set; default: utf8mb4")
    parser.add_argument("--compression-algorithms", default=None,
                        help="optional MySQL 8 compression algorithms, e.g. zstd,zlib,uncompressed")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                        help=f"stream mode read chunk bytes; default: {DEFAULT_CHUNK_SIZE}")
    parser.add_argument("--detect-limit", type=parse_size, default=DEFAULT_DETECT_DB_READ_LIMIT,
                        help="bytes to scan in structure.sql when detecting database names and CREATE DATABASE rewrite need; 0 scans whole file. default: 8M")
    parser.add_argument("--disable-binlog", action="store_true",
                        help="SET SESSION sql_log_bin=0; requires privilege and is unsafe for replication unless intended")
    parser.add_argument("--no-transaction", action="store_true", help="do not wrap data files with autocommit=0/COMMIT")
    parser.add_argument("--force", action="store_true", help="pass --force to mysql client, continue after SQL errors")
    parser.add_argument("--ignore-non-sql", action="store_true", help="ignore non-.sql files under data directories; CSV is not imported")
    parser.add_argument("--auth-method", choices=["defaults-extra-file", "none"], default="defaults-extra-file",
                        help="password delivery method. defaults-extra-file creates a temporary 0600 option file; none passes no password. MYSQL_PWD/env mode is intentionally unsupported. default: defaults-extra-file")
    parser.add_argument("--ask-password", action="store_true", help="prompt for the MySQL password instead of reading it from argv")
    parser.add_argument("--password-file", default=None, help="read the first line from this file as the MySQL password")
    parser.add_argument("--defaults-extra-file", default=None,
                        help="use an existing mysql client option file instead of generating a temporary one")
    parser.add_argument("--login-path", default=None,
                        help="use a mysql_config_editor login path, for example --login-path=restore")
    parser.add_argument("--dry-run", action="store_true", help="print plan and commands without executing mysql")
    parser.add_argument("--keep-manifests-dir", default=None, help="keep generated source-mode manifests in this directory")
    parser.add_argument("--log-file", default=None, help="also write DEBUG-level logs to this file")
    parser.add_argument("--verbose", action="store_true", help="enable DEBUG logging on console")
    parser.add_argument("--quiet", action="store_true", help="show only warnings and errors on console")
    parser.add_argument("--process-cleanup-timeout", "--kill-timeout", dest="process_cleanup_timeout", type=float, default=DEFAULT_PROCESS_CLEANUP_TIMEOUT,
                        help="seconds to wait after terminating a failed mysql child process during cleanup; default: 5")
    return parser.parse_args(argv)


def normalize_only_database(args: argparse.Namespace) -> None:
    if not args.only_database:
        args.only_database = None
        return
    names = []  # type: List[str]
    for item in args.only_database:
        for part in item.split(','):
            part = part.strip()
            if part:
                names.append(part)
    args.only_database = set(names) if names else None


def validate_args(args: argparse.Namespace) -> None:
    if not sys.platform.startswith("linux"):
        die("阿里云RDS MySQL 8.x 快照备份SQL文件快速恢复到自建数据库 supports Linux only")

    if args.workers < 1:
        die("--workers must be >= 1")
    if args.chunk_size <= 0:
        die("--chunk-size must be > 0")
    if args.process_cleanup_timeout < 0:
        die("--process-cleanup-timeout must be >= 0")
    if args.detect_limit < 0:
        die("--detect-limit must be >= 0")
    if args.skip_structure and args.skip_data:
        die("--skip-structure and --skip-data cannot be used together")
    if args.verbose and args.quiet:
        die("--verbose and --quiet cannot be used together")
    if args.drop_database:
        args.existing_database = "drop"
    if args.skip_structure and args.existing_database == "drop":
        die("--skip-structure cannot be combined with --existing-database drop/--drop-database")
    extra_args = []  # type: List[str]
    if args.mysql_extra_args:
        try:
            extra_args.extend(shlex.split(args.mysql_extra_args))
        except ValueError as exc:
            die(f"invalid --mysql-extra-args: {exc}")
        LOGGER.debug("parsed --mysql-extra-args with shlex into: %s", extra_args)
    if args.mysql_extra_arg:
        extra_args.extend(args.mysql_extra_arg)
    for item in extra_args:
        if item.startswith("--defaults-file") or item.startswith("--defaults-extra-file"):
            die("pass --defaults-extra-file through the dedicated option, not --mysql-extra-args")
    args.mysql_extra_args = extra_args
    if args.db_pass == "-":
        die("MYSQL_PWD compatibility has been removed. Use --ask-password, --password-file, --login-path, or --defaults-extra-file.")
    if args.ask_password and args.password_file:
        die("--ask-password and --password-file cannot be used together")


def auth_display(args: argparse.Namespace, auth: AuthManager) -> str:
    if args.defaults_extra_file or auth.generated_option_file is not None:
        return "defaults-extra-file"
    if args.login_path:
        return "login-path"
    return args.auth_method


def log_plan(args: argparse.Namespace, auth: AuthManager, root_dir: Path, db_plans: List[DbPlan]) -> None:
    only_db_msg = ",".join(sorted(args.only_database)) if args.only_database else "<all>"
    LOGGER.info("restore Alibaba Cloud RDS MySQL SQL backup from %s to %s:%s", root_dir, args.db_host, args.db_port)
    LOGGER.info(
        "python=%s; layout=%s; input_mode=%s; workers=%s; fail_fast=%s; existing_database=%s; only_database=%s; auth_method=%s; skip_structure=%s; skip_data=%s; generated_column_mode=%s",
        sys.version.split()[0],
        args.layout,
        args.input_mode,
        args.workers,
        args.fail_fast,
        args.existing_database,
        only_db_msg,
        auth_display(args, auth),
        args.skip_structure,
        args.skip_data,
        args.generated_column_mode,
    )
    for db in db_plans:
        LOGGER.info("detected layout=%s; root/database=%s; structure=%s", db.layout, db.display_name(), db.structure_file)
        if db.layout == "flat" and args.database and db.detected_db_name and args.database != db.detected_db_name:
            LOGGER.warning("-D/--database overrides database detected in root structure.sql: detected=%s, using=%s", db.detected_db_name, args.database)
        if db.layout == "flat" and not db.db_name:
            LOGGER.warning("no selected database; data files must contain USE statements or db-qualified table names")
    db_count, table_count, data_file_count, total_bytes = plan_stats(db_plans)
    LOGGER.info(
        "plan: root/databases=%s, tables=%s, sql_data_files=%s, sql_data_size=%s",
        db_count,
        table_count,
        data_file_count,
        format_bytes(total_bytes),
    )


def resolve_backupset_directory(value: str) -> Path:
    if value == "-":
        line = sys.stdin.readline().strip()
        if not line:
            raise RuntimeError("backupset_directory is '-' but stdin did not provide a path")
        value = line
    return Path(value).expanduser().resolve()


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    setup_logging(args.log_file, args.verbose, args.quiet)
    normalize_only_database(args)
    validate_args(args)
    try:
        root_dir = resolve_backupset_directory(args.backupset_directory)
        db_plans = build_plan(root_dir, args)
        with AuthManager(args) as auth:
            log_plan(args, auth, root_dir, db_plans)
            if not db_plans:
                LOGGER.warning("no database/root plan found; nothing to do")
                return 0
            start = time.time()
            if args.skip_structure:
                LOGGER.info("skip structure restore by request")
            else:
                if args.input_mode == "source":
                    import_structure_source(args, auth, db_plans)
                else:
                    import_structure_stream(args, auth, db_plans)
                LOGGER.info("structure restore finished")
            if args.skip_data:
                LOGGER.info("skip data restore by request")
            else:
                import_all_data(args, auth, db_plans)
            LOGGER.info("restore finished in %.2fs", time.time() - start)
        return 0
    except KeyboardInterrupt:
        LOGGER.error("interrupted")
        terminate_active_processes("keyboard interrupt", args.process_cleanup_timeout)
        return 130
    except Exception as exc:
        LOGGER.error(str(exc))
        LOGGER.debug("exception details", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
