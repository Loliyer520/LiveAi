export interface LiveAiModule {
  readonly name: string;
  start(): Promise<void>;
  stop(): Promise<void>;
}

export type HostStatus = 'created' | 'starting' | 'ready' | 'stopping' | 'stopped' | 'failed';

export interface HostSnapshot {
  readonly status: HostStatus;
  readonly startedModules: readonly string[];
}
