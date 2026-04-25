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
    // P19: split the bundle for caching. The previous fine-grained split
    // produced a circular import between vendor-react and the catchall
    // vendor- chunk (some packages depended on each other across the line),
    // which manifested at runtime as `Cannot read properties of undefined
    // (reading 'createContext')` because module-init order broke. The fix
    // is to keep the lazy route-level split (App.tsx) and only carve out
    // the heavy, independent vendors that have no React-context circular
    // edges: recharts/d3, supabase, and lucide-react. Everything else
    // (react, react-dom, scheduler, react-router, @tanstack, @radix-ui,
    // framer-motion, etc.) lives in the single 'vendor' chunk so module
    // init order is deterministic. Initial JS payload is still small
    // because route chunks lazy-load.
    rollupOptions: {
      output: {
        manualChunks: (id) => {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("recharts") || id.includes("/d3-") || id.includes("victory-vendor")) return "vendor-charts";
          if (id.includes("@supabase/")) return "vendor-supabase";
          if (id.includes("lucide-react")) return "vendor-icons";
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
