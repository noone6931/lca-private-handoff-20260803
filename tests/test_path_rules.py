from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_agent.agent import _messages_with_runtime_context
from local_agent.path_rules import candidate_paths_for_path_rules
from local_agent.path_rules import discover_path_scoped_rules
from local_agent.path_rules import matching_path_rule_context
from local_agent.path_rules import render_path_rule_metadata


class PathRuleTests(unittest.TestCase):
    def test_metadata_is_lightweight_and_matching_body_is_path_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            _write_rule(
                root,
                "java.md",
                (
                    "---\n"
                    "paths:\n"
                    "  - src/**/*.java\n"
                    "priority: 10\n"
                    "description: Java service conventions.\n"
                    "---\n"
                    "JAVA_RULE_BODY\n"
                ),
            )
            index = discover_path_scoped_rules((root,))
            metadata = render_path_rule_metadata(index)
            matched = matching_path_rule_context(
                index,
                candidate_paths_for_path_rules(
                    "inspect src/main/java/App.java",
                    primary_workspace=root,
                ),
            )
            unmatched = matching_path_rule_context(
                index,
                candidate_paths_for_path_rules(
                    "inspect README.md",
                    primary_workspace=root,
                ),
            )

        self.assertIn("[Path-scoped rules]", metadata)
        self.assertIn("java.md", metadata)
        self.assertNotIn("JAVA_RULE_BODY", metadata)
        self.assertIn("[Matched path-scoped rules]", matched)
        self.assertIn("JAVA_RULE_BODY", matched)
        self.assertEqual(unmatched, "")

    def test_multi_root_rules_do_not_cross_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            root_a = parent / "service-a"
            root_b = parent / "service-b"
            _write_rule(root_a, "java.md", _rule_text("A_RULE_BODY", "A conventions"))
            _write_rule(root_b, "java.md", _rule_text("B_RULE_BODY", "B conventions"))
            index = discover_path_scoped_rules((root_a, root_b))
            a_context = matching_path_rule_context(index, (root_a / "src/App.java",))
            b_context = matching_path_rule_context(index, (root_b / "src/App.java",))

        self.assertIn("A_RULE_BODY", a_context)
        self.assertNotIn("B_RULE_BODY", a_context)
        self.assertIn("B_RULE_BODY", b_context)
        self.assertNotIn("A_RULE_BODY", b_context)

    def test_priority_and_bad_configuration_degrade_without_blocking_valid_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            _write_rule(
                root,
                "low.md",
                (
                    "---\npaths:\n  - src/**/*.java\npriority: -10\n---\n"
                    "LOW_PRIORITY\n"
                ),
            )
            _write_rule(
                root,
                "high.md",
                (
                    "---\npaths:\n  - src/**/*.java\npriority: 10\n---\n"
                    "HIGH_PRIORITY\n"
                ),
            )
            _write_rule(
                root,
                "bad.md",
                (
                    "---\npaths:\n  - ../outside/**\npriority: 0\n---\n"
                    "BAD_BODY\n"
                ),
            )
            index = discover_path_scoped_rules((root,))
            matched = matching_path_rule_context(index, (root / "src/App.java",))

        self.assertEqual(len(index.rules), 2)
        self.assertEqual(len(index.diagnostics), 1)
        self.assertLess(matched.index("LOW_PRIORITY"), matched.index("HIGH_PRIORITY"))
        self.assertNotIn("BAD_BODY", matched)

    def test_runtime_context_keeps_matching_rules_after_compaction_like_system_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            _write_rule(root, "java.md", _rule_text("COMPACTION_SAFE_BODY", "Compaction safe"))
            index = discover_path_scoped_rules((root,))
            metadata = render_path_rule_metadata(index)
            matched = matching_path_rule_context(index, (root / "src/App.java",))
            messages = _messages_with_runtime_context(
                [{"role": "system", "content": "[Local context compaction]\nprior summary"}],
                [],
                "",
                "",
                root,
                root / "user-config",
                current_user_request="review src/App.java",
                path_rule_metadata=metadata,
                matched_path_rules=matched,
            )

        system = str(messages[0]["content"])
        self.assertIn("[Local context compaction]", system)
        self.assertIn("[Path-scoped rules]", system)
        self.assertIn("COMPACTION_SAFE_BODY", system)

    def test_relative_request_candidates_are_primary_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            primary = parent / "primary"
            additional = parent / "additional"
            _write_rule(primary, "java.md", _rule_text("PRIMARY_RULE", "primary"))
            _write_rule(additional, "java.md", _rule_text("ADDITIONAL_RULE", "additional"))
            index = discover_path_scoped_rules((primary, additional))
            candidates = candidate_paths_for_path_rules("review src/App.java", primary_workspace=primary)
            matched = matching_path_rule_context(index, candidates)

        self.assertIn("PRIMARY_RULE", matched)
        self.assertNotIn("ADDITIONAL_RULE", matched)


def _write_rule(root: Path, name: str, text: str) -> None:
    rules_dir = root / ".local-agent" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / name).write_text(text, encoding="utf-8")


def _rule_text(body: str, description: str) -> str:
    return (
        "---\n"
        "paths:\n"
        "  - src/**/*.java\n"
        "priority: 0\n"
        f"description: {description}\n"
        "---\n"
        f"{body}\n"
    )


if __name__ == "__main__":
    unittest.main()
