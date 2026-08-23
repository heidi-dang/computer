import { fetchJSON, jsonBody } from './index';

export type AutomationData = {
	id: string;
	user_id: string;
	name: string;
	prompt: string;
	model_id: string;
	workspace: string;
	rrule: string;
	is_active: boolean;
	last_run_at: number | null;
	next_run_at: number | null;
	meta: Record<string, any> | null;
	created_at: number;
	updated_at: number;
	last_run: AutomationRunData | null;
	next_runs: number[] | null;
	has_webhook: boolean;
	webhook_url: string | null;
};

export type AutomationRunData = {
	id: string;
	automation_id: string;
	chat_id: string | null;
	status: string;
	error: string | null;
	created_at: number;
};

export type AutomationForm = {
	name: string;
	prompt: string;
	model_id: string;
	workspace: string;
	rrule: string;
	is_active?: boolean;
};

export async function getAutomations(
	workspace?: string,
	query?: string,
	status?: string,
	page: number = 1
): Promise<{ items: AutomationData[]; total: number }> {
	const params = new URLSearchParams();
	if (workspace) params.set('workspace', workspace);
	if (query) params.set('query', query);
	if (status && status !== 'all') params.set('status', status);
	if (page > 1) params.set('page', page.toString());
	return fetchJSON(`/api/automations?${params}`);
}

export async function createAutomation(form: AutomationForm): Promise<AutomationData> {
	return fetchJSON('/api/automations', jsonBody(form));
}

export async function getAutomationById(id: string): Promise<AutomationData> {
	return fetchJSON(`/api/automations/${id}`);
}

export async function updateAutomation(id: string, form: AutomationForm): Promise<AutomationData> {
	return fetchJSON(`/api/automations/${id}`, jsonBody(form));
}

export async function toggleAutomation(id: string): Promise<AutomationData> {
	return fetchJSON(`/api/automations/${id}/toggle`, { method: 'POST' });
}

export async function runAutomation(id: string): Promise<AutomationData> {
	return fetchJSON(`/api/automations/${id}/run`, { method: 'POST' });
}

export async function deleteAutomation(id: string): Promise<{ ok: boolean }> {
	return fetchJSON(`/api/automations/${id}`, { method: 'DELETE' });
}

export async function getAutomationRuns(
	id: string,
	skip: number = 0,
	limit: number = 50
): Promise<AutomationRunData[]> {
	return fetchJSON(`/api/automations/${id}/runs?skip=${skip}&limit=${limit}`);
}

export async function generateWebhook(id: string): Promise<AutomationData> {
	return fetchJSON(`/api/automations/${id}/webhook`, { method: 'POST' });
}

export async function deleteWebhook(id: string): Promise<AutomationData> {
	return fetchJSON(`/api/automations/${id}/webhook`, { method: 'DELETE' });
}
