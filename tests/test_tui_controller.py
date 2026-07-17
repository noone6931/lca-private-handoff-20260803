from __future__ import annotations

import unittest

from local_agent.frontends.tui.controller import TuiController
from local_agent.frontends.tui.mailbox import TuiMailbox
from local_agent.frontends.tui.messages import TuiCommandCompleted
from local_agent.frontends.tui.messages import TuiInteractionPending
from local_agent.frontends.tui.model import TuiProjector
from local_agent.protocol.commands import CommandResult
from local_agent.protocol.interactions import InteractionRequest


class _FakeBridge:
    def __init__(self) -> None:
        self.resolved = []

    def resolve(self, request_id, result) -> bool:
        self.resolved.append((request_id, result))
        return True


class _FakeWorker:
    def __init__(self) -> None:
        self.submitted = []
        self.interaction_bridge = _FakeBridge()

    def submit(self, command) -> bool:
        self.submitted.append(command)
        return True

    def request_cancel(self) -> bool:
        return True


class TuiControllerTests(unittest.TestCase):
    def test_plain_input_submits_typed_prompt_command(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        controller = TuiController(mailbox, TuiProjector(), worker)  # type: ignore[arg-type]

        for character in "inspect":
            controller.handle_key(character)
        controller.handle_key("ENTER")

        self.assertEqual([command.type for command in worker.submitted], ["SubmitPrompt"])
        self.assertEqual(worker.submitted[0].payload, {"prompt": "inspect"})
        self.assertEqual(controller.view.input_text, "")

    def test_help_and_exit_stay_frontend_local(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        controller = TuiController(mailbox, TuiProjector(), worker)  # type: ignore[arg-type]

        for character in "/help":
            controller.handle_key(character)
        controller.handle_key("ENTER")

        self.assertEqual(worker.submitted, [])
        self.assertIn("Commands:", controller.state.transcript[-1].text)

        for character in "/exit":
            controller.handle_key(character)
        controller.handle_key("ENTER")
        self.assertTrue(controller.exit_requested)

    def test_status_command_routes_through_runtime_and_renders_typed_result(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        controller = TuiController(mailbox, TuiProjector(), worker)  # type: ignore[arg-type]
        for character in "/status":
            controller.handle_key(character)
        controller.handle_key("ENTER")
        command = worker.submitted[0]

        mailbox.put(
            TuiCommandCompleted(
                command,
                CommandResult(command.command_id, "s1", None, "ok", {"text": "workspace status"}),
            )
        )
        controller.poll()

        self.assertEqual(command.type, "GetStatus")
        self.assertEqual(controller.state.transcript[-1].text, "workspace status")

    def test_complete_workspace_command_dispatches_on_first_enter(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        controller = TuiController(mailbox, TuiProjector(), worker)  # type: ignore[arg-type]

        for character in "/workspace list":
            controller.handle_key(character)
        controller.handle_key("ENTER")

        self.assertEqual([command.type for command in worker.submitted], ["ListWorkspaceRoots"])
        self.assertEqual(controller.view.input_text, "")

    def test_workspace_subcommand_completion_preserves_parent_command(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        controller = TuiController(mailbox, TuiProjector(), worker)  # type: ignore[arg-type]

        for character in "/workspace l":
            controller.handle_key(character)
        controller.handle_key("ENTER")

        self.assertEqual(controller.view.input_text, "/workspace list")
        self.assertEqual(worker.submitted, [])

        controller.handle_key("ENTER")
        self.assertEqual([command.type for command in worker.submitted], ["ListWorkspaceRoots"])

    def test_interaction_focus_prevents_slash_command_dispatch(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        controller = TuiController(mailbox, TuiProjector(), worker)  # type: ignore[arg-type]
        mailbox.put(TuiInteractionPending("i1", InteractionRequest("ask", "Which scope?")))
        controller.poll()

        for character in "/help":
            controller.handle_key(character)
        controller.handle_key("ENTER")

        self.assertEqual(worker.submitted, [])
        self.assertEqual(worker.interaction_bridge.resolved, [])
        self.assertIn("focused interaction", controller.view.notice)

        for _ in range(5):
            controller.handle_key("BACKSPACE")
        for character in "/cancel":
            controller.handle_key(character)
        controller.handle_key("ENTER")
        self.assertEqual(worker.interaction_bridge.resolved[0][1].status, "cancelled")
        self.assertEqual(controller.view.focus, "chat")

    def test_command_palette_selects_without_dispatching_until_next_enter(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        controller = TuiController(mailbox, TuiProjector(), worker)  # type: ignore[arg-type]

        controller.handle_key("CTRL_P")
        self.assertTrue(controller.view.palette)
        controller.handle_key("ENTER")

        self.assertTrue(controller.view.input_text.startswith("/"))
        self.assertEqual(worker.submitted, [])

    def test_palette_index_resets_when_typed_completions_shrink(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        controller = TuiController(mailbox, TuiProjector(), worker)  # type: ignore[arg-type]

        controller.handle_key("CTRL_P")
        controller.handle_key("DOWN")
        controller.handle_key("DOWN")
        controller.handle_key("/")
        for character in "help":
            controller.handle_key(character)

        self.assertEqual(controller.view.palette_index, 0)
        controller.handle_key("ENTER")
        self.assertEqual(worker.submitted, [])
        self.assertIn("Commands:", controller.state.transcript[-1].text)

    def test_search_filters_transcript_and_restores_composer_draft(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        projector = TuiProjector()
        projector.append_local("assistant", "alpha result")
        projector.append_local("assistant", "beta result")
        controller = TuiController(mailbox, projector, worker)  # type: ignore[arg-type]
        for character in "draft":
            controller.handle_key(character)

        controller.handle_key("CTRL_F")
        for character in "alpha":
            controller.handle_key(character)
        controller.handle_key("ENTER")

        self.assertEqual(controller.view.search_query, "alpha")
        self.assertEqual(controller.view.input_text, "draft")
        self.assertIn("1 transcript match", controller.view.notice)
        controller.handle_key("ESC")
        self.assertEqual(controller.view.search_query, "")

    def test_copy_uses_only_completed_assistant_answer(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        projector = TuiProjector()
        projector.append_local("assistant", "completed answer")
        controller = TuiController(mailbox, projector, worker)  # type: ignore[arg-type]

        controller.handle_key("CTRL_Y")

        self.assertEqual(controller.take_clipboard_text(), "completed answer")
        self.assertIsNone(controller.take_clipboard_text())


if __name__ == "__main__":
    unittest.main()
