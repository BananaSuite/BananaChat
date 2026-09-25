"""Every `from <our module> import name` must name something that exists.

Python only resolves these at import time, so a name that was renamed or
removed sits there quietly until the line that imports it happens to run. A
function-local import inside a rarely taken branch can stay broken for a long
time. Parsing the tree finds them all without importing anything.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "build", "dist"}


def _module_name(path):
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _names_bound_by(node):
    """Collect every name a statement binds, including inside nested blocks."""
    bound = set()
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(child.name)
        elif isinstance(child, ast.Import):
            for alias in child.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(child, ast.ImportFrom):
            for alias in child.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            bound.add(child.id)
        elif isinstance(child, ast.alias):
            bound.add(child.asname or child.name)
    return bound


def _collect_modules():
    modules = {}
    for path in sorted(ROOT.rglob("*.py")):
        if SKIP_DIRS & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # reported by the syntax check
            continue
        bound = set()
        star_imported = False
        for statement in tree.body:
            bound |= _names_bound_by(statement)
            if isinstance(statement, ast.ImportFrom):
                star_imported |= any(a.name == "*" for a in statement.names)
        modules[_module_name(path)] = (path, bound, tree, star_imported)
    return modules


def test_internal_imports_resolve():
    modules = _collect_modules()
    assert len(modules) > 10, "the module scan found almost nothing, so it is not working"

    broken = []
    for path, _, tree, _ in modules.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level or node.module not in modules:
                continue
            target_path, bound, _, star_imported = modules[node.module]
            if star_imported:  # cannot tell what the star brought in
                continue
            for alias in node.names:
                if alias.name != "*" and alias.name not in bound:
                    broken.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: "
                        f"'{alias.name}' is not defined in {node.module}"
                    )
    assert not broken, "imports that name something missing:\n" + "\n".join(broken)
