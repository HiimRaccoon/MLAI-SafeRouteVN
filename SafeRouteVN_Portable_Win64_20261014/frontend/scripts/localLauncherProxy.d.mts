import type { Plugin } from 'vite';
import type { IncomingMessage, ServerResponse } from 'node:http';
export function localLauncherProxy(options: {
  accessFile: string; authFile?: string; origin?: string; backend?: string;
}): Plugin & { middleware(req: IncomingMessage, res: ServerResponse, next: () => void): void };
