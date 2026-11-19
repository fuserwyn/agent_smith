import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the Python server (agent-studio) runs on 8790; Vite serves the UI with hot reload and
// forwards the API and WebSockets to it. Production: `vite build` -> web/dist, served by agent-studio.
const backend = process.env.STUDIO_BACKEND ?? "http://127.0.0.1:8790";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: backend },
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
  // Monaco is a large chunk by nature; it is loaded lazily when the first file opens.
  build: { outDir: "dist", emptyOutDir: true, sourcemap: true, chunkSizeWarningLimit: 3000 },
});
