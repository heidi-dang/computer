import { writable } from 'svelte/store';
import type { WorkbenchSession, WorkbenchSessionEvent } from '$lib/apis/plugin';

export const PLUGIN_EVENT_WINDOW = 240;

export interface PluginConsoleState {
	sessions: WorkbenchSession[];
	selectedSessionId: string | null;
	eventsBySession: Record<string, WorkbenchSessionEvent[]>;
}

export const initialPluginConsoleState: PluginConsoleState = {
	sessions: [],
	selectedSessionId: null,
	eventsBySession: {}
};

function byMostRecentlyUpdated(a: WorkbenchSession, b: WorkbenchSession) {
	return String(b.updated_at).localeCompare(String(a.updated_at));
}

export function mergePluginSession(
	state: PluginConsoleState,
	session: WorkbenchSession
): PluginConsoleState {
	const sessions = [
		...state.sessions.filter((existing) => existing.session_id !== session.session_id),
		session
	].sort(byMostRecentlyUpdated);
	return {
		...state,
		sessions,
		selectedSessionId: state.selectedSessionId ?? session.session_id
	};
}

export function replacePluginSessions(
	state: PluginConsoleState,
	sessions: WorkbenchSession[]
): PluginConsoleState {
	const selectedStillExists = sessions.some((item) => item.session_id === state.selectedSessionId);
	return {
		...state,
		sessions: [...sessions].sort(byMostRecentlyUpdated),
		selectedSessionId: selectedStillExists ? state.selectedSessionId : (sessions[0]?.session_id ?? null)
	};
}

export function appendPluginEvent(
	state: PluginConsoleState,
	event: WorkbenchSessionEvent
): PluginConsoleState {
	const current = state.eventsBySession[event.session_id] ?? [];
	if (current.some((item) => item.sequence === event.sequence)) return state;
	const events = [...current, event]
		.sort((a, b) => a.sequence - b.sequence)
		.slice(-PLUGIN_EVENT_WINDOW);
	return {
		...state,
		eventsBySession: { ...state.eventsBySession, [event.session_id]: events }
	};
}

export function replacePluginEvents(
	state: PluginConsoleState,
	sessionId: string,
	events: WorkbenchSessionEvent[]
): PluginConsoleState {
	return {
		...state,
		eventsBySession: {
			...state.eventsBySession,
			[sessionId]: [...events].sort((a, b) => a.sequence - b.sequence).slice(-PLUGIN_EVENT_WINDOW)
		}
	};
}

export const pluginConsole = writable<PluginConsoleState>(initialPluginConsoleState);
