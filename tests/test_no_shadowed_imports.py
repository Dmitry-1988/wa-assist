"""No module may import two different things under one name.

`tick.py` imported `read_state` from `.state` (the WhatsApp session record)
and then, two lines later, `read_state` from `.watermarks` (the digest
record). The second silently replaced the first, so the rotation check called
the wrong function and every tick died with

    AttributeError: 'dict' object has no attribute 'linked_at'

before it did anything at all. The daemon kept running and kept logging, so it
looked alive; GROUPSUM requests simply went unanswered for half an hour.

Nothing caught it: the tests exercise these paths with mocks, and Python is
happy to rebind a name. This is cheap to check directly.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "wa_session"
MODULES = sorted(SRC.glob("*.py"))


def bound_names(tree: ast.AST):
    """Every name a module-level import binds, with where it came from."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                yield (alias.asname or alias.name,
                       f"from {'.' * node.level}{node.module or ''} "
                       f"import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield (alias.asname or alias.name.split(".")[0],
                       f"import {alias.name}")


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_import_is_shadowed_by_another(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    seen: dict[str, str] = {}
    for name, source in bound_names(tree):
        if name in seen and seen[name] != source:
            pytest.fail(
                f"{path.name}: '{name}' is bound twice --\n"
                f"    {seen[name]}\n"
                f"    {source}\n"
                "The second silently replaces the first."
            )
        seen[name] = source


def test_the_two_read_functions_still_have_different_names():
    """The specific collision, named so a rename cannot quietly restore it."""
    from wa_session import state, watermarks

    assert hasattr(state, "read_state")
    assert not hasattr(watermarks, "read_state"), \
        "watermarks must not export a name that shadows state.read_state"
    assert hasattr(watermarks, "read_reported")
