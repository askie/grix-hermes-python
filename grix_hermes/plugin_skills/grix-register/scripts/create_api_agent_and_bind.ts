#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

interface Flags {
  accessToken: string;
  agentName: string;
  avatarUrl: string;
  baseUrl: string;
  agentJsonFile: string;
  profileName: string;
  profileMode: "create" | "reuse" | "create-or-reuse";
  isMain: string;
  cloneFrom: string;
  installDir: string;
  accountId: string;
  allowedUsers: string;
  allowAllUsers: string;
  homeChannel: string;
  homeChannelName: string;
  hermes: string;
  node: string;
  inheritKeys: string;
  dryRun: boolean;
  json: boolean;
}

function parseArgs(argv: string[]): Flags {
  const flags: Flags = {
    accessToken: "",
    agentName: "",
    avatarUrl: "",
    baseUrl: "https://grix.dhf.pub",
    agentJsonFile: "",
    profileName: "",
    profileMode: "create-or-reuse",
    isMain: "",
    cloneFrom: "",
    installDir: "",
    accountId: "",
    allowedUsers: "",
    allowAllUsers: "",
    homeChannel: "",
    homeChannelName: "",
    hermes: "hermes",
    node: "node",
    inheritKeys: "",
    dryRun: false,
    json: false,
  };
  const take = (i: number): [string, number] => {
    const next = argv[i + 1];
    if (next === undefined) throw new Error(`Missing value after ${argv[i]}`);
    return [next, i + 1];
  };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i]!;
    if (token === "--access-token") { const [v, j] = take(i); flags.accessToken = v; i = j; continue; }
    if (token === "--agent-name") { const [v, j] = take(i); flags.agentName = v; i = j; continue; }
    if (token === "--avatar-url") { const [v, j] = take(i); flags.avatarUrl = v; i = j; continue; }
    if (token === "--base-url") { const [v, j] = take(i); flags.baseUrl = v; i = j; continue; }
    if (token === "--agent-json-file") { const [v, j] = take(i); flags.agentJsonFile = v; i = j; continue; }
    if (token === "--profile-name") { const [v, j] = take(i); flags.profileName = v; i = j; continue; }
    if (token === "--profile-mode") {
      const [v, j] = take(i);
      if (v !== "create" && v !== "reuse" && v !== "create-or-reuse") {
        throw new Error(`Invalid --profile-mode: ${v}`);
      }
      flags.profileMode = v;
      i = j;
      continue;
    }
    if (token === "--is-main") {
      const [v, j] = take(i);
      if (v !== "" && v !== "true" && v !== "false") throw new Error(`Invalid --is-main: ${v}`);
      flags.isMain = v;
      i = j;
      continue;
    }
    if (token === "--clone-from") { const [v, j] = take(i); flags.cloneFrom = v; i = j; continue; }
    if (token === "--install-dir") { const [v, j] = take(i); flags.installDir = v; i = j; continue; }
    if (token === "--account-id") { const [v, j] = take(i); flags.accountId = v; i = j; continue; }
    if (token === "--allowed-users") { const [v, j] = take(i); flags.allowedUsers = v; i = j; continue; }
    if (token === "--allow-all-users") {
      const [v, j] = take(i);
      if (v !== "" && v !== "true" && v !== "false") throw new Error(`Invalid --allow-all-users: ${v}`);
      flags.allowAllUsers = v;
      i = j;
      continue;
    }
    if (token === "--home-channel") { const [v, j] = take(i); flags.homeChannel = v; i = j; continue; }
    if (token === "--home-channel-name") { const [v, j] = take(i); flags.homeChannelName = v; i = j; continue; }
    if (token === "--hermes") { const [v, j] = take(i); flags.hermes = v; i = j; continue; }
    if (token === "--node") { const [v, j] = take(i); flags.node = v; i = j; continue; }
    if (token === "--inherit-keys") { const [v, j] = take(i); flags.inheritKeys = v; i = j; continue; }
    if (token === "--dry-run") { flags.dryRun = true; continue; }
    if (token === "--json") { flags.json = true; continue; }
  }
  return flags;
}

