import test from 'node:test';
import assert from 'node:assert/strict';
import { InMemoryEventMailbox } from '../src/scope/mailbox.js';
import { EventEnvelope } from '../src/scope/envelope.js';
import { AtomicTurnBatchCoordinator, mergeFollowupItems } from '../src/scope/coordinator.js';
import { CharacterSessionRegistry } from '../src/scope/session.js';
import { ScopeActorDispatcher } from '../src/scope/actor.js';
import { envelopeFromTurnItem, turnItemFromBatch, type TurnItem } from '../src/scope/turn.js';
import type { ChatMessage } from '../src/chat/types.js';

function makeMessage(chatId: number, text: string, userId = 100): ChatMessage {
  return {
    chatType: 'group',
    chatId,
    userId,
    text,
    rawMessage: text,
    sender: { nickname: `user${userId}`, user_id: userId },
    messageId: 1000 + chatId,
    mentionsSelf: false,
    timestamp: Date.now() / 1000,
    rawData: { source: 'qq_group_message' },
  };
}

function makeItem(chatId: number, text: string, epoch = 1): TurnItem {
  const message = makeMessage(chatId, text);
  return {
    kind: 'message',
    message,
    cleaned: text,
    agentId: 'agent_test',
    scopeKey: `group:${chatId}`,
    deferredCount: 0,
    triggerMessages: [{
      user_id: message.userId,
      nickname: 'user100',
      text,
      raw_message: text,
      message_id: message.messageId,
      message_ref: 'AAAA',
      timestamp: message.timestamp,
      source_label: 'QQ群消息',
      source_kind: 'group',
    }],
    messageEpoch: epoch,
    historySeed: null,
    silentEvent: false,
  };
}

function submit(mailbox: InMemoryEventMailbox, item: TurnItem): void {
  mailbox.append(envelopeFromTurnItem(item), item);
}

test('mailbox keeps FIFO order per scope and clears empty queues', () => {
  const mailbox = new InMemoryEventMailbox();
  submit(mailbox, makeItem(1, 'a'));
  submit(mailbox, makeItem(1, 'b'));
  assert.equal(mailbox.pendingCount('group:1'), 2);
  const first = mailbox.popScopeEntry('group:1');
  assert.equal(first?.transient !== null ? (first?.transient as TurnItem).cleaned : '', 'a');
  mailbox.popScopeEntry('group:1');
  assert.equal(mailbox.pendingCount('group:1'), 0);
});

test('mailbox sequences are monotonic and requeueFront parks the scope', () => {
  const mailbox = new InMemoryEventMailbox();
  submit(mailbox, makeItem(1, 'a'));
  submit(mailbox, makeItem(1, 'b'));
  const popped = mailbox.popScopeEntry('group:1');
  assert.equal((popped?.transient as TurnItem).cleaned, 'a');
  mailbox.requeueFront(popped!.envelope, popped!.transient, 1, 50);
  // Head is under backoff: pop and drain both refuse to bypass FIFO.
  assert.equal(mailbox.popScopeEntry('group:1'), null);
  assert.equal(mailbox.drainScope('group:1'), null);
  assert.equal(mailbox.pendingCount('group:1'), 2);
});

test('coordinator drains one scope into a single merged followup turn', () => {
  const mailbox = new InMemoryEventMailbox();
  const coordinator = new AtomicTurnBatchCoordinator(mailbox);
  submit(mailbox, makeItem(7, 'one'));
  submit(mailbox, makeItem(7, 'two'));
  submit(mailbox, makeItem(8, 'other-scope'));

  const batch = coordinator.drainScopeFollowup('group:7', [], { source: 'turn-complete' }, () => false);
  assert.ok(batch !== null);
  assert.equal(batch.turnItem.scopeKey, 'group:7');
  assert.equal(batch.turnItem.triggerMessages.length, 2);
  // Representative = latest live entry.
  assert.equal(batch.turnItem.cleaned, 'two');
  // Other scope untouched.
  assert.equal(mailbox.pendingCount('group:8'), 1);
});

