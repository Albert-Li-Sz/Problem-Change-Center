import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

declare const process: {
  env: Record<string, string | undefined>;
};

const backendHost = process.env.P2H_BACKEND_HOST || "127.0.0.1";
const backendPort = process.env.P2H_BACKEND_PORT || "8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": `http://${backendHost}:${backendPort}`
    }
  }
});
