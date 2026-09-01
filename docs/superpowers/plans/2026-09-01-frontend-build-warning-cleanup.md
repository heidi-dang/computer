# Frontend Build Warning Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all actionable warnings emitted by the CPTR frontend production build while preserving UI behavior, accessibility, MCP functionality, and bounded bundle loading.

**Architecture:** Use the production Vite build as the warning oracle. Fix Svelte 5 migration/accessibility issues at their source, replace ineffective lazy imports with static imports, and use Rolldown's supported bounded code-splitting groups for large application/vendor chunks. Disable only the non-correctness `pluginTimings` profiling diagnostic after product warnings are eliminated.

**Tech Stack:** Svelte 5 runes, SvelteKit 2, Vite 8, Rolldown, TypeScript, Node test runner, Prettier.

**Spec:** `docs/superpowers/specs/2026-09-01-frontend-build-warning-cleanup-design.md`

## Global Constraints

- No new dependencies.
- No blanket `svelte-ignore`, broad Vite `onwarn`, or multi-megabyte `chunkSizeWarningLimit` workaround.
- Preserve NightOwl and `/mcp` behavior.
- Production `npm run build` is the warning acceptance oracle; the repository-wide pre-existing `svelte-check` backlog is out of scope.
- Fix accessibility semantically with associated labels, buttons/keyboard equivalents, and valid HTML.
- Preserve intentional immediate focus via lifecycle focus, not `autofocus`.
- Keep large singleton syntax/WASM assets lazy.

---

### Task 0: Add a production-build warning gate

**Files:**
- Create: `cptr/frontend/scripts/check-production-build.mjs`
- Modify: `cptr/frontend/package.json`

**Interfaces:**
- Produces: `npm run build:clean`, which executes the real Vite production build and fails if the output contains `[vite-plugin-svelte]`, `INEFFECTIVE_DYNAMIC_IMPORT`, `Some chunks are larger than`, or `[PLUGIN_TIMINGS]`.

- [ ] **Step 1: Create the warning gate**

```js
import { spawnSync } from 'node:child_process';

const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const result = spawnSync(npm, ['run', 'build'], {
  cwd: new URL('..', import.meta.url),
  encoding: 'utf8',
  env: process.env
});
const output = `${result.stdout ?? ''}${result.stderr ?? ''}`;
process.stdout.write(output);
const clean = output.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '');
const forbidden = [
  /\[vite-plugin-svelte\]/,
  /INEFFECTIVE_DYNAMIC_IMPORT/,
  /Some chunks are larger than/,
  /\[PLUGIN_TIMINGS\]/
];
if ((result.status ?? 1) !== 0 || forbidden.some((pattern) => pattern.test(clean))) process.exit(1);
```

Add to `package.json`:

```json
"build:clean": "node scripts/check-production-build.mjs"
```

- [ ] **Step 2: Verify RED**

Run: `npm run build:clean`

Expected: FAIL because the baseline production build emits Svelte compiler warnings and bundle diagnostics.

- [ ] **Step 3: Commit the gate**

```bash
git add cptr/frontend/scripts/check-production-build.mjs cptr/frontend/package.json
git commit -m "test: gate frontend build warnings"
```

---

### Task 1: Complete the Svelte 5 reactivity and deprecated-syntax migration

**Files:**
- Modify components currently producing `state_referenced_locally`, `non_reactive_update`, `svelte_self_deprecated`, `svelte_component_deprecated`, and unused-selector warnings, including:
  - `cptr/frontend/src/lib/components/common/Spinner.svelte`
  - `cptr/frontend/src/lib/components/FileEditor.svelte`
  - `cptr/frontend/src/lib/components/BrowserPreview.svelte`
  - `cptr/frontend/src/lib/components/chat/ChatPanel.svelte`
  - `cptr/frontend/src/lib/components/SaveDialog.svelte`
  - `cptr/frontend/src/lib/components/preview/PDFViewer.svelte`
  - `cptr/frontend/src/lib/components/preview/JsonTreeView.svelte`
  - `cptr/frontend/src/lib/components/markdown/BlockRenderer.svelte`
  - `cptr/frontend/src/lib/components/markdown/InlineRenderer.svelte`
  - `cptr/frontend/src/lib/components/chat/UserMessage.svelte`
  - `cptr/frontend/src/lib/components/chat/AssistantMessage.svelte`
  - `cptr/frontend/src/lib/components/chat/OutputEditView.svelte`
  - `cptr/frontend/src/lib/components/chat/ToolCallCollapsible.svelte`
  - `cptr/frontend/src/lib/components/Collapsible.svelte`
  - form/modal files whose initial editable state is copied from props
  - `cptr/frontend/src/lib/components/GroupTabBar.svelte`
  - `cptr/frontend/src/lib/components/Settings/Keyboard.svelte`

