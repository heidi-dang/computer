/**
 * MCP (Model Context Protocol) API — typed wrappers for /api/mcp/* endpoints.
 */
import { fetchJSON, fetchHandler, jsonBody } from '$lib/apis';

// ── Types ────────────────────────────────────────────────────────────────────

export interface McpServer {
	id: string;
	name: string;
	type: 'mcp' | 'mcp_stdio' | string;
	url: string | null;
	command: string | null;
	enabled: boolean;
	health: 'connected' | 'disconnected' | 'timeout' | 'error' | 'unknown' | 'n/a' | string;
}

export interface McpToolSpec {
	name: string;
	description: string;
	parameters: Record<string, unknown>; // JSON Schema object
	_server_id?: string;
	_server_name?: string;
}

export interface McpContentItem {
	type: 'text' | 'image' | 'resource' | string;
	text?: string;
	data?: string;      // base64 for images
	mimeType?: string;
	uri?: string;
}

export interface McpResource {
	uri: string;
	name?: string;
	description?: string;
	mimeType?: string;
}

// ── Server management ────────────────────────────────────────────────────────

export const listMcpServers = () =>
	fetchJSON<{ servers: McpServer[] }>('/api/mcp/servers').then((r) => r.servers);

export const getMcpServerStatus = (serverId: string) =>
	fetchJSON<{ server_id: string; type: string; status: string }>(`/api/mcp/servers/${serverId}/status`);

export const reconnectMcpServer = (serverId: string) =>
	fetchJSON(`/api/mcp/servers/${serverId}/reconnect`, { method: 'POST' });

export const getMcpServerLogs = (serverId: string, limit = 200) =>
	fetchJSON<{ server_id: string; lines: string[]; total_buffered: number }>(
		`/api/mcp/servers/${serverId}/logs?limit=${limit}`
	);

// ── Tool discovery ────────────────────────────────────────────────────────────

export const listServerTools = (serverId: string) =>
	fetchJSON<{ server_id: string; tools: McpToolSpec[] }>(
		`/api/mcp/servers/${serverId}/tools`
	).then((r) => r.tools);

export const listAllTools = () =>
	fetchJSON<{ tools: McpToolSpec[]; count: number }>('/api/mcp/tools').then((r) => r.tools);

export const getToolSchema = (toolName: string) =>
	fetchJSON<McpToolSpec>(`/api/mcp/tools/${encodeURIComponent(toolName)}`);

// ── Tool invocation ──────────────────────────────────────────────────────────

/** Invoke a tool — returns the raw MCP content array. */
export const invokeTool = (
	serverId: string,
	toolName: string,
	args: Record<string, unknown> = {}
) =>
	fetchJSON<{ server_id: string; tool: string; result: McpContentItem[] }>(
		`/api/mcp/servers/${serverId}/tools/${encodeURIComponent(toolName)}/invoke`,
		jsonBody({ arguments: args })
	).then((r) => r.result);

/**
 * Invoke a tool with real SSE streaming via ?stream=1.
 * Emits events as they arrive: onStart → onChunk* → onDone | onError.
 */
export async function invokeToolStreaming(
	serverId: string,
	toolName: string,
	args: Record<string, unknown>,
	callbacks: {
		onStart?: () => void;
		onChunk?: (item: McpContentItem) => void;
		onDone?: (result: McpContentItem[]) => void;
		onError?: (message: string) => void;
	}
): Promise<void> {
	callbacks.onStart?.();

	let res: Response;
	try {
		res = await fetch(
			`/api/mcp/servers/${serverId}/tools/${encodeURIComponent(toolName)}/invoke?stream=1`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ arguments: args })
			}
		);
	} catch (err: unknown) {
		callbacks.onError?.(err instanceof Error ? err.message : String(err));
		return;
	}

	if (!res.ok) {
		const data = await res.json().catch(() => ({}));
		callbacks.onError?.(data.detail || data.error || res.statusText);
		return;
	}

	const reader = res.body?.getReader();
	if (!reader) {
		callbacks.onError?.('No response body');
		return;
	}

	const decoder = new TextDecoder();
	let buffer = '';
	const DOUBLE_NEWLINE = '\n\n';
	const SINGLE_NEWLINE = '\n';

	while (true) {
		const { done, value } = await reader.read();
		if (done) break;
		buffer += decoder.decode(value, { stream: true });

		// Split on SSE double-newline boundaries
		const parts = buffer.split(DOUBLE_NEWLINE);
		buffer = parts.pop() ?? '';

		for (const part of parts) {
			const lines = part.trim().split(SINGLE_NEWLINE);
			let evtName = '';
			let evtData = '';
			for (const line of lines) {
				if (line.startsWith('event: ')) evtName = line.slice(7).trim();
				if (line.startsWith('data: ')) evtData = line.slice(6).trim();
			}
			if (!evtData) continue;
			try {
				const payload = JSON.parse(evtData);
				if (evtName === 'tool_chunk') {
					callbacks.onChunk?.(payload as McpContentItem);
				} else if (evtName === 'tool_done') {
					callbacks.onDone?.((payload.result ?? []) as McpContentItem[]);
				} else if (evtName === 'tool_error') {
					callbacks.onError?.(payload.message ?? 'Unknown error');
				}
			} catch {
				// malformed SSE chunk — skip
			}
		}
	}
}

// ── Resources ────────────────────────────────────────────────────────────────

export const listServerResources = (serverId: string) =>
	fetchJSON<{ server_id: string; resources: McpResource[] }>(
		`/api/mcp/servers/${serverId}/resources/list`,
		{ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }
	).then((r) => r.resources);

export const readServerResource = (serverId: string, uri: string) =>
	fetchJSON<{ server_id: string; uri: string; contents: McpContentItem[] }>(
		`/api/mcp/servers/${serverId}/resources/read`,
		jsonBody({ uri })
	).then((r) => r.contents);