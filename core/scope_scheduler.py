from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from core.character_session import CharacterSession, CharacterSessionRegistry
from core.event_batch_coordinator import AtomicTurnBatchCoordinator, CompletedTurn
from core.event_envelope import EventEnvelope


@dataclass(frozen=True)
class ScopeHandoff:
    followup: dict[str, Any] | None = None
    promoted_task: dict[str, Any] | None = None
    released: bool = False


class ScopeScheduler:
    """Process-local scheduling policy for one consumer per scope.

    This module contains no model, provider, persistence or transport behavior.
    It is an independently testable target for a later orchestrator migration.
    """

    def __init__(self, registry: CharacterSessionRegistry | None = None) -> None:
        self.registry = registry or CharacterSessionRegistry()

    def session(self, scope_type: str, scope_id: str) -> CharacterSession:
        return self.registry.get_or_create(scope_type, scope_id)

    def reserve_or_append(
        self,
        envelope: EventEnvelope,
        *,
        transient: dict[str, Any],
    ) -> bool:
        session = self.session(envelope.scope_type, envelope.scope_id)
        if session.activate():
            return True
        session.append_event(envelope, transient=transient)
        return False

    def append_while_active(
        self,
        envelope: EventEnvelope,
        *,
        transient: dict[str, Any],
    ) -> EventEnvelope:
        session = self.session(envelope.scope_type, envelope.scope_id)
        return session.append_event(envelope, transient=transient)

    def append_task(self, scope_type: str, scope_id: str, task: dict[str, Any]) -> int:
        return self.session(scope_type, scope_id).append_task(task)

    def pop_tool_raw(self, scope_type: str, scope_id: str) -> Any:
        entry = self.session(scope_type, scope_id).pop_raw_entry()
        return None if entry is None else entry.transient

    def handoff_completed_turn(
        self,
        completed_turn: CompletedTurn,
        *,
        is_stale: Callable[[Any], bool],
    ) -> ScopeHandoff:
        identity = completed_turn.scope_key.split(':', 1)
        if len(identity) != 2:
            raise ValueError('completed turn scope_key must be <scope_type>:<scope_id>')
        session = self.session(identity[0], identity[1])
        coordinator = AtomicTurnBatchCoordinator(session.mailbox)
        batch = coordinator.drain_after_completed_turn(completed_turn, is_stale=is_stale)
        if batch is not None:
            batch.turn_item['scope_key'] = session.scope_key
            return ScopeHandoff(followup=batch.turn_item)
        promoted_task = session.promote_task_if_mailbox_empty()
        if promoted_task is not None:
            return ScopeHandoff(promoted_task=promoted_task)
        session.deactivate()
        return ScopeHandoff(released=True)

    def clear_runtime_state(self) -> None:
        self.registry.clear_runtime_state()
