# Frontend Build Warning Cleanup Design

## Goal

Make the CPTR frontend production build clean of actionable Svelte compiler/accessibility warnings, ineffective dynamic-import warnings, and oversized initial/shared JavaScript chunks without globally suppressing correctness warnings or weakening existing behavior.

## Baseline evidence

A fresh `npm run build` on `fix/mcp-mobile-runtime` at `e18461839d99a350dddb4e7d3b4567aed1d6f408` emits:

- 147 unique Svelte compiler warnings across 40 files.
- 48 label/control association warnings.
- 33 `state_referenced_locally` warnings.
- 18 click-only non-interactive-element warnings.
- 13 invalid self-closing non-void element warnings.
- 10 autofocus warnings.
- 8 non-reactive update warnings.
- 8 deprecated `<svelte:self>` warnings.
- 4 icon-button accessible-name warnings.
- 2 unused CSS selector warnings.
- 1 deprecated `<svelte:component>` warning.
- 1 nested interactive-control hydration warning.
- 1 non-interactive listener warning.
- `INEFFECTIVE_DYNAMIC_IMPORT` for `$lib/apis/state` and `$app/navigation`.
- JavaScript chunks larger than Vite's default 500 kB threshold, including a 1.76 MB route node and a 1.37 MB shared library chunk.
- Rolldown `PLUGIN_TIMINGS` diagnostics caused by compiler/plugin profiling rather than application correctness.

`npm run check` is not the acceptance oracle for this task because the repository has a separate pre-existing backlog of TypeScript/i18n diagnostics (`1476 errors and 197 warnings` in the baseline). The production Vite build output is the acceptance oracle for this cleanup.

## Root causes

### Svelte compiler warnings

The frontend was migrated to Svelte 5 runes mode while many components retain Svelte 4-era patterns:

- props copied into non-reactive initial values instead of `$derived`/`$state` synchronization;
- DOM bindings assigned to plain variables rather than `$state`;
- `<svelte:self>` and `<svelte:component>` legacy syntax;
- labels used as visual text without `for`/`id` association;
- `autofocus` attributes instead of explicit focus lifecycle handling;
- click handlers attached to generic `div`/`span`/`label` elements where semantic buttons or keyboard-equivalent controls are required;
- nested `<button>` markup that is invalid HTML and can hydrate differently;
- stale component-scoped selectors.

The fix must use native Svelte 5 patterns and semantic HTML rather than `svelte-ignore` to silence warnings.

### Ineffective dynamic imports

`stores.ts` dynamically imports `$lib/apis/state` even though the same module is already statically imported in that file and several other initial-route modules. `stores/chat.ts` dynamically imports `$app/navigation`, which is already statically imported throughout the initial application graph. These imports cannot create lazy chunks; they should be static and reused.

### Large chunks

The current automatic Rolldown partitioning leaves a 1.76 MB route node and a 1.37 MB shared chunk. Heavy editor, preview, markdown, icon, and syntax-highlight dependencies are not partitioned into bounded vendor/application groups. Several large singleton lazy assets (for example Shiki WASM/large language grammars) are intrinsically above 500 kB and should remain lazy rather than be forced into the initial route.

Use Rolldown's supported `output.codeSplitting.groups` to partition application/vendor modules with bounded group sizes. Only after the initial/shared monoliths are eliminated may `chunkSizeWarningLimit` be raised narrowly enough to accommodate unavoidable lazy singleton assets. Do not set an arbitrary multi-megabyte warning limit.

### Plugin timings

`PLUGIN_TIMINGS` is an opt-in Rolldown performance check. Disable only `checks.pluginTimings` in production build configuration after all application warnings are fixed. Do not install a broad `onwarn` filter and do not disable unrelated checks.

## Behavior constraints

- Preserve all existing UI behavior and NightOwl/MCP work.
- Do not remove keyboard functionality while resolving accessibility warnings.
- Do not replace real labels with `aria-hidden` or blanket ignores.
- Focus behavior that is intentionally immediate (rename/new-item/modal fields) may use `onMount`/`tick` with element refs instead of the HTML `autofocus` attribute.
- Runes state derived from props must continue to react when the prop can change; local editable form state must be synchronized intentionally rather than accidentally reset during user edits.
- Dynamic-import fixes must preserve navigation and workspace deletion behavior.
- Bundle partitioning must preserve execution order and successful production rendering.
- No new dependencies.

## Acceptance

A final fresh production build must:

1. Exit 0.
2. Emit zero `[vite-plugin-svelte]` warnings from `src/`.
3. Emit zero `INEFFECTIVE_DYNAMIC_IMPORT` warnings.
4. Emit zero `Some chunks are larger than ...` warnings.
5. Emit zero `PLUGIN_TIMINGS` diagnostics.
6. Preserve the existing frontend regression suite (`node --test tests/*.mjs`).
7. Pass Prettier for all changed frontend files and `git diff --check`.
8. Keep the MCP `/mcp` browser smoke path working after the cleanup.

The broader pre-existing `svelte-check` TypeScript/i18n backlog is explicitly outside this warning-cleanup scope unless a touched file introduces a new diagnostic.
