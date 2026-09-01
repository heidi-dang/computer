<script lang="ts">
	import { onMount } from 'svelte';
	import Icon from '$lib/components/Icon.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import {
		listMcpServers,
		listServerTools,
		reconnectMcpServer,
		type McpServer,
		type McpToolSpec
	} from '$lib/apis/mcp';
	import { toast } from 'svelte-sonner';

	interface Props {
		selectedServerId: string | null;
		selectedTool: McpToolSpec | null;
		onSelectTool: (serverId: string, tool: McpToolSpec) => void;
	}

	let {
		selectedServerId = $bindable(null),
		selectedTool = $bindable(null),
		onSelectTool
	}: Props = $props();

	let servers = $state<McpServer[]>([]);
	let toolsByServer = $state<Record<string, McpToolSpec[]>>({});
	let expandedServers = $state<Set<string>>(new Set());
	let loadingServers = $state(true);
	let loadingTools = $state<Set<string>>(new Set());
	let reconnecting = $state<Set<string>>(new Set());

	onMount(() => {
		void loadServers(true);
	});

	async function loadTools(server: McpServer) {
		if (toolsByServer[server.id]) return;
		loadingTools = new Set([...loadingTools, server.id]);
		try {
			toolsByServer[server.id] = await listServerTools(server.id);
			toolsByServer = { ...toolsByServer };
		} catch (e: any) {
			toast.error(`Failed to load tools for ${server.name}: ${e.message}`);
		} finally {
			loadingTools.delete(server.id);
			loadingTools = new Set(loadingTools);
		}
	}

	async function ensureServerExpanded(server: McpServer) {
		if (!expandedServers.has(server.id)) {
			expandedServers = new Set([...expandedServers, server.id]);
		}
		await loadTools(server);
	}

	async function loadServers(autoExpand: boolean) {
		loadingServers = true;
		try {
			servers = await listMcpServers();
			if (autoExpand) {
				for (const server of servers) {
					if (server.health === 'connected' || server.health === 'http') {
						await ensureServerExpanded(server);
					}
				}
			}
		} catch (e: any) {
			toast.error(e.message || 'Failed to load MCP servers');
		} finally {
			loadingServers = false;
		}
	}

	async function toggleServer(server: McpServer) {
		if (expandedServers.has(server.id)) {
			expandedServers.delete(server.id);
			expandedServers = new Set(expandedServers);
			return;
		}
		await ensureServerExpanded(server);
	}

	async function handleReconnect(e: MouseEvent, server: McpServer) {
		e.stopPropagation();
		const wasExpanded = expandedServers.has(server.id);
		reconnecting = new Set([...reconnecting, server.id]);
		try {
			await reconnectMcpServer(server.id);
			toast.success(`Reconnected ${server.name}`);
			delete toolsByServer[server.id];
			toolsByServer = { ...toolsByServer };
			await loadServers(false);
			const refreshedServer = servers.find((candidate) => candidate.id === server.id) ?? server;
			if (wasExpanded) await ensureServerExpanded(refreshedServer);
		} catch (e: any) {
			toast.error(`Reconnect failed: ${e.message}`);
		} finally {
			reconnecting.delete(server.id);
			reconnecting = new Set(reconnecting);
		}
	}

	function selectTool(serverId: string, tool: McpToolSpec) {
		selectedServerId = serverId;
		selectedTool = tool;
		onSelectTool(serverId, tool);
	}
</script>

<div class="app-theme flex h-full flex-col">
	<!-- Header -->
	<div class="app-surface flex shrink-0 items-center justify-between border-b px-3 py-2.5">
		<span class="text-xs font-medium uppercase tracking-wider app-muted">MCP Servers</span>
		<button
			class="app-interactive min-h-9 min-w-9 rounded-lg app-muted"
			onclick={() => void loadServers(false)}
			title="Refresh servers"
		>
			<Icon name="refresh" size={13} />
		</button>
	</div>

	<!-- Server list -->
	<div class="flex-1 overflow-y-auto py-1">
		{#if loadingServers}
			<div class="flex justify-center py-6">
				<Spinner size="sm" />
			</div>
		{:else if servers.length === 0}
			<div class="px-3 py-6 text-center text-xs app-muted">
				<p>No MCP servers configured.</p>
				<a href="/admin" class="app-accent mt-1 block hover:underline"
					>Configure in Admin → Tool Servers</a
				>
			</div>
		{:else}
			{#each servers as server (server.id)}
				{@const expanded = expandedServers.has(server.id)}
				{@const isMcp = server.type === 'mcp' || server.type === 'mcp_stdio'}
				{@const tools = toolsByServer[server.id] ?? []}
				{@const isLoadingTools = loadingTools.has(server.id)}
				{@const isReconnecting = reconnecting.has(server.id)}

				<div class="mb-0.5">
					<!-- Server row (div not button to allow nested interactive elements) -->
					<div
						role="button"
						tabindex="0"
						class="app-interactive group flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left"
						onclick={() => isMcp && toggleServer(server)}
						onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && isMcp && toggleServer(server)}
					>
						<!-- Health dot -->
						<span
							class="shrink-0 size-1.5 rounded-full {server.health === 'connected'
								? 'bg-emerald-400'
								: server.health === 'http'
									? 'bg-blue-400'
									: server.health === 'timeout'
										? 'bg-amber-400'
										: server.health === 'n/a'
											? 'bg-current opacity-40'
											: 'bg-red-400'}"
						></span>

						<span class="flex-1 truncate text-xs font-medium">{server.name}</span>

						<!-- Reconnect button for failed servers -->
						{#if isMcp && (server.health === 'disconnected' || server.health?.startsWith('error'))}
							<button
								class="app-subtle-surface rounded border px-1.5 py-1 text-[0.6rem] app-muted opacity-0 transition-all group-hover:opacity-100"
								onclick={(e) => handleReconnect(e, server)}
								disabled={isReconnecting}
							>
								{isReconnecting ? '…' : 'reconnect'}
							</button>
						{/if}

						<!-- Expand chevron -->
						{#if isMcp}
							<svg
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="3"
								class="size-2.5 shrink-0 transition-transform duration-150 app-muted {expanded
									? 'rotate-180'
									: ''}"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="m19.5 8.25-7.5 7.5-7.5-7.5"
								/>
							</svg>
						{/if}
					</div>

					<!-- Tool list -->
					{#if expanded && isMcp}
						<div class="pl-5 pb-1">
							{#if isLoadingTools}
								<div class="py-2 flex justify-center"><Spinner size="xs" /></div>
							{:else if tools.length === 0}
								<p class="px-2 py-1 text-[0.65rem] app-muted">No tools found</p>
							{:else}
								{#each tools as tool (tool.name)}
									<button
										class="app-interactive group/tool flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-left {selectedTool?.name ===
											tool.name && selectedServerId === server.id
											? 'app-interactive-active app-accent'
											: 'app-muted'}"
										onclick={() => selectTool(server.id, tool)}
									>
										<Icon name="tools" size={11} class="shrink-0 app-muted" />
										<span class="text-[0.7rem] font-mono truncate">{tool.name}</span>
									</button>
								{/each}
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		{/if}
	</div>

	<!-- Footer: admin link -->
	<div class="app-surface shrink-0 border-t px-3 py-2">
		<a
			href="/admin"
			class="app-interactive inline-flex min-h-9 items-center rounded-lg px-1.5 text-[0.65rem] app-muted"
		>
			+ Add server in Admin
		</a>
	</div>
</div>
