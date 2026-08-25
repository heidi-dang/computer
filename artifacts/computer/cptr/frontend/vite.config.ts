import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

const backendTarget = process.env.CPTR_BACKEND_URL ?? 'http://localhost:9741';
const port = Number(process.env.PORT ?? '5173');

export default defineConfig({
	plugins: [sveltekit(), tailwindcss()],
	server: {
		host: '0.0.0.0',
		port,
		strictPort: true,
		allowedHosts: true,
		proxy: {
			'/api': {
				target: backendTarget,
changeOrigin: false,
				ws: true
			},
			'/v1': {
				target: backendTarget,
changeOrigin: false
			},
			'/socket.io': {
				target: backendTarget,
changeOrigin: false,
				ws: true
			}
		}
	}
});
