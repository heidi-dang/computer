<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import {
		workspaceList,
		stateLoaded,
		removeWorkspace,
		reorderWorkspaces,
		sidebarOpen,
		activeTab,
		currentWorkspace
	} from '$lib/stores';
	import { chatEnabled, updateChatStatuses } from '$lib/stores/chat';
	import { socketStore } from '$lib/stores/socket.svelte';
	import { workspaceOsStore } from '$lib/stores/workspace-os.svelte';
	import {
		deleteChat as apiDeleteChat,
		getChats,
		updateChatTitle,
		type ChatInfo
	} from '$lib/apis/chat';
	import { t } from '$lib/i18n';
	import { tooltip } from '$lib/tooltip';
	import Sortable from 'sortablejs';
	import { onDestroy, onMount } from 'svelte';
	import ChatItem from './common/ChatItem.svelte';
	import DropdownMenu from './DropdownMenu.svelte';
	import Icon from './Icon.svelte';
	import {
		clearWorkspaceCenterRoute,
		normalizeWorkspaceCenterTab,
		setWorkspaceRouteForPath,
		setWorkspaceCenterRoute,
		WORKSPACE_CENTER_ID_PARAM,
		WORKSPACE_CENTER_TAB_PARAM
	} from '$lib/utils/workspaceRoute';

	type WorkspaceSettingsModalComponent = typeof import('./WorkspaceSettingsModal.svelte').default;

	interface Props {
		onaddworkspace: () => void;
	}

	let { onaddworkspace }: Props = $props();
	let LazyWorkspaceSettingsModal = $state<WorkspaceSettingsModalComponent | null>(null);
	let workspaceSettingsLoad: Promise<void> | null = null;
	let workspaceSettingsLoading = $state(false);
	let workspaceSettingsLoadError = $state('');
	let workspaceSettingsInitialTab = $state('overview');
	let wsMenuPath = $state<string | null>(null);
	let wsMenuAnchor = $state<HTMLElement | null>(null);
	let workspaceSettingsTarget = $state<{
		workspace_id: string;
		path: string;
		name: string;
		slug: string | null;
		workspace_type: string;
	} | null>(null);
	let chatMenu = $state<{ chatId: string; wsPath: string; anchor: HTMLElement } | null>(null);
	let wsListEl: HTMLDivElement | undefined = $state();
	let sortable: Sortable | null = null;
	let unbindSocketListener: (() => void) | null = null;
	let workspacesExpanded = $state(true);

	let expandedWorkspaces = $state<Set<string>>(new Set());
	let wsChatsCache = $state<Map<string, ChatInfo[]>>(new Map());
	let wsChatsHasMore = $state<Map<string, boolean>>(new Map());
	let wsChatsLoading = $state<Set<string>>(new Set());
	let currentPath = $derived($currentWorkspace?.path ?? null);
	let currentChatId = $derived($activeTab?.type === 'chat' ? $activeTab.path : null);
	let liveWorkspaceId = $derived(workspaceOsStore.workspaceId);
	let liveWorkspaceStatus = $derived(workspaceOsStore.connection.status);
	let liveWorkspaceProjection = $derived(workspaceOsStore.projection);
	let liveWorkspaceStatusLabel = $derived.by(() => {
		const health = liveWorkspaceProjection?.health.status ?? 'unknown';
		const sessions = liveWorkspaceProjection?.workbench.active_sessions_count ?? 0;
		return `Workspace OS ${liveWorkspaceStatus} · health ${health} · ${sessions} active Workbench session${sessions === 1 ? '' : 's'}`;
	});
	const WS_CHATS_PAGE_SIZE = 5;

	$effect(() => {
		const workspaceId = $page.url.searchParams.get(WORKSPACE_CENTER_ID_PARAM)?.trim() || '';
		if (!workspaceId) {
			workspaceSettingsTarget = null;
			return;
		}
		const target = $workspaceList.find((workspace) => workspace.workspace_id === workspaceId);
		if (!target) {
			if ($stateLoaded) {
				workspaceSettingsTarget = null;
				const params = clearWorkspaceCenterRoute($page.url.searchParams);
				void goto(`/?${params.toString()}`, {
					replaceState: true,
					noScroll: true,
					keepFocus: true
				});
			}
			return;
		}
		const requestedTab = $page.url.searchParams.get(WORKSPACE_CENTER_TAB_PARAM);
		const normalizedTab = normalizeWorkspaceCenterTab(requestedTab);
		workspaceSettingsInitialTab = normalizedTab;
		if (requestedTab !== normalizedTab) {
			const params = setWorkspaceCenterRoute($page.url.searchParams, workspaceId, normalizedTab);
			void goto(`/?${params.toString()}`, { replaceState: true, noScroll: true, keepFocus: true });
		}
		workspaceSettingsTarget = {
			workspace_id: target.workspace_id,
			path: target.path,
			name: target.name,
			slug: target.slug,
			workspace_type: target.workspace_type
		};
		void ensureWorkspaceSettingsModal();
	});

	function toggleWorkspaceExpand(path: string) {
		const next = new Set(expandedWorkspaces);
		if (next.has(path)) {
			next.delete(path);
		} else {
			next.add(path);
			if (!wsChatsCache.has(path)) fetchWorkspaceChats(path);
		}
		expandedWorkspaces = next;
	}

	async function fetchWorkspaceChats(path: string, append = false, limit = WS_CHATS_PAGE_SIZE) {
		if (wsChatsLoading.has(path)) return;
		wsChatsLoading = new Set([...wsChatsLoading, path]);
		try {
			const existing = wsChatsCache.get(path) ?? [];
			const data = await getChats(
				path,
				append ? WS_CHATS_PAGE_SIZE : limit,
				append ? existing.length : 0,
				'updated_at',
				'desc'
			);
			wsChatsCache = new Map([
				...wsChatsCache,
				[
					path,
					append
						? [...existing, ...(data.chats || [])].sort(
								(a, b) =>
									Number(
										!b.is_active && (b.last_read_at === null || b.updated_at > b.last_read_at)
									) -
										Number(
											!a.is_active && (a.last_read_at === null || a.updated_at > a.last_read_at)
										) || b.updated_at - a.updated_at
							)
						: data.chats || []
				]
			]);
			updateChatStatuses(data.chats || [], path);
			wsChatsHasMore = new Map([...wsChatsHasMore, [path, data.has_more]]);
		} catch {
			wsChatsCache = new Map([...wsChatsCache, [path, []]]);
			wsChatsHasMore = new Map([...wsChatsHasMore, [path, false]]);
		} finally {
			const next = new Set(wsChatsLoading);
			next.delete(path);
			wsChatsLoading = next;
		}
	}

	function reloadWorkspaceChats(path: string) {
		const loadedCount = wsChatsCache.get(path)?.length ?? WS_CHATS_PAGE_SIZE;
		void fetchWorkspaceChats(path, false, Math.max(loadedCount, WS_CHATS_PAGE_SIZE));
	}

	function ensureWorkspaceSettingsModal(): Promise<void> {
		if (LazyWorkspaceSettingsModal) return Promise.resolve();
		if (workspaceSettingsLoad) return workspaceSettingsLoad;
		workspaceSettingsLoading = true;
		workspaceSettingsLoadError = '';
		workspaceSettingsLoad = import('./WorkspaceSettingsModal.svelte')
			.then(({ default: component }) => {
				LazyWorkspaceSettingsModal = component;
			})
			.catch((error: unknown) => {
				workspaceSettingsLoadError =
					error instanceof Error ? error.message : 'Workspace Center failed to load';
				workspaceSettingsLoad = null;
			})
			.finally(() => {
				workspaceSettingsLoading = false;
			});
		return workspaceSettingsLoad;
	}

	function workspaceRoute(path: string, extra?: Record<string, string>): string {
		let params = setWorkspaceRouteForPath(new URLSearchParams(), path, $workspaceList);
		for (const [key, value] of Object.entries(extra ?? {})) params.set(key, value);
		return `/?${params.toString()}`;
	}

	function closeMobileSidebar() {
		if (typeof window !== 'undefined' && window.innerWidth < 768) sidebarOpen.set(false);
	}

	function openWorkspace(e: MouseEvent, path: string) {
		if (e.metaKey || e.ctrlKey) return;
		e.preventDefault();
		goto(workspaceRoute(path));
		closeMobileSidebar();
	}

	function openChat(chatId: string, wsPath: string) {
		goto(workspaceRoute(wsPath, { chatId }));
		closeMobileSidebar();
	}

	function newChat(wsPath: string) {
		goto(workspaceRoute(wsPath, { chatId: '' }));
		closeMobileSidebar();
	}

	function openWsMenu(e: Event, path: string) {
		e.stopPropagation();
		e.preventDefault();
		closeChatMenu();
		wsMenuAnchor = e.currentTarget as HTMLElement;
		wsMenuPath = path;
	}

	function closeWsMenu() {
		wsMenuPath = null;
		wsMenuAnchor = null;
	}

	function openChatMenu(e: MouseEvent, chatId: string, wsPath: string) {
		e.stopPropagation();
		closeWsMenu();
		chatMenu = { chatId, wsPath, anchor: e.currentTarget as HTMLElement };
	}

	function closeChatMenu() {
		chatMenu = null;
	}

	function openWorkspaceSettings(path: string, tab = 'overview') {
		const target = $workspaceList.find((workspace) => workspace.path === path);
		closeWsMenu();
		if (!target?.workspace_id) return;
		void ensureWorkspaceSettingsModal();
		const params = setWorkspaceCenterRoute($page.url.searchParams, target.workspace_id, tab);
		void goto(`/?${params.toString()}`);
	}

	function updateWorkspaceSettingsTab(tab: string) {
		if (!workspaceSettingsTarget) return;
		const params = setWorkspaceCenterRoute(
			$page.url.searchParams,
			workspaceSettingsTarget.workspace_id,
			tab
		);
		void goto(`/?${params.toString()}`, { replaceState: true, noScroll: true, keepFocus: true });
	}

	function closeWorkspaceSettings() {
		workspaceSettingsTarget = null;
		const params = clearWorkspaceCenterRoute($page.url.searchParams);
		void goto(`/?${params.toString()}`, { replaceState: true, noScroll: true, keepFocus: true });
	}

	async function handleRemoveWorkspace(path: string) {
		closeWsMenu();
		await removeWorkspace(path);
		if (currentPath === path) goto('/');
	}

	async function handleDeleteChat() {
		if (!chatMenu) return;
		const { chatId, wsPath } = chatMenu;
		closeChatMenu();
		await apiDeleteChat(chatId);
		const chats = wsChatsCache.get(wsPath) ?? [];
		wsChatsCache = new Map([...wsChatsCache, [wsPath, chats.filter((chat) => chat.id !== chatId)]]);
		if (currentPath === wsPath && currentChatId === chatId) {
			goto(workspaceRoute(wsPath));
		}
	}

	async function handleRenameChat() {
		if (!chatMenu) return;
		const { chatId, wsPath } = chatMenu;
		const chat = (wsChatsCache.get(wsPath) ?? []).find((item) => item.id === chatId);
		const title = window.prompt($t('files.rename'), chat?.title)?.trim();
		if (!title || title === chat?.title) return;
		await updateChatTitle(chatId, title);
		const chats = wsChatsCache.get(wsPath) ?? [];
		wsChatsCache = new Map([
			...wsChatsCache,
			[wsPath, chats.map((item) => (item.id === chatId ? { ...item, title } : item))]
		]);
	}

	function copyChatPath() {
		if (!chatMenu) return;
		const { chatId, wsPath } = chatMenu;
		const chat = (wsChatsCache.get(wsPath) ?? []).find((item) => item.id === chatId);
		if (!chat) return;
		navigator.clipboard.writeText(
			`${wsPath.replace(/\/$/, '')}/.cptr/chats/${chat.folder ? `${chat.folder}/` : ''}${chat.id}.json`
		);
	}

	function handleChatEvent(data: {
		type?: string;
		chat_id: string;
		done?: boolean;
		title?: string;
		delta?: string;
		workspace?: string;
		active?: boolean;
		updated_at?: number;
		last_read_at?: number;
		workspace_unread_count?: number;
	}) {
		if (
			!data.title &&
			typeof data.active !== 'boolean' &&
			typeof data.updated_at !== 'number' &&
			typeof data.last_read_at !== 'number' &&
			typeof data.workspace_unread_count !== 'number'
		) {
			return;
		}
		const unreadCount = data.workspace_unread_count;
		if (data.workspace && typeof unreadCount === 'number') {
			workspaceList.update((workspaces) =>
				workspaces.map((workspace) =>
					workspace.path === data.workspace
						? { ...workspace, unread_count: unreadCount }
						: workspace
				)
			);
		}

		let known = false;
		const shouldReorder =
			typeof data.updated_at === 'number' ||
			typeof data.last_read_at === 'number' ||
			typeof data.active === 'boolean';

		wsChatsCache = new Map(
			[...wsChatsCache].map(([path, chats]) => {
				const nextChats = chats.map((chat) => {
					if (chat.id !== data.chat_id) return chat;
					known = true;
					return {
						...chat,
						...(data.title ? { title: data.title } : {}),
						...(typeof data.updated_at === 'number' ? { updated_at: data.updated_at } : {}),
						...(typeof data.last_read_at === 'number' ? { last_read_at: data.last_read_at } : {}),
						...(typeof data.active === 'boolean' ? { is_active: data.active } : {})
					};
				});
				return [
					path,
					shouldReorder
						? nextChats.sort(
								(a, b) =>
									Number(
										!b.is_active && (b.last_read_at === null || b.updated_at > b.last_read_at)
									) -
										Number(
											!a.is_active && (a.last_read_at === null || a.updated_at > a.last_read_at)
										) || b.updated_at - a.updated_at
							)
						: nextChats
				] as [string, ChatInfo[]];
			})
		);

		// A chat created in another session is not yet in this sidebar's page.
		// Refresh only that expanded workspace; all known rows update in place.
		if (!known && data.workspace && expandedWorkspaces.has(data.workspace)) {
			void fetchWorkspaceChats(data.workspace);
		} else if (
			known &&
			typeof data.last_read_at === 'number' &&
			data.workspace &&
			expandedWorkspaces.has(data.workspace)
		) {
			reloadWorkspaceChats(data.workspace);
		}
	}

	function isTouchDevice(): boolean {
		return (
			typeof window !== 'undefined' && ('ontouchstart' in window || navigator.maxTouchPoints > 0)
		);
	}

	onMount(() => {
		if (wsListEl && !isTouchDevice()) {
			sortable = Sortable.create(wsListEl, {
				animation: 150,
				ghostClass: 'opacity-30',
				dragClass: 'cursor-grabbing',
				direction: 'vertical',
				onEnd: (evt) => {
					if (evt.oldIndex != null && evt.newIndex != null && evt.oldIndex !== evt.newIndex) {
						reorderWorkspaces(evt.oldIndex, evt.newIndex);
					}
				}
			});
		}

		unbindSocketListener = socketStore.on('events:chat', handleChatEvent);
	});

	onDestroy(() => {
		sortable?.destroy();
		unbindSocketListener?.();
		unbindSocketListener = null;
	});
