import { fetchHandler, fetchJSON, jsonBody } from '$lib/apis';

export interface ManagedRuntime {
run_id: string;
state: 'starting' | 'running' | 'crashed' | 'unknown' | 'stopped';
health: string;
port?: number;
command?: string[];
preview_url?: string;
logs?: string;
}

export function startManagedRuntime(workspace: string, idempotencyKey: string) {
return fetchJSON<ManagedRuntime>('/v1/flowdeck/runtime/start', {
...jsonBody({ workspace }),
headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey }
});
}

export function getManagedRuntime(runId: string, workspace: string) {
const query = new URLSearchParams({ workspace });
return fetchJSON<ManagedRuntime>(`/v1/flowdeck/runtime/${encodeURIComponent(runId)}?${query}`);
}

export async function stopManagedRuntime(runId: string, workspace: string) {
const query = new URLSearchParams({ workspace });
const response = await fetchHandler(
`/v1/flowdeck/runtime/${encodeURIComponent(runId)}/stop?${query}`,
jsonBody({})
);
if (!response.ok) throw new Error('Unable to stop managed runtime');
return response.json() as Promise<ManagedRuntime>;
}