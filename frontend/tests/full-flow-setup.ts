import { spawn } from "node:child_process";
import path from "node:path";

export const backendPort = 31848;

export async function startFullFlowBackend(origin: string, scenario: "facts" | "workspace" = "facts") {
  const root = path.resolve(__dirname, "../..");
  const child = spawn("uv", [
    "--cache-dir", path.join(root, ".cache/uv"), "--directory", path.join(root, "backend"),
    "run", "--no-sync", "python", "tests/support/browser_server.py",
    "--port", String(backendPort), "--origin", origin,
    "--scenario", scenario,
  ], { cwd: root, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
  let output = "";
  let spawnError: Error | undefined;
  child.on("error", error => { spawnError = error; });
  child.stdin.on("error", () => {}); // A failed child may close stdin before teardown.
  child.stdout.on("data", chunk => { output += chunk.toString(); });
  child.stderr.on("data", chunk => { output += chunk.toString(); });
  const exited = new Promise<number | null>(resolve => child.once("close", resolve));
  const cleanup = async () => {
    if (child.exitCode === null && !child.killed) child.stdin.end("stop\n");
    const timeout = setTimeout(() => child.kill(), 45_000);
    try {
      const code = await exited;
      if (code !== 0 || !output.includes('"acceptance_cleaned": true')) {
        throw new Error(`Acceptance helper cleanup failed (owned PID ${child.pid}, code ${code}): ${output}`);
      }
    } finally { clearTimeout(timeout); }
  };
  try {
    const deadline = Date.now() + 60_000;
    while (!output.includes('"acceptance_ready": true')) {
      if (spawnError) throw spawnError;
      if (child.exitCode !== null) throw new Error(`Acceptance helper failed: ${output}`);
      if (Date.now() > deadline) throw new Error(`Acceptance helper readiness timed out: ${output}`);
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    const ready = await fetch(`http://127.0.0.1:${backendPort}/health/ready`, { signal: AbortSignal.timeout(5_000) });
    if (!ready.ok) throw new Error(`Acceptance readiness returned ${ready.status}`);
    return cleanup;
  } catch (error) {
    await cleanup().catch(cleanupError => { throw new AggregateError([error, cleanupError], "Acceptance setup and cleanup failed"); });
    throw error;
  }
}
