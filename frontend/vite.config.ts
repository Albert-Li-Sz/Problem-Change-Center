import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

declare const process: {
  env: Record<string, string | undefined>;
};

const backendHost = process.env.P2H_BACKEND_HOST || "127.0.0.1";
const backendPort = process.env.P2H_BACKEND_PORT || "8000";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"]
  },
  server: {
    proxy: {
      "/api": `http://${backendHost}:${backendPort}`
    }
  }
});
