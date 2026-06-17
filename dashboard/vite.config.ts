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
  server: {
    port: 5173,
    proxy: {
      "/dashboard/api": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
      // gRPC-Web: tonic-web serves at :50051 under /ultimate_coders.*/ paths
      "^/ultimate_coders\\.": {
        target: "http://localhost:50051",
        changeOrigin: true,
      },
    },
  },
});
