/**
 * Thin fetch wrapper. Same signature as fetch, but always includes credentials
 * and intercepts 401 responses to trigger session logout.
 */
import { clearSession } from '$lib/session';

/** fetch() with credentials: 'include'. Auto-clears session on 401. */
export async function fetchHandler(path: string, init?: RequestInit): Promise<Response> {
	const res = await fetch(path, {
		...init,
		credentials: 'include'
	});
	// 401 on non-auth endpoints means the session expired; auto-logout.
	// Auth endpoints (login, session check) naturally return 401; don't intercept those.
	if (res.status === 401 && !path.startsWith('/api/auth') && !path.startsWith('/api/config')) {
		clearSession();
	}
	return res;
}

/** fetch() + JSON parse. Throws ApiError on non-2xx. */
export async function fetchJSON<T = unknown>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetchHandler(path, init);
	if (!res.ok) {
		// Authentication responses can contain server-side diagnostics. Never let
		// those details become an error message that a panel might render.
		if (res.status === 401) {
			throw new ApiError(res.status, SESSION_EXPIRED_MESSAGE);
		}
		const data = await res.json().catch(() => ({}));
		throw new ApiError(res.status, data.detail || data.error || data.message || res.statusText);
	}
	return res.json();
}

export class ApiError extends Error {
	status: number;
	constructor(status: number, message: string) {
		super(message);
		this.status = status;
	}
}

export const SESSION_EXPIRED_MESSAGE = 'Your session expired. Sign in again, then retry.';

export function isSessionExpired(error: unknown): boolean {
	return error instanceof ApiError && error.status === 401;
}

export function getRecoveryMessage(error: unknown, fallback: string): string {
	return isSessionExpired(error) ? SESSION_EXPIRED_MESSAGE : fallback;
}

/** Shorthand for JSON POST/PUT body. */
export const jsonBody = (data: unknown): RequestInit => ({
	method: 'POST',
	headers: { 'Content-Type': 'application/json' },
	body: JSON.stringify(data)
});
