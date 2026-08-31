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

	let { selectedServerId = $bindable(null), selectedTool = $bindable(null), onSelectTool }: Props = $props();

	let servers = $state<McpServer[]>([]);
	let toolsByServer = $state<Record<string, McpToolSpec[]>>({});
	let expandedServers = $state<Set<string>>(new Set());
	let loadingServers = $state(true);
	let loadingTools = $state<Set<string>>(new Set());
	let reconnecting = $state<Set<string>>(new Set());

	onMount(loadServers);

	async function loadServers() {
		loadingServers = true;
		try {
			servers = await listMcpServers();
			// Auto-expand connected servers
			for (const s of servers) {
				if (s.health === 'connected' || s.health === 'http') {
					await toggleServer(s);
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
		expandedServers = new Set([...expandedServers, server.id]);
		if (!toolsByServer[server.id]) {
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
	}

	async function handleReconnect(e: MouseEvent, server: McpServer) {
		e.stopPropagation();
		reconnecting = new Set([...reconnecting, server.id]);
		try {
			await reconnectMcpServer(server.id);
			toast.success(`Reconnected ${server.name}`);
			// Reload tools
			delete toolsByServer[server.id];
			toolsByServer = { ...toolsByServer };
			if (expandedServers.has(server.id)) await toggleServer(server);
			await loadServers();
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

<div class="flex flex-col h-full">
	<!-- Header -->
	<div class="flex items-center justify-between px-3 py-2.5 border-b border-gray-100 dark:border-gray-800 shrink-0">
		<span class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">MCP Servers</span>
		<button
			class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
			onclick={loadServers}
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
			<div class="px-3 py-6 text-center text-xs text-gray-400 dark:text-gray-500">
				<p>No MCP servers configured.</p>
				<a href="/admin" class="text-blue-500 hover:underline mt-1 block">Configure in Admin → Tool Servers</a>
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
						class="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors group cursor-pointer"
						onclick={() => isMcp && toggleServer(server)}
						onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && isMcp && toggleServer(server)}
					>
						<!-- Health dot -->
						<span class="shrink-0 size-1.5 rounded-full {
							server.health === 'connected' ? 'bg-emerald-400' :
							server.health === 'http' ? 'bg-blue-400' :
							server.health === 'timeout' ? 'bg-amber-400' :
							server.health === 'n/a' ? 'bg-gray-300 dark:bg-gray-600' :
							'bg-red-400'
						}"></span>

						<span class="flex-1 text-xs font-medium text-gray-700 dark:text-gray-200 truncate">{server.name}</span>

						<!-- Reconnect button for failed servers -->
						{#if isMcp && (server.health === 'disconnected' || server.health?.startsWith('error'))}
							<button
								class="opacity-0 group-hover:opacity-100 text-[0.6rem] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-700 text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition-all"
								onclick={(e) => handleReconnect(e, server)}
								disabled={isReconnecting}
							>
								{isReconnecting ? '…' : 'reconnect'}
							</button>
						{/if}

						<!-- Expand chevron -->
						{#if isMcp}
							<svg
								viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"
								class="size-2.5 text-gray-400 shrink-0 transition-transform duration-150 {expanded ? 'rotate-180' : ''}"
							>
								<path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
							</svg>
						{/if}
					</div>

					<!-- Tool list -->
					{#if expanded && isMcp}
						<div class="pl-5 pb-1">
							{#if isLoadingTools}
								<div class="py-2 flex justify-center"><Spinner size="xs" /></div>
							{:else if tools.length === 0}
								<p class="text-[0.65rem] text-gray-400 dark:text-gray-500 px-2 py-1">No tools found</p>
							{:else}
								{#each tools as tool (tool.name)}
									<button
										class="w-full flex items-center gap-1.5 px-2 py-1 text-left rounded-lg hover:bg-blue-50 dark:hover:bg-blue-900/20 transition-colors group/tool {
											selectedTool?.name === tool.name && selectedServerId === server.id
												? 'bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400'
												: 'text-gray-600 dark:text-gray-300'
										}"
										onclick={() => selectTool(server.id, tool)}
									>
										<Icon name="tools" size={11} class="shrink-0 text-gray-400 dark:text-gray-500" />
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
	<div class="shrink-0 border-t border-gray-100 dark:border-gray-800 px-3 py-2">
		<a
			href="/admin"
			class="text-[0.65rem] text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
		>
			+ Add server in Admin
		</a>
	</div>
</div>
