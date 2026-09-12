import type { LiveAiModule, HostSnapshot, HostStatus } from './types.js';

export class Host {
  private status: HostStatus = 'created';
  private readonly startedModules: LiveAiModule[] = [];

  constructor(private readonly modules: readonly LiveAiModule[]) {}

  async start(): Promise<void> {
    if (this.status === 'ready') {
      return;
    }
    if (this.status !== 'created' && this.status !== 'failed') {
      throw new Error(`Cannot start host from status: ${this.status}`);
    }

    this.status = 'starting';
    try {
      for (const module of this.modules) {
        await module.start();
        this.startedModules.push(module);
      }
      this.status = 'ready';
    } catch (error) {
      this.status = 'failed';
      await this.stopStartedModules();
      throw error;
    }
  }

  async stop(): Promise<void> {
    if (this.status === 'stopped' || this.status === 'created') {
      this.status = 'stopped';
      return;
    }
    if (this.status === 'stopping') {
      return;
    }

    this.status = 'stopping';
    try {
      await this.stopStartedModules();
    } finally {
      this.status = 'stopped';
    }
  }

  snapshot(): HostSnapshot {
    return {
      status: this.status,
      startedModules: this.startedModules.map((module) => module.name),
    };
  }

  private async stopStartedModules(): Promise<void> {
    const modules = this.startedModules.splice(0).reverse();
    const failures: unknown[] = [];

    for (const module of modules) {
      try {
        await module.stop();
      } catch (error) {
        failures.push(error);
      }
    }

    if (failures.length > 0) {
      throw new AggregateError(failures, 'One or more modules failed to stop');
    }
  }
}
