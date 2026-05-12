#!/usr/bin/env node

const { spawnSync } = require("node:child_process");

const candidates = process.platform === "win32"
  ? ["py", "python", "python3"]
  : ["python3", "python"];

let lastError = null;

function spawnPython(command, args, stdio = "inherit") {
  return spawnSync(command, args, { stdio });
}

function findPython(args = ["--version"], requireSuccess = false) {
  for (const command of candidates) {
    const result = spawnPython(command, args, "ignore");
    if (result.error && result.error.code === "ENOENT") {
      lastError = result.error;
      continue;
    }
    if (!requireSuccess || result.status === 0) {
      return command;
    }
  }
  return null;
}

function findContextKernelPython() {
  return findPython(["-c", "import context_kernel"], true);
}

function bootstrapContextKernel() {
  if (process.env.AKERNEL_SKIP_BOOTSTRAP === "1") {
    return false;
  }
  const command = findPython();
  if (!command) {
    return false;
  }
  const source = process.env.AKERNEL_PIP_SOURCE || "context-kernel";
  console.error(`akernel: Python package not found; installing ${source} with pip...`);
  const install = spawnPython(command, ["-m", "pip", "install", "--user", "--upgrade", source], "inherit");
  return install.status === 0;
}

let pythonCommand = findContextKernelPython();
if (!pythonCommand && bootstrapContextKernel()) {
  pythonCommand = findContextKernelPython();
}

if (!pythonCommand) {
  console.error("akernel: unable to install the context-kernel Python package.");
  if (lastError) {
    console.error(String(lastError.message || lastError));
  }
  console.error("Set AKERNEL_PIP_SOURCE to a package or git URL, or install manually with:");
  console.error("  python -m pip install --user context-kernel");
  process.exit(1);
}

const result = spawnPython(pythonCommand, ["-m", "context_kernel.cli", ...process.argv.slice(2)], "inherit");
process.exit(result.status === null ? 1 : result.status);