test('coordinator drops stale entries and returns null when all are stale', () => {
  const mailbox = new InMemoryEventMailbox();
  const coordinator = new AtomicTurnBatchCoordinator(mailbox);
  const stale = makeItem(7, 'old');
  stale.message!.timestamp = Date.now() / 1000 - 9999;
  submit(mailbox, stale);
  const batch = coordinator.drainScopeFollowup(
    'group:7',
    [],
    null,
    (item) => item.message !== null && item.message.timestamp < Date.now() / 1000 - 100,
  );
  assert.equal(batch, null);
});

test('mergeFollowupItems sums deferred counts and dedupes triggers', () => {
  const one = makeItem(7, 'one');
  one.deferredCount = 1;
  const two = makeItem(7, 'two');
  two.deferredCount = 1;
  const duplicateOfTwo = makeItem(7, 'two');
  const merged = mergeFollowupItems('group:7', one, two, duplicateOfTwo, null);
  assert.ok(merged !== null);
  assert.equal(merged.deferredCount, 3);
  assert.equal(merged.triggerMessages.length, 2);
  assert.equal(merged.cleaned, 'two');
  assert.equal(merged.batchItems!.length, 3);
});

test('turnItemFromBatch picks last representative and accumulates triggers', () => {
  const mailbox = new InMemoryEventMailbox();
  submit(mailbox, makeItem(7, 'one'));
  submit(mailbox, makeItem(7, 'two'));
  const snapshot = mailbox.drainScope('group:7');
  assert.ok(snapshot !== null);
  const merged = turnItemFromBatch(snapshot);
  assert.equal(merged.cleaned, 'two');
  assert.equal(merged.triggerMessages.length, 2);
  assert.equal(merged.deferredCount, 2);
});

test('actor processes scope items strictly in order, scopes in parallel', async () => {
  const mailbox = new InMemoryEventMailbox();
  const sessions = new CharacterSessionRegistry(mailbox);
  const order: string[] = [];
  let release: (() => void) | null = null;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });

  const dispatcher = new ScopeActorDispatcher({
    mailbox,
    sessions,
    consume: async (scopeKey, item) => {
      if (item.cleaned === 'slow-first') await gate;
      order.push(`${scopeKey}:${item.cleaned}`);
    },
  });

  dispatcher.submitEvent(envelopeFromTurnItem(makeItem(1, 'slow-first')), makeItem(1, 'slow-first'));
  dispatcher.submitEvent(envelopeFromTurnItem(makeItem(1, 'second')), makeItem(1, 'second'));
  dispatcher.submitEvent(envelopeFromTurnItem(makeItem(2, 'parallel')), makeItem(2, 'parallel'));

  await sleep(30);
  // group:2 finished while group:1 was blocked on the gate; "second" did not jump ahead.
  assert.deepEqual(order, ['group:2:parallel']);
  release!();
  await sleep(30);
  assert.deepEqual(order, ['group:2:parallel', 'group:1:slow-first', 'group:1:second']);
  await dispatcher.close();
});

test('actor skips stale items', async () => {
  const mailbox = new InMemoryEventMailbox();
  const sessions = new CharacterSessionRegistry(mailbox);
  const seen: string[] = [];
  const dispatcher = new ScopeActorDispatcher({
    mailbox,
    sessions,
    consume: async (_scopeKey, item) => {
      seen.push(String(item.cleaned));
    },
    isStale: (item) => item.cleaned === 'stale',
  });
  dispatcher.submitEvent(envelopeFromTurnItem(makeItem(3, 'stale')), makeItem(3, 'stale'));
  dispatcher.submitEvent(envelopeFromTurnItem(makeItem(3, 'fresh')), makeItem(3, 'fresh'));
  await sleep(30);
  assert.deepEqual(seen, ['fresh']);
  await dispatcher.close();
});

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
