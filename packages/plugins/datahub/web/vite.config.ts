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
          // clsx is imported by both the entry (via cn()) and react-apexcharts;
          // without an explicit bucket Rollup merges it into vendor-charts,
          // making the charts chunk load eagerly on every route.
          if (id.includes("/clsx/")) return "vendor-react";
          if (
            id.includes("/react-dom/") || id.includes("/react/") || id.includes("/scheduler/") ||
            id.includes("@radix-ui/") || id.includes("react-remove-scroll") || id.includes("aria-hidden") ||
            id.includes("@floating-ui/") || id.includes("react-style-singleton") || id.includes("get-nonce") ||
            id.includes("/use-callback-ref/") || id.includes("/use-sidecar/") || id.includes("use-sync-external-store")
          ) return "vendor-react";
          if (id.includes("/motion-dom/") || id.includes("/motion-utils/") || id.includes("/framer-motion/") || id.includes("/motion/")) return "vendor-motion";
          if (id.includes("@tanstack/")) return "vendor-tanstack";
          if (
            id.includes("/apexcharts/") || id.includes("/react-apexcharts/") ||
            id.includes("/svg.draggable.js/") || id.includes("/svg.easing.js/") ||
            id.includes("/svg.filter.js/") || id.includes("/svg.pathmorphing.js/") ||
            id.includes("/svg.resize.js/") || id.includes("/svg.select.js/")
          ) return "vendor-charts";
        },
      },
    },
  },
});
