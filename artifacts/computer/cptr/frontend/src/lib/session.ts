/**
 * Session management: user state + 401 handling.
 *
 * No client-side expiry timers; the server is the source of truth.
 * If any API call returns 401, we clear the session and notify the shell.
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
 * Clear session: delete the server cookie and notify the shell to render login.
 */
export function clearSession() {
	session.set(null);
	fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
	window.dispatchEvent(new CustomEvent('cptr:session-expired'));
}
