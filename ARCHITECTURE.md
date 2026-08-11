# Bot Architecture

## Single-process runtime

- `main.py` is the only production entrypoint.
- `pack/napcat.py` owns the NapCat WebSocket receive loop and direct outbound HTTP actions in the same process.
- `SatangyunModule` and `AIOrchestrator` are registered against that same `NapcatBot` instance.
- Per-QQ scope ordering and cross-scope concurrency remain enforced by the existing AI runtime; this change does not replace the agent executor.

## Compatibility code

`core/transport.py` may remain as a protocol/test adapter. Durable inbox/outbox stores and the old receiver/sender service code are not part of the production startup path and must not be configured as runtime owners.

## Operations

Start only:

```bash
/my/bot.sh start
```

or:

```bash
python main.py
```

Do not start a separate receiver or sender process. NapCat must have one WebSocket consumer and one direct-send owner.
