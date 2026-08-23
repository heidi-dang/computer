<script lang="ts">
	import { slide } from 'svelte/transition';

	interface Props {
		events: any[];
		status: string;
	}

	let { events = [], status = '' }: Props = $props();
	let expanded = $state<Record<string, boolean>>({});
	let copied = $state<string | null>(null);

	const toolEvents = $derived.by(() => {
		const calls = new Map<string, any>();
		const outputs = new Map<string, any>();
		for (const event of events) {
			const item = event?.output;
			if (!item) continue;
			if (item.type === 'function_call' && item.call_id) {
				calls.set(item.call_id, { ...calls.get(item.call_id), ...item });
			} else if (item.type === 'function_call_output' && item.call_id) {
				outputs.set(item.call_id, { ...outputs.get(item.call_id), ...item });
			}
		}
		return [...calls.entries()].map(([id, call]) => ({ id, call, output: outputs.get(id) }));
	});

	const evidenceEvents = $derived(
		events.filter((event) => {
			const kind = String(event?.kind || event?.type || '').toUpperCase();
			const payload = event?.payload || {};
			return (
				(kind.includes('OUTCOME') ||
					kind.includes('VERIFICATION') ||
					kind.includes('REVIEW') ||
					kind.includes('COMPLETED')) &&
				(payload.authoritative === true || kind.includes('REVIEW'))
			);
		})
	);

	function callKey(id: string) {
		return `tool-${id}`;
	}

	function format(value: unknown): string {
		if (value == null) return '';
		if (typeof value === 'string') return value;
		try {
			return JSON.stringify(value, null, 2);
		} catch {
			return String(value);
		}
	}

	function toolName(call: any): string {
		return String(call?.name || call?.tool_name || 'Tool activity');
	}

	function language(call: any): string {
		const name = toolName(call);
		if (name === 'run_command') return 'shell';
		if (name.includes('python')) return 'python';
		if (name.includes('javascript') || name.includes('node')) return 'javascript';
		return 'json';
	}

	function input(call: any): string {
		return format(call?.arguments || call?.args || {});
	}

	function output(item: any): string {
		return format(item?.output ?? item?.result ?? '');
	}

	function isRunning(call: any, outputItem: any): boolean {
		return !outputItem && !['completed', 'rejected', 'failed'].includes(String(call?.status || '').toLowerCase());
	}

	async function copy(id: string, value: string) {
		if (!value) return;
		await navigator.clipboard.writeText(value);
		copied = id;
		setTimeout(() => {
			if (copied === id) copied = null;
		}, 1400);
	}
</script>

