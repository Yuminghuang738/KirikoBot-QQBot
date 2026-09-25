"""Static wiring checks for main.py.

``main`` cannot be imported from tests: importing it starts the scheduler, the
WebSocket gateway and the worker pool. So the tool wiring is checked by parsing
the source instead. This guard exists because a rename once silently missed the
``from ai_tools import ...`` list and the container crash-looped on
``NameError: name 'SomeTool' is not defined`` — every other test passed, since
they import ``ai_tools`` directly and never touch ``main``.
"""
from __future__ import annotations


class TestToolWiring:
    def _main_ast(self):
        import ast
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "KirikoBot", "main.py",
        )
        with open(path, encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def test_every_instantiated_tool_is_imported(self):
        import ast

        tree = self._main_ast()
        imported: set[str] = set()
        defined: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "ai_tools":
                imported |= {a.name for a in node.names}
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                defined.add(node.name)

        used = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id.endswith("Tool")
        }
        missing = used - imported - defined
        assert not missing, f"used but never imported: {sorted(missing)}"
