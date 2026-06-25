import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/topology/",
  build: {
    outDir: "../../src/novafabric/serve/static/topology",
    emptyOutDir: true,
  },
  test: {
    environment: "node",
    globals: true,
  },
  worker: {
    format: "es",
  },
});
