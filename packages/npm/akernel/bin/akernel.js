#!/usr/bin/env node

const { spawnSync } = require("node:child_process");
const launcherVersion = require("../package.json").version;

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

function parseVersion(version) {
  return String(version || "")
    .trim()
    .split(".")
    .map((part) => Number.parseInt(part, 10))
    .map((part) => (Number.isFinite(part) ? part : 0));
}

function versionAtLeast(actual, required) {
  const left = parseVersion(actual);
  const right = parseVersion(required);
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const a = left[index] || 0;
    const b = right[index] || 0;
    if (a > b) return true;
    if (a < b) return false;
  }
  return true;
}

function runtimeVersion(command) {
  const result = spawnSync(
    command,
    ["-c", "import context_kernel; print(context_kernel.__version__)"],
    { encoding: "utf8" },
  );
  if (result.status !== 0) {
    return null;
  }
  return String(result.stdout || "").trim();
}

function findContextKernelPython(requireCurrent = true) {
  for (const command of candidates) {
    const version = runtimeVersion(command);
    if (!version) {
      continue;
    }
    if (!requireCurrent || versionAtLeast(version, launcherVersion)) {
      return command;
    }
    lastError = new Error(`runtime ${version} is older than launcher ${launcherVersion}`);
  }
  return null;
}

function bootstrapContextKernel() {
  if (process.env.AKERNEL_SKIP_BOOTSTRAP === "1") {
    return false;
  }
  const command = findPython();
  if (!command) {
    return false;
  }
  const source = process.env.AKERNEL_PIP_SOURCE || `akernel-runtime>=${launcherVersion}`;
  console.error(`akernel: installing/upgrading Python runtime ${source} with pip...`);
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
  console.error("  python -m pip install --user akernel-runtime");
  process.exit(1);
}

const result = spawnPython(pythonCommand, ["-m", "context_kernel.cli", ...process.argv.slice(2)], "inherit");
process.exit(result.status === null ? 1 : result.status);
