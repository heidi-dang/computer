import { fetchHandler, fetchJSON, jsonBody } from '$lib/apis';

export type GeneratedAuthConfig = {
	provider: string;
	supported: boolean;
	verified: boolean;
	preserved_existing_auth: boolean;
	capabilities: { signup: boolean; external_callback: boolean };
};

export async function getGeneratedAuthConfig(workspace: string) {
	return fetchJSON<GeneratedAuthConfig>(
		`/v1/flowdeck/generated-auth/config?workspace=${encodeURIComponent(workspace)}`
	);
}

export async function getGeneratedAuthCsrf(workspace: string) {
	return fetchJSON<{ csrf: string }>(
		`/v1/flowdeck/generated-auth/csrf?workspace=${encodeURIComponent(workspace)}`
	);
}

export async function getGeneratedAuthSession(workspace: string) {
	return fetchJSON<{ user: { email: string; role: string }; expires_at: number }>(
		`/v1/flowdeck/generated-auth/session?workspace=${encodeURIComponent(workspace)}`
	);
}

export async function signInGeneratedAuth(
	workspace: string,
	email: string,
	password: string,
	csrf: string
) {
	return fetchJSON(`/v1/flowdeck/generated-auth/signin`, {
		...jsonBody({ workspace, email, password, csrf })
	});
}

export async function signUpGeneratedAuth(
	workspace: string,
	email: string,
	password: string,
	csrf: string
) {
	return fetchJSON(`/v1/flowdeck/generated-auth/signup`, {
		...jsonBody({ workspace, email, password, csrf })
	});
}

export async function signOutGeneratedAuth(workspace: string) {
	const response = await fetchHandler(
		'/v1/flowdeck/generated-auth/signout',
		jsonBody({ workspace })
	);
	if (!response.ok) throw new Error((await response.text()) || 'Sign out failed');
	return response.json();
}