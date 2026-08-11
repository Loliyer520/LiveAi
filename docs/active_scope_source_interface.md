# Runtime active-state source interface

## Problem found during cutover rehearsal

The runtime observer originally accepted a concrete `set[str]` and used membership directly. Replacing the active owner with `CharacterSessionRegistry` therefore changed an implicit dependency in two places:

- runtime coordination needed mutating operations (`activate`, `deactivate`, `clear`),
- observation only needed a read predicate (`is_active`).

Making the registry imitate a full set would couple observation and mutation again and would add unnecessary collection semantics. The failed rehearsal was rolled back; `_active_scope_turns` remains the sole production active owner.

## Recommended interface split

Use two explicit ports rather than accepting a set-like object.

### Read port

```python
ActiveScopeReader = Callable[[str], bool]
```

`RuntimeScopeObserver` receives `is_active: Callable[[str], bool]` and calls it during both samples. Existing runtime passes:

```python
lambda scope_key: scope_key in self._active_scope_turns
```

After a future cutover it passes:

```python
self._character_sessions.is_active
```

No collection protocol or owner mutation is exposed to the observer.

### Write port

Runtime coordination continues to call its existing wrapper methods:

- `_scope_turn_is_active`
- `_activate_scope_turn`
- `_deactivate_scope_turn`
- `_clear_scope_turn_coordination`

Only those wrappers are changed atomically during cutover. `CharacterSessionRegistry` may expose narrowly scoped methods:

- `is_active(scope_key)`
- `try_activate(scope_key) -> bool`
- `deactivate(scope_key)`
- `clear_active()`

`clear_active()` clears only active bits. It must not clear mailbox events, pending tasks, retire sessions or remove registry entries.

## Lock order

The active-only cutover must preserve:

`registry lock -> session lock`

It must never acquire mailbox/task locks while holding the registry lock. Mailbox, pending task and queue owners remain untouched, so no new cross-owner lock is required.

## Required test matrix

1. Reader adapter: set-backed and registry-backed predicates produce identical active observations.
2. Message reserve: first activation succeeds, same-scope second activation defers.
3. Task reserve: message and task contend for the same active bit.
4. Different scopes activate independently under concurrent calls.
5. Normal release deactivates without retirement.
6. Exception and real `CancelledError` run final release once.
7. Epoch-stale completion uses legacy release semantics.
8. Live batch keeps active ownership.
9. All-stale/empty batch promotes task before deactivation.
10. Tool raw pop does not mutate active state.
11. Agent report `only_if_idle` remains active-only.
12. Debounce remains active OR pending event.
13. Cancellation/shutdown calls `clear_active()` and permits reactivation.
14. Runtime observer switches only its active predicate; mailbox/task/queue sources remain unchanged.
15. Status/WebUI fields remain unchanged.
16. Post-cutover grep shows no `_active_scope_turns` production reference.
17. `_event_mailbox`, `_pending_scope_tasks`, queue and batch coordinator references are unchanged.
18. No durable inbox symbols exist.

## Rollback boundary

The next attempt must back up and atomically restore:

- `core/ai_runtime.py`
- `core/runtime_scope_observer.py`
- `core/character_session.py`
- all fixtures that instantiate `AIOrchestrator` via `object.__new__`

Any compile, interface, semantic or fixture failure restores the full set before further changes. The stable rollback baseline is 126/126 tests.
