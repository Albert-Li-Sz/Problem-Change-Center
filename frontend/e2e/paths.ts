import path from "node:path";
import { fileURLToPath } from "node:url";

export const frontendRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  ".."
);
export const repositoryRoot = path.resolve(frontendRoot, "..");
export const runtimeRoot =
  process.env.P2H_E2E_RUNTIME_ROOT ||
  path.resolve(frontendRoot, "test-results", `e2e-runtime-${process.pid}`);
process.env.P2H_E2E_RUNTIME_ROOT = runtimeRoot;
export const corpusRoot = path.resolve(runtimeRoot, "corpus");
export const dataRoot = path.resolve(runtimeRoot, "backend-data");
export const fakeDockerPath = path.resolve(
  frontendRoot,
  "e2e",
  "fixtures",
  "fake-docker.py"
);
export const pythonExecutable =
  process.env.P2H_E2E_PYTHON ||
  path.resolve(repositoryRoot, "backend", ".venv", "bin", "python");
