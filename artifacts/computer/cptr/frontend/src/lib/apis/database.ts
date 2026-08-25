import { fetchJSON, jsonBody } from '$lib/apis';

export type DatabaseRequest = {
  workspace: string;
  engine: 'sqlite' | 'postgresql';
  database?: string;
};

export async function inspectProjectDatabase(input: DatabaseRequest) {
  return fetchJSON('/v1/flowdeck/database/inspect', {
    ...jsonBody(input),
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `db-inspect-${crypto.randomUUID()}` }
  });
}

export async function queryProjectDatabase(input: DatabaseRequest & { sql: string; params?: unknown[] }) {
  return fetchJSON('/v1/flowdeck/database/query', {
    ...jsonBody(input),
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `db-query-${crypto.randomUUID()}` }
  });
}