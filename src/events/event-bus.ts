export type EventMap = object;
export type EventHandler<T> = (event: T) => void | Promise<void>;

export class EventBus<Events extends EventMap> {
  private readonly handlers = new Map<keyof Events, Set<EventHandler<unknown>>>();

  subscribe<K extends keyof Events>(eventName: K, handler: EventHandler<Events[K]>): () => void {
    const handlers = this.handlers.get(eventName) ?? new Set<EventHandler<unknown>>();
    handlers.add(handler as EventHandler<unknown>);
    this.handlers.set(eventName, handlers);

    return () => {
      handlers.delete(handler as EventHandler<unknown>);
      if (handlers.size === 0) {
        this.handlers.delete(eventName);
      }
    };
  }

  async publish<K extends keyof Events>(eventName: K, event: Events[K]): Promise<void> {
    const handlers = [...(this.handlers.get(eventName) ?? [])];
    await Promise.allSettled(handlers.map((handler) => Promise.resolve().then(() => handler(event))));
  }
}
