#!/usr/bin/env node
// Fleet Ops Console — static server for the mock target site.
//
// Zero dependencies on purpose: `node server.js` just works, no npm install,
// which also sidesteps the corp TLS interception that broke package pulls.
//
//   node server.js            # http://localhost:5055
//   PORT=8080 node server.js
//
// Binding 0.0.0.0 matters: WSL is a separate net namespace, so 127.0.0.1-only
// would be unreachable from Chrome on Windows.

import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PORT = Number(process.env.PORT || 5055);
const HOST = process.env.HOST || "0.0.0.0";
const ROOT = path.dirname(fileURLToPath(import.meta.url));

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const started = Date.now();
  res.on("finish", () =>
    console.log(
      `${req.method} ${url.pathname} -> ${res.statusCode} (${Date.now() - started}ms)`,
    ),
  );

  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { "content-type": "text/plain" });
    return res.end("method not allowed");
  }

  if (url.pathname === "/healthz") {
    res.writeHead(200, { "content-type": "application/json" });
    return res.end('{"ok":true}');
  }

  const rel = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/+/, "");
  // Contain to ROOT — reject anything escaping via ../ or an absolute path.
  const file = path.resolve(ROOT, rel);
  if (file !== ROOT && !file.startsWith(ROOT + path.sep)) {
    res.writeHead(403, { "content-type": "text/plain" });
    return res.end("forbidden");
  }

  try {
    const body = await fs.readFile(file);
    res.writeHead(200, {
      "content-type": TYPES[path.extname(file)] || "application/octet-stream",
      "content-length": body.length,
      // No caching, so an edit to index.html shows up on a plain refresh
      // instead of the agent reading a stale board.
      "cache-control": "no-store",
    });
    res.end(body);
  } catch {
    res.writeHead(404, { "content-type": "text/plain" });
    res.end("not found: " + url.pathname);
  }
});

server.listen(PORT, HOST, () => {
  console.log(`Fleet Ops Console  ->  http://localhost:${PORT}`);
});

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    console.log(`\n${sig} - shutting down`);
    server.close(() => process.exit(0));
  });
}
