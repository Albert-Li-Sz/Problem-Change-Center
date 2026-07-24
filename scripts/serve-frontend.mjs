#!/usr/bin/env node

import { createReadStream } from "node:fs";
import { realpath, stat } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(
  process.env.P2H_FRONTEND_ROOT || path.join(scriptDirectory, "../frontend/dist")
);
const frontendRootReal = await realpath(frontendRoot);
const host = process.env.P2H_FRONTEND_HOST || "0.0.0.0";
const port = Number.parseInt(process.env.P2H_FRONTEND_PORT || "11452", 10);

const contentTypes = new Map([
  [".css", "text/css; charset=utf-8"],
  [".gif", "image/gif"],
  [".html", "text/html; charset=utf-8"],
  [".ico", "image/x-icon"],
  [".jpeg", "image/jpeg"],
  [".jpg", "image/jpeg"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".map", "application/json; charset=utf-8"],
  [".png", "image/png"],
  [".svg", "image/svg+xml"],
  [".txt", "text/plain; charset=utf-8"],
  [".webp", "image/webp"],
  [".woff", "font/woff"],
  [".woff2", "font/woff2"]
]);

function applySecurityHeaders(response) {
  response.setHeader("Content-Security-Policy", [
    "default-src 'self'",
    "base-uri 'none'",
    "connect-src 'self' http:",
    "font-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "img-src 'self' data:",
    "object-src 'none'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'"
  ].join("; "));
  response.setHeader("Cross-Origin-Resource-Policy", "same-origin");
  response.setHeader("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()");
  response.setHeader("Referrer-Policy", "no-referrer");
  response.setHeader("X-Content-Type-Options", "nosniff");
  response.setHeader("X-Frame-Options", "DENY");
}

function sendText(response, statusCode, message) {
  response.statusCode = statusCode;
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("Content-Type", "text/plain; charset=utf-8");
  response.end(`${message}\n`);
}

function resolveRequestPath(requestUrl) {
  const parsed = new URL(requestUrl || "/", "http://localhost");
  const decoded = decodeURIComponent(parsed.pathname).replaceAll("\\", "/");
  if (decoded.includes("\0")) throw new URIError("NUL is not allowed");
  const candidate = path.resolve(frontendRoot, `.${decoded}`);
  if (candidate !== frontendRoot && !candidate.startsWith(`${frontendRoot}${path.sep}`)) {
    return null;
  }
  return candidate;
}

async function regularFile(candidate) {
  try {
    const resolved = await realpath(candidate);
    if (
      resolved !== frontendRootReal &&
      !resolved.startsWith(`${frontendRootReal}${path.sep}`)
    ) {
      return null;
    }
    const details = await stat(resolved);
    if (details.isFile()) return resolved;
    if (details.isDirectory()) {
      const indexPath = await realpath(path.join(resolved, "index.html"));
      if (
        indexPath.startsWith(`${frontendRootReal}${path.sep}`) &&
        (await stat(indexPath)).isFile()
      ) {
        return indexPath;
      }
    }
  } catch {
    return null;
  }
  return null;
}

async function handleRequest(request, response) {
  applySecurityHeaders(response);
  if (request.method !== "GET" && request.method !== "HEAD") {
    response.setHeader("Allow", "GET, HEAD");
    sendText(response, 405, "Method Not Allowed");
    return;
  }

  let candidate;
  try {
    candidate = resolveRequestPath(request.url);
  } catch {
    sendText(response, 400, "Bad Request");
    return;
  }
  if (candidate === null) {
    sendText(response, 403, "Forbidden");
    return;
  }

  let filePath = await regularFile(candidate);
  if (!filePath && !path.extname(candidate)) {
    filePath = await regularFile(path.join(frontendRoot, "index.html"));
  }
  if (!filePath) {
    sendText(response, 404, "Not Found");
    return;
  }

  response.statusCode = 200;
  response.setHeader(
    "Cache-Control",
    filePath.includes(`${path.sep}assets${path.sep}`)
      ? "public, max-age=31536000, immutable"
      : "no-cache"
  );
  response.setHeader(
    "Content-Type",
    contentTypes.get(path.extname(filePath).toLowerCase()) || "application/octet-stream"
  );
  if (request.method === "HEAD") {
    response.end();
    return;
  }
  createReadStream(filePath)
    .on("error", () => {
      if (!response.headersSent) sendText(response, 500, "Internal Server Error");
      else response.destroy();
    })
    .pipe(response);
}

if (!Number.isInteger(port) || port < 0 || port > 65535) {
  throw new Error("P2H_FRONTEND_PORT must be a valid TCP port");
}

const server = createServer((request, response) => {
  void handleRequest(request, response);
});

server.listen(port, host, () => {
  const address = server.address();
  const listeningPort = typeof address === "object" && address ? address.port : port;
  console.log(`Frontend: http://${host}:${listeningPort}`);
});