{#if toolEvents.length || evidenceEvents.length}
	<section class="flowdeck-timeline" aria-label="FlowDeck execution activity">
		<div class="mb-2 flex items-center gap-2 px-1">
			<span class="flowdeck-kicker">FlowDeck execution</span>
			<span class="flowdeck-rule"></span>
			<span class="flowdeck-live-status">{status || 'observed'}</span>
		</div>

		<div class="space-y-2">
			{#each toolEvents as item (item.id)}
				{@const key = callKey(item.id)}
				{@const open = expanded[key] ?? isRunning(item.call, item.output)}
				<div class="flowdeck-tool-card {isRunning(item.call, item.output) ? 'is-running' : ''}">
					<button
						type="button"
						class="flex min-h-10 w-full items-center gap-2 px-3 py-2 text-left"
						aria-expanded={open}
						onclick={() => (expanded = { ...expanded, [key]: !open })}
					>
						<span class="flowdeck-tool-dot" aria-hidden="true"></span>
						<span class="min-w-0 flex-1">
							<strong class="block truncate text-xs font-semibold">{toolName(item.call)}</strong>
							<span class="app-muted block text-[0.625rem]">
								{item.output ? 'Output received' : isRunning(item.call, item.output) ? 'Running' : String(item.call?.status || 'Observed')}
							</span>
						</span>
						<span class="flowdeck-meta hidden shrink-0 sm:inline">{item.call?.call_id?.slice?.(0, 10) || 'tool'}</span>
						<span class="text-xs opacity-50">{open ? '−' : '+'}</span>
					</button>

					{#if open}
						<div class="flowdeck-tool-body" transition:slide={{ duration: 180 }}>
							<div class="flowdeck-section-label">
								<span>Code / input</span>
								<span class="flowdeck-language">{language(item.call)}</span>
								<button type="button" class="flowdeck-copy" onclick={() => copy(`${key}-input`, input(item.call))}>
									{copied === `${key}-input` ? 'Copied' : 'Copy'}
								</button>
							</div>
							<pre class="flowdeck-code"><code>{input(item.call)}</code></pre>

							{#if item.output}
								<div class="flowdeck-section-label mt-3">
									<span>Output</span>
									<button type="button" class="flowdeck-copy" onclick={() => copy(`${key}-output`, output(item.output))}>
										{copied === `${key}-output` ? 'Copied' : 'Copy'}
									</button>
								</div>
								<pre class="flowdeck-output"><code>{output(item.output)}</code></pre>
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		</div>

		{#if evidenceEvents.length}
			<div class="flowdeck-evidence">
				<div class="flowdeck-section-label"><span>Authoritative verification evidence</span></div>
				{#each evidenceEvents as event}
					{@const payload = event.payload || {}}
					<div class="flowdeck-evidence-row">
						<span class="flowdeck-evidence-mark">✓</span>
						<span class="min-w-0 flex-1">
							<strong class="block text-xs">{String(event.kind || event.type || 'Verified')}</strong>
							<span class="app-muted block truncate text-[0.6875rem]">{String(payload.observation || payload.observed_outcome || payload.status || 'Authoritative result reported by FlowDeck')}</span>
						</span>
					</div>
				{/each}
			</div>
		{/if}
	</section>
{/if}

<style>
	.flowdeck-timeline { margin: 0.65rem 0; max-width: 42rem; }
	.flowdeck-kicker { color: #67e8f9; font-size: 0.62rem; font-weight: 700; letter-spacing: .13em; text-transform: uppercase; }
	.flowdeck-rule { height: 1px; flex: 1; background: color-mix(in oklab, #22d3ee 22%, transparent); }
	.flowdeck-live-status { color: color-mix(in oklab, var(--app-fg) 50%, transparent); font-size: .62rem; text-transform: capitalize; }
	.flowdeck-tool-card, .flowdeck-evidence { overflow: hidden; border: 1px solid color-mix(in oklab, #22d3ee 22%, transparent); border-radius: .9rem; background: color-mix(in oklab, var(--app-surface) 94%, #083344); }
	.flowdeck-tool-card.is-running { border-color: color-mix(in oklab, #22d3ee 50%, transparent); box-shadow: 0 0 18px color-mix(in oklab, #22d3ee 9%, transparent); }
	.flowdeck-tool-dot { width: .45rem; height: .45rem; flex: 0 0 auto; border-radius: 999px; background: #22d3ee; }
	.is-running .flowdeck-tool-dot { animation: flowdeck-dot 1.5s ease-in-out infinite; }
	.flowdeck-meta, .flowdeck-language { border-radius: .35rem; background: color-mix(in oklab, var(--app-fg) 8%, transparent); color: color-mix(in oklab, var(--app-fg) 58%, transparent); font: .6rem ui-monospace, SFMono-Regular, monospace; padding: .18rem .35rem; }
	.flowdeck-tool-body { border-top: 1px solid color-mix(in oklab, #22d3ee 14%, transparent); padding: .7rem .75rem .8rem; }
	.flowdeck-section-label { display: flex; align-items: center; gap: .45rem; color: color-mix(in oklab, var(--app-fg) 60%, transparent); font-size: .62rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
	.flowdeck-copy { margin-left: auto; border-radius: .35rem; padding: .2rem .45rem; color: #67e8f9; font-size: .62rem; text-transform: none; letter-spacing: normal; }
	.flowdeck-copy:hover { background: color-mix(in oklab, #22d3ee 12%, transparent); }
	.flowdeck-code, .flowdeck-output { max-height: 13rem; overflow: auto; border-radius: .6rem; padding: .65rem .7rem; font: .7rem/1.5 ui-monospace, SFMono-Regular, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
	.flowdeck-code { margin-top: .4rem; background: color-mix(in oklab, #0f172a 78%, var(--app-surface)); color: #bae6fd; }
	.flowdeck-output { margin-top: .4rem; background: color-mix(in oklab, var(--app-fg) 5%, transparent); color: color-mix(in oklab, var(--app-fg) 76%, transparent); }
	.flowdeck-evidence { margin-top: .65rem; padding: .7rem .75rem; border-color: color-mix(in oklab, #34d399 28%, transparent); }
	.flowdeck-evidence-row { display: flex; align-items: flex-start; gap: .55rem; padding-top: .55rem; }
	.flowdeck-evidence-mark { color: #34d399; font-weight: 700; }
	@keyframes flowdeck-dot { 50% { box-shadow: 0 0 0 5px color-mix(in oklab, #22d3ee 0%, transparent); opacity: .65; } }
	@media (prefers-reduced-motion: reduce) { .is-running .flowdeck-tool-dot { animation: none; } }
</style>