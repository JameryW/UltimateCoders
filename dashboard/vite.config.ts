import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    // Mermaid's largest lazy diagram chunk is ~594 kB (~138 kB gzip).
    // Keep the threshold close to that known boundary so initial-bundle regressions still warn.
    chunkSizeWarningLimit: 600,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: "vendor",
              test: /node_modules[\\/]/,
              tags: ["$initial"],
              maxSize: 400 * 1024,
              entriesAware: true,
            },
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      // gRPC-Web: tonic-web serves at :50051 under /ultimate_coders.*/ paths
      "/ultimate_coders.": {
        target: "http://localhost:50051",
        changeOrigin: true,
      },
      // TUI WebSocket → FastAPI backend
      "/ws/tui": {
        target: "http://localhost:8080",
        ws: true,
        changeOrigin: true,
      },
      // Dashboard REST/SSE API → FastAPI backend
      "/dashboard/api": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
    },
  },
});
