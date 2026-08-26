import { fetchJSON, jsonBody } from '$lib/apis';

function operationHeaders() {
return { 'Idempotency-Key': `generated-auth-${crypto.randomUUID()}` };
}

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
...jsonBody({ workspace, email, password, csrf }), headers: operationHeaders()
	});
}

export async function signUpGeneratedAuth(
	workspace: string,
	email: string,
	password: string,
	csrf: string
) {
	return fetchJSON(`/v1/flowdeck/generated-auth/signup`, {
...jsonBody({ workspace, email, password, csrf }), headers: operationHeaders()
	});
}

export async function signOutGeneratedAuth(workspace: string) {
	return fetchJSON(
		'/v1/flowdeck/generated-auth/signout',
		{ ...jsonBody({ workspace }), headers: operationHeaders() }
	);
}