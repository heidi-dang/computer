/**
 * Session management: user state + 401 handling.
 *
 * No client-side expiry timers; the server is the source of truth.
 * If any API call returns 401, we clear the session and notify the shell.
 */

import { writable } from 'svelte/store';

export const SESSION_EXPIRED_EVENT = 'cptr:session-expired';
export const FLOWDECK_DRAFT_STORAGE_KEY = 'flowdeck:composer-draft:retained';
export const FLOWDECK_DRAFT_CHANGED_EVENT = 'cptr:flowdeck-draft-changed';
export const FLOWDECK_DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000;

const MAX_FLOWDECK_DRAFT_OBJECTIVE_LENGTH = 20_000;
const MAX_FLOWDECK_DRAFT_WORKSPACE_LENGTH = 4_096;

export interface RetainedFlowDeckDraft {
	mode: 'composer';
	workspace: string;
	objective: string;
	expiresAt: number;
}

function safeDraftWorkspace(value: unknown): string {
	if (typeof value !== 'string') return '';
	const candidate = value.trim();
	return candidate &&
		candidate.length <= MAX_FLOWDECK_DRAFT_WORKSPACE_LENGTH &&
		!/[\u0000-\u001f\u007f]/.test(candidate)
		? candidate
		: '';
}

function safeDraftObjective(value: unknown): string {
	if (typeof value !== 'string') return '';
	return value.length <= MAX_FLOWDECK_DRAFT_OBJECTIVE_LENGTH && !/[\u0000]/.test(value)
		? value
		: '';
}

/**
 * Read the optional longer-lived FlowDeck draft.
 *
 * This intentionally has its own record and never shares storage with the
 * session recovery or owned-run records. Expired or malformed data is removed
 * instead of being rehydrated.
 */
export function readRetainedFlowDeckDraft(): RetainedFlowDeckDraft | null {
	if (typeof localStorage === 'undefined') return null;
	try {
		const value: unknown = JSON.parse(localStorage.getItem(FLOWDECK_DRAFT_STORAGE_KEY) ?? 'null');
		if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
		const record = value as Record<string, unknown>;
		const workspace = safeDraftWorkspace(record.workspace);
		const objective = safeDraftObjective(record.objective);
		const expiresAt = record.expiresAt;
		if (
			record.mode !== 'composer' ||
			(!workspace && !objective) ||
			typeof expiresAt !== 'number' ||
			!Number.isFinite(expiresAt) ||
			expiresAt <= Date.now()
		) {
			localStorage.removeItem(FLOWDECK_DRAFT_STORAGE_KEY);
			return null;
		}
		return { mode: 'composer', workspace, objective, expiresAt };
	} catch {
		clearRetainedFlowDeckDraft();
		return null;
	}
}

export function writeRetainedFlowDeckDraft(
	draft: Pick<RetainedFlowDeckDraft, 'workspace' | 'objective'>
): RetainedFlowDeckDraft | null {
	if (typeof localStorage === 'undefined') return null;
	const retained: RetainedFlowDeckDraft = {
		mode: 'composer',
		workspace: safeDraftWorkspace(draft.workspace),
		objective: safeDraftObjective(draft.objective),
		expiresAt: Date.now() + FLOWDECK_DRAFT_TTL_MS
	};
	if (!retained.workspace && !retained.objective) {
		clearRetainedFlowDeckDraft();
		return null;
	}
	try {
		localStorage.setItem(FLOWDECK_DRAFT_STORAGE_KEY, JSON.stringify(retained));
		notifyRetainedFlowDeckDraftChanged();
		return retained;
	} catch {
		return null;
	}
}

export function clearRetainedFlowDeckDraft() {
	if (typeof localStorage === 'undefined') return;
	try {
		localStorage.removeItem(FLOWDECK_DRAFT_STORAGE_KEY);
		notifyRetainedFlowDeckDraftChanged();
	} catch {
		// Storage can be unavailable in private browsing; there is nothing else to clear.
	}
}

function notifyRetainedFlowDeckDraftChanged() {
	if (typeof window === 'undefined') return;
	window.dispatchEvent(new CustomEvent(FLOWDECK_DRAFT_CHANGED_EVENT));
}

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
	window.dispatchEvent(new CustomEvent(SESSION_EXPIRED_EVENT));
}