**Interfaces:**
- Props that are presentation-derived use `$derived`.
- Locally editable form state uses `$state` plus narrowly scoped `$effect` synchronization only when the source entity identity changes.
- Bound DOM/component references use `$state` when their updates are observed.
- Recursive Svelte components import themselves explicitly.

- [ ] **Step 1: Use the Task 0 production build gate as RED evidence**

Run: `npm run build:clean`

Expected: FAIL with the current runes/deprecation warnings.

- [ ] **Step 2: Apply minimal Svelte 5 migrations**

Representative patterns:

```svelte
let { size = 16, borderWidth }: Props = $props();
const px = $derived(typeof size === 'number' ? size : parseFloat(size) * 4);
const bw = $derived(borderWidth ?? (px <= 12 ? 1.5 : 2));
```

```svelte
let messagesEl = $state<HTMLDivElement>();
```

```svelte
<script lang="ts">
  import JsonTreeView from './JsonTreeView.svelte';
</script>
<JsonTreeView ... />
```

```svelte
{@const Component = RichTextEditor}
<Component ... />
```

Remove only selectors proven unused by the Svelte compiler.

- [ ] **Step 3: Run focused frontend regressions and build gate**

Run:

```bash
node --test tests/*.mjs
npm run build:clean
```

Expected: all tests pass; any remaining build-gate failure must come from another warning class, not these migration warnings.

- [ ] **Step 4: Commit**

```bash
git add cptr/frontend/src/lib/components
git commit -m "fix: complete Svelte 5 component migration"
```

---

### Task 2: Repair semantic accessibility and form markup

**Files:**
- Modify all components currently producing `a11y_label`, `a11y_click_events_have_key_events`, `a11y_no_noninteractive_element_interactions`, `a11y_autofocus`, explicit accessible-name, invalid nested button, and invalid self-closing-option warnings, including:
  - `Admin/ToolServers.svelte`
  - `Admin/CreateBotModal.svelte`
  - `Admin/CreateConnectionModal.svelte`
  - `Admin/EditConnectionModal.svelte`
  - `Admin/CreateUserModal.svelte`
  - `Admin/EditUserModal.svelte`
  - `Settings/Notifications.svelte`
  - `SetupWizard.svelte`
  - `automations/AutomationsPanel.svelte`
  - `GitBar.svelte`, `GitView.svelte`, `GroupTabBar.svelte`
  - `DropdownMenu.svelte`, `SidebarFooter.svelte`, `SidebarWorkspaceList.svelte`
  - `FileBrowser.svelte`, `DirectoryPicker.svelte`
  - `VoiceMemoModal.svelte`, `mcp/McpToolForm.svelte`
  - chat icon/action components and `AskUserCard.svelte`

**Interfaces:**
- Form labels use stable `id` + `for` pairs or `aria-labelledby` for composite controls.
- Clickable generic elements become `<button type="button">` when semantically actions; otherwise they gain correct keyboard behavior and role only when a button would alter layout/semantics.
- Icon-only buttons receive `aria-label` or `title` derived from the existing localized action copy.
- Intentional focus uses `bind:this` + `tick()`/`onMount` or an existing open-state effect.
- `<option>` always uses explicit closing tags.
- No button may contain another button.

- [ ] **Step 1: Keep `npm run build:clean` RED for accessibility warnings**

Run: `npm run build:clean`

Expected: FAIL while these warnings remain.

- [ ] **Step 2: Implement semantic fixes file-by-file**

Representative patterns:

```svelte
<label for="server-name">Name</label>
<input id="server-name" bind:value={name} />
```

```svelte
<button type="button" class="contents" onclick={openItem} aria-label={label}>...</button>
```

```svelte
<option value="https://api.openai.com/v1"></option>
```

