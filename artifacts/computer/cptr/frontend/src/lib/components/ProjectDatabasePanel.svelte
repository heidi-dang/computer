<script lang="ts">
import { inspectProjectDatabase, queryProjectDatabase, type DatabaseRequest } from '$lib/apis/database';

let { workspace }: { workspace: string } = $props();
let engine = $state<'sqlite' | 'postgresql'>('sqlite');
let database = $state('project.db');
let sql = $state('SELECT name, type FROM sqlite_master WHERE type = "table" ORDER BY name');
let loading = $state(false);
let error = $state('');
let inspection = $state<any>(null);
let result = $state<any>(null);

async function inspect() {
  loading = true; error = '';
  try { inspection = await inspectProjectDatabase({ workspace, engine, database } satisfies DatabaseRequest); }
  catch (cause) { error = cause instanceof Error ? cause.message : 'Database inspection failed'; }
  finally { loading = false; }
}

async function query() {
  loading = true; error = '';
  try { result = await queryProjectDatabase({ workspace, engine, database, sql }); }
  catch (cause) { error = cause instanceof Error ? cause.message : 'Database query failed'; }
  finally { loading = false; }
}
</script>

<section class="database-panel" data-testid="project-database-panel" aria-labelledby="database-title">
  <div class="heading">
    <div><span class="eyebrow">PROJECT DATA</span><h2 id="database-title">Database inspector</h2></div>
    <span class="badge">{engine}</span>
  </div>
  <div class="controls">
    <select bind:value={engine} aria-label="Database engine"><option value="sqlite">SQLite</option><option value="postgresql">PostgreSQL</option></select>
    <input bind:value={database} aria-label="Database path" placeholder="project.db" />
    <button type="button" onclick={inspect} disabled={loading || !workspace}>Inspect schema</button>
  </div>
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if inspection}
    <div class="summary"><strong>{inspection.schema?.tables?.length ?? 0} tables</strong><span>Integrity: {inspection.integrity ?? 'verified'}</span><span>Fingerprint: {inspection.schema_fingerprint?.slice(0, 12)}</span></div>
    <div class="tables">
      {#each inspection.schema?.tables ?? [] as table}
        <details><summary>{table.name} <small>{table.columns.length} columns · {table.indexes.length} indexes</small></summary><ul>{#each table.columns as column}<li><code>{column.name}</code> {column.type || 'any'} {column.primary_key ? 'PRIMARY KEY' : ''} {column.nullable ? 'nullable' : 'required'}</li>{/each}</ul></details>
      {/each}
    </div>
  {/if}
  <label class="query-label" for="project-db-query">Read-only query</label>
  <textarea id="project-db-query" bind:value={sql} rows="3" spellcheck="false"></textarea>
  <button type="button" class="secondary" onclick={query} disabled={loading || !workspace}>Run query</button>
  {#if result}<pre class="result">{JSON.stringify(result, null, 2)}</pre>{/if}
</section>

<style>
.database-panel{margin-top:1.25rem;padding:1.25rem;border:1px solid #30343c;border-radius:1rem;background:#111318;color:#e7e9ed}
.heading{display:flex;justify-content:space-between;align-items:start}.eyebrow{font:600 .65rem ui-monospace;color:#8f98a8;letter-spacing:.12em}h2{margin:.25rem 0 0;font-size:1.1rem}.badge{padding:.25rem .45rem;border-radius:.4rem;background:#252a33;color:#b7c1cf;font:600 .7rem ui-monospace}
.controls{display:flex;gap:.55rem;margin:1rem 0;flex-wrap:wrap}select,input,textarea{border:1px solid #3b424e;border-radius:.45rem;padding:.55rem;background:#191d24;color:#e7e9ed}input{min-width:12rem;flex:1}textarea{display:block;width:100%;margin:.4rem 0 .6rem;font: .78rem ui-monospace}
button{border:0;border-radius:.45rem;padding:.55rem .75rem;background:#e8edf4;color:#111318;font-weight:600}button.secondary{background:#252a33;color:#e7e9ed}.summary{display:flex;gap:1rem;flex-wrap:wrap;color:#b7c1cf;font-size:.78rem;margin:.75rem 0}.tables{display:grid;gap:.4rem}.tables details{border:1px solid #30343c;border-radius:.4rem;padding:.5rem}.tables summary{cursor:pointer}.tables small{color:#8f98a8}.tables ul{padding-left:1.2rem;color:#b7c1cf}.query-label{display:block;margin-top:1rem;font-size:.78rem;color:#9ea7b5}.result{max-height:180px;overflow:auto;color:#b7c1cf;background:#0b0d10;padding:.7rem;border-radius:.4rem}.error{color:#ff9d9d}
</style>