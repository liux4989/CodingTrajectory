import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          if (
            id.includes("/react-dom/") || id.includes("/react/") || id.includes("/scheduler/") ||
            id.includes("@radix-ui/") || id.includes("react-remove-scroll") || id.includes("aria-hidden") ||
            id.includes("@floating-ui/") || id.includes("react-style-singleton") || id.includes("get-nonce") ||
            id.includes("/use-callback-ref/") || id.includes("/use-sidecar/") || id.includes("use-sync-external-store")
          ) return "vendor-react";
          if (id.includes("/motion-dom/") || id.includes("/motion-utils/") || id.includes("/framer-motion/") || id.includes("/motion/")) return "vendor-motion";
          if (id.includes("@tanstack/")) return "vendor-tanstack";
          if (
            id.includes("/recharts/") || id.includes("/d3-") || id.includes("/victory-vendor/") ||
            id.includes("/decimal.js") || id.includes("/internmap/") || id.includes("/react-redux/") ||
            id.includes("/redux") || id.includes("/immer/") || id.includes("/reselect/") ||
            id.includes("/es-toolkit/") || id.includes("/eventemitter3/") || id.includes("/tiny-invariant/") ||
            id.includes("/react-is/")
          ) return "vendor-charts";
        },
      },
    },
  },
});
