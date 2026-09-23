import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    outDir: "../target/generated-resources/static/sop-editor",
    emptyOutDir: true,
    lib: {
      entry: "src/main.tsx",
      name: "SopEditorBundle",
      formats: ["iife"],
      fileName: () => "editor.js",
    },
    rollupOptions: { output: { assetFileNames: "editor.[ext]" } },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
