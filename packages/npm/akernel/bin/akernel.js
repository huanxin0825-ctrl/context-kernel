#!/usr/bin/env node

const { spawnSync } = require("node:child_process");

const candidates = process.platform === "win32"
  ? [["py", ["-m", "context_kernel.cli"]], ["python", ["-m", "context_kernel.cli"]], ["python3", ["-m", "context_kernel.cli"]]]
  : [["python3", ["-m", "context_kernel.cli"]], ["python", ["-m", "context_kernel.cli"]]];

let lastError = null;
for (const [command, baseArgs] of candidates) {
  const result = spawnSync(command, [...baseArgs, ...process.argv.slice(2)], { stdio: "inherit" });
  if (result.error && result.error.code === "ENOENT") {
    lastError = result.error;
    continue;
  }
  process.exit(result.status === null ? 1 : result.status);
}

console.error("akernel: Python launcher not found.");
if (lastError) {
  console.error(String(lastError.message || lastError));
}
console.error("Install Python 3.10+ and the context-kernel package, then retry.");
process.exit(1);
