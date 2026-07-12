from __future__ import annotations

import unittest

from local_agent.provider_protocol import classify_provider_content_artifact


class ProviderProtocolTests(unittest.TestCase):
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
