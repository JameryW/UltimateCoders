import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";
import fs from "fs";

// ponytail: silent logger — writes Vite logs to file instead of stdout
// keeps TUI terminal clean when dashboard dev server runs alongside
const logFile = fs.createWriteStream("vite-dev.log", { flags: "a" });
const silentLogger = {
  hasWarned: false as boolean,
  hasErrorLogged: false as boolean,
  clearScreen() {},
  info(msg: string) { logFile.write(`[info] ${msg}\n`); },
  warn(msg: string) { logFile.write(`[warn] ${msg}\n`); },
  error(msg: string) { logFile.write(`[error] ${msg}\n`); },
};

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
      // gRPC-Web: tonic-web serves at :50051 under /ultimate_coders.*/ paths
      "/ultimate_coders.": {
        target: "http://localhost:50051",
        changeOrigin: true,
      },
    },
    // ponytail: ignore auto-generated proto file changes to avoid HMR reload loops
    watch: {
      ignored: ["**/src/grpc/engine_pb.ts"],
    },
  },
  customLogger: silentLogger,
});
