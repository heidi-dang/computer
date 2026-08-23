/**
 * Auth API: login, setup, session, logout.
 */
import { fetchHandler, fetchJSON, jsonBody } from '$lib/apis';

interface SessionResponse {
	authenticated: boolean;
	user_id?: string;
	username?: string;
	display_name?: string | null;
	role?: string;
	profile_image_url?: string | null;
	exp?: number;
}

interface ConfigResponse {
	auth_mode: string;
	needs_setup: boolean;
	signup_enabled: boolean;
	version: string;
}

export const getSession = () => fetchJSON<SessionResponse>('/api/auth');

export const getConfig = () => fetchJSON<ConfigResponse>('/api/config');

export const login = (username: string, password: string) =>
	fetchJSON('/api/auth/login', jsonBody({ username, password }));

export const setup = (username: string, password: string, token: string) =>
	fetchJSON('/api/auth/setup', jsonBody({ username, password, token }));

export const signup = (username: string, password: string) =>
	fetchJSON<{ ok?: boolean; pending?: boolean }>(
		'/api/auth/signup',
		jsonBody({ username, password })
	);

export const logout = () => fetchHandler('/api/auth/logout', { method: 'POST' }).catch(() => {});

export const updatePassword = (currentPassword: string, newPassword: string) =>
	fetchJSON(
		'/api/auth/password',
		jsonBody({
			current_password: currentPassword,
			new_password: newPassword
		})
	);

/** Upload avatar (resized client-side). Returns { ok, profile_image_url }. */
export const uploadAvatar = async (
	blob: Blob
): Promise<{ ok: boolean; profile_image_url: string }> => {
	const form = new FormData();
	form.append('file', blob, 'avatar.png');
	return fetchJSON('/api/auth/avatar', { method: 'PUT', body: form });
};

/** Delete avatar. */
export const deleteAvatar = () => fetchJSON('/api/auth/avatar', { method: 'DELETE' });

/** Update display name. */
export const updateProfile = (display_name: string | null) =>
	fetchJSON('/api/auth/profile', { ...jsonBody({ display_name }), method: 'PUT' });
