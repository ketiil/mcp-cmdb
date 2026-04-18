"""Tests for tools/_tree_format.py — ASCII tree rendering."""

from servicenow_cmdb_mcp.tools._tree_format import render_ascii_tree


class TestRenderAsciiTree:
    def test_single_node_no_children(self):
        tree = {
            "ci": {"name": "Server-A", "sys_class_name": "cmdb_ci_server"},
            "children": [],
        }
        result = render_ascii_tree(tree)
        assert result == "Server-A  (cmdb_ci_server)"

    def test_one_child(self):
        tree = {
            "ci": {"name": "Server-A", "sys_class_name": "cmdb_ci_server"},
            "children": [
                {
                    "ci": {"name": "App-B", "sys_class_name": "cmdb_ci_appl"},
                    "children": [],
                    "relationship_type": {"name": "Runs on::Runs"},
                },
            ],
        }
        result = render_ascii_tree(tree)
        lines = result.split("\n")
        assert lines[0] == "Server-A  (cmdb_ci_server)"
        assert lines[1] == "  L-- [Runs on::Runs] App-B  (cmdb_ci_appl)"

    def test_two_children(self):
        tree = {
            "ci": {"name": "Root", "sys_class_name": "cmdb_ci"},
            "children": [
                {
                    "ci": {"name": "Child-1", "sys_class_name": "cmdb_ci_server"},
                    "children": [],
                    "relationship_type": {"name": "Depends on::Used by"},
                },
                {
                    "ci": {"name": "Child-2", "sys_class_name": "cmdb_ci_appl"},
                    "children": [],
                    "relationship_type": {"name": "Runs on::Runs"},
                },
            ],
        }
        result = render_ascii_tree(tree)
        lines = result.split("\n")
        assert lines[0] == "Root  (cmdb_ci)"
        assert lines[1] == "  +-- [Depends on::Used by] Child-1  (cmdb_ci_server)"
        assert lines[2] == "  L-- [Runs on::Runs] Child-2  (cmdb_ci_appl)"

    def test_nested_depth(self):
        tree = {
            "ci": {"name": "A", "sys_class_name": "cmdb_ci"},
            "children": [
                {
                    "ci": {"name": "B", "sys_class_name": "cmdb_ci_server"},
                    "children": [
                        {
                            "ci": {"name": "C", "sys_class_name": "cmdb_ci_appl"},
                            "children": [],
                            "relationship_type": {"name": "Runs on::Runs"},
                        },
                    ],
                    "relationship_type": {"name": "Depends on::Used by"},
                },
            ],
        }
        result = render_ascii_tree(tree)
        lines = result.split("\n")
        assert lines[0] == "A  (cmdb_ci)"
        assert lines[1] == "  L-- [Depends on::Used by] B  (cmdb_ci_server)"
        assert lines[2] == "        L-- [Runs on::Runs] C  (cmdb_ci_appl)"

    def test_missing_relationship_type(self):
        """Children at root level may not have relationship_type."""
        tree = {
            "ci": {"name": "Root", "sys_class_name": "cmdb_ci"},
            "children": [
                {
                    "ci": {"name": "Child", "sys_class_name": "cmdb_ci_server"},
                    "children": [],
                },
            ],
        }
        result = render_ascii_tree(tree)
        lines = result.split("\n")
        assert lines[1] == "  L-- Child  (cmdb_ci_server)"
