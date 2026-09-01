<script lang="ts">
	import McpActivityFeed from './McpActivityFeed.svelte';
	import McpToolForm from './McpToolForm.svelte';
	import McpServerList from './McpServerList.svelte';
	import { invokeToolStreaming, type McpContentItem, type McpToolSpec } from '$lib/apis/mcp';
	import type { McpActivityRow } from '$lib/stores/mcp-activity';
	import { toast } from 'svelte-sonner';

	type MobileConsoleView = 'servers' | 'console' | 'tool';
	type Props = {
		focusRequestId?: string | null;
		focusCorrelationId?: string | null;
	};

	let { focusRequestId = null, focusCorrelationId = null }: Props = $props();
	let consoleRows = $state<McpActivityRow[]>([]);
	let selectedServerId = $state<string | null>(null);
	let selectedTool = $state<McpToolSpec | null>(null);
	let isInvoking = $state(false);
	let mobileView = $state<MobileConsoleView>('servers');

	$effect(() => {
		if (focusRequestId || focusCorrelationId) mobileView = 'console';
	});

	function boundedJson(value: unknown): string {
		try {
			return JSON.stringify(value).slice(0, 13_000);
		} catch {
			return JSON.stringify({ value: String(value) }).slice(0, 13_000);
		}
	}

	function updateConsoleRow(id: string, update: (row: McpActivityRow) => McpActivityRow) {
		consoleRows = consoleRows.map((row) => (row.id === id ? update(row) : row));
	}

	async function handleInvoke(serverId: string, toolName: string, args: Record<string, unknown>) {
		if (isInvoking) return;
		isInvoking = true;
		const id = crypto.randomUUID();
		const startedAt = Date.now();
		const serverName = selectedTool?._server_name ?? serverId;
		const row: McpActivityRow = {
			id,
			correlationKey: `console:${id}`,
			source: 'console',
			sequence: 0,
			clientId: null,
			clientLabel: 'Console invocation',
			clientVersion: null,
			toolName,
			title: selectedTool?.description?.trim() || toolName,
			phase: 'started',
			summary: `Console invocation · ${serverName}`,
			startedAt,
			completedAt: null,
			durationMs: null,
			argumentsJson: boundedJson(args),
			resultJson: null,
			errorJson: null,
			requestId: null,
			sessionId: null,
			contentItems: []
		};
		consoleRows = [...consoleRows, row];
		mobileView = 'console';

		try {
			await invokeToolStreaming(serverId, toolName, args, {
				onChunk(item: McpContentItem) {
					updateConsoleRow(id, (current) => ({
						...current,
						contentItems: [...(current.contentItems ?? []), item]
					}));
				},
				onDone(result: McpContentItem[]) {
					const completedAt = Date.now();
					updateConsoleRow(id, (current) => ({
						...current,
						phase: 'complete',
						summary: `Console invocation completed · ${serverName}`,
						completedAt,
						durationMs: completedAt - current.startedAt,
						resultJson: boundedJson(result),
						contentItems: result,
						errorJson: null
					}));
				},
				onError(message: string) {
					const completedAt = Date.now();
					updateConsoleRow(id, (current) => ({
						...current,
						phase: 'failed',
						summary: `Console invocation failed · ${serverName}`,
						completedAt,
						durationMs: completedAt - current.startedAt,
						resultJson: null,
						errorJson: boundedJson({ message })
					}));
					toast.error(`Tool error: ${message}`);
				}
			});
		} catch (error: unknown) {
			const completedAt = Date.now();
			const message = error instanceof Error ? error.message : String(error);
			updateConsoleRow(id, (current) => ({
				...current,
				phase: 'failed',
				summary: `Console invocation failed · ${serverName}`,
				completedAt,
				durationMs: completedAt - current.startedAt,
				resultJson: null,
				errorJson: boundedJson({ message })
			}));
			toast.error(`Tool error: ${message}`);
		} finally {
			isInvoking = false;
		}
	}

	function clearConsoleRows() {
		consoleRows = [];
	}

	function handleSelectTool(serverId: string, tool: McpToolSpec) {
		selectedServerId = serverId;
		selectedTool = { ...tool, _server_name: tool._server_name ?? serverId };
		mobileView = 'tool';
	}
</script>

<div class="app-theme flex h-full min-h-0 flex-col overflow-hidden lg:flex-row">
	<nav
		class="app-subtle-surface grid shrink-0 grid-cols-3 gap-1 border-b p-2 lg:hidden"
		aria-label="MCP Console sections"
	>
		<button
			class="app-interactive min-h-11 rounded-lg px-3 text-xs font-medium {mobileView === 'servers'
				? 'app-accent-surface'
				: 'app-muted'}"
			aria-pressed={mobileView === 'servers'}
			onclick={() => (mobileView = 'servers')}
		>
			Servers
		</button>
		<button
			class="app-interactive min-h-11 rounded-lg px-3 text-xs font-medium {mobileView === 'console'
				? 'app-accent-surface'
				: 'app-muted'}"
			aria-pressed={mobileView === 'console'}
			onclick={() => (mobileView = 'console')}
		>
			Activity
		</button>
		<button
			class="app-interactive min-h-11 rounded-lg px-3 text-xs font-medium {mobileView === 'tool'
				? 'app-accent-surface'
				: 'app-muted'}"
			aria-pressed={mobileView === 'tool'}
			onclick={() => (mobileView = 'tool')}
		>
			Tool
		</button>
	</nav>

	<aside
		class="{mobileView === 'servers'
			? 'flex'
			: 'hidden'} app-surface min-h-0 min-w-0 flex-1 flex-col overflow-hidden lg:flex lg:w-56 lg:flex-none lg:border-r"
	>
		<McpServerList bind:selectedServerId bind:selectedTool onSelectTool={handleSelectTool} />
	</aside>

	<main
		class="{mobileView === 'console'
			? 'flex'
			: 'hidden'} app-surface min-h-0 min-w-0 flex-1 flex-col overflow-hidden lg:flex"
	>
		<McpActivityFeed
			{consoleRows}
			{focusRequestId}
			{focusCorrelationId}
			onClearConsole={clearConsoleRows}
		/>
	</main>

	<aside
		class="{mobileView === 'tool'
			? 'flex'
			: 'hidden'} app-subtle-surface min-h-0 min-w-0 flex-1 flex-col overflow-hidden lg:flex lg:w-72 lg:flex-none lg:border-l"
	>
		<McpToolForm
			tool={selectedTool}
			serverId={selectedServerId}
			onInvoke={handleInvoke}
			disabled={isInvoking}
		/>
	</aside>
</div>
