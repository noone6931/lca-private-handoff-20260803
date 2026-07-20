from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from local_agent.frontends.composer_history import ComposerHistory
from local_agent.frontends.tui.controller import TuiController
from local_agent.frontends.tui.mailbox import TuiMailbox
from local_agent.frontends.tui.messages import TuiCommandCompleted
from local_agent.frontends.tui.messages import TuiInteractionPending
from local_agent.frontends.tui.messages import TuiEvent
from local_agent.frontends.tui.model import TuiProjector
from local_agent.protocol.commands import CommandResult
from local_agent.protocol.commands import new_command
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
        history = ComposerHistory(None)
        controller = TuiController(
            mailbox, TuiProjector(), worker, composer_history=history  # type: ignore[arg-type]
        )

        for character in "inspect":
            controller.handle_key(character)
        controller.handle_key("ENTER")

        self.assertEqual([command.type for command in worker.submitted], ["SubmitPrompt"])
        self.assertEqual(worker.submitted[0].payload, {"prompt": "inspect"})
        self.assertEqual(controller.view.input_text, "")
        self.assertEqual(history.snapshot.local_entries, ("inspect",))

    def test_multiline_paste_is_inserted_atomically_until_explicit_enter(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        controller = TuiController(mailbox, TuiProjector(), worker)  # type: ignore[arg-type]

        controller.handle_paste("first\r\nsecond\nthird")

        self.assertEqual(worker.submitted, [])
        self.assertEqual(controller.view.input_text, "first\nsecond\nthird")
        controller.handle_key("ENTER")
        self.assertEqual(worker.submitted[0].payload, {"prompt": "first\nsecond\nthird"})

    def test_alt_enter_inserts_newline_and_enter_submits_multiline_payload_once(self) -> None:
        worker = _FakeWorker()
        controller = TuiController(TuiMailbox(capacity=8), TuiProjector(), worker)  # type: ignore[arg-type]

        for character in "first":
            controller.handle_key(character)
        controller.handle_key("ALT_ENTER")
        for character in "second":
            controller.handle_key(character)

        self.assertEqual(worker.submitted, [])
        self.assertEqual(controller.view.input_text, "first\nsecond")
        controller.handle_key("ENTER")
        self.assertEqual(len(worker.submitted), 1)
        self.assertEqual(worker.submitted[0].payload, {"prompt": "first\nsecond"})

    def test_up_down_move_visual_rows_before_history_recall(self) -> None:
        history = ComposerHistory(None)
        history.append("saved history")
        controller = TuiController(
            TuiMailbox(capacity=8),
            TuiProjector(),
            _FakeWorker(),  # type: ignore[arg-type]
            composer_history=history,
        )
        controller.update_viewport(8, 12)
        controller.handle_paste("abcdefgh")

        controller.handle_key("UP")
        self.assertEqual(controller.view.input_text, "abcdefgh")
        self.assertLess(controller.view.cursor, len("abcdefgh"))
        visual_cursor = controller.view.cursor
        controller.handle_key("UP")
        self.assertEqual(controller.view.cursor, visual_cursor)
        self.assertEqual(controller.view.input_text, "abcdefgh")
        controller.handle_key("DOWN")
        self.assertEqual(controller.view.cursor, len("abcdefgh"))

    def test_vertical_navigation_keeps_preferred_cell_across_short_line(self) -> None:
        controller = TuiController(TuiMailbox(capacity=8), TuiProjector(), _FakeWorker())  # type: ignore[arg-type]
        controller.update_viewport(30, 12)
        text = "123456\nx\n123456"
        controller.handle_paste(text)

        controller.handle_key("UP")
        self.assertEqual(controller.view.cursor, text.index("x") + 1)
        controller.handle_key("UP")
        self.assertEqual(controller.view.cursor, 6)
        controller.handle_key("DOWN")
        controller.handle_key("BACKSPACE")
        controller.handle_key("DOWN")
        self.assertEqual(controller.view.cursor, controller.view.input_text.rindex("123456"))

    def test_soft_wrapped_recalled_entry_reaches_older_history_only_at_visual_top(self) -> None:
        history = ComposerHistory(None)
        history.append("older prompt")
        history.append("abcdefgh")
        controller = TuiController(
            TuiMailbox(capacity=8),
            TuiProjector(),
            _FakeWorker(),  # type: ignore[arg-type]
            composer_history=history,
        )
        controller.update_viewport(8, 12)

        controller.handle_key("UP")
        self.assertEqual(controller.view.input_text, "abcdefgh")
        controller.handle_key("UP")
        self.assertEqual(controller.view.input_text, "abcdefgh")
        self.assertLess(controller.view.cursor, len("abcdefgh"))
        controller.handle_key("UP")
        self.assertEqual(controller.view.input_text, "older prompt")

        controller.update_viewport(30, 12)
        controller.handle_key("LEFT")
        interior = controller.view.cursor
        controller.handle_key("UP")
        self.assertEqual(controller.view.cursor, interior)

    def test_initial_prompt_uses_typed_worker_boundary_once(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        controller = TuiController(mailbox, TuiProjector(), worker)  # type: ignore[arg-type]

        self.assertTrue(controller.submit_initial_prompt(" inspect this "))
        self.assertFalse(controller.submit_initial_prompt("second"))

        self.assertEqual([command.type for command in worker.submitted], ["SubmitPrompt"])
        self.assertEqual(worker.submitted[0].payload, {"prompt": "inspect this"})

    def test_page_and_wheel_scroll_clamp_and_return_to_follow_mode(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        projector = TuiProjector()
        for index in range(20):
            projector.append_local("assistant", f"line {index}")
        controller = TuiController(mailbox, projector, worker)  # type: ignore[arg-type]
        controller.update_viewport(50, 10)
        bottom = controller.view.viewport.top

        controller.handle_key("PAGE_UP")
        self.assertLess(controller.view.viewport.top, bottom)
        self.assertFalse(controller.view.viewport.follow_bottom)
        for _ in range(100):
            controller.handle_key("WHEEL_UP")
        self.assertEqual(controller.view.viewport.top, 0)
        for _ in range(100):
            controller.handle_key("WHEEL_DOWN")
        self.assertEqual(controller.view.viewport.top, controller.view.viewport.max_top)
        self.assertTrue(controller.view.viewport.follow_bottom)

    def test_wheel_scrolls_transcript_while_empty_arrows_are_reserved_for_history(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        projector = TuiProjector()
        for index in range(100):
            projector.append_local("assistant", f"line {index}")
        controller = TuiController(mailbox, projector, worker)  # type: ignore[arg-type]
        controller.update_viewport(50, 20)
        bottom = controller.view.viewport.top
        expected_step = max(controller.view.viewport.visible_rows // 3, 3)

        controller.handle_key("WHEEL_UP")
        self.assertEqual(controller.view.viewport.top, bottom - expected_step)
        controller.handle_key("UP")
        self.assertEqual(controller.view.viewport.top, bottom - expected_step)
        controller.handle_key("DOWN")
        self.assertEqual(controller.view.viewport.top, bottom - expected_step)

    def test_empty_composer_recalls_history_and_down_restores_draft(self) -> None:
        history = ComposerHistory(None)
        history.append("older")
        history.append("newer")
        controller = TuiController(
            TuiMailbox(capacity=8),
            TuiProjector(),
            _FakeWorker(),  # type: ignore[arg-type]
            composer_history=history,
        )

        controller.handle_key("UP")
        self.assertEqual(controller.view.input_text, "newer")
        controller.handle_key("UP")
        self.assertEqual(controller.view.input_text, "older")
        controller.handle_key("DOWN")
        self.assertEqual(controller.view.input_text, "newer")
        controller.handle_key("DOWN")
        self.assertEqual(controller.view.input_text, "")

    def test_history_navigation_does_not_capture_multiline_interior_search_palette_or_inflight(self) -> None:
        history = ComposerHistory(None)
        history.append("saved")
        worker = _FakeWorker()
        controller = TuiController(
            TuiMailbox(capacity=8),
            TuiProjector(),
            worker,  # type: ignore[arg-type]
            composer_history=history,
        )
        controller.handle_key("UP")
        controller.handle_key("LEFT")
        controller.handle_key("UP")
        self.assertEqual(controller.view.cursor, len("saved") - 1)

        controller = TuiController(
            TuiMailbox(capacity=8), TuiProjector(), worker, composer_history=history  # type: ignore[arg-type]
        )
        controller.handle_paste("one\ntwo")
        controller.handle_key("UP")
        self.assertEqual(controller.view.input_text, "one\ntwo")

        controller = TuiController(
            TuiMailbox(capacity=8), TuiProjector(), worker, composer_history=history  # type: ignore[arg-type]
        )
        controller.handle_key("CTRL_F")
        controller.handle_key("UP")
        self.assertEqual(controller.view.focus, "search")
        controller.handle_key("ESC")
        controller.handle_key("CTRL_P")
        controller.handle_key("UP")
        self.assertEqual(controller.view.input_text, "")
        controller.handle_key("ESC")
        controller.submit_initial_prompt("submitted")
        controller.handle_key("UP")
        self.assertEqual(controller.view.input_text, "")

    def test_only_successfully_queued_prompts_enter_tui_history(self) -> None:
        class FullWorker(_FakeWorker):
            def submit(self, command) -> bool:
                self.submitted.append(command)
                return False

        history = ComposerHistory(None)
        controller = TuiController(
            TuiMailbox(capacity=8),
            TuiProjector(),
            FullWorker(),  # type: ignore[arg-type]
            composer_history=history,
        )
        for character in "not queued":
            controller.handle_key(character)
        controller.handle_key("ENTER")

        self.assertEqual(history.snapshot.local_entries, ())

    def test_submit_returns_historical_view_to_live_tail(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        projector = TuiProjector()
        for index in range(30):
            projector.append_local("assistant", f"line {index}")
        controller = TuiController(mailbox, projector, worker)  # type: ignore[arg-type]
        controller.update_viewport(50, 10)
        controller.handle_key("PAGE_UP")
        self.assertFalse(controller.view.viewport.follow_bottom)

        for character in "continue":
            controller.handle_key(character)
        controller.handle_key("ENTER")

        self.assertTrue(controller.view.viewport.follow_bottom)
        self.assertEqual(controller.view.viewport.top, controller.view.viewport.max_top)

    def test_stream_growth_keeps_history_anchor_until_user_returns_to_bottom(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        projector = TuiProjector()
        for index in range(20):
            projector.append_local("assistant", f"line {index}")
        controller = TuiController(mailbox, projector, worker)  # type: ignore[arg-type]
        controller.update_viewport(50, 10)
        controller.handle_key("PAGE_UP")
        anchored_top = controller.view.viewport.top

        projector.append_local("assistant", "new streaming content " * 8)
        controller.update_viewport(50, 10)

        self.assertEqual(controller.view.viewport.top, anchored_top)
        self.assertFalse(controller.view.viewport.follow_bottom)
        while not controller.view.viewport.follow_bottom:
            controller.handle_key("PAGE_DOWN")
        projector.append_local("assistant", "followed content")
        controller.update_viewport(50, 10)
        self.assertEqual(controller.view.viewport.top, controller.view.viewport.max_top)

    def test_streaming_delta_from_mailbox_does_not_move_historical_view(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        projector = TuiProjector()
        for index in range(20):
            projector.append_local("assistant", f"line {index}")
        controller = TuiController(mailbox, projector, worker)  # type: ignore[arg-type]
        controller.update_viewport(50, 10)
        controller.handle_key("PAGE_UP")
        anchored_top = controller.view.viewport.top
        mailbox.put(TuiEvent("TurnStarted", 1, "s1", "r1", "c1"))
        mailbox.put(
            TuiEvent(
                "AssistantDelta",
                2,
                "s1",
                "r1",
                "c1",
                (("message_id", "m1"), ("delta", "stream " * 30), ("delta_index", 0), ("delta_span", 1)),
            )
        )

        controller.poll()

        self.assertEqual(controller.view.viewport.top, anchored_top)
        self.assertFalse(controller.view.viewport.follow_bottom)

    def test_resize_clamps_history_viewport_without_empty_screen(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        projector = TuiProjector()
        projector.append_local("assistant", "wrapped " * 100)
        controller = TuiController(mailbox, projector, worker)  # type: ignore[arg-type]
        controller.update_viewport(24, 8)
        controller.handle_key("PAGE_UP")
        controller.update_viewport(120, 40)

        self.assertGreaterEqual(controller.view.viewport.top, 0)
        self.assertLessEqual(controller.view.viewport.top, controller.view.viewport.max_top)
        from local_agent.frontends.tui.view import render_frame

        frame = render_frame(controller.state, controller.view, 120, 40)
        self.assertTrue(any("• " in line for line in frame.lines))

    def test_help_and_exit_stay_frontend_local(self) -> None:
        mailbox = TuiMailbox(capacity=8)
        worker = _FakeWorker()
        history = ComposerHistory(None)
        controller = TuiController(
            mailbox, TuiProjector(), worker, composer_history=history  # type: ignore[arg-type]
        )

        for character in "/help":
            controller.handle_key(character)
        controller.handle_key("ENTER")

        self.assertEqual(worker.submitted, [])
        self.assertIn("Commands:", controller.state.transcript[-1].text)

        for character in "/exit":
            controller.handle_key(character)
        controller.handle_key("ENTER")
        self.assertTrue(controller.exit_requested)
        self.assertEqual(history.snapshot.local_entries, ())

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

    def test_workspace_move_rebinds_history_only_from_successful_typed_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = ComposerHistory(root / "old" / "composer_history.jsonl")
            history.append("old prompt")
            mailbox = TuiMailbox(capacity=8)
            worker = _FakeWorker()
            controller = TuiController(
                mailbox,
                TuiProjector(),
                worker,  # type: ignore[arg-type]
                composer_history=history,
            )
            for character in "/move /new":
                controller.handle_key(character)
            controller.handle_key("ENTER")
            command = worker.submitted[-1]

            mailbox.put(
                TuiCommandCompleted(
                    command,
                    CommandResult(
                        command.command_id,
                        "s1",
                        None,
                        "ok",
                        {"text": "moved", "state_dir": str(root / "new")},
                    ),
                )
            )
            controller.poll()

            self.assertEqual(history.path, (root / "new" / "composer_history.jsonl").resolve())
            self.assertEqual(history.snapshot.local_entries, ())

    def test_failed_or_missing_move_does_not_retain_a_stale_history_partition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = ComposerHistory(root / "old" / "composer_history.jsonl")
            mailbox = TuiMailbox(capacity=8)
            worker = _FakeWorker()
            controller = TuiController(
                mailbox,
                TuiProjector(),
                worker,  # type: ignore[arg-type]
                composer_history=history,
            )
            for character in "/move /new":
                controller.handle_key(character)
            controller.handle_key("ENTER")
            command = worker.submitted[-1]
            old_path = history.path
            mailbox.put(
                TuiCommandCompleted(
                    command,
                    CommandResult(command.command_id, "s1", None, "error", {}, "move_failed", "failed"),
                )
            )
            controller.poll()
            self.assertEqual(history.path, old_path)

            for character in "/move /new":
                controller.handle_key(character)
            controller.handle_key("ENTER")
            missing_command = worker.submitted[-1]
            mailbox.put(
                TuiCommandCompleted(
                    missing_command,
                    CommandResult(missing_command.command_id, "s1", None, "ok", {"text": "moved"}),
                )
            )
            controller.poll()
            self.assertIsNone(history.path)
            self.assertFalse(history.snapshot.persistence_enabled)

    def test_unavailable_move_partition_clears_local_history_and_disables_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocked_state = root / "blocked-state"
            blocked_state.write_text("not a directory", encoding="utf-8")
            history = ComposerHistory(root / "old" / "composer_history.jsonl")
            history.append("stale local")
            mailbox = TuiMailbox(capacity=8)
            worker = _FakeWorker()
            controller = TuiController(
                mailbox,
                TuiProjector(),
                worker,  # type: ignore[arg-type]
                composer_history=history,
            )
            for character in "/move /blocked":
                controller.handle_key(character)
            controller.handle_key("ENTER")
            command = worker.submitted[-1]
            mailbox.put(
                TuiCommandCompleted(
                    command,
                    CommandResult(
                        command.command_id,
                        "s1",
                        None,
                        "ok",
                        {"text": "moved", "state_dir": str(blocked_state)},
                    ),
                )
            )

            controller.poll()

            self.assertIsNone(history.path)
            self.assertEqual(history.snapshot.local_entries, ())
            self.assertFalse(history.snapshot.persistence_enabled)
            self.assertIn("unavailable", controller.view.notice)

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

    def test_ask_and_approval_answers_never_enter_prompt_history(self) -> None:
        for kind in ("ask", "approval"):
            with self.subTest(kind=kind):
                mailbox = TuiMailbox(capacity=8)
                worker = _FakeWorker()
                history = ComposerHistory(None)
                controller = TuiController(
                    mailbox,
                    TuiProjector(),
                    worker,  # type: ignore[arg-type]
                    composer_history=history,
                )
                mailbox.put(TuiInteractionPending("i1", InteractionRequest(kind, "Continue?")))
                controller.poll()
                for character in "yes":
                    controller.handle_key(character)
                controller.handle_key("ENTER")

                self.assertEqual(history.snapshot.local_entries, ())

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
        controller.handle_paste("draft\nsecond")
        controller.handle_key("LEFT")
        controller.handle_key("LEFT")
        expected_draft = (controller.view.input_text, controller.view.cursor)

        controller.handle_key("CTRL_F")
        for character in "alpha":
            controller.handle_key(character)
        controller.handle_key("ENTER")

        self.assertEqual(controller.view.search_query, "alpha")
        self.assertEqual((controller.view.input_text, controller.view.cursor), expected_draft)
        self.assertIn("1 transcript match", controller.view.notice)
        controller.handle_key("ESC")
        self.assertEqual(controller.view.search_query, "")

    def test_ctrl_r_search_accepts_without_submit_then_explicit_enter_submits(self) -> None:
        history = ComposerHistory(None)
        history.append("alpha older")
        history.append("alpha newest")
        worker = _FakeWorker()
        controller = TuiController(
            TuiMailbox(capacity=8),
            TuiProjector(),
            worker,  # type: ignore[arg-type]
            composer_history=history,
        )
        controller.handle_paste("multi\nline draft")
        controller.handle_key("LEFT")
        draft_cursor = controller.view.cursor

        controller.handle_key("CTRL_R")
        controller.handle_paste("ALPX")
        controller.handle_key("BACKSPACE")
        controller.handle_key("h")
        controller.handle_key("a")
        self.assertEqual(controller.view.history_search_match, "alpha newest")
        controller.handle_key("CTRL_R")
        self.assertEqual(controller.view.history_search_match, "alpha older")
        controller.handle_key("DOWN")
        self.assertEqual(controller.view.history_search_match, "alpha newest")
        controller.handle_key("UP")
        controller.handle_key("ENTER")

        self.assertEqual(controller.view.focus, "chat")
        self.assertEqual(controller.view.input_text, "alpha older")
        self.assertEqual(worker.submitted, [])
        controller.handle_key("ENTER")
        self.assertEqual(worker.submitted[0].payload, {"prompt": "alpha older"})
        self.assertEqual(history.snapshot.local_entries[-1], "alpha older")
        self.assertNotEqual(draft_cursor, len("multi\nline draft"))

    def test_history_search_cancel_restores_multiline_draft_and_interior_cursor(self) -> None:
        history = ComposerHistory(None)
        history.append("saved prompt")
        controller = TuiController(
            TuiMailbox(capacity=8),
            TuiProjector(),
            _FakeWorker(),  # type: ignore[arg-type]
            composer_history=history,
        )
        controller.handle_paste("first\nsecond")
        for _ in range(3):
            controller.handle_key("LEFT")
        expected = (controller.view.input_text, controller.view.cursor)
        controller.handle_key("CTRL_R")
        controller.handle_paste("saved")
        controller.handle_key("CTRL_C")

        self.assertEqual((controller.view.input_text, controller.view.cursor), expected)
        self.assertEqual(controller.view.focus, "chat")
        self.assertEqual(controller.view.history_search_status, "inactive")

    def test_history_search_guards_palette_transcript_search_interaction_and_inflight(self) -> None:
        history = ComposerHistory(None)
        history.append("saved")

        palette = TuiController(
            TuiMailbox(capacity=8), TuiProjector(), _FakeWorker(), composer_history=history  # type: ignore[arg-type]
        )
        palette.handle_key("CTRL_P")
        palette.handle_key("CTRL_R")
        self.assertTrue(palette.view.palette)
        self.assertEqual(palette.view.focus, "chat")

        transcript = TuiController(
            TuiMailbox(capacity=8), TuiProjector(), _FakeWorker(), composer_history=history  # type: ignore[arg-type]
        )
        transcript.handle_key("CTRL_F")
        transcript.handle_key("CTRL_R")
        self.assertEqual(transcript.view.focus, "search")

        mailbox = TuiMailbox(capacity=8)
        interaction = TuiController(
            mailbox, TuiProjector(), _FakeWorker(), composer_history=history  # type: ignore[arg-type]
        )
        mailbox.put(TuiInteractionPending("i1", InteractionRequest("ask", "Which?")))
        interaction.poll()
        interaction.handle_key("CTRL_R")
        self.assertEqual(interaction.view.focus, "ask")

        inflight = TuiController(
            TuiMailbox(capacity=8), TuiProjector(), _FakeWorker(), composer_history=history  # type: ignore[arg-type]
        )
        inflight.submit_initial_prompt("running")
        inflight.handle_key("CTRL_R")
        self.assertEqual(inflight.view.focus, "chat")

    def test_history_search_keeps_wheel_and_page_bound_to_transcript(self) -> None:
        history = ComposerHistory(None)
        history.append("saved")
        projector = TuiProjector()
        for index in range(80):
            projector.append_local("assistant", f"line {index}")
        controller = TuiController(
            TuiMailbox(capacity=8), projector, _FakeWorker(), composer_history=history  # type: ignore[arg-type]
        )
        controller.update_viewport(50, 12)
        bottom = controller.view.viewport.top
        controller.handle_key("CTRL_R")
        controller.handle_key("WHEEL_UP")
        wheel_top = controller.view.viewport.top
        controller.handle_key("PAGE_UP")

        self.assertLess(wheel_top, bottom)
        self.assertLess(controller.view.viewport.top, wheel_top)
        self.assertEqual(controller.view.focus, "history_search")

    def test_history_search_does_not_append_or_write_until_accepted_prompt_is_submitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            history = ComposerHistory(path)
            history.append("persisted prompt")
            before = path.read_bytes()
            controller = TuiController(
                TuiMailbox(capacity=8),
                TuiProjector(),
                _FakeWorker(),  # type: ignore[arg-type]
                composer_history=history,
            )
            controller.handle_key("CTRL_R")
            controller.handle_paste("persisted")
            controller.handle_key("UP")
            controller.handle_key("DOWN")
            controller.handle_key("ESC")

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(history.snapshot.local_entries, ("persisted prompt",))

    def test_workspace_rebind_result_clears_active_search_for_success_failure_and_missing_target(self) -> None:
        for status, payload in (
            ("error", {}),
            ("ok", {}),
            ("ok", {"state_dir": ""}),
        ):
            with self.subTest(status=status, payload=payload), tempfile.TemporaryDirectory() as tmp:
                history = ComposerHistory(Path(tmp) / "history.jsonl")
                history.append("saved")
                mailbox = TuiMailbox(capacity=8)
                controller = TuiController(
                    mailbox,
                    TuiProjector(),
                    _FakeWorker(),  # type: ignore[arg-type]
                    composer_history=history,
                )
                controller.handle_paste("draft")
                controller.handle_key("CTRL_R")
                controller.handle_paste("saved")
                command = new_command("MoveWorkspace", {"path": "/new"})
                mailbox.put(
                    TuiCommandCompleted(
                        command,
                        CommandResult(command.command_id, "s1", None, status, payload, "failed", "failed"),
                    )
                )
                controller.poll()

                self.assertEqual(controller.view.focus, "chat")
                self.assertEqual(controller.view.input_text, "draft")
                self.assertEqual(controller.view.history_search_status, "inactive")

    def test_pending_interaction_closes_history_search_before_borrowing_composer(self) -> None:
        history = ComposerHistory(None)
        history.append("saved")
        mailbox = TuiMailbox(capacity=8)
        controller = TuiController(
            mailbox, TuiProjector(), _FakeWorker(), composer_history=history  # type: ignore[arg-type]
        )
        controller.handle_paste("draft")
        controller.handle_key("CTRL_R")
        controller.handle_paste("saved")
        mailbox.put(TuiInteractionPending("i1", InteractionRequest("approval", "Continue?")))
        controller.poll()
        controller.handle_key("ESC")

        self.assertEqual(controller.view.focus, "chat")
        self.assertEqual(controller.view.input_text, "draft")
        self.assertEqual(controller.view.history_search_status, "inactive")

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
