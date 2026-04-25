import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "path";

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => ({
  server: {
    host: "::",
    port: 8080,
    hmr: {
      overlay: false,
    },
  },
  plugins: [react()].filter(Boolean),
  build: {
    // BUG-R1-22: Explicitly disable source maps in production builds.
    // Vite defaults to false anyway, but explicit is safer — prevents
    // accidentally enabling them via a future config change.
    sourcemap: false,
    // P19: split the bundle. Vendor libs are stable + cacheable, the
    // graph + chat surfaces are heavy and only needed by signed-in users,
    // so they get their own chunks instead of bloating the initial JS.
    rollupOptions: {
      output: {
        manualChunks: (id) => {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("/react/") || id.includes("/react-dom/") || id.includes("/scheduler/")) return "vendor-react";
          if (id.includes("react-router")) return "vendor-router";
          if (id.includes("@tanstack/")) return "vendor-query";
          if (id.includes("@radix-ui/")) return "vendor-radix";
          if (id.includes("recharts") || id.includes("d3-")) return "vendor-charts";
          if (id.includes("@supabase/")) return "vendor-supabase";
          if (id.includes("lucide-react")) return "vendor-icons";
          if (id.includes("framer-motion") || id.includes("motion-utils")) return "vendor-motion";
          return "vendor";
        },
      },
    },
    chunkSizeWarningLimit: 600,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
    dedupe: ["react", "react-dom", "react/jsx-runtime", "react/jsx-dev-runtime", "@tanstack/react-query", "@tanstack/query-core"],
  },
}));
