from __future__ import annotations

import unittest

from local_agent.run_context import RunContext
from local_agent.runtime_tool_directive import RuntimeToolDirectivePhase
from local_agent.temporary_tool_directive import MAX_DIRECTIVE_ATTEMPTS_PER_SOURCE
from local_agent.temporary_tool_directive import TemporaryToolDirectiveOwner


class TemporaryToolDirectiveOwnerTests(unittest.TestCase):
    def test_allowed_success_resolves_the_directive(self) -> None:
        owner = TemporaryToolDirectiveOwner()
        owner.activate("negative_existence", {"glob_files"})
        owner.begin_turn()
        attempt = owner.reserve_attempt("glob_files")
        owner.record_attempt_outcome(attempt, is_error=False)

        transition = owner.finish_turn()

        self.assertIsNotNone(transition)
        self.assertEqual(transition.status, "resolved")
        self.assertIsNone(owner.active_allowed_tools)

    def test_schema_violating_success_never_resolves_a_glob_only_directive(self) -> None:
        owner = TemporaryToolDirectiveOwner()
        owner.activate("negative_existence", {"glob_files"})
        owner.begin_turn()
        attempt = owner.reserve_attempt("read_file")
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.reason, "tool_not_allowed")

        # A gateway/provider defect must not convert an out-of-schema result
        # into evidence that satisfied the directive.
        owner.record_attempt_outcome(attempt, is_error=False)
        transition = owner.finish_turn()

        self.assertIsNotNone(transition)
        self.assertEqual(transition.status, "rejected")
        self.assertEqual(transition.reason, "tool_error")

    def test_last_allowed_attempt_executes_then_forces_one_truthful_final(self) -> None:
        runtime = _RuntimeStub()
        phase = RuntimeToolDirectivePhase(runtime)
        phase.begin_run()
        phase.apply_steering("negative_existence", {"glob_files"})
        phase.before_model_turn()

        for _ in range(MAX_DIRECTIVE_ATTEMPTS_PER_SOURCE):
            attempt = phase.before_tool_attempt("glob_files")
            self.assertIsNotNone(attempt)
            phase.after_tool_attempt(attempt, tool_name="glob_files", is_error=True)

        self.assertEqual(runtime.final_requests, 1)
        self.assertIsNone(runtime._run.temporary_tool_allowlist)
        # The subsequent no-tool model turn cannot enqueue the same final
        # rewrite again.
        phase.after_model_turn()
        self.assertEqual(runtime.final_requests, 1)

    def test_error_does_not_resolve_and_next_turn_restores_regular_schema(self) -> None:
        owner = TemporaryToolDirectiveOwner()
        owner.activate("negative_existence", {"glob_files"})
        owner.begin_turn()
        attempt = owner.reserve_attempt("glob_files")
        owner.record_attempt_outcome(attempt, is_error=True)

        transition = owner.finish_turn()

        self.assertEqual(transition.status, "rejected")
        self.assertIsNone(owner.active_allowed_tools)


class _SessionStub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def append(self, kind: str, payload: dict[str, object]) -> None:
        self.events.append((kind, payload))


class _RuntimeStub:
    def __init__(self) -> None:
        self._run = RunContext()
        self._session = _SessionStub()
        self.final_requests = 0

    def _apply_final_answer_steering(self, _decision) -> bool:
        self.final_requests += 1
        return True


if __name__ == "__main__":
    unittest.main()
