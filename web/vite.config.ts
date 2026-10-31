import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// The Python API serves the built app from web/dist in production, so the dev
// server proxies /api to it rather than the app knowing two different base URLs.
const API = process.env.FINANCES_API ?? "http://127.0.0.1:8787"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    // Statement data is personal; no reason to ship maps that quote it back.
    sourcemap: false,
  },
})
