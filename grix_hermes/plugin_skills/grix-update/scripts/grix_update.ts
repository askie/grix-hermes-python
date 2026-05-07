#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

interface Flags {
  installDir: string;
  npm: string;
  node: string;
  dryRun: boolean;
  json: boolean;
}

interface CommandResult {
  cmd: string[];
  code: number;
  stdout: string;
  stderr: string;
}

function cleanText(value: unknown): string {
  return String(value ?? "").trim();
}

function expandHome(value: string): string {
  if (!value) return value;
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
}

function defaultInstallDir(): string {
  const hermesHome = path.resolve(expandHome(cleanText(process.env.HERMES_HOME) || "~/.hermes"));
  return path.join(hermesHome, "skills", "grix-hermes");
}

function runCommand(cmd: string[], check = true): CommandResult {
  const [bin, ...rest] = cmd;
  if (!bin) throw new Error("runCommand received empty cmd");
  const result = spawnSync(bin, rest, { encoding: "utf8" });
  const payload: CommandResult = {
    cmd,
    code: result.status ?? -1,
    stdout: (result.stdout || "").trim(),
    stderr: (result.stderr || "").trim(),
  };
  if (check && payload.code !== 0) {
    throw new Error(payload.stderr || payload.stdout || `command failed: ${cmd.join(" ")}`);
  }
  return payload;
}

function resolveGlobalPackageDir(npmBin: string): string {
  const result = runCommand([npmBin, "root", "-g"]);
  return path.join(result.stdout, "@dhf-hermes", "grix");
}

function readPkgVersion(dir: string): string {
  const pkgPath = path.join(dir, "package.json");
  if (!fs.existsSync(pkgPath)) return "";
  const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8")) as { version?: string };
  return cleanText(pkg.version);
}

function parseArgs(argv: string[]): Flags {
  const flags: Flags = {
    installDir: "",
    npm: "npm",
    node: "node",
    dryRun: false,
    json: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    const next = argv[i + 1];
    if (token === "--install-dir" && next !== undefined) { flags.installDir = next; i += 1; continue; }
    if (token === "--npm" && next !== undefined) { flags.npm = next; i += 1; continue; }
    if (token === "--node" && next !== undefined) { flags.node = next; i += 1; continue; }
    if (token === "--dry-run") { flags.dryRun = true; continue; }
    if (token === "--json") { flags.json = true; continue; }
  }
  return flags;
}

function main(): number {
  let flags: Flags;
  try {
    flags = parseArgs(process.argv.slice(2));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`${JSON.stringify({ ok: false, error: message })}\n`);
    return 1;
  }

  try {
    const installDir = path.resolve(expandHome(cleanText(flags.installDir) || defaultInstallDir()));

    // Resolve global package dir to get current version before update.
    let globalDir = "";
    try {
      globalDir = resolveGlobalPackageDir(flags.npm);
    } catch {
      // npm root -g failed; will discover after update.
    }
    const versionBefore = globalDir ? readPkgVersion(globalDir) : "";

    const updateCmd = [flags.npm, "update", "-g", "@dhf-hermes/grix"];
    const rootCmd = [flags.npm, "root", "-g"];

    if (flags.dryRun) {
      const payload = {
        ok: true,
        dry_run: true,
        install_dir: installDir,
        version_before: versionBefore || "unknown",
        commands: [updateCmd, rootCmd],
      };
      if (flags.json) {
        process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
      } else {
        process.stdout.write(`dry_run=true install_dir=${installDir}\n`);
        process.stdout.write(`$ ${updateCmd.join(" ")}\n`);
        process.stdout.write(`$ ${rootCmd.join(" ")}\n`);
      }
      return 0;
    }

    // Step 1: npm update -g
    runCommand(updateCmd);

    // Step 2: npm root -g
    globalDir = resolveGlobalPackageDir(flags.npm);

    const versionAfter = readPkgVersion(globalDir);

    // Step 3: reinstall to target dir
    const installCmd = [
      flags.node,
      path.join(globalDir, "bin", "grix-hermes.js"),
      "install",
      "--dest",
      installDir,
      "--force",
    ];
    runCommand(installCmd);

    const payload = {
      ok: true,
      dry_run: false,
      install_dir: installDir,
      version_before: versionBefore || "unknown",
      version_after: versionAfter,
      global_dir: globalDir,
    };
    if (flags.json) {
      process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
    } else {
      process.stdout.write(
        `updated ${versionBefore || "unknown"} -> ${versionAfter} install_dir=${installDir}\n`,
      );
    }
    return 0;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const payload = { ok: false, error: message };
    if (flags.json) {
      process.stderr.write(`${JSON.stringify(payload, null, 2)}\n`);
    } else {
      process.stderr.write(`${message}\n`);
    }
    return 1;
  }
}

process.exit(main());
