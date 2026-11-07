/**
 * Protocol-level errors shared by the streaming and non-streaming paths.
 *
 * Lives in its own module so `protocol.ts` and `stream.ts` can both use it
 * without an import cycle.
 */
export class ProtocolError extends Error {
  constructor(
    message: string,
    readonly status: number | undefined,
    readonly body: string | undefined,
    readonly transient: boolean,
  ) {
    super(message);
    this.name = 'ProtocolError';
  }
}
