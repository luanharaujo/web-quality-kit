export const CI_FLOOR_MS: number;
export function inCI(): boolean;
export function ciTimeout(local: number, floor?: number): number;
export function vitestTimeouts(): { testTimeout: number; hookTimeout: number; teardownTimeout: number };
export function playwrightTimeouts(): { timeout: number; expect: { timeout: number } };
