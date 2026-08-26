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

export const FDX_CONTAINMENT_CATEGORIES = [
	'containment_failure',
	'configuration_violation',
	'process_failure',
	'protocol_violation',
	'timeout',
	'workspace_cleanup_race',
	'workspace_lease',
	'workspace_side_effect'
] as const;

export type FdxContainmentCategory = (typeof FDX_CONTAINMENT_CATEGORIES)[number];

const FDX_RUN_STATUSES = [
	'pending',
	'running',
	'orphaned',
	'recovering',
	'succeeded',
	'failed',
	'manual_review_required',
	'cancelled'
] as const;

export interface FdxContainmentDiagnostic {
id: string;
run_id: string;
sequence: number;
	category: FdxContainmentCategory;
fallback: 'native';
run_status: string;
run_outcome: string | null;
created_at: number;
}

export interface FdxContainmentDiagnosticsResponse {
categories: string[];
diagnostics: FdxContainmentDiagnostic[];
total: number;
has_more: boolean;
}

export const FDX_CONTAINMENT_DIAGNOSTICS_PAGE_SIZE = 50;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isSafeIdentifier(value: unknown): value is string {
	return typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}

function isContainmentCategory(value: unknown): value is FdxContainmentCategory {
	return (
		typeof value === 'string' &&
		(FDX_CONTAINMENT_CATEGORIES as readonly string[]).includes(value)
	);
}

function isRunStatus(value: unknown): value is (typeof FDX_RUN_STATUSES)[number] {
	return typeof value === 'string' && (FDX_RUN_STATUSES as readonly string[]).includes(value);
}

function isSafeTimestamp(value: unknown): value is number {
	return (
		typeof value === 'number' &&
		Number.isSafeInteger(value) &&
		value >= 0 &&
		value <= 8_640_000_000_000_000
	);
}

function sanitizeDiagnostic(value: unknown): FdxContainmentDiagnostic | null {
	if (!isRecord(value)) return null;
	if (
		!isSafeIdentifier(value.id) ||
		!isSafeIdentifier(value.run_id) ||
		typeof value.sequence !== 'number' ||
		!Number.isSafeInteger(value.sequence) ||
		value.sequence < 0 ||
		!isContainmentCategory(value.category) ||
		value.fallback !== 'native' ||
		value.run_outcome !== 'native_fallback' ||
		!isRunStatus(value.run_status) ||
		!isSafeTimestamp(value.created_at)
	) {
		return null;
	}
	return {
		id: value.id,
		run_id: value.run_id,
		sequence: value.sequence,
		category: value.category,
		fallback: 'native',
		run_status: value.run_status,
		run_outcome: 'native_fallback',
		created_at: value.created_at
	};
}

/** Keep operator diagnostics safe even if an older/future server responds. */
export function sanitizeFdxContainmentDiagnostics(
	value: unknown
): FdxContainmentDiagnosticsResponse {
	if (!isRecord(value)) {
return { categories: [], diagnostics: [], total: 0, has_more: false };
	}
	const categories = Array.isArray(value.categories)
		? value.categories.filter(isContainmentCategory)
		: [];
const diagnostics = Array.isArray(value.diagnostics)
		? value.diagnostics.map(sanitizeDiagnostic).filter(
				(item): item is FdxContainmentDiagnostic => item !== null
			)
		: [];
const total =
typeof value.total === 'number' &&
Number.isSafeInteger(value.total) &&
value.total >= diagnostics.length
? value.total
: diagnostics.length;
return {
categories,
diagnostics,
total,
has_more: value.has_more === true
};
}

export async function getFdxContainmentDiagnostics(
category = '',
limit = FDX_CONTAINMENT_DIAGNOSTICS_PAGE_SIZE
): Promise<FdxContainmentDiagnosticsResponse> {
const params = new URLSearchParams({
limit: String(Math.max(1, Math.min(limit, FDX_CONTAINMENT_DIAGNOSTICS_PAGE_SIZE)))
});
if (category) params.set('category', category);
const query = `?${params.toString()}`;
	const response = await fetchJSON<unknown>(`/v1/flowdeck/diagnostics/fdx-containment${query}`);
	return sanitizeFdxContainmentDiagnostics(response);
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
