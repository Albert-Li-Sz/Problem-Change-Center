import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import {
  corpusRoot,
  pythonExecutable,
  repositoryRoot,
  runtimeRoot
} from "./paths";

export default function globalSetup() {
  fs.mkdirSync(runtimeRoot, { recursive: true });
  fs.rmSync(corpusRoot, { recursive: true, force: true });
  execFileSync(
    pythonExecutable,
    [path.resolve(repositoryRoot, "compat", "corpus", "build.py"), corpusRoot],
    { stdio: "inherit" }
  );
}
