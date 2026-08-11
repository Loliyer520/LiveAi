from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from core.event_adapters import scope_turn_item_from_batch
from core.event_mailbox import EventBatch, InMemoryEventMailbox, MailboxEntry


@dataclass(frozen=True)
class CompletedTurn:
    """Proof that the current turn has finished all persistence work.

    The coordinator deliberately accepts this type instead of a boolean so a
    future runtime integration has one explicit call site after assistant,
    tool and turn metadata commits have completed.
    """

    scope_key: str
    history_seed: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not str(self.scope_key or '').strip():
            raise ValueError('scope_key must be non-empty')


@dataclass(frozen=True)
class CoordinatedBatch:
    """One atomic mailbox snapshot prepared for a normal follow-up turn."""

    snapshot: EventBatch
    entries: tuple[MailboxEntry, ...]
    representative: Any
    turn_item: dict[str, Any]


class AtomicTurnBatchCoordinator:
    """Pure coordination policy; it is not wired into AIOrchestrator yet.

    All follow-up turns atomically drain one scope snapshot and apply stale
    filtering. The runtime can reuse the same policy both after a completed
    turn and during a live tool loop, so mailbox ingestion does not fork into
    separate "raw pop" and "batch drain" state machines.
    """

    def __init__(self, mailbox: InMemoryEventMailbox) -> None:
        if not isinstance(mailbox, InMemoryEventMailbox):
            raise TypeError('mailbox must be an InMemoryEventMailbox')
        self._mailbox = mailbox

    def drain_after_completed_turn(
        self,
        completed_turn: CompletedTurn,
        *,
        is_stale: Callable[[Any], bool],
    ) -> CoordinatedBatch | None:
        if not isinstance(completed_turn, CompletedTurn):
            raise TypeError('completed_turn must be a CompletedTurn')
        if not callable(is_stale):
            raise TypeError('is_stale must be callable')

        return self.drain_scope_followup(
            completed_turn.scope_key,
            history_seed=completed_turn.history_seed,
            metadata=completed_turn.metadata,
            is_stale=is_stale,
        )

    def drain_scope_followup(
        self,
        scope_key: str,
        *,
        history_seed: tuple[Mapping[str, Any], ...] = (),
        metadata: Mapping[str, Any] | None = None,
        is_stale: Callable[[Any], bool],
    ) -> CoordinatedBatch | None:
        """Drain one scope into a single follow-up turn item.

        `history_seed` and `metadata` are caller-owned contextual additions.
        They are attached to the merged follow-up item without mutating the
        mailbox transient objects, so the same coordinator can be used both for
        "completed turn handoff" and "mid-turn tool-result follow-up".
        """
        scope_key = str(scope_key or '').strip()
        if not scope_key:
            raise ValueError('scope_key must be non-empty')
        if not callable(is_stale):
            raise TypeError('is_stale must be callable')

        snapshot = self._mailbox.drain_scope(scope_key)
        if snapshot is None:
            return None

        live_entries = tuple(
            entry
            for entry in snapshot.entries
            if not is_stale(entry.transient)
        )
        if not live_entries:
            return None

        live_batch = EventBatch(
            scope_key=snapshot.scope_key,
            events=tuple(entry.envelope for entry in live_entries),
            transients=tuple(entry.transient for entry in live_entries),
        )
        turn_item = scope_turn_item_from_batch(live_batch)

        # Deterministic normal-follow-up rules:
        # - latest live FIFO entry is the representative turn/message;
        # - trigger_messages remain FIFO via the adapter;
        # - the just-completed turn's history seed takes precedence;
        # - coordinator metadata is additive and does not mutate transients.
        representative = live_entries[-1].transient
        if representative is not None:
            turn_item['message'] = representative.get('message', turn_item.get('message'))
            turn_item['cleaned'] = representative.get('cleaned', turn_item.get('cleaned'))
            turn_item['agent_id'] = representative.get('agent_id', turn_item.get('agent_id'))
            turn_item['kind'] = representative.get('kind', turn_item.get('kind'))
        if history_seed:
            turn_item['history_seed'] = [dict(entry) for entry in history_seed]
        if metadata is not None:
            turn_item['turn_metadata'] = dict(metadata or {})
        turn_item['batch_metadata'] = {
            'event_count': len(live_entries),
            'event_ids': [entry.envelope.event_id for entry in live_entries],
            'sequences': [entry.envelope.mailbox_sequence for entry in live_entries],
            'first_sequence': live_entries[0].envelope.mailbox_sequence,
            'last_sequence': live_entries[-1].envelope.mailbox_sequence,
        }
        return CoordinatedBatch(
            snapshot=snapshot,
            entries=live_entries,
            representative=representative,
            turn_item=turn_item,
        )

    def pop_tool_raw(self, scope_key: str) -> Any:
        """Preserve the existing tool-loop contract: one raw FIFO identity."""
        entry = self._mailbox.pop_scope_entry(scope_key)
        if entry is None:
            return None
        return entry.transient
