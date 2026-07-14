from __future__ import annotations

import json
import unittest

from local_agent.provider_protocol import classify_provider_content_artifact
from local_agent.provider_protocol import normalize_provider_dialect_message


class ProviderProtocolTests(unittest.TestCase):
    def test_normalizes_complete_bailian_xml_inside_matching_structured_tool_call(self) -> None:
        message, artifacts = normalize_provider_dialect_message(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-glob",
                        "type": "function",
                        "function": {
                            "name": "glob_files",
                            "arguments": (
                                "<tool_call>\n<function=glob_files>\n"
                                "<parameter=paths>[\"/repo/**/*\"]</parameter>\n"
                                "<parameter=gitignore>True</parameter>\n"
                                "<parameter=hidden>False</parameter>\n"
                                "<parameter=limit>200</parameter>\n"
                                "</function>\n</tool_call>"
                            ),
                        },
                    }
                ],
            },
            provider="bailian",
        )

        arguments = json.loads(message["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(
            arguments,
            {"paths": ["/repo/**/*"], "gitignore": True, "hidden": False, "limit": 200},
        )
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].kind, "bailian_xml_structured_arguments")
        self.assertEqual(artifacts[0].tool_name, "glob_files")
        self.assertNotIn("/repo", artifacts[0].preview)

    def test_rejects_bailian_xml_argument_envelope_with_prose_or_mismatched_tool(self) -> None:
        for outer_name, arguments in (
            (
                "glob_files",
                "I will inspect.\n<tool_call><function=glob_files><parameter=paths>[]</parameter></function></tool_call>",
            ),
            (
                "read_file",
                "<tool_call><function=glob_files><parameter=paths>[]</parameter></function></tool_call>",
            ),
        ):
            with self.subTest(outer_name=outer_name):
                message, artifacts = normalize_provider_dialect_message(
                    {
                        "tool_calls": [
                            {
                                "id": "call-invalid",
                                "type": "function",
                                "function": {"name": outer_name, "arguments": arguments},
                            }
                        ]
                    },
                    provider="bailian",
                )
                self.assertEqual(message["tool_calls"][0]["function"]["arguments"], arguments)
                self.assertEqual(artifacts, ())

    def test_classifies_complete_bailian_xml_tool_envelope_at_content_tail(self) -> None:
        artifact = classify_provider_content_artifact(
            "bailian",
            "I will inspect it.\n<tool_call>\n<function=read_file>\n<parameter=path>secret/path.py</parameter>\n</function>\n</tool_call>",
        )

        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.kind, "bailian_xml_tool_envelope")
        self.assertEqual(artifact.tool_name, "read_file")
        self.assertEqual(artifact.parameter_names, ("path",))
        self.assertNotIn("secret/path.py", artifact.preview)
        self.assertNotIn("<tool_call>", artifact.preview)
        self.assertIn("tool=read_file", artifact.preview)

    def test_does_not_classify_code_fence_quotes_or_unknown_xml(self) -> None:
        samples = (
            "```xml\n<tool_call><function=read_file><parameter=path>x.py</parameter></function></tool_call>\n```",
            'The literal "<tool_call><function=read_file><parameter=path>x.py</parameter></function></tool_call>" is documentation.',
            "<tool_call><function=read_file>not-a-parameter</function></tool_call>",
            "<tool_call><function=read_file><parameter=path>x.py</parameter></function></tool_call> trailing text",
        )

        for content in samples:
            with self.subTest(content=content):
                self.assertIsNone(classify_provider_content_artifact("bailian", content))
        self.assertIsNone(
            classify_provider_content_artifact(
                "openai-compatible",
                "<tool_call><function=read_file><parameter=path>x.py</parameter></function></tool_call>",
            )
        )


if __name__ == "__main__":
    unittest.main()