</script>

<div class="flex items-center justify-between min-h-9 pl-3.5 pr-1.5 shrink-0">
	<button
		class="group flex flex-1 h-full items-center gap-1 text-left text-xs text-gray-400 hover:text-gray-500 dark:text-gray-500 dark:hover:text-gray-400 transition-colors duration-100"
		onclick={() => (workspacesExpanded = !workspacesExpanded)}
		aria-expanded={workspacesExpanded}
		aria-controls="workspace-list"
	>
		<span>{$t('sidebar.workspaces')}</span>
		<span
			class="flex opacity-0 group-hover:opacity-100 transition-all duration-100"
			style="transform: rotate({workspacesExpanded ? '90deg' : '0deg'})"
		>
			<Icon name="chevron-right" size={11} />
		</span>
	</button>
	<button
		class="touch-target app-interactive flex items-center justify-center w-8 h-8 rounded-xl text-gray-300 hover:text-gray-500 dark:text-gray-600 dark:hover:text-gray-400 transition-colors duration-100"
		onclick={onaddworkspace}
		aria-label={$t('sidebar.addWorkspace')}
		use:tooltip={$t('sidebar.addWorkspace')}
	>
		<Icon name="plus" size={14} />
	</button>
</div>

<div
	id="workspace-list"
	bind:this={wsListEl}
	class="flex-1 overflow-y-auto px-1.5"
	class:invisible={!workspacesExpanded}
