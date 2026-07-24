// @vitest-environment node

import { spawn, ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtemp, mkdir, rm, symlink, writeFile } from "node:fs/promises";
import { get } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";


type HttpResult = {
  status: number;
  headers: Record<string, string | string[] | undefined>;
  body: string;
};

function request(port: number, requestPath: string): Promise<HttpResult> {
  return new Promise((resolve, reject) => {
    get(
      { hostname: "127.0.0.1", port, path: requestPath },
      (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("end", () => {
          resolve({
            status: response.statusCode ?? 0,
            headers: response.headers,
            body: Buffer.concat(chunks).toString("utf-8")
          });
        });
      }
    ).on("error", reject);
  });
}

describe("production frontend server", () => {
  let root = "";
  let server: ChildProcessWithoutNullStreams;
  let port = 0;

  beforeAll(async () => {
    root = await mkdtemp(path.join(tmpdir(), "p2h-frontend-"));
    await mkdir(path.join(root, "assets"));
    await writeFile(path.join(root, "index.html"), "<main>workspace</main>");
    await writeFile(path.join(root, "assets/app.js"), "console.log('ok');");
    if (process.platform !== "win32") {
      await symlink("/etc/passwd", path.join(root, "outside.txt"));
    }
    const script = fileURLToPath(
      new URL("../../scripts/serve-frontend.mjs", import.meta.url)
    );
    server = spawn(process.execPath, [script], {
      env: {
        ...process.env,
        P2H_FRONTEND_HOST: "127.0.0.1",
        P2H_FRONTEND_PORT: "0",
        P2H_FRONTEND_ROOT: root
      }
    });
    port = await new Promise<number>((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("frontend server timeout")), 5000);
      server.once("error", reject);
      server.stdout.on("data", (chunk: Buffer) => {
        const match = chunk.toString("utf-8").match(/Frontend: .*:(\d+)/);
        if (!match) return;
        clearTimeout(timeout);
        resolve(Number(match[1]));
      });
    });
  });

  afterAll(async () => {
    server?.kill("SIGTERM");
    await rm(root, { recursive: true, force: true });
  });

  it("serves assets with security headers and SPA fallback", async () => {
    const asset = await request(port, "/assets/app.js");
    const route = await request(port, "/jobs/current");

    expect(asset.status).toBe(200);
    expect(asset.headers["cache-control"]).toContain("immutable");
    expect(asset.headers["content-security-policy"]).toContain("default-src 'self'");
    expect(asset.headers["strict-transport-security"]).toBeUndefined();
    expect(route.status).toBe(200);
    expect(route.body).toContain("workspace");
  });

  it("rejects traversal, malformed paths, and escaping symlinks", async () => {
    expect((await request(port, "/..%2f..%2fetc/passwd")).status).toBe(403);
    expect((await request(port, "/%zz")).status).toBe(400);
    if (process.platform !== "win32") {
      expect((await request(port, "/outside.txt")).status).toBe(404);
    }
  });
});