function loadOrCreatePayload(flags: Flags, authScript: string): Record<string, unknown> {
  if (flags.agentJsonFile) {
    return JSON.parse(fs.readFileSync(flags.agentJsonFile, "utf8")) as Record<string, unknown>;
  }
  const requestedIsMain = flags.isMain || "true";
  const cmd = [
    authScript,
    "--base-url",
    flags.baseUrl,
    "create-api-agent",
    "--access-token",
    flags.accessToken,
    "--agent-name",
    flags.agentName,
    "--is-main",
    requestedIsMain,
  ];
  if (flags.avatarUrl) cmd.push("--avatar-url", flags.avatarUrl);
  const result = spawnSync(flags.node, cmd, { encoding: "utf8" });
  if ((result.status ?? -1) !== 0) {
    throw new Error(((result.stderr || result.stdout) || "").trim());
  }
  return JSON.parse(result.stdout || "{}") as Record<string, unknown>;
}

function maskSecrets(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => maskSecrets(item));
  if (!value || typeof value !== "object") return value;
  const masked: Record<string, unknown> = {};
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, "");
    masked[key] = normalized === "apikey" || normalized.endsWith("apikey")
      ? (String(child ?? "").trim() ? "ak_***" : "")
      : maskSecrets(child);
  }
  return masked;
}

function main(): number {
  let flags: Flags;
  try {
    flags = parseArgs(process.argv.slice(2));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`${message}\n`);
    return 1;
  }
  try {
    if (!flags.agentJsonFile && (!flags.accessToken || !flags.agentName)) {
      throw new Error("Need --agent-json-file or both --access-token and --agent-name.");
    }
    const here = path.dirname(fileURLToPath(import.meta.url));
    const authScript = path.join(here, "grix_auth.js");
    const bindScript = path.resolve(here, "..", "..", "grix-egg", "scripts", "bind_local.js");

    const createdPayload = loadOrCreatePayload(flags, authScript);

    const cmd = [
      bindScript,
      "--from-json",
      "-",
      "--profile-mode",
      flags.profileMode,
      "--hermes",
      flags.hermes,
      "--node",
      flags.node,
    ];
    const bindIsMain = flags.isMain || (flags.agentJsonFile ? "" : "true");
    if (bindIsMain) cmd.push("--is-main", bindIsMain);
    if (flags.profileName) cmd.push("--profile-name", flags.profileName);
    if (flags.cloneFrom) cmd.push("--clone-from", flags.cloneFrom);
    if (flags.installDir) cmd.push("--install-dir", flags.installDir);
    if (flags.accountId) cmd.push("--account-id", flags.accountId);
    if (flags.allowedUsers) cmd.push("--allowed-users", flags.allowedUsers);
    if (flags.allowAllUsers) cmd.push("--allow-all-users", flags.allowAllUsers);
    if (flags.homeChannel) cmd.push("--home-channel", flags.homeChannel);
    if (flags.homeChannelName) cmd.push("--home-channel-name", flags.homeChannelName);
    if (flags.inheritKeys) cmd.push("--inherit-keys", flags.inheritKeys);
    if (flags.dryRun) cmd.push("--dry-run");
    if (flags.json) cmd.push("--json");

    const bindResult = spawnSync(flags.node, cmd, {
      input: JSON.stringify(createdPayload),
      encoding: "utf8",
    });
    if ((bindResult.status ?? -1) !== 0) {
      throw new Error(((bindResult.stderr || bindResult.stdout) || "").trim());
    }

    if (flags.json) {
      const stdout = (bindResult.stdout || "").trim();
      const payload = {
        ok: true,
        created_agent: maskSecrets(createdPayload),
        bind_result: stdout ? JSON.parse(stdout) : null,
      };
      process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
    } else if (bindResult.stdout) {
      process.stdout.write(bindResult.stdout);
    }
    return 0;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (flags.json) {
      process.stderr.write(`${JSON.stringify({ ok: false, error: message }, null, 2)}\n`);
    } else {
      process.stderr.write(`${message}\n`);
    }
    return 1;
  }
}

process.exit(main());
