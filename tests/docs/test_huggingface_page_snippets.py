"""The Hugging Face integration page only shows code that can be true.

``docs/integrations/huggingface.md`` is experimental and unreleased, which makes drift the main risk:
a renamed function or keyword would leave the page teaching code that cannot run. This does not
execute the snippets (several need ``smolagents`` or a live Hub); it checks, statically, that every
Python block parses, every ``novafabric`` name it imports exists, every keyword argument it passes to
such a name exists in that callable's signature, and that the sample ``.eval_results`` entry is valid
under the module's own validator.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.eval.hf_receipt import validate_eval_results

PAGE = Path(__file__).resolve().parents[2] / "docs" / "integrations" / "huggingface.md"
_FENCE = re.compile(r"^```(\w+)\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _blocks(language: str) -> list[str]:
    return [body for lang, body in _FENCE.findall(PAGE.read_text()) if lang == language]


def test_the_page_has_python_and_yaml_examples_at_all() -> None:
    assert len(_blocks("python")) >= 3 and len(_blocks("yaml")) >= 1  # a vacuous pass is a defect


@pytest.mark.parametrize("source", _blocks("python"), ids=lambda s: s.strip().splitlines()[0][:40])
def test_every_python_block_parses_and_its_novafabric_names_and_keywords_exist(source: str) -> None:
    tree = ast.parse(source)  # a syntax error is a doc bug
    names: dict[str, Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("novafabric"):
            module = importlib.import_module(node.module or "")
            for alias in node.names:
                assert hasattr(module, alias.name), f"{node.module} has no {alias.name}"
                names[alias.asname or alias.name] = getattr(module, alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names:
            params = inspect.signature(names[node.func.id]).parameters
            takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
            for keyword in node.keywords:
                assert keyword.arg is None or takes_any or keyword.arg in params, (
                    f"{node.func.id}() has no parameter {keyword.arg!r}"
                )


def test_the_sample_eval_results_entry_is_valid_under_the_modules_own_validator() -> None:
    for block in _blocks("yaml"):
        entries = yaml.safe_load(block)
        if isinstance(entries, list) and entries and "dataset" in entries[0]:
            assert validate_eval_results(entries) == []
            assert "verifyToken" not in block
            return
    pytest.fail("no .eval_results sample found on the page")
