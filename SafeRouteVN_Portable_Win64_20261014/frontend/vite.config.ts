import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { buildGraphPlugin } from "./scripts/buildGraphPlugin.ts";
import { localLauncherProxy } from "./scripts/localLauncherProxy.mjs";

export default defineConfig(({ command, mode }) => {
  const explicitMock = mode === "mock";
  const local = command === 'serve' && !explicitMock && process.env.SAFEROUTE_LOCAL_ACCESS_FILE;
  const launcher = local ? localLauncherProxy({accessFile:local,authFile:process.env.SAFEROUTE_LOCAL_AUTH_FILE,
    origin:process.env.VITE_M3_BASE_URL,backend:process.env.SAFEROUTE_LOCAL_BACKEND_URL}) : undefined;
  const mockModule = command !== "build" || explicitMock ? "./src/services/api/mockEntry.ts" : "./src/config/mockUnavailable.ts";
  return {
    plugins: [react(), buildGraphPlugin(explicitMock ? "mock" : "backend"), ...(launcher?[launcher]:[])],
    resolve: { alias: { "@dispatch-mock-entry": fileURLToPath(new URL(mockModule, import.meta.url)) } },
    ...(explicitMock ? { define: { "import.meta.env.VITE_DISPATCH_MODE": JSON.stringify("mock") } } : {}),
    test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], css: true, exclude: ["**/node_modules/**", "**/scripts/*.check.mjs"] }
  };
});
