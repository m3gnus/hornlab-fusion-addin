"""Keep every packaged WGLink Python module within the Python 3.9 floor."""

from __future__ import annotations

import ast
import io
from pathlib import Path
import tokenize

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADDIN = ROOT / "fusion-addins" / "WGLink"


def _same_quote_fstring_line(source: str) -> int | None:
    """Find PEP 701 same-quote strings that ast feature_version misses."""
    start_kind = getattr(tokenize, "FSTRING_START", None)
    end_kind = getattr(tokenize, "FSTRING_END", None)
    if start_kind is None or end_kind is None:
        return None

    stack = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == start_kind:
            stack.append({"quote": token.string[-1], "expression_depth": 0})
        elif token.type == end_kind:
            if stack:
                stack.pop()
        elif stack:
            current = stack[-1]
            if token.type == tokenize.OP and token.string == "{":
                current["expression_depth"] += 1
            elif token.type == tokenize.OP and token.string == "}":
                current["expression_depth"] = max(
                    0, current["expression_depth"] - 1
                )
            elif (
                current["expression_depth"]
                and token.type == tokenize.STRING
            ):
                inner_quote = next(
                    (character for character in token.string if character in "'\""),
                    None,
                )
                if inner_quote == current["quote"]:
                    return token.start[0]
    return None


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _annotation_union_nodes(tree: ast.Module):
    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    return [
        child
        for annotation in annotations
        for child in ast.walk(annotation)
        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr)
    ]


def _parse_python39(path: Path, source: str) -> None:
    tree = ast.parse(source, filename=str(path), feature_version=(3, 9))
    same_quote_line = _same_quote_fstring_line(source)
    if same_quote_line is not None:
        raise SyntaxError(
            "same-quote string inside an f-string expression is not valid in Python 3.9",
            (str(path), same_quote_line, 1, source.splitlines()[same_quote_line - 1]),
        )
    if _annotation_union_nodes(tree) and not _has_future_annotations(tree):
        raise SyntaxError(
            "PEP 604 annotation would be evaluated at runtime before Python 3.10",
            (str(path), 1, 1, source.splitlines()[0] if source.splitlines() else ""),
        )
    compile(tree, str(path), "exec")


def test_checker_rejects_python312_same_quote_fstring_syntax():
    if not hasattr(tokenize, "FSTRING_START"):
        pytest.skip("this Python tokenizer already rejects same-quote f-strings")
    source = "message = f'{record['chirality']!r}'\n"
    with pytest.raises(SyntaxError, match="same-quote string"):
        _parse_python39(Path("same_quote.py"), source)


def test_all_wglink_modules_parse_with_python39_grammar():
    files = sorted(ADDIN.rglob("*.py"))
    assert files
    failures = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        try:
            _parse_python39(path, source)
        except (SyntaxError, tokenize.TokenError) as exc:
            failures.append(f"{path.relative_to(ROOT)}: {exc}")
    assert not failures, "WGLink Python 3.9 compatibility failures:\n" + "\n".join(
        failures
    )
