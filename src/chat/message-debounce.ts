/** Per-conversation trailing-edge quiet window for incoming user messages. */
export class MessageDebounce {
  private readonly windows = new Map<string, {
    timer: ReturnType<typeof setTimeout>;
    ready: Promise<void>;
    resolve: () => void;
  }>();

  constructor(private readonly delayMs = 2_000) {}

  touch(scopeKey: string): void {
    if (this.delayMs <= 0) return;
    const previous = this.windows.get(scopeKey);
    if (previous) clearTimeout(previous.timer);
    const deferred = Promise.withResolvers<void>();
    const ready = previous?.ready ?? deferred.promise;
    const resolve = previous?.resolve ?? deferred.resolve;
    const timer = setTimeout(() => {
      this.windows.delete(scopeKey);
      resolve();
    }, this.delayMs);
    this.windows.set(scopeKey, { timer, ready, resolve });
  }

  wait(scopeKey: string): Promise<void> | null {
    return this.windows.get(scopeKey)?.ready ?? null;
  }

  clear(): void {
    for (const window of this.windows.values()) {
      clearTimeout(window.timer);
      window.resolve();
    }
    this.windows.clear();
  }
}
