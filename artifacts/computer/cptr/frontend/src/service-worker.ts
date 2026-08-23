/// <reference types="@sveltejs/kit" />
/// <reference lib="webworker" />

import { build, files, version } from '$service-worker';

const sw = self as unknown as ServiceWorkerGlobalScope;
const CACHE = `cptr-${version}`;
const ASSETS = [...new Set([...build, ...files, '/manifest.json'])].filter(
	(path) => !path.includes('sql-wasm.wasm')
);
const DB_NAME = 'cptr-shares';
const DB_VERSION = 1;
const SHARE_STORE = 'share-payloads';

sw.addEventListener('install', (event) => {
	event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)));
});

sw.addEventListener('activate', (event) => {
	event.waitUntil(
		(async () => {
			for (const key of await caches.keys()) {
				if (key.startsWith('cptr-') && key !== CACHE) await caches.delete(key);
			}
			await sw.clients.claim();
		})()
	);
});

sw.addEventListener('message', (event) => {
	if (event.data?.type === 'SKIP_WAITING') sw.skipWaiting();
});

sw.addEventListener('fetch', (event) => {
	const url = new URL(event.request.url);
	if (event.request.method === 'POST' && url.searchParams.get('intent') === 'share') {
		event.respondWith(handleShareTarget(event.request));
		return;
	}

	if (event.request.method !== 'GET') return;
	if (isLiveBackendPath(url.pathname)) return;

	if (event.request.mode === 'navigate') {
		event.respondWith(
			(async () => {
				try {
					return await fetch(event.request);
				} catch {
					return (
						(await caches.match('/offline.html')) ??
						new Response('Computer is unreachable', { status: 503 })
					);
				}
			})()
		);
		return;
	}

	event.respondWith(
		(async () => {
			const cached = await caches.match(event.request);
			if (cached) return cached;
			const response = await fetch(event.request);
			if (response.ok && isStaticAsset(url.pathname)) {
				const cache = await caches.open(CACHE);
				cache.put(event.request, response.clone());
			}
			return response;
		})()
	);
});

async function handleShareTarget(request: Request): Promise<Response> {
	const form = await request.formData();
	const id = crypto.randomUUID();
	const files = form
		.getAll('files')
		.filter((item): item is File => item instanceof File)
		.map((file) => ({
			name: file.name,
			type: file.type,
			lastModified: file.lastModified,
			file
		}));

	await putSharePayload(id, {
		id,
		title: stringValue(form.get('title')),
		text: stringValue(form.get('text')),
		url: stringValue(form.get('url')),
		files
	});

	return Response.redirect(`/?intent=share&payload=${encodeURIComponent(id)}`, 303);
}

function stringValue(value: FormDataEntryValue | null): string | undefined {
	return typeof value === 'string' && value.trim() ? value : undefined;
}

function isLiveBackendPath(pathname: string): boolean {
	return (
		pathname.startsWith('/api/') ||
		pathname.startsWith('/socket.io/') ||
		pathname.startsWith('/v1/') ||
		pathname.startsWith('/workspace/files/')
	);
}

function isStaticAsset(pathname: string): boolean {
	return pathname.startsWith('/_app/') || pathname.startsWith('/icon-') || pathname.includes('.');
}

function openDb(): Promise<IDBDatabase> {
	return new Promise((resolve, reject) => {
		const req = indexedDB.open(DB_NAME, DB_VERSION);
		req.onupgradeneeded = () => {
			const db = req.result;
			if (!db.objectStoreNames.contains(SHARE_STORE)) db.createObjectStore(SHARE_STORE);
		};
		req.onsuccess = () => resolve(req.result);
		req.onerror = () => reject(req.error);
	});
}

async function putSharePayload(id: string, payload: unknown): Promise<void> {
	const db = await openDb();
	await new Promise<void>((resolve, reject) => {
		const tx = db.transaction(SHARE_STORE, 'readwrite');
		const req = tx.objectStore(SHARE_STORE).put(payload, id);
		req.onsuccess = () => resolve();
		req.onerror = () => reject(req.error);
		tx.oncomplete = () => db.close();
	});
}
