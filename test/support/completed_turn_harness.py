from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.event_batch_coordinator import AtomicTurnBatchCoordinator, CompletedTurn


@dataclass
class HarnessResult:
    followup: dict[str, Any] | None
    promoted_task: dict[str, Any] | None
    scope_released: bool


class CompletedTurnIntegrationHarness:
    """Offline model of the future post-commit handoff.

    This harness intentionally does not import AIOrchestrator. It demonstrates
    the single-consumer transaction required at the future integration point:
    after the current turn's outbound/history and turn log are committed, drain
    the mailbox exactly once; only promote a task when no live batch remains.
    """

    def __init__(self, coordinator: AtomicTurnBatchCoordinator) -> None:
        self.coordinator = coordinator
        self.pending_tasks: dict[str, list[dict[str, Any]]] = {}
        self.commit_trace: list[str] = []
        self.followup_runs: list[dict[str, Any]] = []

    def commit_current_turn(self, *, outbound: bool, turn_log: bool, metadata: bool) -> None:
        if outbound:
            self.commit_trace.append('outbound_history')
        if turn_log:
            self.commit_trace.append('turn_log')
        if metadata:
            self.commit_trace.append('turn_metadata')

    def handoff(self, completed: CompletedTurn, *, is_stale) -> HarnessResult:
        required = {'outbound_history', 'turn_log', 'turn_metadata'}
        if not required.issubset(self.commit_trace):
            raise RuntimeError('completed turn persistence is incomplete')
        batch = self.coordinator.drain_after_completed_turn(completed, is_stale=is_stale)
        if batch is not None:
            self.followup_runs.append(batch.turn_item)
            return HarnessResult(batch.turn_item, None, False)
        tasks = self.pending_tasks.get(completed.scope_key) or []
        if tasks:
            promoted = tasks.pop(0)
            return HarnessResult(None, promoted, False)
        return HarnessResult(None, None, True)


def existing_turn_log_metadata(
    *,
    agent_id: str,
    temperature: float,
    turn_meta: dict[str, Any],
    tool_iterations: list[dict[str, Any]],
    generation_ms: int | None,
    note: str | None = None,
) -> dict[str, Any]:
    """Mirror the existing repository turn-log metadata fields, not a new schema."""
    return {
        'agent_id': agent_id,
        'temperature': temperature,
        'turn_meta': dict(turn_meta),
        'tool_iterations': [dict(item) for item in tool_iterations],
        'generation_ms': generation_ms,
        'note': note,
    }


def assert_trigger_context_preserved(trigger_messages: list[dict[str, Any]]) -> None:
    """Validate fields consumed by current image/source/reply-context paths."""
    required = {
        'text',
        'raw_message',
        'message_id',
        'source_label',
        'source_kind',
        'raw_source',
    }
    for entry in trigger_messages:
        missing = required - set(entry)
        if missing:
            raise ValueError(f'trigger context fields missing: {sorted(missing)}')