```svelte
let nameInput = $state<HTMLInputElement>();
$effect(() => {
  if (open && nameInput) void tick().then(() => nameInput?.focus());
});
```

- [ ] **Step 3: Verify frontend behavior**

Run:

```bash
node --test tests/*.mjs
npm run build:clean
```

Expected: regressions pass and no Svelte accessibility/markup warning remains.

- [ ] **Step 4: Commit**

```bash
git add cptr/frontend/src/lib/components
git commit -m "fix: resolve frontend accessibility warnings"
```

---

### Task 3: Remove ineffective imports and bound production chunks

**Files:**
- Modify: `cptr/frontend/src/lib/stores.ts`
- Modify: `cptr/frontend/src/lib/stores/chat.ts`
- Modify: `cptr/frontend/vite.config.ts`

**Interfaces:**
- `deleteWorkspace` is imported statically from `$lib/apis/state` and reused by `stores.ts`.
- `goto` is imported statically from `$app/navigation` and reused by `stores/chat.ts`.
- Vite passes Rolldown a bounded `output.codeSplitting` configuration for application and vendor modules.
- Only `checks.pluginTimings` is disabled; all correctness checks remain enabled.
- `chunkSizeWarningLimit` may be raised only to a narrow value (target 700 kB) after the 1.76 MB route and 1.37 MB shared monoliths are split. This accommodates known lazy singleton Shiki WASM/grammar assets while continuing to catch regressions above that bound.

- [ ] **Step 1: Verify RED for import/chunk diagnostics**

Run: `npm run build:clean`

Expected: FAIL with `INEFFECTIVE_DYNAMIC_IMPORT`, oversized chunks, and `PLUGIN_TIMINGS` while not yet fixed.

- [ ] **Step 2: Replace ineffective dynamic imports**

```ts
import {
  deleteWorkspace as deleteWorkspaceApi,
  getPreferences,
  ...
} from '$lib/apis/state';
```

and call `deleteWorkspaceApi(...)` directly.

```ts
import { goto } from '$app/navigation';
```

and call `goto(...)` directly from the notification handler.

- [ ] **Step 3: Configure bounded Rolldown code splitting**

Use Vite 8's `build.rolldownOptions.output.codeSplitting.groups` with separate vendor/application groups and a `maxSize` below the warning limit. Use `entriesAware: true` to avoid forcing unrelated entry dependencies into one vendor chunk. Keep the exact configuration minimal and verify produced chunk sizes before accepting it.

The configuration must preserve execution order; if Rolldown reports a chunk-cycle/order problem, do not silence it—adjust groups instead.

- [ ] **Step 4: Disable only plugin timing profiling**

Configure Rolldown input checks so only `pluginTimings` is false. Do not set `checks: false`.

- [ ] **Step 5: Verify bundle output**

Run:

```bash
npm run build:clean
```

Expected: PASS, with no ineffective-import, oversized-chunk, or plugin-timing diagnostic.

Inspect `.svelte-kit/output/client/.vite/manifest.json` and verify the previous 1.76 MB route and 1.37 MB shared chunk are no longer emitted as monoliths.

- [ ] **Step 6: Commit**

```bash
git add cptr/frontend/src/lib/stores.ts cptr/frontend/src/lib/stores/chat.ts cptr/frontend/vite.config.ts
git commit -m "perf: bound frontend production chunks"
```

---

### Task 4: Final integrated verification

**Files:**
- Verify all files changed by Tasks 0–3.

- [ ] **Step 1: Run fresh warning-clean production build**

Run: `npm run build:clean`

Expected: exit 0 with none of the four forbidden warning signatures.

- [ ] **Step 2: Run frontend regressions and formatting**

```bash
node --test tests/*.mjs
npx prettier --check src scripts tests vite.config.ts package.json
git diff --check
```

Expected: all pass.

- [ ] **Step 3: Browser smoke the MCP route**

Use the existing disposable authenticated `/mcp` fixture approach and managed Chrome at desktop and `390x844`. Verify Topology, Console, Activity, Back navigation, and mobile one-pane switching still render and operate.

- [ ] **Step 4: Inspect final repository state**

```bash
git status --short --branch
git log --oneline -8
```

Expected: clean local feature branch with no push/deployment unless explicitly requested by the user.