>
	{#each $workspaceList as ws (ws.path)}
		{@const isExpanded = expandedWorkspaces.has(ws.path)}
		{@const chats = wsChatsCache.get(ws.path)}
		{@const hasMoreChats = wsChatsHasMore.get(ws.path)}
		{@const isLoading = wsChatsLoading.has(ws.path)}
		<div class="ws-item">
			<div
				class="workspace-row app-interactive group flex items-center gap-1 w-full min-h-8 px-2.5 rounded-xl text-xs font-medium transition-colors duration-100
				{ws.path === currentPath
					? 'app-interactive-active text-gray-900 dark:text-white'
					: 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'}"
			>
				<a
					href={workspaceRoute(ws.path)}
					class="flex items-center gap-1.5 flex-1 min-w-0 no-underline text-inherit"
					onclick={(e) => openWorkspace(e, ws.path)}
				>
					{#if $chatEnabled}
						<span
							class="ws-icon-toggle shrink-0"
							role="button"
							tabindex="0"
							onclick={(e) => {
								e.stopPropagation();
								e.preventDefault();
								toggleWorkspaceExpand(ws.path);
							}}
							onkeydown={(e) => {
								if (e.key !== 'Enter' && e.key !== ' ') return;
								e.stopPropagation();
								e.preventDefault();
								toggleWorkspaceExpand(ws.path);
							}}
							aria-label={isExpanded ? $t('sidebar.collapse') : $t('sidebar.addWorkspace')}
						>
							<span class="ws-icon-folder"><Icon name="folder" size={14} /></span>
							<span
								class="ws-icon-chevron"
								style="transform: rotate({isExpanded ? '90deg' : '0deg'})"
							>
								<Icon name="chevron-right" size={11} />
							</span>
						</span>
					{:else}
						<Icon name="folder" size={14} />
					{/if}
					<span class="min-w-0 truncate text-left">{ws.name}</span>
					{#if ws.workspace_id === liveWorkspaceId}
						<span
							class="workspace-live-indicator"
							data-status={liveWorkspaceStatus}
							role="status"
							aria-label={liveWorkspaceStatusLabel}
							title={liveWorkspaceStatusLabel}
						></span>
						{#if (liveWorkspaceProjection?.workbench.active_sessions_count ?? 0) > 0}
							<span
								class="workspace-workbench-count"
								title={liveWorkspaceStatusLabel}
								aria-label={`${liveWorkspaceProjection?.workbench.active_sessions_count ?? 0} active Workbench sessions`}
							>
								WB {liveWorkspaceProjection?.workbench.active_sessions_count ?? 0}
							</span>
						{/if}
					{/if}
					{#if ws.unread_count > 0}
						<span
							class="inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-md bg-sky-500/10 px-1 text-[0.625rem] font-semibold text-sky-600 dark:bg-sky-400/10 dark:text-sky-300"
						>
							{new Intl.NumberFormat(undefined, {
								notation: 'compact',
								compactDisplay: 'short'
							}).format(ws.unread_count)}
						</span>
					{/if}
				</a>
				<span
					class="flex items-center justify-center w-4 h-4 shrink-0 text-gray-400 opacity-0 group-hover:opacity-100 hover:text-gray-600 dark:hover:text-gray-300 transition-all duration-75"
					role="button"
					tabindex="0"
					onclick={(e) => openWsMenu(e, ws.path)}
					onkeydown={(e) => {
						if (e.key === 'Enter' || e.key === ' ') openWsMenu(e, ws.path);
					}}
					aria-label={$t('sidebar.workspaceOptions')}
				>
					<Icon name="three-dots" size={11} />
				</span>
				{#if $chatEnabled}
					<span
						class="flex items-center justify-center w-4 h-4 shrink-0 text-gray-400 opacity-0 group-hover:opacity-100 hover:text-gray-600 dark:hover:text-gray-300 transition-all duration-75"
						role="button"
						tabindex="0"
						onclick={() => newChat(ws.path)}
						onkeydown={(e) => {
							if (e.key === 'Enter' || e.key === ' ') newChat(ws.path);
						}}
						aria-label={$t('bar.newChat')}
						use:tooltip={$t('bar.newChat')}
					>
						<Icon name="pencil" size={11} />
					</span>
				{/if}
			</div>

			{#if $chatEnabled && isExpanded}
				<div class="ws-chats">
					{#if isLoading && !chats}
						<div class="ws-chat-loading">
							<span class="ws-chat-loading-dot"></span>
							<span class="ws-chat-loading-dot"></span>
							<span class="ws-chat-loading-dot"></span>
						</div>
					{:else if chats && chats.length > 0}
						{#each chats as chat (chat.id)}
							<ChatItem
								{chat}
								isSelected={chat.id === currentChatId}
								onclick={() => openChat(chat.id, ws.path)}
								onmenu={(e) => openChatMenu(e, chat.id, ws.path)}
							/>
						{/each}
						{#if hasMoreChats}
							<button
								class="ws-chat-show-more"
								disabled={isLoading}
								onclick={() => fetchWorkspaceChats(ws.path, true)}
							>
								{$t('sidebar.showMore')}
							</button>
						{/if}
					{/if}
				</div>
			{/if}
		</div>
	{/each}

	{#if $workspaceList.length === 0}
		<div class="flex flex-col items-center justify-center py-12">
			<p class="text-xs text-gray-400 dark:text-gray-600">{$t('sidebar.noWorkspaces')}</p>
		</div>
	{/if}
</div>

{#if wsMenuPath && wsMenuAnchor}
	<DropdownMenu
		anchor={wsMenuAnchor}
		items={[
			{
				label: 'Workspace Center',
				icon: 'settings',
				onclick: () => openWorkspaceSettings(wsMenuPath!)
			},
			{
				label: $t('sidebar.remove'),
				icon: 'xmark',
				onclick: () => handleRemoveWorkspace(wsMenuPath!)
			}
		]}
		onclose={closeWsMenu}
	/>
{/if}

{#if workspaceSettingsTarget && LazyWorkspaceSettingsModal}
	{#key workspaceSettingsTarget.workspace_id}
		<LazyWorkspaceSettingsModal
			workspace={workspaceSettingsTarget}
			initialTab={workspaceSettingsInitialTab}
			onclose={closeWorkspaceSettings}
			ontabchange={updateWorkspaceSettingsTab}
		/>
	{/key}
{:else if workspaceSettingsTarget}
	<div
		class="fixed inset-0 z-[80] grid place-items-center bg-black/35 p-4 backdrop-blur-[1px]"
		role="dialog"
		aria-modal="true"
		aria-label="Workspace Center loading"
	>
		<div
			class="w-full max-w-sm rounded-2xl border border-[var(--app-border)] bg-[var(--app-bg)] p-5 shadow-2xl"
		>
			{#if workspaceSettingsLoadError}
				<h2 class="text-sm font-semibold">Workspace Center could not load</h2>
				<p class="mt-2 break-words text-xs text-[var(--app-muted-fg)]">
					{workspaceSettingsLoadError}
				</p>
				<div class="mt-4 flex justify-end gap-2">
					<button class="min-h-11 rounded-lg px-3 text-xs" onclick={closeWorkspaceSettings}
						>Close</button
					>
					<button
						class="min-h-11 rounded-lg bg-[var(--app-fg)] px-3 text-xs font-semibold text-[var(--app-bg)]"
						disabled={workspaceSettingsLoading}
						onclick={() => void ensureWorkspaceSettingsModal()}>Retry</button
					>
				</div>
			{:else}
				<div class="flex items-center gap-3" role="status">
					<span
						class="h-4 w-4 animate-spin rounded-full border-2 border-current border-r-transparent motion-reduce:animate-none"
					></span>
					<div>
						<strong class="text-sm">Loading Workspace Center…</strong>
						<p class="mt-1 text-xs text-[var(--app-muted-fg)]">
							Loading only the Workspace management bundle.
						</p>
					</div>
				</div>
			{/if}
		</div>
	</div>
{/if}

{#if chatMenu}
	<DropdownMenu
		anchor={chatMenu.anchor}
		align="end"
		items={[
			{
				label: $t('files.copyPath'),
				icon: 'copy',
				onclick: copyChatPath
			},
			{
				label: $t('files.rename'),
				icon: 'pencil',
				onclick: handleRenameChat
			},
			{
				label: $t('chat.history.delete'),
				icon: 'trash',
				onclick: handleDeleteChat
			}
		]}
		onclose={closeChatMenu}
	/>
{/if}

<style>
	@reference "../../app.css";

	.ws-item {
		margin-bottom: 0.1875rem;
	}

	.workspace-row:hover {
		background: var(--app-hover);
	}

	.workspace-live-indicator {
		width: 0.42rem;
		height: 0.42rem;
		flex: none;
		border-radius: 999px;
		background: var(--app-fg-subtle);
		opacity: 0.5;
	}

	.workspace-live-indicator[data-status='live'] {
		background: rgb(34 197 94);
		opacity: 0.9;
		box-shadow: 0 0 0 0.15rem rgb(34 197 94 / 0.12);
	}

	.workspace-live-indicator[data-status='connecting'],
	.workspace-live-indicator[data-status='reconnecting'] {
		background: rgb(234 179 8);
		opacity: 0.85;
	}

	.workspace-live-indicator[data-status='failed'] {
		background: rgb(239 68 68);
		opacity: 0.9;
	}

	.workspace-workbench-count {
		display: inline-flex;
		align-items: center;
		min-height: 1.1rem;
		border: 1px solid color-mix(in oklab, var(--app-accent) 18%, var(--app-border));
		border-radius: 999px;
		padding: 0 0.3rem;
		font-size: 0.55rem;
		font-weight: 650;
		color: var(--app-muted-fg);
		white-space: nowrap;
	}

	@media (max-width: 767px) {
		.workspace-row {
			min-height: 2.75rem;
		}
	}

	.ws-icon-toggle {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 0.875rem;
		height: 0.875rem;
		cursor: pointer;
	}

	.ws-icon-folder {
		display: flex;
		transition: opacity 0.1s;
	}

	.ws-icon-chevron {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		opacity: 0;
		transition:
			opacity 0.1s,
			transform 0.1s;
		color: var(--app-fg-subtle);
	}

	:global(.dark) .ws-icon-chevron {
		color: var(--app-fg-muted);
	}

	.ws-icon-toggle:hover .ws-icon-folder {
		opacity: 0;
	}

	.ws-icon-toggle:hover .ws-icon-chevron {
		opacity: 1;
	}

	.ws-chats {
		margin-top: 0.125rem;
		padding-bottom: 0.25rem;
	}

	.ws-chat-show-more {
		display: block;
		width: 100%;
		padding: 0.125rem 0.5rem;
		border: none;
		background: none;
		cursor: pointer;
		font-size: 0.6875rem;
		color: var(--app-fg-subtle);
		text-align: left;
		transition: color 0.1s;
	}

	.ws-chat-show-more:hover {
		color: var(--app-fg);
	}

	.ws-chat-loading {
		display: flex;
		gap: 0.25rem;
		padding: 0.375rem 0.5rem;
	}

	.ws-chat-loading-dot {
		width: 0.25rem;
		height: 0.25rem;
		border-radius: 50%;
		background: var(--app-fg-subtle);
		animation: dotPulse 1s ease-in-out infinite;
	}

	:global(.dark) .ws-chat-loading-dot {
		background: var(--app-fg-muted);
	}

	.ws-chat-loading-dot:nth-child(2) {
		animation-delay: 0.15s;
	}

	.ws-chat-loading-dot:nth-child(3) {
		animation-delay: 0.3s;
	}

	@keyframes dotPulse {
		0%,
		100% {
			opacity: 0.3;
		}
		50% {
			opacity: 1;
		}
	}
</style>
