# Active scope ownership migration assessment

## Current state

The runtime currently has four independent scheduling owners:

- `_active_scope_turns`: active scope ownership.
- `_event_mailbox`: pending message/event ownership.
- `_pending_scope_tasks`: deferred task ownership.
- `queue`: process worker dispatch.

`CharacterSessionRegistry` remains shadow-only. `RuntimeScopeObserver` reads the four current owners on demand and does not mutate either runtime or session state. Existing status/WebUI responses remain unchanged.

## Consistency boundary of shadow observations

There is no common lock spanning the active set, mailbox, task dictionary and `asyncio.Queue`. A runtime observation therefore performs two best-effort samples:

- `consistent=true` means the two consecutive samples matched.
- It does **not** claim a transactional snapshot against concurrent mutation.
- `runtime_queue_size` is process-global and is not a per-scope count.
- Observation never creates a `CharacterSession`, reserves a scope, appends an event, promotes a task or clears state.

This boundary is intentional; adding a common observation lock now would alter lock ordering and production behavior.

## Minimum active-owner-only migration slice

A future first ownership migration may move only `_active_scope_turns` into `CharacterSession.active`. It must leave these owners unchanged:

- `_event_mailbox`
- `_pending_scope_tasks`
- runtime `queue`
- `AtomicTurnBatchCoordinator`

The minimum production replacement surface is limited to:

1. `_scope_turn_is_active(scope_key)`
2. `_activate_scope_turn(scope_key)`
3. `_deactivate_scope_turn(scope_key)`
4. active-state clearing inside `_clear_scope_turn_coordination()`
5. initialization/removal of `_active_scope_turns`
6. `RuntimeScopeObserver` active-state source

All callers remain unchanged, including message reserve/release, task reserve/release, Agent report idle checks, self-message interrupts, debounce and status text.

## Required compatibility semantics

### Single consumer

`activate()` must atomically return false when another message or task owns the same scope. Message and task reservation must continue to share the same active bit.

### Message and task ordering

Moving the active bit must not move task or mailbox ownership. Message-before-task promotion remains implemented by the existing coordinator methods.

### Completed-turn batch handoff

A live batch keeps the session active. All-stale or empty batch promotes a task first; only an empty mailbox with no promoted task deactivates the session.

### Tool raw pop

Tool-loop raw pop remains a direct one-entry mailbox operation. Active ownership migration must not introduce batch drain or stale filtering into that path.

### Agent reports

`only_if_idle` continues to check active state only. Pending events without an active owner do not become equivalent to active for Agent report requeue decisions.

### Cancellation and epoch reset

`_cancel_active_requests()` must clear all active session bits while separately clearing mailbox and task owners through their existing paths. Clearing is not retirement: scopes can be activated again after cancellation.

### Retirement

Registry `discard_if_idle()` is lifecycle cleanup, not normal scope release. Normal `_deactivate_scope_turn()` must not retire or remove sessions. Retirement is only safe when active=false and both current mailbox/task owners are empty.

## Lock-order requirement

The current session order is:

`registry lock -> session lock -> mailbox lock`

An active-owner-only migration should call `registry.get_or_create()` before entering session methods and must not hold mailbox or task locks while acquiring the registry lock. Existing mailbox/task code has no shared coordinator lock, so the migration must not add cross-owner locking.

## Test matrix before cutover

- Message reserve succeeds once and subsequent reserve defers.
- Task reserve and message reserve contend for the same session active bit.
- Same scope message/task serialization; different scopes remain independent.
- Live batch retains active ownership.
- Empty/all-stale batch promotes task before deactivate.
- Tool raw pop does not change active ownership.
- Agent report active-only behavior remains unchanged.
- Debounce busy remains active OR pending event.
- Status text reports generating from session active and pending count from mailbox.
- Cancellation clears active state and permits reactivation.
- Stale references cannot activate a retired session.
- Runtime observer and session snapshot agree after the active source is switched.
- No `_active_scope_turns` production references remain after cutover.
- `_event_mailbox`, `_pending_scope_tasks`, queue and status/WebUI schemas are unchanged.

## Rollback boundary

The active-owner cutover must be one atomic change across `ai_runtime.py`, observer construction and owner-specific fixtures. On any failure, restore only those files. CharacterSession, Mailbox, batch coordinator and task owner remain intact. No migration should proceed until the pre-cutover equivalence harness exercises both active-set and session-backed implementations with identical observable results.
