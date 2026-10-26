import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Procfile.dev binds uvicorn to 127.0.0.1:8000. Naming the IPv4 address avoids a
// `localhost` → ::1 lookup that nothing listens on. HARMONY_API_ORIGIN overrides it.
const apiOrigin = process.env.HARMONY_API_ORIGIN ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    // Same origin in dev as in production (FastAPI serves the SPA), so the session
    // cookie and EventSource need no CORS.
    proxy: {
      "/api": { target: apiOrigin },
    },
  },
  build: {
    // FastAPI serves web/dist: index.html with no-cache, /assets/* as immutable.
    outDir: "dist",
    assetsDir: "assets",
  },
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    environment: "node",
    restoreMocks: true,
    unstubGlobals: true,
  },
});
