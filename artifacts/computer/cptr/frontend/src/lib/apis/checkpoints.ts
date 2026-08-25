import { fetchJSON, jsonBody } from '$lib/apis';

export interface Checkpoint {
	checkpoint_id: string;
	revision: string;
	status: string;
	created_at: number;
	restored_at?: number | null;
}

export function listCheckpoints(workspace: string) {
	return fetchJSON<{ checkpoints: Checkpoint[] }>(
		`/v1/flowdeck/checkpoints?workspace=${encodeURIComponent(workspace)}`
	);
}

export function captureCheckpoint(workspace: string, idempotencyKey: string) {
	return fetchJSON<Checkpoint & { run_id: string }>('/v1/flowdeck/checkpoints/capture', {
		...jsonBody({ workspace }),
		headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey }
	});
}

export function restoreCheckpoint(
	workspace: string,
	checkpoint_id: string,
	idempotencyKey: string
) {
	return fetchJSON<Checkpoint & { run_id: string }>('/v1/flowdeck/checkpoints/restore', {
		...jsonBody({ workspace, checkpoint_id }),
		headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey }
	});
}