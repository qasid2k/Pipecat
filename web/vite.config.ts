// Build and dev settings for the supervisor web app ([[decisions]] 056).
//
// `npm run build` writes into ../api/static, which IS committed: the VM serves
// those files and never needs Node. `npm run dev` proxies the API to a local
// silent-engine bot on 18091 -- never 8091, which on the laptop can be a VS
// Code port forward to the LIVE VM.
import { defineConfig } from "vitest/config";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";

const API = "http://127.0.0.1:18091";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../api/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": API,
      "/health": API,
      "/pool": API,
      "/calls": API,
      "/history": API,
      "/metrics": API,
      "/live": { target: API, ws: true },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
