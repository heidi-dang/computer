<script lang="ts">
	import {
		getChat,
		getChats,
		deleteChat as apiDeleteChat,
		updateChatTitle,
		forkChat as apiForkChat,
		sendMessage as apiSendMessage,
		resolveToolCall,
		answerAskUser,
		cancelTask,
		compactChat as apiCompactChat,
		updateCurrentMessage,
		updateMessage,
		createMessage,
		queueSendNow as apiQueueSendNow,
		queueDelete as apiQueueDelete,
		updateChatSettings,
		type ChatMessageRow,
		type ChatSendParams,
		type ChatInfo,
		type ToolApprovalMode,
		type ContextUsage,
		type ChatTask
	} from '$lib/apis/chat';
	import {
		cancelFlowDeckOrchestration,
		createAudit,
		createFlowDeckOrchestration,
		getFlowDeckOrchestration,
		steerFlowDeckOrchestration
	} from '$lib/apis/flowdeck';
	import type { ChatAgent } from '../common/AgentSelector.svelte';
	import {
		chatModels,
		defaultModel,
		setChatReadAt,
		streamingChatTabs,
		registerStreamingChat,
		unregisterStreamingChat,
		updateChatStatuses,
		registerToolApprovalShortcutHandler
	} from '$lib/stores/chat';
	import { socketStore } from '$lib/stores/socket.svelte';
	import { onMount, onDestroy, tick } from 'svelte';
	import { get } from 'svelte/store';
	import {
		currentWorkspace,
		openChatTab,
		streamingBehavior,
		toolApprovalMode as defaultToolApprovalMode,
		widescreenMode
	} from '$lib/stores';
	import { getPathDisplayName } from '$lib/utils/paths';
	import {
		ttsEnabled,
		ttsConfigured,
		ttsFormat,
		setTtsAudioPlaybackSource,
		ttsAutoStreamEnabled,
		ttsPlaybackEnabled,
		ttsVoice,
		unlockTtsAudioPlayback
	} from '$lib/stores/audio';

	import ChatInput from './ChatInput.svelte';
	import FlowDeckStatusStrip from './FlowDeckStatusStrip.svelte';
	import type { DesignerAction } from './DesignerResults.svelte';
	import UserMessage from './UserMessage.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import ChatHistory from './ChatHistory.svelte';
	import StatusModal from './StatusModal.svelte';
	import SkillsModal from './SkillsModal.svelte';
	import { listCommandSessions, type CommandSession } from '$lib/apis/terminal';
	import { getSkills, type SkillInfo } from '$lib/apis/skills';
	import Spinner from '../common/Spinner.svelte';
	import Icon from '../Icon.svelte';
	import { tooltip } from '$lib/tooltip';
	import { toast } from 'svelte-sonner';
	import { t } from '$lib/i18n';

	type PreparedTtsAudio = {
		promise: Promise<Blob>;
		controller: AbortController;
		generation: number;
	};

	interface Props {
		workspace?: string;
		chatId?: string;
		tabId?: string;
		active?: boolean;
		ontabupdate?: (tabId: string, chatId: string, label: string) => void;
		onopenchat?: (chatId?: string) => void;
	}
	let {
		workspace = '',
		chatId: initialChatId,
		tabId,
		active = true,
		ontabupdate,
		onopenchat
	}: Props = $props();

	let inputText = $state('');
	let chatId = $state<string | null>(initialChatId ?? null);
	let selectedModel = $state('');
	let selectedAgent = $state<ChatAgent>('computer');
	let toolApprovalMode = $state<ToolApprovalMode>('auto');
	let planMode = $state(false);
	let requestParams = $state<Record<string, unknown>>({});
	let voiceModeEnabled = $state(false);
	let allMessages = $state<ChatMessageRow[]>([]);
	const pendingAskUser = $derived.by(() => {
		for (const message of [...allMessages].reverse()) {
			const item = message.output?.find(
				(output: any) =>
					output.type === 'function_call' &&
					output.name === 'ask_user' &&
					output.status === 'pending'
			);
			if (item) {
				return {
					item,
					output: message.output?.find(
						(output: any) =>
							output.type === 'function_call_output' && output.call_id === item.call_id
					),
					chatId,
					messageId: message.id
				};
			}
		}
		return null;
	});
	let currentMessageId = $state<string | null>(null);
	let contextUsage = $state<ContextUsage | null>(null);
	let chatTasks = $state<ChatTask[]>([]);
	let showStatusModal = $state(false);
	let showSkillsModal = $state(false);
	let skillsModalList = $state<SkillInfo[]>([]);
	let commandSessions = $state<CommandSession[]>([]);
	let initialCommandSessionId = $state<string | null>(null);
	let previousChats = $state<ChatInfo[]>([]);
	let messagesEl: HTMLDivElement;
	let chatInputEl: ChatInput;
	let statusButtonEl: HTMLButtonElement | undefined = $state();
	let sending = $state(false);
	let flowdeckStatus = $state('');
	let flowdeckRunId = $state('');
	let flowdeckIsAudit = $state(false);
	const flowdeckTelemetry = $derived.by(() => {
		const totals = {
			input_tokens: 0,
			output_tokens: 0,
			total_tokens: 0,
			cost: null as number | null
		};
		const seen = new Set<string>();
		const model = $chatModels.find((item) => item.id === selectedModel);
		const inputPrice = model?.input_price_per_1m;
		const outputPrice = model?.output_price_per_1m;
		for (const message of allMessages) {
			if (message.role !== 'assistant' || !message.usage || seen.has(message.id)) continue;
			if (!message.meta?.flowdeck && message.meta?.flowdeck_run_id !== flowdeckRunId) continue;
			seen.add(message.id);
			const usage = message.usage;
			totals.input_tokens += Number(usage.input_tokens || 0);
			totals.output_tokens += Number(usage.output_tokens || 0);
			totals.total_tokens += Number(usage.total_tokens || 0);
			if (
				inputPrice !== null &&
				inputPrice !== undefined &&
				outputPrice !== null &&
				outputPrice !== undefined
			) {
				totals.cost =
					(totals.cost || 0) +
					(Number(usage.input_tokens || 0) * inputPrice +
						Number(usage.output_tokens || 0) * outputPrice) /
						1_000_000;
			}
		}
		return totals;
	});
	let flowdeckPoller: ReturnType<typeof setInterval> | null = null;
	let flowdeckEvents = $state<any[]>([]);
	let flowdeckClarification = $state('');
	let flowdeckMessageId = $state<string | null>(null);
	const FLOWDECK_EVENT_LIMIT = 200;
	const FLOWDECK_TERMINAL_STATUSES = new Set([
		'succeeded',
		'completed',
		'failed',
		'cancelled',
		'unknown',
		'manual_review',
		'manual_review_required',
		'orphaned'
	]);

	function isFlowDeckTerminal(status: string) {
		return FLOWDECK_TERMINAL_STATUSES.has(status.toLowerCase());
	}
	let autoScroll = $state(true);
	let cancelledMessageId: string | null = null;
	let loading = $state(!!initialChatId);
	let chatTitle = $state('');
	let ttsQueue: string[] = [];
	let ttsBuffer = '';
	let ttsInsideCodeFence = false;
	let ttsPlaying = false;
	let ttsGeneration = 0;
	let ttsPrepareCursor = 0;
	let ttsPreparing = 0;
	let ttsAudio: HTMLAudioElement | null = null;
	let ttsObjectUrl: string | null = null;
	let ttsErrorShown = false;
	let speakingMessageId = $state<string | null>(null);
	let ttsStopRequested = false;
	let commandSessionsChatId: string | null = null;
	let taskClearTimer: ReturnType<typeof setTimeout> | null = null;
	// This browser memory cache only helps while the current page is open, such as when
	// someone taps the same speak button twice. The backend cache is the durable source
	// for cross-session reuse and the workspace data flywheel.
	let ttsAudioCacheBytes = 0;
	const ttsAudioCache = new Map<string, Blob>();
	const ttsPreparedAudio = new Map<string, PreparedTtsAudio>();
	const TTS_AUDIO_CACHE_LIMIT_BYTES = 20 * 1024 * 1024;
	const TTS_MAX_PREFETCH = 2;
	let unbindSocketListeners: (() => void) | null = null;
	let commandSessionsTimer: ReturnType<typeof setInterval> | null = null;

	onMount(() => {
		if (initialChatId || typeof sessionStorage === 'undefined') return;
		const key = `cptr:intent:chatDraft:${workspace}`;
		const draft = sessionStorage.getItem(key);
		if (draft) {
			inputText = draft;
			sessionStorage.removeItem(key);
		}
	});

	// ── Windowed rendering ──────────────────────────────────────
	// Only render the last N turns to keep the DOM light for long chats.
	// A "turn" is a user+assistant message pair, so 6 turns = 12 messages.

	const TURNS_PER_PAGE = 6;
	const MESSAGES_PER_PAGE = TURNS_PER_PAGE * 2; // user + assistant
	let visibleCount = $state(MESSAGES_PER_PAGE);

	// Reset visible window when chat changes
	$effect(() => {
		if (chatId) {
			visibleCount = MESSAGES_PER_PAGE;
		}
	});

	// ── Tree-aware path computation ─────────────────────────────

	interface PathEntry {
		msg: ChatMessageRow;
		siblingIds: string[];
		siblingIndex: number;
	}

	function buildChildrenMap(msgs: ChatMessageRow[]): Map<string | null, ChatMessageRow[]> {
		const map = new Map<string | null, ChatMessageRow[]>();
		for (const m of msgs) {
			const key = m.parent_id ?? null;
			if (!map.has(key)) map.set(key, []);
			map.get(key)!.push(m);
		}
		return map;
	}

	function isPendingHiddenMessage(m: ChatMessageRow): boolean {
		return !!(
			m.meta?.queued ||
			m.meta?.async_subagent_pending ||
			(m.meta?.internal === true && m.meta?.type === 'subagent' && m.meta?.status === 'pending')
		);
	}

	function nearestVisibleAncestorId(
		messageId: string | null,
		allMsgMap: Map<string, ChatMessageRow>,
		visibleMsgMap: Map<string, ChatMessageRow>
	): string | null {
		let cur = messageId;
		const seen = new Set<string>();
		while (cur && !seen.has(cur)) {
			seen.add(cur);
			if (visibleMsgMap.has(cur)) return cur;
			cur = allMsgMap.get(cur)?.parent_id ?? null;
		}
		return null;
	}

	function findLeaf(messageId: string, childrenMap: Map<string | null, ChatMessageRow[]>): string {
		let cur = messageId;
		while (true) {
			const children = childrenMap.get(cur);
			if (!children?.length) return cur;
			cur = children[children.length - 1].id; // follow latest child
		}
	}

	const activePath = $derived.by((): PathEntry[] => {
		if (!allMessages.length) return [];

		// Exclude pending internal inputs until the parent has processed them.
		const displayMessages = allMessages.filter((m) => !isPendingHiddenMessage(m));
		if (!displayMessages.length) return [];

		const allMsgMap = new Map(allMessages.map((m) => [m.id, m]));
		const msgMap = new Map(displayMessages.map((m) => [m.id, m]));
		const childrenMap = buildChildrenMap(displayMessages);

		// Determine effective currentId. If the persisted current leaf points at
		// a hidden queued/internal input, anchor the display at its visible parent
		// instead of falling onto an unrelated branch.
		const visibleCurrentId = nearestVisibleAncestorId(currentMessageId, allMsgMap, msgMap);
		const effectiveId =
			visibleCurrentId && msgMap.has(visibleCurrentId)
				? visibleCurrentId
				: displayMessages.length > 0
					? displayMessages[displayMessages.length - 1].id
					: null;

		if (!effectiveId) return [];

		// Walk up from effectiveId to root to find ancestor set
		const ancestorIds = new Set<string>();
		let cur: string | null = effectiveId;
		while (cur) {
			ancestorIds.add(cur);
			const msg = msgMap.get(cur);
			cur = msg?.parent_id ?? null;
		}

		// Walk down from root, at each level pick the child in ancestorIds
		const path: PathEntry[] = [];
		let parentId: string | null = null;
		while (true) {
			const siblings = childrenMap.get(parentId);
			if (!siblings?.length) break;
			const next = siblings.find((c) => ancestorIds.has(c.id)) || siblings[0];
			path.push({
				msg: next,
				siblingIds: siblings.map((s) => s.id),
				siblingIndex: siblings.findIndex((s) => s.id === next.id)
			});
			parentId = next.id;
		}

		return path;
	});
	const activePathIds = $derived(new Set(activePath.map(({ msg }) => msg.id)));

	// ── Visible slice of the active path ────────────────────────
	const hasHiddenMessages = $derived(activePath.length > visibleCount);
	const visiblePath = $derived(
		hasHiddenMessages ? activePath.slice(activePath.length - visibleCount) : activePath
	);

	let loadSentinelEl: HTMLDivElement;
	let loadObserver: IntersectionObserver | null = null;
	let loadingMore = false;

	function loadMoreMessages() {
		if (!messagesEl || loadingMore) return;
		loadingMore = true;
		// Remember scroll height before loading more so we can maintain position
		const prevScrollHeight = messagesEl.scrollHeight;

		visibleCount = Math.min(visibleCount + MESSAGES_PER_PAGE, activePath.length);

		// After DOM updates, restore scroll position so the view doesn't jump
		tick().then(() => {
			if (messagesEl) {
				const newScrollHeight = messagesEl.scrollHeight;
				messagesEl.scrollTop += newScrollHeight - prevScrollHeight;
			}
			loadingMore = false;
		});
	}

	// Set up IntersectionObserver to auto-load earlier messages when sentinel is visible
	$effect(() => {
		if (hasHiddenMessages && loadSentinelEl && messagesEl) {
			loadObserver = new IntersectionObserver(
				(entries) => {
					if (entries[0]?.isIntersecting && hasHiddenMessages) {
						loadMoreMessages();
					}
				},
				{ root: messagesEl, threshold: 0 }
			);
			loadObserver.observe(loadSentinelEl);
		}

		return () => {
			if (loadObserver) {
				loadObserver.disconnect();
				loadObserver = null;
			}
		};
	});

	const streaming = $derived(allMessages.some((m) => m.role === 'assistant' && !m.done));
	const isLanding = $derived(allMessages.length === 0 && !chatId);
	const hasChatContent = $derived(
		activePath.some(({ msg }) => msg.role === 'user' && msg.content.trim())
	);
	const workspaceDisplayName = $derived(
		workspace ? getPathDisplayName(workspace, 'workspace') : 'Computer'
	);
	const displayChatTitle = $derived(chatTitle || firstUserMessageTitle() || workspaceDisplayName);
	const runningCommandSessions = $derived(commandSessions.filter((session) => !session.done));
	const summaryCount = $derived(runningCommandSessions.length);
	const statusButtonClass =
		'text-gray-400 hover:bg-gray-50 hover:text-gray-700 dark:text-gray-600 dark:hover:bg-white/5 dark:hover:text-gray-300';
	const statusTitle = $derived(
		summaryCount > 0 ? `${summaryCount} active command session` : 'Status'
	);

	// Queued messages: user-authored messages waiting behind an active response.
	const queuedMessages = $derived(
		allMessages
			.filter(
				(m) =>
					m.role === 'user' &&
					m.meta?.queued &&
					(m.parent_id ? activePathIds.has(m.parent_id) : activePath.length === 0)
			)
			.map((m) => ({ id: m.id, content: m.content }))
	);

	function setChatTasks(tasks: ChatTask[]) {
		if (taskClearTimer) {
			clearTimeout(taskClearTimer);
			taskClearTimer = null;
		}
		chatTasks = tasks;
		if (
			tasks.length > 0 &&
			!tasks.some((task) => task.status === 'pending' || task.status === 'in_progress')
		) {
			taskClearTimer = setTimeout(() => {
				taskClearTimer = null;
				chatTasks = [];
			}, 4000);
		}
	}

	$effect(() => {
		if (chatId === commandSessionsChatId) return;
		commandSessionsChatId = chatId;
		commandSessions = [];
		if (chatId) refreshCommandSessions();
	});

	// ── Load chat from DB ───────────────────────────────────────

	let loadGeneration = 0;
	let loadedChatId = $state<string | null>(null);

	function markChatRead(id: string) {
		setChatReadAt(id);
		socketStore.getSocket()?.emit('chat:read', { chat_id: id });
	}

	async function loadChat(id: string) {
		if (chatId && chatId !== id) stopTtsPlayback();
		chatId = id;
		loadedChatId = null;
		const gen = ++loadGeneration;
		// Only show loading spinner on initial load (no messages yet).
		// On reloads (e.g. after cancel/done), keep the DOM intact to preserve scroll position.
		const isInitialLoad = allMessages.length === 0;
		if (isInitialLoad) loading = true;

		// Snapshot scroll position before replacing messages so we can restore it
		const savedScroll = !isInitialLoad && messagesEl ? messagesEl.scrollTop : -1;

		try {
			const data = await getChat(id);
			// Discard stale response if a newer loadChat was called while we waited
			if (gen !== loadGeneration) return;
			allMessages = data.messages;
			loadChatSettings(data.chat.meta);
			const flowdeckMeta = data.chat.meta as Record<string, any> | null;
			const persistedFlowDeckRunId = String(flowdeckMeta?.flowdeck_run_id || '');
			flowdeckIsAudit = flowdeckMeta?.audit === true;
			const persistedFlowDeckStatus = String(flowdeckMeta?.flowdeck_status || '').toLowerCase();
			const activeFlowDeckAssistant = [...data.messages]
				.reverse()
				.find(
					(message) =>
						message.role === 'assistant' &&
						(message.meta as any)?.flowdeck_run_id === persistedFlowDeckRunId
				);
			if (persistedFlowDeckRunId) {
				flowdeckRunId = persistedFlowDeckRunId;
				flowdeckStatus = persistedFlowDeckStatus || 'active';
				flowdeckMessageId = activeFlowDeckAssistant?.id || flowdeckMessageId;
				if (!isFlowDeckTerminal(flowdeckStatus)) startFlowDeckPolling(flowdeckRunId);
			}
			currentMessageId = data.chat.current_message_id;
			contextUsage = data.context_usage ?? null;
			setChatTasks(
				(data.tasks ?? []).some(
					(task) => task.status === 'pending' || task.status === 'in_progress'
				)
					? (data.tasks ?? [])
					: []
			);
			// Update tab label with the real title from the DB
			if (tabId && data.chat.title) {
				chatTitle = data.chat.title;
				updateTab(tabId, id, data.chat.title);
			}
			loadedChatId = id;
			updateChatStatuses([data.chat], workspace);
		} finally {
			if (isInitialLoad && gen === loadGeneration) loading = false;
		}

		// On reloads, restore scroll position after the DOM re-renders.
		// Skip if autoScroll is active — the auto-scroll effect will keep us at the bottom.
		if (savedScroll >= 0 && !autoScroll) {
			await tick();
			if (messagesEl) {
				messagesEl.scrollTop = savedScroll;
				// Also restore after rAF to beat any competing scroll operations
				requestAnimationFrame(() => {
					if (messagesEl) messagesEl.scrollTop = savedScroll;
				});
			}
		}
	}

	const CHATS_PAGE_SIZE = 10;
	let chatPage = $state(1);
	let totalChats = $state(0);
	let chatSortBy = $state<'title' | 'updated_at'>('updated_at');
	let chatSortDir = $state<'asc' | 'desc'>('desc');
	const totalPages = $derived(Math.max(1, Math.ceil(totalChats / CHATS_PAGE_SIZE)));

	async function loadPreviousChats(page = 1) {
		try {
			const offset = (page - 1) * CHATS_PAGE_SIZE;
			const data = await getChats(workspace, CHATS_PAGE_SIZE, offset, chatSortBy, chatSortDir);
			previousChats =
				chatSortBy === 'updated_at'
					? [...(data.chats || [])].sort(
							(a, b) =>
								Number(!b.is_active && (b.last_read_at === null || b.updated_at > b.last_read_at)) -
									Number(
										!a.is_active && (a.last_read_at === null || a.updated_at > a.last_read_at)
									) ||
								(chatSortDir === 'desc' ? b.updated_at - a.updated_at : a.updated_at - b.updated_at)
						)
					: data.chats || [];
			totalChats = data.total;
			chatPage = page;
		} catch {
			previousChats = [];
			totalChats = 0;
		}
	}

	function handlePageChange(page: number) {
		loadPreviousChats(page);
	}

	function handleSort(field: 'title' | 'updated_at') {
		if (chatSortBy === field) {
			chatSortDir = chatSortDir === 'asc' ? 'desc' : 'asc';
		} else {
			chatSortBy = field;
			chatSortDir = field === 'title' ? 'asc' : 'desc';
		}
		loadPreviousChats(1);
	}

	async function openChat(id: string) {
		await loadChat(id);
		const chat = previousChats.find((c) => c.id === id);
		if (tabId) updateTab(tabId, id, chat?.title || $t('chat.fallbackTitle'));
	}

	async function deleteChat(id: string) {
		await apiDeleteChat(id);
		previousChats = previousChats.filter((c) => c.id !== id);
	}

	async function renameChat(id: string) {
		const chat = previousChats.find((c) => c.id === id);
		const title = window.prompt($t('files.rename'), chat?.title)?.trim();
		if (!title || title === chat?.title) return;
		try {
			await updateChatTitle(id, title);
			previousChats = previousChats.map((c) => (c.id === id ? { ...c, title } : c));
			if (chatId === id) {
				chatTitle = title;
				if (tabId) updateTab(tabId, id, title);
			}
		} catch (e) {
			toast.error(e instanceof Error ? e.message : $t('files.rename'));
		}
	}

	function copyChatPath(id: string) {
		if (!workspace) return;
		const chat = previousChats.find((c) => c.id === id);
		if (!chat) return;
		navigator.clipboard.writeText(
			`${workspace.replace(/\/$/, '')}/.cptr/chats/${chat.folder ? `${chat.folder}/` : ''}${chat.id}.json`
		);
	}

	// ── Socket listener ─────────────────────────────────────────

	let landingRefreshTimer: ReturnType<typeof setTimeout> | null = null;

	function handleSocketEvent(data: {
		type?: string;
		chat_id: string;
		message_id?: string;
		delta?: string;
		output?: any;
		tasks?: ChatTask[];
		context_usage?: ContextUsage | null;
		done?: boolean;
		error?: string;
		pending_inputs_processed?: boolean;
		async_subagent_pending?: boolean;
		title?: string;
		workspace?: string;
		active?: boolean;
		updated_at?: number;
		last_read_at?: number;
		flowdeck_run_id?: string;
		flowdeck_parent_run_id?: string;
		kind?: string;
		payload?: Record<string, unknown>;
		sequence?: number;
		created_at?: number;
		status?: string;
		content?: string;
	}) {
		const parentRunId = data.flowdeck_parent_run_id || data.flowdeck_run_id;
		if (parentRunId) {
			if (!flowdeckRunId && flowdeckMessageId && !isFlowDeckTerminal(flowdeckStatus)) {
				flowdeckRunId = parentRunId;
				startFlowDeckPolling(flowdeckRunId);
			}
			mergeFlowDeckEvent(data);
		}

		// On the landing page, update the chat list in place from socket events
		if (isLanding) {
			const knownChat = previousChats.some((c) => c.id === data.chat_id);
			if (!knownChat) {
				// New chat created elsewhere — debounce-reload the list
				if (landingRefreshTimer) clearTimeout(landingRefreshTimer);
				landingRefreshTimer = setTimeout(() => {
					landingRefreshTimer = null;
					loadPreviousChats(chatPage);
				}, 300);
			} else {
				const nextChats = previousChats.map((c) =>
					c.id === data.chat_id
						? {
								...c,
								...(data.title ? { title: data.title } : {}),
								...(data.done ? { is_active: false } : {}),
								...(typeof data.active === 'boolean' ? { is_active: data.active } : {}),
								...(typeof data.updated_at === 'number' ? { updated_at: data.updated_at } : {}),
								...(typeof data.last_read_at === 'number'
									? { last_read_at: data.last_read_at }
									: {})
							}
						: c
				);
				previousChats =
					chatSortBy === 'updated_at'
						? nextChats.sort(
								(a, b) =>
									Number(
										!b.is_active && (b.last_read_at === null || b.updated_at > b.last_read_at)
									) -
										Number(
											!a.is_active && (a.last_read_at === null || a.updated_at > a.last_read_at)
										) ||
									(chatSortDir === 'desc'
										? b.updated_at - a.updated_at
										: a.updated_at - b.updated_at)
							)
						: nextChats;
				if (typeof data.last_read_at === 'number') {
					if (landingRefreshTimer) clearTimeout(landingRefreshTimer);
					landingRefreshTimer = setTimeout(() => {
						landingRefreshTimer = null;
						loadPreviousChats(chatPage);
					}, 100);
				}
			}
		}

		// FlowDeck specialist chats have their own chat IDs. Their authenticated
		// events still belong in Heidi's native-looking assistant turn.
		if (parentRunId && parentRunId === flowdeckRunId) {
			applyFlowDeckEventToMessage(data);
			return;
		}
		if (data.chat_id !== chatId) return;

		if (data.type === 'chat:tasks') {
			setChatTasks(data.tasks ?? []);
			return;
		}

		// Title updates also keep open chat tabs in sync.
		if (data.title && tabId) {
			chatTitle = data.title;
			updateTab(tabId, data.chat_id, data.title);
		}
		if (data.context_usage) {
			contextUsage = data.context_usage;
		}

		// Follow-up state changed server-side: reload to see new transcript/generation state.
		if (data.pending_inputs_processed || data.async_subagent_pending) {
			loadChat(data.chat_id);
			return;
		}

		if (!data.message_id) return;
		const msg = allMessages.find((m) => m.id === data.message_id);
		if (!msg) return;
		applyNativeTranscriptEvent(data, msg);
	}

	function applyNativeTranscriptEvent(data: any, msg: any) {
		if (data.delta) {
			msg.content += data.delta;
			allMessages = [...allMessages];
			if (data.message_id) handleTtsDelta(data.message_id, data.delta);
		}
		if (data.output) {
			if (data.output.type === 'function_call') {
				if (data.output.status === 'pending') stopTtsPlayback();
				else resetTtsBuffer();
			}
			const existing = msg.output || [];
			const callId = data.output.call_id;
			const itemType = data.output.type;
			if (itemType === 'reasoning') {
				const itemId = data.output.id;
				const existingIdx = itemId
					? existing.findIndex((o: any) => o.type === 'reasoning' && o.id === itemId)
					: existing.findIndex((o: any) => o.type === 'reasoning');
				if (existingIdx >= 0) {
					existing[existingIdx] = data.output;
					msg.output = [...existing];
				} else {
					msg.output = [...existing, data.output];
				}
			} else if (callId && itemType) {
				const existingIdx = existing.findIndex(
					(o: any) => o.type === itemType && o.call_id === callId
				);
				if (existingIdx >= 0) {
					existing[existingIdx] = { ...existing[existingIdx], ...data.output };
					msg.output = [...existing];
				} else {
					msg.output = [...existing, data.output];
				}
			} else {
				msg.output = [...existing, data.output];
			}
			allMessages = [...allMessages];
		}
		if (data.error) toast.error(data.error, { duration: 8000 });
		if (!data.done) return;
		const isChildFlowDeckCompletion =
			data.flowdeck_parent_run_id &&
			data.flowdeck_run_id &&
			data.flowdeck_parent_run_id !== data.flowdeck_run_id;
		if (isChildFlowDeckCompletion) {
			// Child native events share Heidi's transcript message, but only the
			// parent run may end native streaming or move the durable status to
			// verification. Child completion is already represented by the
			// FlowDeck event bridge above.
			return;
		}

		flushTtsBuffer();
		sending = false;
		if (tabId) {
			streamingChatTabs.update((s) => {
				const n = new Set(s);
				n.delete(tabId);
				return n;
			});
		}
		if (cancelledMessageId === data.message_id) {
			cancelledMessageId = null;
			return;
		}
		msg.done = true;
		if (data.error && data.content && !msg.content) msg.content = data.content;
		if (chatTasks.some((task) => task.status === 'pending' || task.status === 'in_progress')) {
			setChatTasks([]);
		}
		if (data.flowdeck_run_id) {
			// Native completion ends transcript streaming; durable FlowDeck
			// completion still owns the orchestration status and is reconciled
			// by the existing poller.
			flowdeckStatus = data.error ? 'failed' : 'verifying';
		} else if (data.chat_id) {
			loadChat(data.chat_id);
		}
		allMessages = [...allMessages];
	}

	function flowDeckEventKey(event: any): string {
		const run = String(
			event?.flowdeck_parent_run_id ||
				event?.flowdeck_run_id ||
				event?.run_id ||
				flowdeckRunId ||
				''
		);
		if (event?.id || event?.event_id) return `${run}:id:${event.id || event.event_id}`;
		const output = event?.output || {};
		const payload = event?.payload || {};
		return [
			run,
			event?.sequence ?? '',
			event?.message_id ?? '',
			event?.kind || event?.type || '',
			output.type || '',
			output.call_id || output.id || '',
			output.status || '',
			payload.step_id || payload.specialist_id || '',
			event?.delta || ''
		].join('|');
	}

	function mergeFlowDeckEvents(events: any[]) {
		for (const event of events) mergeFlowDeckEvent(event);
	}

	function mergeFlowDeckEvent(event: any) {
		const parentRunId =
			event?.flowdeck_parent_run_id ||
			event?.run_id ||
			(event?.flowdeck_run_id && !event?.output && !event?.delta ? event.flowdeck_run_id : null);
		if (!parentRunId || parentRunId !== flowdeckRunId) return;
		const normalized = event?.run_id ? { ...event, flowdeck_parent_run_id: event.run_id } : event;
		const next = [...flowdeckEvents];
		const key = flowDeckEventKey(normalized);
		const index = next.findIndex((item) => flowDeckEventKey(item) === key);
		if (index >= 0) next[index] = { ...next[index], ...normalized };
		// Poll responses are authoritative and carry sequence numbers. Sort only
		// when both records have durable sequence positions; otherwise retain
		// arrival order so a socket update cannot jump ahead of an earlier call.
		if (next.every((item) => Number.isFinite(Number(item?.sequence)))) {
			next.sort((a, b) => Number(a.sequence) - Number(b.sequence));
		}
		flowdeckEvents = next.slice(-FLOWDECK_EVENT_LIMIT);
	}

	function flowDeckActivityText(event: any): string | null {
		const kind = String(event?.kind || event?.type || '').toUpperCase();
		const payload = event?.payload || {};
		if (kind === 'BUILD_BRIEF_CREATED') return 'Build · product brief created';
		if (kind === 'BUILD_COMPLETION_CONTRACT_CREATED')
			return 'Build · completion contract ready for review';
if (kind === 'HEIDI_VALIDATION_STARTED')
return 'Heidi · validating the task against the implementation and platform boundaries';
if (kind === 'HEIDI_VALIDATION_PASSED')
return 'Heidi · validation passed; proceeding with the qualified CPTR path';
if (kind === 'HEIDI_VALIDATION_FAILED')
return `Heidi · execution stopped: ${String(payload.reason || 'request is outside the current boundaries')}`;
if (kind === 'HEIDI_VALIDATION_CLARIFICATION')
return 'Heidi · clarification required before execution';
		if (kind === 'BUILD_VERIFICATION_REQUIRED')
			return 'Build · verification required before completion';
		if (kind === 'SPECIALIST_DISPATCHED' || kind.includes('DELEGAT')) {
			return `Specialist handoff · ${String(payload.specialist_id || payload.child_agent_id || 'qualified specialist')}`;
		}
		if (kind.includes('VERIF') || kind.includes('OUTCOME') || kind.includes('REVIEW')) {
			return `Verification · ${String(payload.status || payload.observed_outcome || 'activity recorded')}`;
		}
		if (kind.includes('STEP_STARTED')) return 'Planning · specialist step started';
		if (kind.includes('STEP_COMPLETED')) return 'Working · specialist step completed';
		if (kind === 'RUN_CLARIFICATION') return null;
		return null;
	}

	function flowDeckStatusForEvent(event: any): string | null {
		const kind = String(event?.kind || event?.type || '').toUpperCase();
		const reported = String(event?.status || event?.payload?.status || '').toLowerCase();
		if (isFlowDeckTerminal(reported)) return reported;
if (reported && ['active', 'planning', 'validating', 'verifying'].includes(reported)) return reported;
if (kind.includes('VALIDATION')) return 'validating';
		if (kind.includes('VERIF') || kind.includes('OUTCOME') || kind.includes('REVIEW'))
			return 'verifying';
		if (kind.includes('STEP_STARTED') || kind.includes('PLAN')) return 'planning';
		if (kind.includes('RUN_STARTED') || kind.includes('DISPATCH') || kind.includes('DELEGAT'))
			return 'active';
		return null;
	}

	function applyFlowDeckEventToMessage(event: any) {
		if (!flowdeckMessageId) return;
		const reportedStatus = flowDeckStatusForEvent(event);
		if (reportedStatus) flowdeckStatus = reportedStatus;
		const message = allMessages.find((item) => item.id === flowdeckMessageId);
		if (!message) return;
		applyNativeTranscriptEvent(event, message);
		const activity = flowDeckActivityText(event);
		if (
			activity &&
			!(message.output || []).some((item: any) =>
				(item.content || []).some((part: any) => part.text === activity)
			)
		) {
			message.output = [
				...(message.output || []),
				{
					type: 'message',
					id: `flowdeck-activity-${flowDeckEventKey(event)}`,
					status: 'completed',
					role: 'assistant',
					content: [{ type: 'output_text', text: activity }]
				}
			];
		}
		const eventStatus = String(event?.status || event?.payload?.status || '').toLowerCase();
		const kind = String(event?.kind || event?.type || '').toUpperCase();
		if (
			kind === 'RUN_COMPLETED' ||
			kind === 'RUN_CANCELLED' ||
			kind === 'RUN_ORPHANED' ||
			kind === 'RUN_FAILED' ||
			kind === 'RUN_UNKNOWN' ||
			kind === 'RUN_MANUAL_REVIEW'
		) {
			flowdeckStatus =
				eventStatus ||
				(kind === 'RUN_CANCELLED'
					? 'cancelled'
					: kind === 'RUN_FAILED' || kind === 'RUN_ORPHANED'
						? 'failed'
						: 'succeeded');
			message.done = true;
			stopFlowDeckPolling();
		}
		allMessages = [...allMessages];
	}

	function handleReconnect() {
		if (chatId) loadChat(chatId);
	}

	function handleDesignerAction(action: DesignerAction) {
		const variantText = action.variantIds?.length ? ` (${action.variantIds.join(', ')})` : '';
		void steerActiveFlowDeck(`${action.label}${variantText}. Keep the native transcript linked and report the resulting evidence.`);
	}

	function resetChatSettings() {
		const models = get(chatModels);
		const dm = get(defaultModel);
		if (dm) selectedModel = dm;
		else if (models.length) selectedModel = models[0].id;
		toolApprovalMode = get(defaultToolApprovalMode);
		planMode = false;
		requestParams = {};
		voiceModeEnabled = false;
		selectedAgent = 'computer';
		flowdeckStatus = '';
		flowdeckRunId = '';
		flowdeckIsAudit = false;
		flowdeckEvents = [];
		flowdeckClarification = '';
		flowdeckMessageId = null;
	}

	function loadChatSettings(meta: Record<string, any> | null) {
		resetChatSettings();
		const params = meta?.params;
		if (!params || typeof params !== 'object') return;
		if (
			params.tool_approval_mode === 'ask' ||
			params.tool_approval_mode === 'auto' ||
			params.tool_approval_mode === 'full'
		) {
			toolApprovalMode = params.tool_approval_mode;
		}
		planMode = params.plan_mode === true;
		if (params.request_params && typeof params.request_params === 'object') {
			requestParams = params.request_params;
		}
		voiceModeEnabled = params.voice_mode === true;
		if (params.agent === 'heidi' || params.agent === 'computer') selectedAgent = params.agent;
		const models = get(chatModels);
		if (
			typeof meta?.last_model === 'string' &&
			models.some((model) => model.id === meta.last_model)
		) {
			selectedModel = meta.last_model;
		}
	}

	async function persistChatSettings() {
		if (!chatId || (selectedAgent !== 'heidi' && !selectedModel)) return;
		await updateChatSettings(chatId, selectedModel, getChatSendParams()).catch(() => {});
	}

	onMount(() => {
		resetChatSettings();

		if (chatId) {
			loadChat(chatId);
		} else {
			loadPreviousChats();
		}
		refreshCommandSessions();
		commandSessionsTimer = setInterval(refreshCommandSessions, 5000);
		window.addEventListener('computer:inspectCommandSession', handleInspectCommandSession);

		const offChat = socketStore.on('events:chat', handleSocketEvent);
		const offConnect = socketStore.on('connect', handleReconnect);
		unbindSocketListeners = () => {
			offChat();
			offConnect();
		};
	});

	onDestroy(() => {
		stopTtsPlayback();
		unbindSocketListeners?.();
		unbindSocketListeners = null;
		stopFlowDeckPolling();
		if (commandSessionsTimer) clearInterval(commandSessionsTimer);
		commandSessionsTimer = null;
		window.removeEventListener('computer:inspectCommandSession', handleInspectCommandSession);
		if (landingRefreshTimer) clearTimeout(landingRefreshTimer);
		if (taskClearTimer) clearTimeout(taskClearTimer);
		// Don't clear streamingChatTabs here -- the global listener in
		// chat.ts handles cleanup when the "done" event arrives, so the
		// spinner persists even when the chat tab is not active.
	});

	$effect(() => {
		if (!$ttsEnabled || !$ttsConfigured) {
			voiceModeEnabled = false;
			ttsPlaybackEnabled.set(false);
			stopTtsPlayback();
		} else if (
			(!$ttsPlaybackEnabled && !voiceModeEnabled && !$ttsAutoStreamEnabled) ||
			!$ttsConfigured
		) {
			stopTtsPlayback();
		}
	});

	// ── Sync streaming state to shared store for tab icon ────

	$effect(() => {
		if (!tabId) return;
		const tid = tabId;
		streamingChatTabs.update((s) => {
			const next = new Set(s);
			if (streaming) next.add(tid);
			else next.delete(tid);
			return next;
		});
	});

	// ── Register chatId→tabId mapping for global done listener ─

	$effect(() => {
		if (!chatId || !tabId) return;
		registerStreamingChat(chatId, tabId);
		return () => unregisterStreamingChat(chatId!, tabId!);
	});

	$effect(() => {
		if (!active) return;
		return registerToolApprovalShortcutHandler(handleApproveToolCallShortcut);
	});

	// ── Persist read state separately from visibility tracking ──

	$effect(() => {
		if (!chatId || loadedChatId !== chatId || !active) return;
		const id = chatId;
		const send = () => markChatRead(id);
		send();
		return socketStore.on('connect', send);
	});

	$effect(() => {
		if (!chatId || !active) return;
		const id = chatId;
		return () => markChatRead(id);
	});

	$effect(() => {
		if (!chatId || !tabId) return;
		const send = () =>
			socketStore
				.getSocket()
				?.emit('chat:view', { chat_id: chatId, view_id: tabId, visible: active });
		send();
		const off = socketStore.on('connect', send);
		return () => {
			off();
			socketStore
				.getSocket()
				?.emit('chat:view', { chat_id: chatId, view_id: tabId, visible: false });
		};
	});

	// ── Auto-scroll ─────────────────────────────────────────────

	function scrollToBottom() {
		if (!messagesEl) return;
		messagesEl.scrollTop = messagesEl.scrollHeight;
		// Follow-up scroll to account for content-visibility re-layouts
		requestAnimationFrame(() => {
			if (messagesEl) {
				messagesEl.scrollTop = messagesEl.scrollHeight;
			}
		});
	}

	let lastScrollTop = 0;

	function handleMessagesScroll() {
		if (!messagesEl) return;
		const { scrollTop, scrollHeight, clientHeight } = messagesEl;

		// If the user scrolled upward, immediately disengage auto-scroll
		if (scrollTop < lastScrollTop) {
			autoScroll = false;
		} else {
			// Re-engage only when very close to the bottom
			autoScroll = scrollHeight - scrollTop <= clientHeight + 200;
		}

		lastScrollTop = scrollTop;
	}

	$effect(() => {
		if (activePath.length && messagesEl && autoScroll) {
			requestAnimationFrame(() => {
				scrollToBottom();
			});
		}
	});

	// ── Actions ─────────────────────────────────────────────────

	function getChatSendParams(): ChatSendParams {
		const params: ChatSendParams = {
			agent: selectedAgent,
			tool_approval_mode: toolApprovalMode,
			plan_mode: planMode,
			request_params: requestParams
		};
		if (voiceModeEnabled) params.voice_mode = true;
		return params;
	}

	async function send() {
		let text = inputText.trim();
		if (!text || (selectedAgent !== 'heidi' && !selectedModel)) return;
		if (sending) return;
		if (hasChatContent && text === '/compact') {
			await handleManualCompact();
			return;
		}
		if (hasChatContent && text === '/fork') {
			await handleForkChat();
			return;
		}
		if (text === '/plan') {
			handlePlanCommand();
			inputText = '';
			return;
		}
		if (hasChatContent && text === '/status') {
			handleStatusCommand();
			inputText = '';
			return;
		}
		if (hasChatContent && text === '/skills:list') {
			await handleSkillsListCommand();
			inputText = '';
			return;
		}
		if (
			selectedAgent === 'heidi' &&
			chatId &&
			flowdeckRunId &&
			!isFlowDeckTerminal(flowdeckStatus)
		) {
			await steerActiveFlowDeck(text);
			return;
		}
		if (selectedAgent === 'heidi') {
			await sendToFlowDeck(text);
			return;
		}
		stopTtsPlayback();
		if (shouldStreamTts()) void unlockTtsAudioPlayback();
		sending = true;
		const files = chatInputEl?.getFiles() ?? [];
		// Transform TipTap mention format to markdown file links
		text = text.replace(
			/\[@\s+id="([^"]+)"\s+label="([^"]+)"\]/g,
			(_: string, id: string, label: string) => `[${label}](file://${id})`
		);
		inputText = '';
		chatInputEl?.clearUploads();
		autoScroll = true;
		await tick();
		chatInputEl?.resetHeight();

		// parent_id = last message in active path, or null for first message
		const lastMsg = activePath.length > 0 ? activePath[activePath.length - 1].msg : null;
		const parentId = lastMsg?.id ?? null;
		const isNew = !chatId;

		// When streaming and behavior is 'queue', backend will enqueue
		// When streaming and behavior is 'interrupt', cancel first then send normally
		if (streaming && chatId) {
			const behavior = get(streamingBehavior);
			if (behavior === 'interrupt') {
				// Cancel active generation, then fall through to normal send
				const active = allMessages.find((m) => m.role === 'assistant' && !m.done);
				if (active) {
					try {
						await cancelTask(chatId, active.id);
					} catch {}
					await loadChat(chatId);
				}
			} else {
				try {
					const result = await apiSendMessage(
						text,
						selectedModel,
						workspace,
						chatId,
						parentId,
						getChatSendParams(),
						undefined,
						files
					);
					if (result.queued) {
						// Add directly to allMessages so it appears in queue UI instantly
						allMessages = [
							...allMessages,
							{
								id: result.message_id,
								parent_id: parentId,
								role: 'user' as const,
								content: text,
								model: selectedModel,
								done: true,
								output: null,
								usage: null,
								meta: { queued: true },
								created_at: Date.now()
							}
						];
					} else if (result.assistant_message) {
						const newMessages = result.user_message
							? [result.user_message, result.assistant_message]
							: [result.assistant_message];
						allMessages = [...allMessages, ...newMessages];
						currentMessageId = result.message_id;
					}
				} catch (e) {
					console.error('[chat] send (queue) error', e);
				} finally {
					sending = false;
					chatInputEl?.focus();
				}
				return;
			} // end queue behavior
		} // end streaming check

		// ── Normal flow: optimistic UI ────────────────────────────
		const tempId = `temp-${Date.now()}`;
		const optimisticMsg: ChatMessageRow = {
			id: tempId,
			parent_id: parentId,
			role: 'user',
			content: text,
			model: selectedModel,
			done: true,
			output: null,
			usage: null,
			meta: null,
			created_at: Date.now() / 1000
		};
		allMessages = [...allMessages, optimisticMsg];
		currentMessageId = tempId;

		// Update tab label instantly for new chats
		if (isNew && tabId) {
			updateTab(tabId, `pending-${tempId}`, text.slice(0, 40) || $t('chat.fallbackTitle'));
		}

		try {
			const result = await apiSendMessage(
				text,
				selectedModel,
				workspace,
				chatId ?? undefined,
				parentId,
				getChatSendParams(),
				undefined,
				files
			);

			// Swap optimistic temp msg with real messages from backend.
			chatId = result.chat_id;
			const withoutTemp = allMessages.filter((m) => m.id !== tempId);
			if (result.user_message && result.assistant_message) {
				allMessages = [...withoutTemp, result.user_message, result.assistant_message];
			} else if (result.assistant_message) {
				allMessages = [...withoutTemp, result.assistant_message];
			}
			currentMessageId = result.message_id;

			if (isNew && tabId) {
				updateTab(tabId, result.chat_id, text.slice(0, 40) || $t('chat.fallbackTitle'));
			}
		} catch (e) {
			console.error('[chat] send error', e);
			allMessages = allMessages.filter((m) => m.id !== tempId);
			currentMessageId = parentId;
			throw e;
		} finally {
			sending = false;
			chatInputEl?.focus();
		}
	}

	async function steerActiveFlowDeck(text: string) {
		if (!chatId || !flowdeckRunId) return;
		stopTtsPlayback();
		sending = true;
		inputText = '';
		try {
			const result = await steerFlowDeckOrchestration(
				flowdeckRunId,
				chatId,
				text,
				`chat-flowdeck-steer-${crypto.randomUUID()}`
			);
			if (!result.accepted) {
				toast.message(result.message || 'Heidi is no longer running.');
				return;
			}
			const steeringMessageId = String(result.message_id || '');
			if (steeringMessageId && !allMessages.some((message) => message.id === steeringMessageId)) {
				const parent = allMessages.find((message) => message.id === flowdeckMessageId);
				allMessages = [
					...allMessages,
					{
						id: steeringMessageId,
						parent_id: parent?.id || null,
						role: 'user',
						content: text,
						model: parent?.model || selectedModel,
						done: true,
						output: null,
						usage: null,
						meta: {
							agent: 'heidi',
							flowdeck: true,
							flowdeck_steering: true,
							flowdeck_run_id: flowdeckRunId,
							queued: true
						},
						created_at: Date.now() / 1000
					}
				];
			}
		} catch (error) {
			toast.error(
				error instanceof Error ? error.message : 'Heidi could not queue that instruction.'
			);
		} finally {
			sending = false;
			chatInputEl?.focus();
		}
	}

	function stopFlowDeckPolling() {
		if (flowdeckPoller) clearInterval(flowdeckPoller);
		flowdeckPoller = null;
	}

	function startFlowDeckPolling(runId: string) {
		stopFlowDeckPolling();
		flowdeckPoller = setInterval(async () => {
			try {
				const state = await getFlowDeckOrchestration(runId, workspace);
				flowdeckStatus = String(state.status || state.state || 'active').toLowerCase();
				if (Array.isArray(state.events)) mergeFlowDeckEvents(state.events);
				for (const event of (state.events as any[]) || []) applyFlowDeckEventToMessage(event);
				if (isFlowDeckTerminal(flowdeckStatus)) {
					if (flowdeckMessageId) {
						const message = allMessages.find((item) => item.id === flowdeckMessageId);
						if (message) {
							message.done = true;
							allMessages = [...allMessages];
						}
					}
					stopFlowDeckPolling();
				}
			} catch {
				// Keep the last truthful state; the next poll or completion response can update it.
			}
		}, 2500);
	}

	async function sendToFlowDeck(text: string) {
		if (!workspace) {
			toast.error('Choose a workspace before using Heidi.');
			return;
		}
		stopTtsPlayback();
		sending = true;
		// Show truthful feedback while the immediate durable-run POST is in flight.
		flowdeckStatus = 'preparing';
		flowdeckRunId = '';
		flowdeckIsAudit = /\baudit\b/i.test(text);
		flowdeckEvents = [];
		flowdeckClarification = '';
		autoScroll = true;
		const parentId = activePath.length ? activePath[activePath.length - 1].msg.id : null;
		const wasNewChat = !chatId;
		const userId = `heidi-user-${Date.now()}`;
		const assistantId = `heidi-assistant-${Date.now()}`;
		flowdeckMessageId = assistantId;
		allMessages = [
			...allMessages,
			{
				id: userId,
				parent_id: parentId,
				role: 'user',
				content: text,
				model: selectedModel,
				done: true,
				output: null,
				usage: null,
				meta: { agent: 'heidi', flowdeck: true },
				created_at: Date.now() / 1000
			},
			{
				id: assistantId,
				parent_id: userId,
				role: 'assistant',
				content: '',
				model: selectedModel,
				done: false,
				output: [],
				usage: null,
				meta: { agent: 'heidi', flowdeck: true },
				created_at: Date.now() / 1000
			}
		];
		currentMessageId = assistantId;
		try {
			const result = flowdeckIsAudit
				? await createAudit(
						{
							workspace,
							objective: text,
							scope: {
								categories: [
									'architecture',
									'module_boundaries',
									'dependencies',
									'configuration',
									'migrations',
									'tests',
									'runtime_entry_points',
									'data_flows',
									'authentication_authorization',
									'state_ownership',
									'provider_boundaries',
									'recent_changes'
								]
							},
							completion_contract: [
								'evidence_bound_findings',
								'unknowns_preserved',
								'authoritative_readiness'
							],
							metadata: { source: 'chat-composer', chat_id: chatId, model: selectedModel }
						},
						`chat-flowdeck-audit-${crypto.randomUUID()}`
					)
				: await createFlowDeckOrchestration(
						{
							workspace,
							objective: text,
							metadata: { source: 'chat-composer', chat_id: chatId, model: selectedModel }
						},
						`chat-flowdeck-${crypto.randomUUID()}`
					);
			// Heidi messages are real CPTR rows. Replace the optimistic pair
			// immediately so polling, reconnects, and loadChat all converge on
			// the same durable IDs instead of losing the turn.
			const durableUser = result.user_message as ChatMessageRow | undefined;
			const durableAssistant = result.assistant_message as ChatMessageRow | undefined;
			chatId = String(result.chat_id || chatId || '');
			if (durableUser && durableAssistant) {
				allMessages = [
					...allMessages.filter((message) => message.id !== userId && message.id !== assistantId),
					durableUser,
					durableAssistant
				];
				flowdeckMessageId = durableAssistant.id;
				currentMessageId = flowdeckMessageId;
				// Socket.IO can deliver child activity before the orchestration
				// POST resolves. Reapply the buffered lifecycle/native events to
				// the durable assistant after replacing the optimistic IDs.
				for (const event of flowdeckEvents) applyFlowDeckEventToMessage(event);
				if (wasNewChat && tabId) {
					updateTab(tabId, chatId, text.slice(0, 40) || $t('chat.fallbackTitle'));
				}
			}
			flowdeckRunId = result.run_id || '';
			flowdeckIsAudit = result.audit === true || flowdeckIsAudit;
			if (Array.isArray(result.events)) {
				mergeFlowDeckEvents(result.events);
				for (const event of result.events) applyFlowDeckEventToMessage(event);
			}
			flowdeckStatus = String(result.status || 'active').toLowerCase();
			flowdeckClarification = result.outcome === 'clarification' ? result.message || '' : '';
			if (flowdeckClarification && flowdeckMessageId) {
				const message = allMessages.find((item) => item.id === flowdeckMessageId);
				if (message) {
					message.content = flowdeckClarification;
					message.done = true;
					allMessages = [...allMessages];
				}
			}
			if (flowdeckRunId && !isFlowDeckTerminal(flowdeckStatus)) {
				startFlowDeckPolling(flowdeckRunId);
			} else if (flowdeckStatus !== 'clarification' && flowdeckMessageId) {
				const message = allMessages.find((item) => item.id === flowdeckMessageId);
				if (message) {
					message.done = true;
					allMessages = [...allMessages];
				}
			}
			if (result.outcome === 'clarification') {
				flowdeckStatus = 'clarification';
			} else if (flowdeckStatus === 'succeeded') {
				toast.success('Heidi completed the FlowDeck orchestration.');
			}
		} catch (error) {
			flowdeckStatus = 'failed';
			toast.error(
				error instanceof Error ? error.message : 'Heidi could not start FlowDeck orchestration.'
			);
		} finally {
			inputText = '';
			sending = false;
			chatInputEl?.focus();
		}
	}

	async function handleManualCompact() {
		if (!chatId) {
			toast.message($t('chat.compactNoChat'));
			return;
		}
		if (sending || streaming) return;
		sending = true;
		inputText = '';
		const toastId = toast.loading($t('chat.compacting'));
		try {
			const result = await apiCompactChat(chatId, selectedModel || null);
			contextUsage = result.context_usage ?? contextUsage;
			if (result.compacted) {
				toast.success($t('chat.compactDone'), { id: toastId });
			} else {
				toast.message($t('chat.compactSkipped'), { id: toastId });
			}
			await loadChat(chatId);
		} catch (err: any) {
			toast.error(err?.message || $t('chat.compactFailed'), { id: toastId });
		} finally {
			sending = false;
			chatInputEl?.focus();
		}
	}

	async function handleForkChat(messageId?: string | null) {
		if (!chatId || sending || streaming) return;
		sending = true;
		inputText = '';
		const toastId = toast.loading($t('chat.forking'));
		try {
			const result = await apiForkChat(chatId, messageId ?? currentMessageId);
			if (onopenchat) onopenchat(result.chat_id);
			else openChatTab(result.chat_id);
			toast.success($t('chat.forkDone'), { id: toastId });
		} catch (err: any) {
			toast.error(err?.message || $t('chat.forkFailed'), { id: toastId });
		} finally {
			sending = false;
			chatInputEl?.focus();
		}
	}

	function handlePlanCommand() {
		planMode = !planMode;
		persistChatSettings();
	}

	function handleToolApprovalModeChange(mode: ToolApprovalMode) {
		toolApprovalMode = mode;
		defaultToolApprovalMode.set(mode);
		persistChatSettings();
	}

	function firstUserMessageTitle(): string {
		const message = allMessages.find((m) => m.role === 'user' && !isPendingHiddenMessage(m));
		return message ? message.content.replace(/\s+/g, ' ').trim().slice(0, 80) : '';
	}

	function handleStatusCommand() {
		if (isLanding) return;
		initialCommandSessionId = null;
		showStatusModal = true;
		refreshCommandSessions();
	}

	async function handleSkillsListCommand() {
		try {
			skillsModalList = await getSkills(workspace);
			showSkillsModal = true;
		} catch (err: any) {
			toast.error(err?.message || 'Failed to load skills');
		}
	}

	async function refreshCommandSessions() {
		const currentChatId = chatId;
		if (!currentChatId || !workspace) {
			commandSessions = [];
			return;
		}
		try {
			const sessions = await listCommandSessions(workspace, currentChatId);
			if (chatId !== currentChatId) return;
			commandSessions = sessions;
		} catch (err) {
			console.error('[chat] command sessions refresh error', err);
			if (chatId === currentChatId) commandSessions = [];
		}
	}

	function handleInspectCommandSession(e: Event) {
		const id = (e as CustomEvent<{ commandSessionId?: string }>).detail?.commandSessionId;
		if (!id) return;
		initialCommandSessionId = id;
		showStatusModal = true;
		refreshCommandSessions();
	}

	// ── Queue actions ──────────────────────────────────────────

	async function handleQueueSendNow(messageId: string) {
		if (!chatId) return;
		try {
			await apiQueueSendNow(chatId, messageId);
			await loadChat(chatId);
		} catch (e) {
			console.error('[chat] queue send-now error', e);
		}
	}

	async function handleQueueEdit(messageId: string) {
		if (!chatId) return;
		// Move content back to input, delete from queue
		const msg = allMessages.find((m) => m.id === messageId);
		if (msg) {
			inputText = msg.content;
			chatInputEl?.focus();
		}
		try {
			await apiQueueDelete(chatId, messageId);
			await loadChat(chatId);
		} catch (e) {
			console.error('[chat] queue edit error', e);
		}
	}

	async function handleQueueDelete(messageId: string) {
		if (!chatId) return;
		try {
			await apiQueueDelete(chatId, messageId);
			await loadChat(chatId);
		} catch (e) {
			console.error('[chat] queue delete error', e);
		}
	}

	async function handleCancel() {
		const active = allMessages.find((m) => m.role === 'assistant' && !m.done);
		if (active && flowdeckMessageId === active.id && flowdeckRunId) {
			stopTtsPlayback();
			active.done = true;
			allMessages = [...allMessages];
			flowdeckStatus = 'cancelled';
			stopFlowDeckPolling();
			try {
				await cancelFlowDeckOrchestration(flowdeckRunId, workspace);
			} catch (err) {
				console.error('[chat] FlowDeck cancel error', err);
			}
			return;
		}
		if (!active || !chatId) return;
		stopTtsPlayback();

		// Tell the socket handler to skip the reload for this message
		cancelledMessageId = active.id;

		// Optimistically mark as done so streaming indicator disappears immediately
		if (active.output) {
			for (const item of active.output) {
				if (item.type === 'function_call' && item.status === 'pending') {
					item.status = 'rejected';
				}
			}
		}

		// Flush any pending streaming text into the output array so it renders
		// when done=true. The AssistantMessage component only shows output items
		// for done messages — raw content is only shown during streaming (!done).
		const flushedText = (active.output || [])
			.filter((i: any) => i.type === 'message')
			.flatMap((i: any) => i.content || [])
			.map((c: any) => c.text)
			.join('');
		const pendingText = (active.content || '').slice(flushedText.length);
		if (pendingText) {
			active.output = [
				...(active.output || []),
				{
					type: 'message',
					id: `flush-${Date.now()}`,
					status: 'completed',
					role: 'assistant',
					content: [{ type: 'output_text', text: pendingText }]
				}
			];
		}

		active.done = true;
		allMessages = [...allMessages];

		try {
			await cancelTask(chatId, active.id);
		} catch (err) {
			console.error('[chat] cancel error', err);
		}
	}

	function handleApprove(messageId: string, callId: string, approved: boolean) {
		if (!chatId) return;

		// Optimistically update local state so Allow/Deny buttons disappear immediately
		const msg = allMessages.find((m) => m.id === messageId);
		if (msg?.output) {
			const call = msg.output.find(
				(item: any) =>
					item.type === 'function_call' && item.call_id === callId && item.status === 'pending'
			);
			if (call) {
				call.status = approved ? 'queued' : 'rejected';
				if (approved) call.approved = true;
				allMessages = [...allMessages]; // trigger reactivity
			}
		}

		resolveToolCall(chatId, messageId, callId, approved ? 'approve' : 'reject').catch((err) => {
			console.error('[chat] approve error', err);
			// Revert on failure: reload from DB to get true state
			loadChat(chatId!);
		});
	}

	function handleApproveToolCallShortcut(approved: boolean) {
		for (const { msg } of [...activePath].reverse()) {
			const item = msg.output?.find(
				(output: any) =>
					output.type === 'function_call' &&
					output.name !== 'ask_user' &&
					output.status === 'pending'
			);
			if (!item) continue;

			handleApprove(msg.id, item.call_id, approved);
			return true;
		}
		return false;
	}

	function handleAskUserAnswer(
		messageId: string,
		callId: string,
		answers: Record<string, string>,
		timedOut: boolean
	) {
		if (!chatId) return;
		answerAskUser(chatId, messageId, callId, answers, timedOut).catch((err) => {
			console.error('[chat] ask_user answer error', err);
			loadChat(chatId!);
		});
	}

	function handleNavigate(messageId: string, direction: -1 | 1) {
		const entry = activePath.find((p) => p.msg.id === messageId);
		if (!entry) return;

		const newIndex = entry.siblingIndex + direction;
		if (newIndex < 0 || newIndex >= entry.siblingIds.length) return;

		const targetId = entry.siblingIds[newIndex];
		const childrenMap = buildChildrenMap(allMessages);
		const leafId = findLeaf(targetId, childrenMap);

		currentMessageId = leafId;
		if (chatId) updateCurrentMessage(chatId, leafId).catch(() => {});
	}

	async function handleRegenerate(messageId: string) {
		const msg = allMessages.find((m) => m.id === messageId);
		if (!msg?.parent_id || !chatId || !selectedModel) return;

		try {
			const result = await apiSendMessage(
				'',
				selectedModel,
				workspace,
				chatId,
				msg.parent_id,
				getChatSendParams()
			);
			if (result.assistant_message) {
				allMessages = [...allMessages, result.assistant_message];
				currentMessageId = result.message_id;
			}
		} catch (e) {
			console.error('[chat] regenerate error', e);
		}
	}

	async function handleEditMessage(
		messageId: string,
		content: string,
		output: any[] | null,
		submit: boolean
	) {
		const msg = allMessages.find((m) => m.id === messageId);
		if (!msg || !chatId) return;

		if (!submit) {
			// Save in-place
			const updates: { content?: string; output?: any[] } = {};
			if (msg.role === 'user') {
				updates.content = content;
			} else {
				updates.content = content;
				if (output) updates.output = output;
			}
			await updateMessage(chatId, messageId, updates);
			await loadChat(chatId);
			return;
		}

		if (msg.role === 'user') {
			// Send: create new sibling user message + trigger LLM
			try {
				const result = await apiSendMessage(
					content,
					selectedModel,
					workspace,
					chatId,
					msg.parent_id,
					getChatSendParams()
				);
				if (result.user_message && result.assistant_message) {
					allMessages = [...allMessages, result.user_message, result.assistant_message];
					currentMessageId = result.message_id;
				}
			} catch (e) {
				console.error('[chat] edit-send error', e);
			}
		} else {
			// Save As Copy: create new sibling assistant message (no LLM)
			try {
				await createMessage(
					chatId,
					msg.parent_id ?? null,
					'assistant',
					content,
					output ?? undefined
				);
				await loadChat(chatId);
			} catch (e) {
				console.error('[chat] save-as-copy error', e);
			}
		}
	}

	function updateTab(tid: string, newChatId: string, label: string) {
		if (ontabupdate) return ontabupdate(tid, newChatId, label);
		currentWorkspace.update((ws) => {
			if (!ws) return ws;
			return {
				...ws,
				groups: ws.groups.map((g) => ({
					...g,
					tabs: g.tabs.map((t) => (t.id === tid ? { ...t, path: newChatId, label } : t))
				}))
			};
		});
	}

	function handleNewChatCommand() {
		if (onopenchat) return onopenchat();
		openChatTab();
	}

	function shouldUseTts() {
		return (
			$ttsEnabled &&
			$ttsConfigured &&
			($ttsPlaybackEnabled || voiceModeEnabled || $ttsAutoStreamEnabled)
		);
	}

	function shouldStreamTts() {
		return $ttsEnabled && $ttsConfigured && (voiceModeEnabled || $ttsAutoStreamEnabled);
	}

	function resetTtsBuffer() {
		ttsBuffer = '';
		ttsInsideCodeFence = false;
	}

	function stopTtsPlayback() {
		ttsStopRequested = true;
		ttsGeneration += 1;
		ttsQueue = [];
		ttsPrepareCursor = 0;
		ttsPreparing = 0;
		resetTtsBuffer();
		for (const pending of ttsPreparedAudio.values()) pending.controller.abort();
		ttsPreparedAudio.clear();
		if (ttsAudio) {
			ttsAudio.pause();
			ttsAudio.src = '';
			ttsAudio = null;
		}
		if (ttsObjectUrl) {
			URL.revokeObjectURL(ttsObjectUrl);
			ttsObjectUrl = null;
		}
		ttsPlaying = false;
		speakingMessageId = null;
	}

	function stripCodeFenceDelta(delta: string): string {
		let out = '';
		for (let i = 0; i < delta.length; i++) {
			if (delta.startsWith('```', i)) {
				ttsInsideCodeFence = !ttsInsideCodeFence;
				i += 2;
				continue;
			}
			if (!ttsInsideCodeFence) out += delta[i];
		}
		return out;
	}

	function cleanSpeechText(text: string): string {
		return text
			.replace(/```[\s\S]*?```/g, ' ')
			.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
			.replace(/`([^`]+)`/g, '$1')
			.replace(/^#{1,6}\s+/gm, '')
			.replace(/^\s*[-*+]\s+/gm, '')
			.replace(/\s+/g, ' ')
			.trim();
	}

	function findSpeechBoundary(text: string, firstChunk = false): number {
		const min = firstChunk ? 35 : 60;
		const scanFrom = firstChunk ? 30 : 40;
		const hardMax = firstChunk ? 95 : 220;
		if (text.length < min) return -1;
		for (let i = scanFrom; i < text.length; i++) {
			const ch = text[i];
			const next = text[i + 1] || '';
			if ((ch === '.' || ch === '!' || ch === '?' || ch === '\n') && (!next || /\s/.test(next))) {
				return i + 1;
			}
		}
		if (text.length > hardMax) {
			const idx = text.lastIndexOf(' ', hardMax);
			return idx > min ? idx : hardMax;
		}
		return -1;
	}

	function handleTtsDelta(messageId: string, delta: string) {
		if (!shouldStreamTts()) return;
		if (!delta.trim()) return;

		const active = allMessages.find((m) => m.id === messageId);
		if (!active || active.role !== 'assistant') return;

		const speakable = stripCodeFenceDelta(delta);
		if (!speakable.trim()) return;

		ttsBuffer += speakable;
		let boundary = findSpeechBoundary(ttsBuffer, ttsQueue.length === 0 && !ttsPlaying);
		while (boundary > 0) {
			const chunk = cleanSpeechText(ttsBuffer.slice(0, boundary));
			ttsBuffer = ttsBuffer.slice(boundary);
			if (chunk.length > 1) enqueueSpeech(chunk);
			boundary = findSpeechBoundary(ttsBuffer);
		}
	}

	function flushTtsBuffer() {
		if (!shouldStreamTts()) return;
		const chunk = cleanSpeechText(ttsBuffer);
		resetTtsBuffer();
		if (chunk.length > 1) enqueueSpeech(chunk);
	}

	function enqueueSpeech(text: string) {
		ttsQueue = [...ttsQueue, text];
		scheduleTtsPrepare(ttsGeneration);
		if (!ttsPlaying) void playTtsQueue(ttsGeneration);
	}

	function chunkSpeechText(text: string): string[] {
		const chunks: string[] = [];
		let remaining = cleanSpeechText(text);
		while (remaining) {
			const boundary = findSpeechBoundary(remaining);
			const end = boundary > 0 ? boundary : remaining.length;
			const chunk = cleanSpeechText(remaining.slice(0, end));
			if (chunk.length > 1) chunks.push(chunk);
			remaining = remaining.slice(end).trimStart();
		}
		return chunks;
	}

	function getAssistantSpeechText(msg: ChatMessageRow): string {
		const outputText = (msg.output || [])
			.filter((item: any) => item.type === 'message')
			.flatMap((item: any) => item.content || [])
			.map((part: any) => part.text || part.content || '')
			.join(' ');
		return cleanSpeechText(outputText || msg.content || '');
	}

	function speakMessage(messageId: string) {
		const msg = allMessages.find((m) => m.id === messageId && m.role === 'assistant');
		if (!msg || !$ttsEnabled || !$ttsConfigured) return;
		const text = getAssistantSpeechText(msg);
		if (!text) return;
		if (speakingMessageId === messageId && ttsPlaying && $ttsPlaybackEnabled) {
			ttsPlaybackEnabled.set(false);
			stopTtsPlayback();
			return;
		}
		stopTtsPlayback();
		void unlockTtsAudioPlayback();
		speakingMessageId = messageId;
		ttsPlaybackEnabled.set(true);
		ttsErrorShown = false;
		for (const chunk of chunkSpeechText(text)) enqueueSpeech(chunk);
	}

	function ttsCacheKey(text: string): string {
		return [$ttsVoice, $ttsFormat, text].join('\u001f');
	}

	function getCachedTtsAudio(key: string): Blob | null {
		const cached = ttsAudioCache.get(key);
		if (!cached) return null;
		ttsAudioCache.delete(key);
		ttsAudioCache.set(key, cached);
		return cached;
	}

	function cacheTtsAudio(key: string, blob: Blob) {
		if (blob.size > TTS_AUDIO_CACHE_LIMIT_BYTES) return;
		const existing = ttsAudioCache.get(key);
		if (existing) {
			ttsAudioCacheBytes -= existing.size;
			ttsAudioCache.delete(key);
		}
		while (ttsAudioCacheBytes + blob.size > TTS_AUDIO_CACHE_LIMIT_BYTES) {
			const oldest = ttsAudioCache.entries().next().value;
			if (!oldest) break;
			ttsAudioCache.delete(oldest[0]);
			ttsAudioCacheBytes -= oldest[1].size;
		}
		ttsAudioCache.set(key, blob);
		ttsAudioCacheBytes += blob.size;
	}

	async function readTtsError(response: Response): Promise<string> {
		try {
			const data = await response.json();
			return data?.detail || data?.error || `${response.status}`;
		} catch {
			return `${response.status}`;
		}
	}

	function showTtsFailure(detail: string) {
		if (!ttsErrorShown) {
			ttsErrorShown = true;
			toast.error($t('admin.audio.tts') + ': ' + detail);
		}
		ttsPlaybackEnabled.set(false);
		voiceModeEnabled = false;
	}

	function scheduleTtsPrepare(generation: number) {
		while (
			generation === ttsGeneration &&
			ttsPreparing < TTS_MAX_PREFETCH &&
			ttsPrepareCursor < ttsQueue.length
		) {
			const text = ttsQueue[ttsPrepareCursor++];
			ttsPreparing += 1;
			void prepareTtsAudio(text, generation)
				.catch(() => {})
				.finally(() => {
					if (generation !== ttsGeneration) return;
					ttsPreparing = Math.max(0, ttsPreparing - 1);
					scheduleTtsPrepare(generation);
				});
		}
	}

	function prepareTtsAudio(text: string, generation: number): Promise<Blob> {
		const cacheKey = ttsCacheKey(text);
		const cached = getCachedTtsAudio(cacheKey);
		if (cached) return Promise.resolve(cached);

		const existing = ttsPreparedAudio.get(cacheKey);
		if (existing && existing.generation === generation) return existing.promise;
		if (existing) existing.controller.abort();

		const controller = new AbortController();
		let promise!: Promise<Blob>;
		promise = fetch('/api/audio/speech', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ text, voice: $ttsVoice, workspace }),
			signal: controller.signal
		})
			.then(async (response) => {
				if (!response.ok) throw new Error(await readTtsError(response));
				const blob = await response.blob();
				if (blob.size <= 0) throw new Error('empty audio response');
				cacheTtsAudio(cacheKey, blob);
				return blob;
			})
			.finally(() => {
				const current = ttsPreparedAudio.get(cacheKey);
				if (current?.promise === promise) ttsPreparedAudio.delete(cacheKey);
			});
		void promise.catch(() => {});
		ttsPreparedAudio.set(cacheKey, { promise, controller, generation });
		return promise;
	}

	async function playTtsQueue(generation: number) {
		ttsStopRequested = false;
		ttsPlaying = true;
		try {
			while (generation === ttsGeneration && ttsQueue.length > 0) {
				if (!shouldUseTts()) break;
				const text = ttsQueue[0];
				if (!text) {
					ttsQueue = ttsQueue.slice(1);
					if (ttsPrepareCursor > 0) ttsPrepareCursor -= 1;
					continue;
				}
				scheduleTtsPrepare(generation);

				const blob = await prepareTtsAudio(text, generation);

				if (generation !== ttsGeneration) break;
				if (ttsObjectUrl) URL.revokeObjectURL(ttsObjectUrl);
				ttsObjectUrl = URL.createObjectURL(blob);
				ttsAudio = setTtsAudioPlaybackSource(ttsObjectUrl, chatTitle) ?? new Audio(ttsObjectUrl);
				await new Promise<void>((resolve, reject) => {
					const audio = ttsAudio!;
					audio.onended = () => resolve();
					audio.onerror = () => {
						if (ttsStopRequested || generation !== ttsGeneration) resolve();
						else reject(new Error('audio playback failed'));
					};
					const started = audio.play();
					if (started) started.catch(reject);
				});
				if (ttsObjectUrl) {
					URL.revokeObjectURL(ttsObjectUrl);
					ttsObjectUrl = null;
				}
				ttsAudio = null;
				if (generation !== ttsGeneration) break;
				if (ttsQueue[0] === text) {
					ttsQueue = ttsQueue.slice(1);
					if (ttsPrepareCursor > 0) ttsPrepareCursor -= 1;
				}
			}
		} catch (err: any) {
			if (!ttsStopRequested && err?.name !== 'AbortError' && !ttsErrorShown) {
				showTtsFailure(err?.message || 'playback failed');
			}
		} finally {
			if (generation === ttsGeneration && ttsObjectUrl) {
				URL.revokeObjectURL(ttsObjectUrl);
				ttsObjectUrl = null;
			}
			if (generation === ttsGeneration) ttsAudio = null;
			if (generation === ttsGeneration) ttsPlaying = false;
			if (generation === ttsGeneration) speakingMessageId = null;
			if (generation === ttsGeneration) ttsStopRequested = false;
			if (generation === ttsGeneration && !voiceModeEnabled) ttsPlaybackEnabled.set(false);
		}
	}
</script>

<div
	class="app-theme relative flex h-full flex-col"
	style="background: var(--app-bg); color: var(--app-fg);"
>
	{#if !isLanding}
		<div
			class="relative z-30 -mb-12 flex h-7 shrink-0 items-center gap-2 pl-3 pr-2 dark:border-white/6"
			style="border-color: color-mix(in oklab, var(--app-fg) 8%, transparent);"
		>
			<div
				aria-hidden="true"
				class="pointer-events-none absolute inset-0 -bottom-10 -z-10"
				style="background: linear-gradient(to bottom, var(--app-bg), color-mix(in oklab, var(--app-bg) 95%, transparent) 40%, transparent 97%);"
			></div>
			<div
				class="min-w-0 flex-1 truncate text-[0.6875rem] font-medium text-gray-600 dark:text-gray-400"
			>
				{displayChatTitle}
			</div>
			<div class="flex shrink-0 items-center gap-0.5">
				<button
					type="button"
					class="relative flex size-6 shrink-0 items-center justify-center rounded-lg transition-colors duration-75 {statusButtonClass}"
					aria-label={$t('bar.newChat')}
					title={$t('bar.newChat')}
					use:tooltip={$t('bar.newChat')}
					onclick={handleNewChatCommand}
				>
					<Icon name="chat-plus" size={14} />
				</button>
				<button
					bind:this={statusButtonEl}
					type="button"
					class="relative flex size-6 shrink-0 items-center justify-center rounded-lg transition-colors duration-75 {statusButtonClass}"
					aria-label={statusTitle}
					title={statusTitle}
					use:tooltip={statusTitle}
					onclick={handleStatusCommand}
				>
					<Icon name="list" size={13} />
				</button>
			</div>
		</div>
	{/if}

	{#if isLanding}
		<!-- Landing: input + recent chats -->
		<div class="flex-1 overflow-y-auto flex flex-col">
			<div
				class="max-w-xl w-full mx-auto px-4 flex flex-col my-auto pt-6 {previousChats.length === 0
					? 'pb-20'
					: 'pb-6'}"
			>
				<!-- Greeting -->
				<div class="mb-8 text-center">
					<h1 class="text-lg font-normal text-gray-800 dark:text-gray-200 tracking-tight">
						{$t('chat.greeting')}
					</h1>
				</div>

				{#if selectedAgent === 'heidi' && (flowdeckStatus || sending)}
					<FlowDeckStatusStrip
						status={flowdeckStatus}
						runId={flowdeckRunId}
						{sending}
						isAudit={flowdeckIsAudit}
						events={flowdeckEvents}
						telemetry={flowdeckTelemetry}
						oncancel={handleCancel}
onreconnect={handleReconnect}
onaction={handleDesignerAction}
nativeMessageId={flowdeckMessageId}
					/>
				{/if}
				<ChatInput
					bind:this={chatInputEl}
					bind:inputText
					bind:selectedModel
					bind:selectedAgent
					bind:toolApprovalMode
					bind:planMode
					bind:requestParams
					bind:voiceModeEnabled
					{sending}
					{workspace}
					placeholder={$t('chat.placeholder', { name: workspaceDisplayName })}
					tasks={chatTasks}
					askUser={pendingAskUser}
					onaskuseranswer={handleAskUserAnswer}
					onsend={send}
					onplan={handlePlanCommand}
					onsettingschange={persistChatSettings}
					ontoolapprovalchange={handleToolApprovalModeChange}
					{queuedMessages}
					onqueuesendnow={handleQueueSendNow}
					onqueueedit={handleQueueEdit}
					onqueuedelete={handleQueueDelete}
				/>
				<ChatHistory
					chats={previousChats}
					onopen={openChat}
					ondelete={deleteChat}
					onrename={renameChat}
					oncopy={workspace ? copyChatPath : undefined}
					page={chatPage}
					{totalPages}
					perPage={CHATS_PAGE_SIZE}
					onpagechange={handlePageChange}
					sortBy={chatSortBy}
					sortDir={chatSortDir}
					onsort={handleSort}
				/>
			</div>
		</div>
	{:else}
		<!-- Conversation view -->
		{#if loading}
			<div class="flex-1 flex items-center justify-center text-gray-400 dark:text-gray-500">
				<Spinner size={24} />
			</div>
		{:else}
			<div
				bind:this={messagesEl}
				class="min-h-0 flex-1 overflow-y-auto"
				style="background: var(--app-bg); color: var(--app-fg);"
				onscroll={handleMessagesScroll}
			>
				<div
					class="{$widescreenMode
						? 'max-w-full'
						: 'max-w-2xl'} mx-auto w-full px-4 pt-16 pb-32 flex flex-col gap-4"
				>
					{#if hasHiddenMessages}
						<div bind:this={loadSentinelEl} class="h-1 w-full" aria-hidden="true"></div>
					{/if}
					{#each visiblePath as { msg, siblingIds, siblingIndex } (msg.id)}
						{#if msg.role === 'user'}
							<UserMessage
								content={msg.content}
								meta={msg.meta}
								createdAt={msg.created_at}
								{siblingIndex}
								siblingTotal={siblingIds.length}
								onnavigate={(dir) => handleNavigate(msg.id, dir)}
								onedit={(c, submit) => handleEditMessage(msg.id, c, null, submit)}
							/>
						{:else}
							<AssistantMessage
								content={msg.content}
								meta={msg.meta}
								done={msg.done}
								output={msg.output}
								usage={msg.usage}
								{chatId}
								messageId={msg.id}
								createdAt={msg.created_at}
								{siblingIndex}
								siblingTotal={siblingIds.length}
								speaking={speakingMessageId === msg.id}
								onnavigate={(dir) => handleNavigate(msg.id, dir)}
								onfork={sending || streaming ? undefined : () => handleForkChat(msg.id)}
								onregenerate={() => handleRegenerate(msg.id)}
								onedit={(c, o, submit) => handleEditMessage(msg.id, c, o, submit)}
								onspeak={() => speakMessage(msg.id)}
								onapprove={handleApprove}
							/>
						{/if}
					{/each}
				</div>
			</div>
		{/if}

		<!-- Input area -->
		<div
			class="shrink-0 px-4 py-3"
			style="background: var(--app-bg); padding-bottom: calc(0.75rem + env(safe-area-inset-bottom, 0px));"
		>
			<div class="{$widescreenMode ? 'max-w-full' : 'max-w-2xl'} mx-auto w-full relative">
				{#if !autoScroll && activePath.length > 0}
					<div
						class="absolute -top-10 left-0 right-0 pr-2 flex justify-end z-30 pointer-events-none"
					>
						<button
							class="app-surface app-interactive border p-1 rounded-full pointer-events-auto shadow-sm transition-colors"
							onclick={() => {
								autoScroll = true;
								scrollToBottom();
							}}
							aria-label={$t('chat.scrollToBottom')}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 20 20"
								fill="currentColor"
								class="w-4 h-4 text-gray-500 dark:text-gray-400"
							>
								<path
									fill-rule="evenodd"
									d="M10 3a.75.75 0 01.75.75v10.638l3.96-4.158a.75.75 0 111.08 1.04l-5.25 5.5a.75.75 0 01-1.08 0l-5.25-5.5a.75.75 0 111.08-1.04l3.96 4.158V3.75A.75.75 0 0110 3z"
									clip-rule="evenodd"
								/>
							</svg>
						</button>
					</div>
				{/if}
				{#if selectedAgent === 'heidi' && (flowdeckStatus || sending)}
					<FlowDeckStatusStrip
						status={flowdeckStatus}
						runId={flowdeckRunId}
						{sending}
						isAudit={flowdeckIsAudit}
						events={flowdeckEvents}
						telemetry={flowdeckTelemetry}
						oncancel={handleCancel}
onreconnect={handleReconnect}
onaction={handleDesignerAction}
nativeMessageId={flowdeckMessageId}
					/>
				{/if}
				<ChatInput
					bind:this={chatInputEl}
					bind:inputText
					bind:selectedModel
					bind:selectedAgent
					bind:toolApprovalMode
					bind:planMode
					bind:requestParams
					bind:voiceModeEnabled
					{sending}
					{streaming}
					{workspace}
					{contextUsage}
					{hasChatContent}
					tasks={chatTasks}
					askUser={pendingAskUser}
					onaskuseranswer={handleAskUserAnswer}
					onsend={send}
					oncompact={handleManualCompact}
					onfork={handleForkChat}
					onplan={handlePlanCommand}
					onsettingschange={persistChatSettings}
					ontoolapprovalchange={handleToolApprovalModeChange}
					onstatus={handleStatusCommand}
					onskillslist={handleSkillsListCommand}
					oncancel={handleCancel}
					{queuedMessages}
					onqueuesendnow={handleQueueSendNow}
					onqueueedit={handleQueueEdit}
					onqueuedelete={handleQueueDelete}
				/>
			</div>
		</div>
	{/if}
</div>

{#if showStatusModal}
	<StatusModal
		{chatId}
		{contextUsage}
		{commandSessions}
		{queuedMessages}
		{initialCommandSessionId}
		anchor={statusButtonEl ?? { x: 0, y: 0 }}
		onclose={() => {
			showStatusModal = false;
			initialCommandSessionId = null;
		}}
	/>
{/if}

{#if showSkillsModal}
	<SkillsModal
		skills={skillsModalList}
		onclose={() => {
			showSkillsModal = false;
			skillsModalList = [];
		}}
	/>
{/if}
