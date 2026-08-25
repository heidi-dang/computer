<script lang="ts">
	type UnknownRecord = Record<string, unknown>;

	export interface DesignerAction {
		type: 'apply_variant' | 'mix_variants' | 'repair_comparison';
		variantIds?: string[];
		label: string;
	}

	interface Props {
		events?: readonly unknown[];
		status?: string;
		runId?: string;
		nativeMessageId?: string | null;
		oncancel?: () => void;
		onreconnect?: () => void;
		onaction?: (action: DesignerAction) => void;
	}

	let {
		events = [],
		status = '',
		runId = '',
		nativeMessageId = '',
		oncancel,
		onreconnect,
		onaction
	}: Props = $props();

	const terminalStatuses = new Set([
		'cancelled',
		'succeeded',
		'completed',
		'failed',
		'unknown',
		'manual_review',
		'manual_review_required',
		'orphaned'
	]);
	const reconnectStatuses = new Set(['reconnecting', 'disconnected', 'connection_lost', 'orphaned']);

	function record(value: unknown): UnknownRecord {
		return value && typeof value === 'object' && !Array.isArray(value) ? (value as UnknownRecord) : {};
	}

	function list(value: unknown): unknown[] {
		return Array.isArray(value) ? value : [];
	}

	function text(value: unknown, fallback = ''): string {
		return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback;
	}

	function firstRecord(...values: unknown[]): UnknownRecord {
		for (const value of values) {
			const candidate = record(value);
			if (Object.keys(candidate).length) return candidate;
		}
		return {};
	}

	function eventKind(event: unknown): string {
		const item = record(event);
		return text(item.kind || item.type || item.event_type).toUpperCase();
	}

	function payloadOf(event: unknown): UnknownRecord {
		const item = record(event);
		return firstRecord(item.payload, item.data, item.result, item.output);
	}

	function valueFrom(event: unknown, ...keys: string[]): unknown {
		const item = record(event);
		const payload = payloadOf(event);
		for (const key of keys) {
			if (item[key] !== undefined) return item[key];
			if (payload[key] !== undefined) return payload[key];
		}
		return undefined;
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

	function slug(value: unknown, index: number): string {
		const raw = text(record(value).id || record(value).variant_id || record(value).name, `variant-${index + 1}`);
		return raw.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || `variant-${index + 1}`;
	}

	function color(value: unknown, fallback: string): string {
		const candidate = text(value, fallback);
		return /^#[0-9a-f]{3,8}$/i.test(candidate) || /^rgb/.test(candidate) ? candidate : fallback;
	}

	const normalizedEvents = $derived(events.filter((event) => event && typeof event === 'object'));
	const designerEvents = $derived(
		normalizedEvents.filter((event) => {
			const kind = eventKind(event);
			const payload = payloadOf(event);
			return (
				kind.includes('DESIGN') ||
				kind.includes('VARIANT') ||
				kind.includes('SCREENSHOT') ||
				kind.includes('RESPONSIVE') ||
				kind.includes('VIEWPORT') ||
				kind.includes('COMPARISON') ||
				kind.includes('DIFF') ||
				kind.includes('RECONSTRUCT') ||
				kind.includes('REPAIR') ||
				kind.includes('TOKEN') ||
				kind.includes('DESIGNER') ||
				['design_system', 'designSystem', 'extraction', 'variants', 'reconstruction', 'screenshot_comparison', 'viewport_checks', 'evidence'].some(
					(key) => payload[key] !== undefined
				)
			);
		})
	);
	const designerPresent = $derived(designerEvents.length > 0);
	const latestPayload = $derived(
		designerEvents.reduce<UnknownRecord>((acc, event) => ({ ...acc, ...payloadOf(event) }), {})
	);
	const designSystem = $derived.by(() => {
		const candidate = firstRecord(
			latestPayload.design_system,
			latestPayload.designSystem,
			latestPayload.extraction,
			latestPayload.tokens
		);
		return Object.keys(candidate).length ? candidate : latestPayload;
	});
	const tokens = $derived.by(() => {
		const candidate = designSystem.tokens;
		return candidate && typeof candidate === 'object' && !Array.isArray(candidate)
			? (candidate as UnknownRecord)
			: {};
	});
	const tokenEntries = $derived(
		Object.entries(tokens).filter(([, value]) => value !== null && value !== undefined).slice(0, 12)
	);
	const variants = $derived.by(() => {
		const found: unknown[] = [];
		for (const event of designerEvents) {
			const candidate = valueFrom(event, 'variants', 'variant_cards', 'variantCards');
			if (Array.isArray(candidate)) found.push(...candidate);
		}
		return found
			.filter((item) => item && typeof item === 'object')
			.filter((item, index, all) => slug(item, index) && all.findIndex((other, otherIndex) => slug(other, otherIndex) === slug(item, index)) === index)
			.slice(0, 8);
	});
	const reconstruction = $derived(
		firstRecord(
			latestPayload.reconstruction,
			latestPayload.screenshot_to_ui,
			latestPayload.screenshotToUi,
			latestPayload.ui
		)
	);
	const viewports = $derived.by(() => {
		const found: unknown[] = [];
		for (const event of designerEvents) {
			const candidate = valueFrom(event, 'viewports', 'viewport_checks', 'responsive_checks', 'responsiveChecks');
			if (Array.isArray(candidate)) found.push(...candidate);
		}
		return found.slice(0, 8);
	});
	const comparison = $derived(
		firstRecord(
			latestPayload.comparison,
			latestPayload.screenshot_comparison,
			latestPayload.screenshotComparison,
			latestPayload.repair
		)
	);
	const evidence = $derived.by(() => {
		const found: unknown[] = [];
		for (const event of normalizedEvents) {
			const candidate = valueFrom(event, 'evidence', 'proof', 'sources');
			if (Array.isArray(candidate)) found.push(...candidate);
		}
		return found.slice(0, 12);
	});
	const linkage = $derived.by(() => {
		const event = designerEvents[designerEvents.length - 1];
		const item = record(event);
		const payload = payloadOf(event);
		return firstRecord(payload.linkage, payload.transcript, item.linkage, {
			message_id: item.message_id || payload.message_id || nativeMessageId,
			event_id: item.event_id || item.id,
			chat_id: item.chat_id || payload.chat_id
		});
	});

	let selectedVariants = $state<string[]>([]);
	let evidenceOpen = $state(false);
	let detailsOpen = $state(false);
	let actionNotice = $state('');

	const normalizedStatus = $derived(status.toLowerCase());
	const isTerminal = $derived(terminalStatuses.has(normalizedStatus));
	const needsReconnect = $derived(reconnectStatuses.has(normalizedStatus));
	const hasDesignSystem = $derived(
		tokenEntries.length > 0 ||
			Boolean(
				designSystem.name ||
					designSystem.title ||
					designSystem.colors ||
					designSystem.typography ||
					designSystem.spacing
			)
	);
	const hasReconstruction = $derived(Object.keys(reconstruction).length > 0);
	const hasComparison = $derived(Object.keys(comparison).length > 0);

	function variantId(item: unknown, index: number): string {
		return slug(item, index);
	}

	function toggleVariant(id: string) {
		selectedVariants = selectedVariants.includes(id)
			? selectedVariants.filter((item) => item !== id)
			: [...selectedVariants, id];
		actionNotice = '';
	}

	function applyVariant(item: unknown, index: number) {
		const id = variantId(item, index);
		const label = text(record(item).name || record(item).title, `Variant ${index + 1}`);
		actionNotice = onaction ? `Applying ${label}…` : 'Selection recorded locally; waiting for the Designer action contract.';
		onaction?.({ type: 'apply_variant', variantIds: [id], label });
	}

	function mixSelected() {
		if (selectedVariants.length < 2) return;
		actionNotice = onaction ? `Mixing ${selectedVariants.length} selected variants…` : 'Mix selection recorded locally; waiting for the Designer action contract.';
		onaction?.({ type: 'mix_variants', variantIds: selectedVariants, label: `Mix ${selectedVariants.length} variants` });
	}

	function requestRepair() {
		actionNotice = onaction ? 'Requesting a screenshot repair pass…' : 'Repair selection recorded locally; waiting for the Designer action contract.';
		onaction?.({ type: 'repair_comparison', label: 'Repair screenshot comparison' });
	}

	function checkStatus(item: unknown): string {
		return text(record(item).status || record(item).result || record(item).outcome, 'unverified').toLowerCase();
	}

	function checkLabel(item: unknown, index: number): string {
		const value = record(item);
		return text(value.viewport || value.label || value.name || value.width, `Viewport ${index + 1}`);
	}

	function scoreWidth(item: unknown, check: string): number {
		const value = record(item);
		const raw = Number(value.score || value.coverage);
		const score = Number.isFinite(raw) ? raw : check === 'passed' ? 100 : check === 'failed' ? 34 : 62;
		return Math.min(100, Math.max(6, score));
	}

	function statusLabel() {
		if (needsReconnect) return 'Connection needs attention';
		if (normalizedStatus === 'cancelled') return 'Designer run cancelled';
		if (normalizedStatus === 'failed') return 'Designer run failed';
		if (isTerminal) return 'Designer result';
		return normalizedStatus ? `Designer · ${normalizedStatus}` : 'Designer result';
	}
</script>

{#if designerPresent}
	<section class="designer-results" aria-label="Designer results" data-testid="designer-results">
		<header class="designer-header">
			<div class="designer-heading">
				<div class="designer-mark" aria-hidden="true">D</div>
				<div>
					<p class="designer-eyebrow">Designer result surface</p>
					<h2>{statusLabel()}</h2>
				</div>
			</div>
			<div class="designer-header-actions">
				{#if needsReconnect && onreconnect}
					<button type="button" class="designer-button designer-button-quiet" data-testid="button-designer-reconnect" onclick={onreconnect}>
						Reconnect
					</button>
				{/if}
				{#if oncancel && !isTerminal}
					<button type="button" class="designer-button designer-button-danger" data-testid="button-designer-cancel" onclick={oncancel}>
						Cancel
					</button>
				{/if}
			</div>
		</header>

		{#if needsReconnect}
			<div class="designer-alert designer-alert-warn" role="alert" data-testid="status-designer-reconnect">
				<span class="designer-alert-icon" aria-hidden="true">!</span>
				<div>
					<strong>Live updates paused.</strong>
					<p>The last received result is still shown. Reconnect to rehydrate this run; no completion is inferred.</p>
				</div>
			</div>
		{:else if normalizedStatus === 'cancelled'}
			<div class="designer-alert designer-alert-muted" role="status" data-testid="status-designer-cancelled">
				<strong>Cancellation received.</strong>
				<span>Partial evidence remains available below. No design was claimed as applied.</span>
			</div>
		{/if}

		{#if hasDesignSystem}
			<section class="designer-section" aria-labelledby="designer-system-heading">
				<div class="section-heading">
					<div>
						<p class="designer-eyebrow">Extracted system</p>
						<h3 id="designer-system-heading">{text(designSystem.name || designSystem.title, 'Design language found')}</h3>
					</div>
					<span class="designer-count">{tokenEntries.length || '—'} tokens surfaced</span>
				</div>
				{#if tokenEntries.length}
					<div class="token-grid">
						{#each tokenEntries as [key, value] (key)}
							<div class="token-card" data-testid={`token-${key}`}>
								<div class="token-swatch" style={`background:${color(value, 'var(--designer-coral)')}`} aria-hidden="true"></div>
								<div class="token-copy">
									<strong>{key.replace(/[-_]/g, ' ')}</strong>
									<code>{format(value)}</code>
								</div>
							</div>
						{/each}
					</div>
				{:else}
					<p class="designer-muted">Extraction metadata received. Token values were not included in this event.</p>
				{/if}
			</section>
		{/if}

		{#if variants.length}
			<section class="designer-section" aria-labelledby="designer-variants-heading">
				<div class="section-heading">
					<div>
						<p class="designer-eyebrow">Explore the direction</p>
						<h3 id="designer-variants-heading">Choose a starting point</h3>
					</div>
					{#if selectedVariants.length}
						<button type="button" class="designer-button designer-button-accent" data-testid="button-designer-mix" disabled={selectedVariants.length < 2} onclick={mixSelected}>
							Mix {selectedVariants.length}
						</button>
					{/if}
				</div>
				<div class="variant-grid">
					{#each variants as item, index (variantId(item, index))}
						{@const variant = record(item)}
						{@const id = variantId(item, index)}
						{@const selected = selectedVariants.includes(id)}
						<article class:selected={selected} class="variant-card" data-testid={`card-designer-variant-${id}`}>
							<button type="button" class="variant-preview" aria-pressed={selected} data-testid={`button-select-variant-${id}`} onclick={() => toggleVariant(id)}>
								<div class="preview-window">
									<div class="preview-topbar"><span></span><span></span><span></span></div>
									<div class="preview-layout">
										<div class="preview-rail"></div>
										<div class="preview-content">
											<div class="preview-line preview-line-wide"></div>
											<div class="preview-line"></div>
											<div class="preview-block"></div>
										</div>
									</div>
								</div>
								<span class="variant-select">{selected ? 'Selected for mix' : 'Select for mix'}</span>
							</button>
							<div class="variant-copy">
								<div>
									<h4>{text(variant.name || variant.title, `Variant ${index + 1}`)}</h4>
									<p>{text(variant.description || variant.summary, 'A direction extracted from the supplied references.')}</p>
								</div>
								<button type="button" class="designer-button designer-button-small" data-testid={`button-apply-variant-${id}`} onclick={() => applyVariant(item, index)}>Apply</button>
							</div>
							{#if variant.confidence || variant.source}
								<div class="variant-meta">{text(variant.confidence, 'Candidate')} {variant.source ? `· ${text(variant.source)}` : ''}</div>
							{/if}
						</article>
					{/each}
				</div>
			</section>
		{/if}

		{#if hasReconstruction}
			<section class="designer-section" aria-labelledby="designer-reconstruction-heading">
				<div class="section-heading">
					<div>
						<p class="designer-eyebrow">Screenshot → UI</p>
						<h3 id="designer-reconstruction-heading">{text(reconstruction.title || reconstruction.name, 'Reconstruction review')}</h3>
					</div>
					<span class="designer-chip">{text(reconstruction.status, 'Draft')}</span>
				</div>
				<div class="reconstruction-grid">
					<div class="reconstruction-figure" aria-label="Reconstructed interface preview">
						<div class="reconstruction-glow"></div>
						<div class="reconstruction-frame">
							<div class="reconstruction-nav"></div>
							<div class="reconstruction-hero"></div>
							<div class="reconstruction-columns"><span></span><span></span><span></span></div>
						</div>
					</div>
					<div class="reconstruction-copy">
						<p>{text(reconstruction.summary || reconstruction.description, 'A structural reconstruction is ready for responsive review. Compare the generated hierarchy before applying it to the workspace.')}</p>
						<div class="reconstruction-facts">
							<span><b>Source</b>{text(reconstruction.source || reconstruction.screenshot, 'Screenshot reference')}</span>
							<span><b>Components</b>{text(reconstruction.component_count || reconstruction.components, 'Awaiting inventory')}</span>
						</div>
					</div>
				</div>
			</section>
		{/if}

		{#if viewports.length}
			<section class="designer-section" aria-labelledby="designer-viewports-heading">
				<div class="section-heading">
					<div>
						<p class="designer-eyebrow">Responsive checks</p>
						<h3 id="designer-viewports-heading">Viewport confidence</h3>
					</div>
					<span class="designer-count">{viewports.length} checks</span>
				</div>
				<div class="viewport-list">
					{#each viewports as item, index (checkLabel(item, index))}
						{@const check = checkStatus(item)}
						<div class="viewport-row" data-testid={`row-viewport-${index}`}>
							<span class={`check-dot check-${check}`} aria-hidden="true"></span>
							<strong>{checkLabel(item, index)}</strong>
							<span class="viewport-bar"><span style={`width:${scoreWidth(item, check)}%`}></span></span>
							<span class={`check-label check-label-${check}`}>{check}</span>
						</div>
					{/each}
				</div>
			</section>
		{/if}

		{#if hasComparison}
			<section class="designer-section comparison-section" aria-labelledby="designer-comparison-heading">
				<div class="section-heading">
					<div>
						<p class="designer-eyebrow">Screenshot comparison</p>
						<h3 id="designer-comparison-heading">{text(comparison.title || comparison.name, 'Repair pass')}</h3>
					</div>
					<span class="designer-chip designer-chip-coral">{text(comparison.status || comparison.outcome, 'Review')}</span>
				</div>
				<div class="comparison-readout">
					<div class="compare-box"><span>Reference</span><div class="compare-art compare-reference"></div></div>
					<div class="compare-arrow" aria-hidden="true">→</div>
					<div class="compare-box"><span>Reconstructed</span><div class="compare-art compare-reconstructed"></div></div>
					<div class="compare-score">
						<strong>{text(comparison.score || comparison.similarity, '—')}</strong>
						<span>reported similarity</span>
					</div>
				</div>
				{#if comparison.recommendation || comparison.next_step || comparison.repairs}
					<p class="comparison-note">{text(comparison.recommendation || comparison.next_step || comparison.repairs)}</p>
				{/if}
				<button type="button" class="designer-button designer-button-accent" data-testid="button-designer-repair" onclick={requestRepair}>Request repair pass</button>
			</section>
		{/if}

		<footer class="designer-footer">
			<div class="designer-footer-line">
				<span class="link-dot" aria-hidden="true"></span>
				<span>Native transcript linked</span>
				<code>{text(linkage.message_id, nativeMessageId || 'assistant turn pending')}</code>
			</div>
			{#if runId}<code class="designer-run">run {runId.slice(0, 8)}</code>{/if}
			<button type="button" class="designer-disclosure" aria-expanded={evidenceOpen} data-testid="button-designer-evidence" onclick={() => (evidenceOpen = !evidenceOpen)}>
				{evidenceOpen ? 'Hide evidence' : 'Show evidence'} {evidence.length ? `(${evidence.length})` : ''}
			</button>
		</footer>
		{#if actionNotice}<p class="designer-action-notice" role="status" data-testid="status-designer-action">{actionNotice}</p>{/if}
		{#if evidenceOpen}
			<div class="designer-evidence" data-testid="designer-evidence">
				{#if evidence.length}
					{#each evidence as item, index}
						<div class="evidence-row">
							<span aria-hidden="true">↳</span>
							<span>{typeof item === 'string' ? item : format(item)}</span>
						</div>
					{/each}
				{:else}
					<p class="designer-muted">No evidence payload was included in the received events.</p>
				{/if}
			</div>
		{/if}
		<button type="button" class="designer-details-toggle" aria-expanded={detailsOpen} data-testid="button-designer-event-details" onclick={() => (detailsOpen = !detailsOpen)}>
			{detailsOpen ? 'Hide event details' : 'Inspect event details'}
		</button>
		{#if detailsOpen}
			<pre class="designer-event-details">{format(designerEvents.map((event) => ({ kind: eventKind(event), sequence: record(event).sequence, id: record(event).id })))}</pre>
		{/if}
	</section>
{/if}

<style>
	.designer-results {
		--designer-ink: var(--app-fg, #33413b);
		--designer-muted: var(--app-fg-muted, #69766d);
		--designer-line: color-mix(in oklab, var(--designer-ink) 12%, transparent);
		--designer-paper: color-mix(in oklab, var(--app-bg, #f7f7f3) 96%, #e7b8a5);
		--designer-coral: #c95f49;
		--designer-moss: #567664;
		max-width: 42rem;
		margin: 0.3rem 0 0.9rem 1.1rem;
		overflow: hidden;
		border: 1px solid color-mix(in oklab, var(--designer-coral) 30%, var(--designer-line));
		border-radius: 1rem;
		background: var(--designer-paper);
		color: var(--designer-ink);
		box-shadow: 0 12px 32px color-mix(in oklab, var(--designer-coral) 7%, transparent);
	}
	.designer-header, .section-heading, .designer-footer, .designer-footer-line, .variant-copy, .designer-alert, .viewport-row {
		display: flex;
		align-items: center;
	}
	.designer-header {
		justify-content: space-between;
		gap: 1rem;
		padding: 0.85rem 0.95rem;
		background: color-mix(in oklab, var(--designer-coral) 9%, transparent);
		border-bottom: 1px solid var(--designer-line);
	}
	.designer-heading { display: flex; align-items: center; gap: 0.6rem; min-width: 0; }
	.designer-mark {
		display: grid;
		width: 1.8rem;
		height: 1.8rem;
		place-items: center;
		border-radius: 0.55rem;
		background: var(--designer-coral);
		color: #fff5ec;
		font: 700 0.85rem 'Space Mono', monospace;
		transform: rotate(-4deg);
	}
	.designer-eyebrow {
		margin: 0 0 0.18rem;
		color: var(--designer-coral);
		font: 700 0.57rem/1.2 'Space Mono', monospace;
		letter-spacing: 0.12em;
		text-transform: uppercase;
	}
	.designer-header h2, .section-heading h3, .variant-copy h4 { margin: 0; letter-spacing: -0.02em; }
	.designer-header h2 { overflow: hidden; text-overflow: ellipsis; font-size: 0.85rem; font-weight: 700; white-space: nowrap; }
	.designer-header-actions { display: flex; align-items: center; gap: 0.35rem; flex: 0 0 auto; }
	.designer-button {
		border: 1px solid var(--designer-line);
		border-radius: 0.45rem;
		padding: 0.38rem 0.62rem;
		background: color-mix(in oklab, var(--app-bg, #f7f7f3) 60%, transparent);
		color: var(--designer-ink);
		font-size: 0.64rem;
		font-weight: 700;
		transition: background 140ms ease, border-color 140ms ease, transform 140ms ease;
	}
	.designer-button:hover:not(:disabled) { border-color: color-mix(in oklab, var(--designer-coral) 55%, var(--designer-line)); background: color-mix(in oklab, var(--designer-coral) 12%, transparent); transform: translateY(-1px); }
	.designer-button:focus-visible, .variant-preview:focus-visible, .designer-disclosure:focus-visible, .designer-details-toggle:focus-visible { outline: 2px solid var(--designer-coral); outline-offset: 2px; }
	.designer-button:disabled { cursor: not-allowed; opacity: 0.45; }
	.designer-button-small { padding: 0.3rem 0.48rem; }
	.designer-button-accent { border-color: color-mix(in oklab, var(--designer-coral) 42%, var(--designer-line)); color: var(--designer-coral); }
	.designer-button-danger { color: #a13e33; }
	.designer-alert { gap: 0.6rem; margin: 0.7rem 0.85rem 0; padding: 0.58rem 0.65rem; border: 1px solid color-mix(in oklab, #d18a45 38%, var(--designer-line)); border-radius: 0.65rem; font-size: 0.66rem; line-height: 1.35; }
	.designer-alert p { margin: 0.15rem 0 0; color: var(--designer-muted); }
	.designer-alert-icon { display: grid; width: 1.2rem; height: 1.2rem; flex: 0 0 auto; place-items: center; border-radius: 50%; background: #d18a45; color: #fff8ec; font: 700 0.7rem 'Space Mono', monospace; }
	.designer-alert-muted { justify-content: flex-start; border-color: var(--designer-line); color: var(--designer-muted); }
	.designer-section { padding: 0.9rem 0.95rem; border-bottom: 1px solid var(--designer-line); }
	.section-heading { justify-content: space-between; gap: 0.75rem; margin-bottom: 0.7rem; }
	.section-heading h3 { font-size: 0.8rem; }
	.designer-count, .designer-chip, .variant-meta { color: var(--designer-muted); font: 0.58rem 'Space Mono', monospace; }
	.designer-chip { padding: 0.24rem 0.42rem; border: 1px solid var(--designer-line); border-radius: 99px; text-transform: capitalize; }
	.designer-chip-coral { border-color: color-mix(in oklab, var(--designer-coral) 35%, var(--designer-line)); color: var(--designer-coral); }
	.token-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.45rem; }
	.token-card { display: flex; align-items: center; gap: 0.48rem; min-width: 0; padding: 0.46rem; border: 1px solid var(--designer-line); border-radius: 0.6rem; background: color-mix(in oklab, var(--app-bg, #f7f7f3) 70%, transparent); }
	.token-swatch { width: 1.15rem; height: 1.15rem; flex: 0 0 auto; border: 1px solid color-mix(in oklab, var(--designer-ink) 18%, transparent); border-radius: 0.35rem; }
	.token-copy { min-width: 0; }
	.token-copy strong { display: block; overflow: hidden; color: var(--designer-ink); font-size: 0.64rem; font-weight: 600; text-overflow: ellipsis; text-transform: capitalize; white-space: nowrap; }
	.token-copy code { display: block; overflow: hidden; color: var(--designer-muted); font: 0.53rem 'Space Mono', monospace; text-overflow: ellipsis; white-space: nowrap; }
	.variant-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.6rem; }
	.variant-card { overflow: hidden; border: 1px solid var(--designer-line); border-radius: 0.75rem; background: color-mix(in oklab, var(--app-bg, #f7f7f3) 68%, transparent); transition: border-color 140ms ease, transform 140ms ease; }
	.variant-card:hover, .variant-card.selected { border-color: color-mix(in oklab, var(--designer-coral) 65%, var(--designer-line)); transform: translateY(-1px); }
	.variant-preview { display: block; width: 100%; padding: 0; background: transparent; color: inherit; text-align: left; }
	.preview-window { height: 6.2rem; padding: 0.5rem; background: #283934; }
	.preview-topbar { display: flex; gap: 0.2rem; height: 0.35rem; }
	.preview-topbar span { width: 0.25rem; height: 0.25rem; border-radius: 50%; background: #d58567; opacity: 0.8; }
	.preview-layout { display: flex; gap: 0.45rem; height: 4.9rem; margin-top: 0.45rem; }
	.preview-rail { width: 22%; border-radius: 0.22rem; background: #b3c0a8; opacity: 0.68; }
	.preview-content { flex: 1; padding: 0.3rem; border-radius: 0.22rem; background: #f0dfc8; }
	.preview-line { width: 62%; height: 0.3rem; margin-top: 0.25rem; border-radius: 99px; background: #6b7e6a; opacity: 0.68; }
	.preview-line-wide { width: 84%; height: 0.42rem; margin-top: 0; background: #c95f49; opacity: 0.8; }
	.preview-block { height: 2.25rem; margin-top: 0.55rem; border-radius: 0.18rem; background: #c5a67d; opacity: 0.55; }
	.variant-select { display: block; padding: 0.35rem 0.5rem; color: var(--designer-muted); font-size: 0.56rem; }
	.variant-card.selected .variant-select { color: var(--designer-coral); font-weight: 700; }
	.variant-copy { align-items: flex-end; gap: 0.5rem; padding: 0.55rem 0.6rem 0.3rem; }
	.variant-copy > div { min-width: 0; flex: 1; }
	.variant-copy h4 { font-size: 0.7rem; }
	.variant-copy p { display: -webkit-box; overflow: hidden; margin: 0.25rem 0 0; color: var(--designer-muted); font-size: 0.6rem; line-height: 1.35; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
	.variant-meta { padding: 0.35rem 0.6rem 0.55rem; }
	.reconstruction-grid { display: grid; grid-template-columns: 1.05fr 0.95fr; gap: 0.8rem; align-items: center; }
	.reconstruction-figure { position: relative; min-height: 8.5rem; overflow: hidden; border-radius: 0.72rem; background: #263a35; }
	.reconstruction-glow { position: absolute; inset: 0; background: radial-gradient(circle at 70% 20%, #d47d5c 0, transparent 42%), linear-gradient(135deg, #2d4a44, #172822); opacity: 0.9; }
	.reconstruction-frame { position: absolute; top: 1rem; right: 0.9rem; bottom: -1.1rem; left: 0.9rem; padding: 0.45rem; border: 1px solid #f0dfc8aa; border-radius: 0.4rem 0.4rem 0 0; background: #efe1ce; transform: rotate(-3deg); }
	.reconstruction-nav { width: 70%; height: 0.28rem; border-radius: 99px; background: #6b7e6a; }
	.reconstruction-hero { height: 3rem; margin-top: 0.45rem; border-radius: 0.28rem; background: linear-gradient(110deg, #c95f49 20%, #e4b18d 20% 56%, #758d77 56%); }
	.reconstruction-columns { display: flex; gap: 0.25rem; margin-top: 0.45rem; }
	.reconstruction-columns span { height: 1.4rem; flex: 1; border-radius: 0.2rem; background: #b3c0a8; }
	.reconstruction-copy p, .comparison-note { margin: 0; color: var(--designer-muted); font-size: 0.66rem; line-height: 1.45; }
	.reconstruction-facts { display: grid; gap: 0.38rem; margin-top: 0.75rem; }
	.reconstruction-facts span { display: grid; gap: 0.12rem; color: var(--designer-muted); font: 0.56rem 'Space Mono', monospace; }
	.reconstruction-facts b { color: var(--designer-ink); font-family: 'DM Sans', sans-serif; font-size: 0.58rem; text-transform: uppercase; }
	.viewport-list { display: grid; gap: 0.48rem; }
	.viewport-row { gap: 0.48rem; min-width: 0; color: var(--designer-ink); font-size: 0.62rem; }
	.viewport-row strong { width: 5.2rem; overflow: hidden; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
	.check-dot { width: 0.45rem; height: 0.45rem; flex: 0 0 auto; border-radius: 50%; background: #d18a45; }
	.check-passed { background: var(--designer-moss); }
	.check-failed { background: var(--designer-coral); }
	.viewport-bar { height: 0.25rem; flex: 1; overflow: hidden; border-radius: 99px; background: color-mix(in oklab, var(--designer-ink) 10%, transparent); }
	.viewport-bar span { display: block; height: 100%; border-radius: inherit; background: var(--designer-moss); }
	.check-label { width: 4.2rem; color: var(--designer-muted); font: 0.53rem 'Space Mono', monospace; text-align: right; text-transform: uppercase; }
	.check-label-failed { color: var(--designer-coral); }
	.comparison-readout { display: grid; grid-template-columns: 1fr auto 1fr 0.7fr; gap: 0.45rem; align-items: end; }
	.compare-box { color: var(--designer-muted); font-size: 0.55rem; }
	.compare-art { height: 4.4rem; margin-top: 0.28rem; border: 1px solid var(--designer-line); border-radius: 0.45rem; background: #344a43; }
	.compare-reference { background: linear-gradient(145deg, #ead5b9 0 39%, #c95f49 39% 55%, #7b927e 55%); }
	.compare-reconstructed { background: linear-gradient(155deg, #ead5b9 0 34%, #c95f49 34% 48%, #809780 48% 74%, #ead5b9 74%); }
	.compare-arrow { color: var(--designer-coral); font-size: 1rem; }
	.compare-score { display: grid; gap: 0.2rem; padding-bottom: 0.2rem; }
	.compare-score strong { color: var(--designer-coral); font: 700 1.1rem 'Space Mono', monospace; }
	.compare-score span { color: var(--designer-muted); font-size: 0.52rem; line-height: 1.2; }
	.comparison-note { margin: 0.7rem 0; }
	.designer-footer { flex-wrap: wrap; gap: 0.5rem 0.7rem; padding: 0.7rem 0.95rem; }
	.designer-footer-line { min-width: 0; gap: 0.35rem; color: var(--designer-muted); font-size: 0.58rem; }
	.designer-footer-line code, .designer-run { max-width: 9rem; overflow: hidden; color: var(--designer-ink); font: 0.53rem 'Space Mono', monospace; text-overflow: ellipsis; white-space: nowrap; }
	.link-dot { width: 0.4rem; height: 0.4rem; border: 1px solid var(--designer-moss); border-radius: 50%; background: color-mix(in oklab, var(--designer-moss) 28%, transparent); }
	.designer-run { margin-left: auto; color: var(--designer-muted); }
	.designer-disclosure, .designer-details-toggle { border-radius: 0.35rem; padding: 0.2rem 0.35rem; color: var(--designer-coral); font-size: 0.58rem; }
	.designer-disclosure:hover, .designer-details-toggle:hover { background: color-mix(in oklab, var(--designer-coral) 10%, transparent); }
	.designer-action-notice { margin: 0; padding: 0 0.95rem 0.65rem; color: var(--designer-coral); font-size: 0.6rem; }
	.designer-evidence { margin: 0 0.95rem 0.65rem; padding: 0.55rem 0.6rem; border: 1px dashed color-mix(in oklab, var(--designer-coral) 34%, var(--designer-line)); border-radius: 0.5rem; }
	.evidence-row { display: flex; gap: 0.4rem; padding: 0.3rem 0; color: var(--designer-muted); font: 0.58rem/1.4 'Space Mono', monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
	.evidence-row + .evidence-row { border-top: 1px solid var(--designer-line); }
	.designer-muted { margin: 0; color: var(--designer-muted); font-size: 0.64rem; line-height: 1.45; }
	.designer-details-toggle { display: block; margin: 0 0.95rem 0.65rem; color: var(--designer-muted); }
	.designer-event-details { max-height: 10rem; overflow: auto; margin: 0 0.95rem 0.8rem; padding: 0.6rem; border-radius: 0.5rem; background: #263a35; color: #ead5b9; font: 0.55rem/1.45 'Space Mono', monospace; white-space: pre-wrap; }
	@media (max-width: 520px) {
		.designer-results { margin-left: 0.1rem; margin-right: 0.1rem; }
		.token-grid, .variant-grid, .reconstruction-grid { grid-template-columns: 1fr; }
		.comparison-readout { grid-template-columns: 1fr 1fr; }
		.compare-arrow { display: none; }
		.compare-score { grid-column: 1 / -1; display: flex; align-items: baseline; gap: 0.35rem; }
		.designer-run { display: none; }
	}
	@media (prefers-reduced-motion: reduce) {
		.designer-button, .variant-card { transition: none; }
	}
</style>