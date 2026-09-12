/**
 * Thin fetch wrapper. Same signature as fetch, but always includes credentials
 * and intercepts 401 responses to trigger session logout.
 */
import { clearSession } from '$lib/session';

let sessionValidation: Promise<boolean> | null = null;

async function browserSessionIsInvalid(): Promise<boolean> {
	if (!sessionValidation) {
		sessionValidation = fetch('/api/auth', { credentials: 'include' })
			.then(async (response) => {
				if (!response.ok) return response.status === 401;
				const payload = (await response.json().catch(() => null)) as {
					authenticated?: boolean;
				} | null;
				return payload?.authenticated === false;
			})
			.catch(() => false)
			.finally(() => {
				sessionValidation = null;
			});
	}
	return sessionValidation;
}

/** fetch() with credentials: 'include'. Clears only a confirmed-invalid browser session. */
export async function fetchHandler(path: string, init?: RequestInit): Promise<Response> {
	const res = await fetch(path, {
		...init,
		credentials: 'include'
	});
	// Secondary APIs can have their own authentication boundary (for example Control API
	// bearer scopes). A 401 there must not destroy a still-valid browser session.
	if (
		res.status === 401 &&
		!path.startsWith('/api/auth') &&
		!path.startsWith('/api/config') &&
		(await browserSessionIsInvalid())
	) {
		clearSession();
	}
	return res;
}

/** fetch() + JSON parse. Throws ApiError on non-2xx. */
export async function fetchJSON<T = unknown>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetchHandler(path, init);
	if (!res.ok) {
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

/** Shorthand for JSON POST/PUT body. */
export const jsonBody = (data: unknown): RequestInit => ({
	method: 'POST',
	headers: { 'Content-Type': 'application/json' },
	body: JSON.stringify(data)
});
