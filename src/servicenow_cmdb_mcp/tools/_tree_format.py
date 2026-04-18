"""ASCII tree rendering for dependency tree output."""

from __future__ import annotations

from typing import Any


def render_ascii_tree(tree: dict[str, Any]) -> str:
    """Render a dependency tree dict as an ASCII tree string.

    Expects the nested structure returned by get_dependency_tree:
    {"ci": {"name": ..., "sys_class_name": ...}, "children": [...], "relationship_type": {...}}

    Returns a multi-line string using +-- and L-- connectors (ASCII-safe).
    """
    lines: list[str] = []
    ci = tree.get("ci", {})
    lines.append(f"{ci.get('name', '?')}  ({ci.get('sys_class_name', '')})")
    children = tree.get("children", [])
    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        _render_node(child, prefix="  ", is_last=is_last, lines=lines)
    return "\n".join(lines)


def _render_node(
    node: dict[str, Any],
    prefix: str,
    is_last: bool,
    lines: list[str],
) -> None:
    """Recursively render a single tree node and its children."""
    connector = "L-- " if is_last else "+-- "
    ci = node.get("ci", {})
    rel_type = node.get("relationship_type", {})
    rel_name = rel_type.get("name", "") if rel_type else ""
    label = f"[{rel_name}] " if rel_name else ""
    lines.append(f"{prefix}{connector}{label}{ci.get('name', '?')}  ({ci.get('sys_class_name', '')})")

    children = node.get("children", [])
    extension = prefix + ("      " if is_last else "|     ")
    for i, child in enumerate(children):
        child_is_last = i == len(children) - 1
        _render_node(child, extension, child_is_last, lines)
