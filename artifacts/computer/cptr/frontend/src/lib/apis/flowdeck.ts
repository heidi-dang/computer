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
	audit?: boolean;
evidence_summary?: {
run_id: string;
owner: string;
entries: Array<{
id: string;
run_id: string;
owner: string;
sequence: number;
kind: string;
authority: 'authoritative' | 'advisory';
source: string;
payload: Record<string, string | number | boolean>;
created_at: number;
}>;
total: number;
truncated: boolean;
};
	[key: string]: unknown;
}

export interface CreateOrchestrationInput {
	workspace: string;
	objective: string;
	metadata?: Record<string, unknown>;
}

export interface CreateAuditInput {
	workspace: string;
	objective: string;
	scope: Record<string, unknown>;
	completion_contract: string[];
	metadata?: Record<string, unknown>;
}

export interface NewFlowDeckRunInput extends CreateOrchestrationInput {
	original_run_id: string;
}

export async function createAudit(
	input: CreateAuditInput,
	idempotencyKey: string
): Promise<FlowDeckOrchestration> {
	return fetchJSON<FlowDeckOrchestration>('/v1/flowdeck/audits', {
		...jsonBody(input),
		headers: {
			'Content-Type': 'application/json',
			'Idempotency-Key': idempotencyKey
		}
	});
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

export async function createNewFlowDeckRun(
	input: NewFlowDeckRunInput,
	idempotencyKey: string
): Promise<FlowDeckOrchestration> {
	return fetchJSON<FlowDeckOrchestration>(
		`/v1/flowdeck/orchestrations/${encodeURIComponent(input.original_run_id)}/new-run`,
		{
		...jsonBody(input),
		headers: {
			'Content-Type': 'application/json',
			'Idempotency-Key': idempotencyKey
		}
		}
	);
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

export async function getAudit(runId: string, workspace: string): Promise<FlowDeckOrchestration> {
	const query = new URLSearchParams({ workspace });
	return fetchJSON<FlowDeckOrchestration>(
		`/v1/flowdeck/audits/${encodeURIComponent(runId)}?${query.toString()}`
	);
}

export async function downloadFlowDeckEvidenceReport(
runId: string,
workspace: string
): Promise<Blob> {
const query = new URLSearchParams({ workspace });
const response = await fetchHandler(
`/v1/flowdeck/orchestrations/${encodeURIComponent(runId)}/evidence-report?${query.toString()}`
);
if (!response.ok) {
const data = await response.json().catch(() => ({}));
throw new Error(
data.detail || data.error || data.message || response.statusText || 'Unable to export evidence'
);
}
return response.blob();
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
): Promise<
	FlowDeckOrchestration & {
		accepted?: boolean;
		queued?: boolean;
		duplicate?: boolean;
		message?: string;
	}
> {
	return fetchJSON(`/v1/flowdeck/orchestrations/${encodeURIComponent(runId)}/steer`, {
		...jsonBody({ chat_id: chatId, instruction }),
		headers: {
			'Content-Type': 'application/json',
			'Idempotency-Key': idempotencyKey
		}
	});
}
