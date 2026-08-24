import { fetchHandler, fetchJSON, jsonBody } from '$lib/apis';

export interface FlowDeckOrchestration {
	run_id?: string;
	id?: string;
	workspace?: string;
	objective?: string;
	status?: string;
	outcome?: 'completed' | 'clarification';
	message?: string;
	state?: string;
	created_at?: string;
	updated_at?: string;
	started_at?: string;
	finished_at?: string;
	monitor_id?: string;
	[key: string]: unknown;
}

export interface CreateOrchestrationInput {
	workspace: string;
	objective: string;
	metadata?: Record<string, unknown>;
}

export async function createFlowDeckOrchestration(
	input: CreateOrchestrationInput,
	idempotencyKey: string
): Promise<FlowDeckOrchestration> {
	return fetchJSON<FlowDeckOrchestration>('/v1/flowdeck/orchestrations', {
		...jsonBody(input),
		headers: {
			'Content-Type': 'application/json',
			'Idempotency-Key': idempotencyKey
		}
	});
}

export async function getFlowDeckOrchestration(
	runId: string,
	workspace: string
): Promise<FlowDeckOrchestration> {
	const query = new URLSearchParams({ workspace });
	return fetchJSON<FlowDeckOrchestration>(
		`/v1/flowdeck/orchestrations/${encodeURIComponent(runId)}?${query.toString()}`
	);
}

export async function cancelFlowDeckOrchestration(
	runId: string,
	workspace: string
): Promise<FlowDeckOrchestration> {
	const query = new URLSearchParams({ workspace });
	const response = await fetchHandler(
		`/v1/flowdeck/orchestrations/${encodeURIComponent(runId)}/cancel?${query.toString()}`,
		jsonBody({})
	);
	if (!response.ok) {
		const data = await response.json().catch(() => ({}));
		const message =
			data.detail || data.error || data.message || response.statusText || 'Unable to cancel run';
		throw new Error(message);
	}
	if (response.status === 204) return { run_id: runId };
	return response.json();
}

export async function steerFlowDeckOrchestration(
	runId: string,
	chatId: string,
	instruction: string,
	idempotencyKey: string
): Promise<FlowDeckOrchestration & { accepted?: boolean; queued?: boolean; duplicate?: boolean; message?: string }> {
	return fetchJSON(`/v1/flowdeck/orchestrations/${encodeURIComponent(runId)}/steer`, {
		...jsonBody({ chat_id: chatId, instruction }),
		headers: {
			'Content-Type': 'application/json',
			'Idempotency-Key': idempotencyKey
		}
	});
}