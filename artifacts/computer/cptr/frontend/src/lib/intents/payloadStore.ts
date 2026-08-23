import type { SharePayload } from './types';

const DB_NAME = 'cptr-shares';
const DB_VERSION = 1;
const SHARE_STORE = 'share-payloads';

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

export async function getSharePayload(id: string): Promise<SharePayload | null> {
	if (typeof indexedDB === 'undefined') return null;
	const db = await openDb();
	return new Promise((resolve, reject) => {
		const tx = db.transaction(SHARE_STORE, 'readonly');
		const req = tx.objectStore(SHARE_STORE).get(id);
		req.onsuccess = () => resolve((req.result as SharePayload | undefined) ?? null);
		req.onerror = () => reject(req.error);
		tx.oncomplete = () => db.close();
	});
}

export async function deleteSharePayload(id: string): Promise<void> {
	if (typeof indexedDB === 'undefined') return;
	const db = await openDb();
	return new Promise((resolve, reject) => {
		const tx = db.transaction(SHARE_STORE, 'readwrite');
		const req = tx.objectStore(SHARE_STORE).delete(id);
		req.onsuccess = () => resolve();
		req.onerror = () => reject(req.error);
		tx.oncomplete = () => db.close();
	});
}
