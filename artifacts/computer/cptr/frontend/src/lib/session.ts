/**
 * Session management: user state + 401 handling.
 *
 * No client-side expiry timers; the server is the source of truth.
 * If any API call returns 401, we clear the session and reload.
 */

import { writable } from 'svelte/store';

export interface Session {
	user_id: string;
	username: string;
	display_name?: string | null;
	role: string;
	profile_image_url?: string | null;
}

export const session = writable<Session | null>(null);

/**
 * Set session after successful auth check.
 */
export function setSession(s: Session | null) {
	session.set(s);
}

/**
 * Clear session: delete cookie, reload to login screen.
 */
export function clearSession() {
	session.set(null);
	fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
	window.location.reload();
}
