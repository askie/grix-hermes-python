#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import YAML from "yaml";

type ProfileMode = "create" | "reuse" | "create-or-reuse";

interface Flags {
  agentName: string;
  agentId: string;
  apiEndpoint: string;
  apiKey: string;
  profileName: string;
  profileMode: ProfileMode;
  isMain: string;
  cloneFrom: string;
  installDir: string;
  accountId: string;
  allowedUsers: string;
  allowAllUsers: string;
  homeChannel: string;
  homeChannelName: string;
  hermesHome: string;
  hermes: string;
  node: string;
  dryRun: boolean;
  json: boolean;
  fromJson: string;
  inheritKeys: string;
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

function ensureDir(dirPath: string): void {
  fs.mkdirSync(dirPath, { recursive: true, mode: 0o700 });
  try {
    fs.chmodSync(dirPath, 0o700);
  } catch {
    // Best-effort on filesystems that do not support POSIX permissions.
  }
}

function writePrivateFile(filePath: string, contents: string): void {
  fs.writeFileSync(filePath, contents, { encoding: "utf8", mode: 0o600 });
  try {
    fs.chmodSync(filePath, 0o600);
  } catch {
    // Best-effort on filesystems that do not support POSIX permissions.
  }
}

function ensureBlankProfileState(profileDir: string): void {
  ensureDir(profileDir);
  ensureDir(path.join(profileDir, "memories"));
  writePrivateFile(path.join(profileDir, "SOUL.md"), "");
  writePrivateFile(path.join(profileDir, "memories", "USER.md"), "");
  writePrivateFile(path.join(profileDir, "memories", "MEMORY.md"), "");
}

function resolveHermesHome(explicit: string): string {
  const raw = cleanText(explicit) || cleanText(process.env.HERMES_HOME) || "~/.hermes";
  return path.resolve(expandHome(raw));
}

function resolveProfileRoot(hermesHome: string): string {
  let current = path.resolve(hermesHome);
  while (path.basename(path.dirname(current)) === "profiles") {
    current = path.dirname(path.dirname(current));
  }
  return current;
}

function resolveDefaultInstallDir(hermesHome: string): string {
  return path.join(hermesHome, "skills", "grix-hermes");
}

function resolveProfileDir(hermesHome: string, profileName: string): string {
  const normalized = cleanText(profileName);
  if (!normalized || normalized === "default") return hermesHome;
  return path.resolve(path.join(hermesHome, "profiles", normalized));
}

function resolveInstallDir(hermesHome: string, rawInstallDir: string): string {
  const raw = cleanText(rawInstallDir);
  return raw ? path.resolve(expandHome(raw)) : resolveDefaultInstallDir(hermesHome);
}

function isGrixBundleDir(candidate: string): boolean {
  const requiredEntries = [
    path.join(candidate, "bin", "grix-hermes.js"),
    path.join(candidate, "lib", "manifest.js"),
    path.join(candidate, "grix-admin", "SKILL.md"),
  ];
  const sharedCliCandidates = [
    path.join(candidate, "shared", "cli", "skill-wrapper.js"),
    path.join(candidate, "shared", "cli", "grix-hermes.js"),
  ];
  return (
    requiredEntries.every((entry) => fs.existsSync(entry)) &&
    sharedCliCandidates.some((entry) => fs.existsSync(entry))
  );
}

function validateInstallDir(installDir: string): void {
  if (!fs.existsSync(installDir)) {
    throw new Error(
      `grix-hermes install dir does not exist: ${installDir}. ` +
        "Run `npx -y @dhf-hermes/grix install` first or pass --install-dir.",
    );
  }
  if (fs.existsSync(path.join(installDir, ".git")) && !isGrixBundleDir(installDir)) {
    throw new Error(
      `Install dir points to a git checkout without a usable grix-hermes bundle: ${installDir}. ` +
        "Build or install grix-hermes first, or pass a bundle directory.",
    );
  }
  if (!isGrixBundleDir(installDir)) {
    throw new Error(
      `Install dir is not a valid grix-hermes bundle: ${installDir}. ` +
        "Reinstall the published package and retry.",
    );
  }
}

function formatEnvValue(value: string): string {
  if (!value) return "";
  if (/[\s"#]/.test(value)) {
    const escaped = value.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
    return `"${escaped}"`;
  }
  return value;
}

function readEnvLines(envPath: string): string[] {
  if (!fs.existsSync(envPath)) return [];
  return fs.readFileSync(envPath, "utf8").split("\n");
}

interface EnvResult {
  env_path: string;
  changed_keys: string[];
}

function applyEnvChanges(
  envPath: string,
  updates: Record<string, string>,
  removals: Set<string>,
): EnvResult {
  const lines = readEnvLines(envPath);
  const resultLines: string[] = [];
  const changedKeys: string[] = [];
  const seen = new Set<string>();

  for (const rawLine of lines) {
    const stripped = rawLine.trim();
    if (!stripped || stripped.startsWith("#") || !stripped.includes("=")) {
      resultLines.push(rawLine);
      continue;
    }
    const key = stripped.split("=")[0]!.trim();
    if (removals.has(key)) {
      changedKeys.push(key);
      continue;
    }
    if (key in updates) {
      resultLines.push(`${key}=${formatEnvValue(updates[key]!)}`);
      changedKeys.push(key);
      seen.add(key);
      continue;
    }
    resultLines.push(rawLine);
  }

  for (const [key, value] of Object.entries(updates)) {
    if (seen.has(key)) continue;
    resultLines.push(`${key}=${formatEnvValue(value)}`);
    changedKeys.push(key);
  }

  ensureDir(path.dirname(envPath));
  writePrivateFile(envPath, `${resultLines.join("\n").trimEnd()}\n`);
  return {
    env_path: envPath,
    changed_keys: [...new Set(changedKeys)].sort(),
  };
}

function maskPlanForOutput(plan: Plan): Plan {
  return {
    ...plan,
    env_updates: {
      ...plan.env_updates,
      GRIX_API_KEY: plan.env_updates.GRIX_API_KEY ? "ak_***" : "",
    },
  };
}

function parseOptionalBool(value: unknown): boolean | null {
  const normalized = cleanText(value).toLowerCase();
  if (normalized === "true") return true;
  if (normalized === "false") return false;
  return null;
}

function cleanBoolText(value: unknown): string {
  if (typeof value === "boolean") return value ? "true" : "false";
  const normalized = cleanText(value).toLowerCase();
  if (normalized === "true" || normalized === "false") return normalized;
  return "";
}

function resolveManagementPolicy(profileExists: boolean, isMain: boolean | null): string {
  if (isMain === true) return "main";
  if (isMain === false) return "restricted";
  return profileExists ? "preserve" : "restricted";
}

const LLM_KEY_PATTERN = /^(?:.*_)?(?:API_KEY|BASE_URL|MODEL|URL)$/;
const LLM_CONFIG_KEYS = [
  "model",
  "custom_providers",
  "providers",
  "fallback_providers",
  "smart_model_routing",
  "auxiliary",
] as const;

function readYamlObject(configPath: string): Record<string, unknown> {
  if (!fs.existsSync(configPath)) return {};
  const raw = fs.readFileSync(configPath, "utf8").trim();
  if (!raw) return {};
  const parsed = YAML.parse(raw);
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? (parsed as Record<string, unknown>)
    : {};
}

function hasMeaningfulConfigValue(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === "string") return value.trim().length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value as Record<string, unknown>).length > 0;
  return true;
}

function inheritLlmConfig(
  hermesHome: string,
  targetConfigPath: string,
  sourceProfileName: string | null,
): string[] {
  const candidates: string[] = [];
  if (sourceProfileName && sourceProfileName !== "default") {
    candidates.push(path.join(hermesHome, "profiles", sourceProfileName, "config.yaml"));
  }
  candidates.push(path.join(hermesHome, "config.yaml"));

  let sourceConfig: Record<string, unknown> = {};
  for (const candidate of candidates) {
    if (!fs.existsSync(candidate)) continue;
    sourceConfig = readYamlObject(candidate);
    if (Object.keys(sourceConfig).length > 0) break;
  }
  if (Object.keys(sourceConfig).length === 0) return [];

  const targetConfig = readYamlObject(targetConfigPath);
  const inheritedKeys: string[] = [];
  for (const key of LLM_CONFIG_KEYS) {
    const sourceValue = sourceConfig[key];
    if (!hasMeaningfulConfigValue(sourceValue)) continue;
    if (hasMeaningfulConfigValue(targetConfig[key])) continue;
    targetConfig[key] = sourceValue;
    inheritedKeys.push(key);
  }
  if (inheritedKeys.length === 0) return [];

  ensureDir(path.dirname(targetConfigPath));
  fs.writeFileSync(targetConfigPath, YAML.stringify(targetConfig), "utf8");
  return inheritedKeys;
}

function inheritLlmKeys(hermesHome: string, targetEnvPath: string, sourceProfileName: string | null): string[] {
  const candidates: string[] = [];
  if (sourceProfileName && sourceProfileName !== "default") {
    candidates.push(path.join(hermesHome, "profiles", sourceProfileName, ".env"));
  }
  candidates.push(path.join(hermesHome, ".env"));

  let sourceLines: string[] = [];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      sourceLines = readEnvLines(candidate);
      break;
    }
  }
  if (sourceLines.length === 0) return [];

  const llmEntries: Record<string, string> = {};
  for (const line of sourceLines) {
    const stripped = line.trim();
    if (!stripped || stripped.startsWith("#") || !stripped.includes("=")) continue;
    const eq = stripped.indexOf("=");
    const key = stripped.slice(0, eq).trim();
    const value = stripped.slice(eq + 1).trim();
    if (LLM_KEY_PATTERN.test(key) && !key.startsWith("GRIX_") && value && !value.includes("***")) {
      llmEntries[key] = value;
    }
  }

  if (Object.keys(llmEntries).length === 0) return [];
  const result = applyEnvChanges(targetEnvPath, llmEntries, new Set());
  return result.changed_keys;
}

// --- --from-json extraction logic (merged from bind_from_json.py) ---

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function firstPresent(...values: unknown[]): unknown {
  for (const value of values) {
    if (value !== undefined && value !== null) return value;
  }
  return null;
}

interface BindFields {
  profile_name: string;
  agent_name: string;
  agent_id: string;
  api_endpoint: string;
  api_key: string;
  is_main: string;
}

function extractBindFields(payload: Record<string, unknown>): BindFields {
  const handoff = asRecord(payload.handoff);
  const bindHermes = asRecord(handoff.bind_hermes);
  if (Object.keys(bindHermes).length > 0) {
    return {
      profile_name: cleanText(bindHermes.profile_name),
      agent_name: cleanText(bindHermes.agent_name),
      agent_id: cleanText(bindHermes.agent_id),
      api_endpoint: cleanText(bindHermes.api_endpoint),
      api_key: cleanText(bindHermes.api_key),
      is_main: cleanBoolText(bindHermes.is_main),
    };
  }

  const bindLocal = asRecord(handoff.bind_local);
  if (Object.keys(bindLocal).length > 0) {
    return {
      profile_name: cleanText(bindLocal.profile_name || bindLocal.agent_name),
      agent_name: cleanText(bindLocal.agent_name),
      agent_id: cleanText(bindLocal.agent_id),
      api_endpoint: cleanText(bindLocal.api_endpoint),
      api_key: cleanText(bindLocal.api_key),
      is_main: cleanBoolText(bindLocal.is_main),
    };
  }

  const createdAgent = asRecord(payload.createdAgent);
  if (Object.keys(createdAgent).length > 0) {
    return {
      profile_name: cleanText(
        createdAgent.profile_name || createdAgent.agent_name || createdAgent.name,
      ),
      agent_name: cleanText(createdAgent.agent_name || createdAgent.name),
      agent_id: cleanText(createdAgent.id || createdAgent.agent_id),
      api_endpoint: cleanText(createdAgent.api_endpoint || payload.api_endpoint),
      api_key: cleanText(createdAgent.api_key || payload.api_key),
      is_main: cleanBoolText(
        firstPresent(
          createdAgent.is_main,
          payload.requestedIsMain,
          payload.requested_is_main,
          payload.is_main,
        ),
      ),
    };
  }

  return {
    profile_name: cleanText(payload.profile_name || payload.agent_name || payload.name),
    agent_name: cleanText(payload.agent_name || payload.name),
    agent_id: cleanText(payload.agent_id || payload.id),
    api_endpoint: cleanText(payload.api_endpoint),
    api_key: cleanText(payload.api_key),
    is_main: cleanBoolText(payload.is_main),
  };
}

// --- Plan & execution ---

interface Plan {
  profile_name: string;
  profile_dir: string;
  profile_exists: boolean;
  profile_mode: ProfileMode;
  agent_name: string;
  agent_id: string;
  is_main: boolean | null;
  management_policy: string;
  api_endpoint: string;
  install_dir: string;
  env_path: string;
  config_path: string;
  env_updates: Record<string, string>;
  env_removals: string[];
  commands: string[][];
}

function buildPlan(flags: Flags): Plan {
  const runtimeHermesHome = resolveHermesHome(flags.hermesHome);
  const profileRoot = resolveProfileRoot(runtimeHermesHome);
  const profileName = cleanText(flags.profileName) || flags.agentName;
  const profileDir = resolveProfileDir(profileRoot, profileName);
  const profileExists = fs.existsSync(profileDir);
  const isMain = parseOptionalBool(flags.isMain);
  const managementPolicy = resolveManagementPolicy(profileExists, isMain);
  const installDir = resolveInstallDir(runtimeHermesHome, flags.installDir);

  if (profileExists && flags.profileMode === "create") {
    throw new Error(`Hermes profile already exists: ${profileName}`);
  }
  if (!profileExists && flags.profileMode === "reuse") {
    throw new Error(`Hermes profile does not exist: ${profileName}`);
  }
  if (!flags.dryRun) validateInstallDir(installDir);

  const createCmd: string[] | null = !profileExists
    ? [flags.hermes, "profile", "create", profileName]
    : null;
  if (createCmd && cleanText(flags.cloneFrom)) {
    createCmd.push("--clone-from", cleanText(flags.cloneFrom));
  }

  const envUpdates: Record<string, string> = {
    GRIX_ENDPOINT: flags.apiEndpoint,
    GRIX_AGENT_ID: flags.agentId,
    GRIX_API_KEY: flags.apiKey,
  };
  const envRemovals = new Set<string>();

  const accountId = cleanText(flags.accountId);
  if (accountId) envUpdates.GRIX_ACCOUNT_ID = accountId;

  const allowedUsers = cleanText(flags.allowedUsers);
  const allowAllUsers = cleanText(flags.allowAllUsers).toLowerCase();
  if (allowedUsers) {
    envUpdates.GRIX_ALLOWED_USERS = allowedUsers;
    envRemovals.add("GRIX_ALLOW_ALL_USERS");
  } else if (allowAllUsers === "true") {
    envUpdates.GRIX_ALLOW_ALL_USERS = "true";
    envRemovals.add("GRIX_ALLOWED_USERS");
  } else if (allowAllUsers === "false") {
    envRemovals.add("GRIX_ALLOW_ALL_USERS");
  }

  const homeChannel = cleanText(flags.homeChannel);
  const homeChannelName = cleanText(flags.homeChannelName);
  if (homeChannel) envUpdates.GRIX_HOME_CHANNEL = homeChannel;
  if (homeChannelName) envUpdates.GRIX_HOME_CHANNEL_NAME = homeChannelName;

  const configPath = path.join(profileDir, "config.yaml");
  const envPath = path.join(profileDir, ".env");
  const grixWsUrl = cleanText(flags.apiEndpoint);

  const here = path.dirname(fileURLToPath(import.meta.url));
  const patchScript = path.join(here, "patch_profile_config.js");

  const commands: string[][] = [];
  if (createCmd) commands.push(createCmd);
  commands.push([
    flags.node,
    patchScript,
    "--config",
    configPath,
    "--external-dir",
    installDir,
    "--management-policy",
    managementPolicy,
    "--channel-grix-ws-url",
    grixWsUrl,
    "--json",
  ]);

  return {
    profile_name: profileName,
    profile_dir: profileDir,
    profile_exists: profileExists,
    profile_mode: flags.profileMode,
    agent_name: flags.agentName,
    agent_id: flags.agentId,
    is_main: isMain,
    management_policy: managementPolicy,
    api_endpoint: flags.apiEndpoint,
    install_dir: installDir,
    env_path: envPath,
    config_path: configPath,
    env_updates: envUpdates,
    env_removals: [...envRemovals].sort(),
    commands,
  };
}

function parseArgs(argv: string[]): Flags {
  const flags: Flags = {
    agentName: "",
    agentId: "",
    apiEndpoint: "",
    apiKey: "",
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
    hermesHome: "",
    hermes: "hermes",
    node: "node",
    dryRun: false,
    json: false,
    fromJson: "",
    inheritKeys: "",
  };
  const take = (i: number): [string, number] => {
    const next = argv[i + 1];
    if (next === undefined) throw new Error(`Missing value after ${argv[i]}`);
    return [next, i + 1];
  };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i]!;
    if (token === "--agent-name") { const [v, j] = take(i); flags.agentName = v; i = j; continue; }
    if (token === "--agent-id") { const [v, j] = take(i); flags.agentId = v; i = j; continue; }
    if (token === "--api-endpoint") { const [v, j] = take(i); flags.apiEndpoint = v; i = j; continue; }
    if (token === "--api-key") { const [v, j] = take(i); flags.apiKey = v; i = j; continue; }
    if (token === "--profile-name") { const [v, j] = take(i); flags.profileName = v; i = j; continue; }
    if (token === "--profile-mode") {
      const [v, j] = take(i);
      if (v !== "create" && v !== "reuse" && v !== "create-or-reuse") {
        throw new Error(`Invalid --profile-mode: ${v}`);
      }
      flags.profileMode = v as ProfileMode;
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
    if (token === "--hermes-home") { const [v, j] = take(i); flags.hermesHome = v; i = j; continue; }
    if (token === "--hermes") { const [v, j] = take(i); flags.hermes = v; i = j; continue; }
    if (token === "--node") { const [v, j] = take(i); flags.node = v; i = j; continue; }
    if (token === "--from-json") { const [v, j] = take(i); flags.fromJson = v; i = j; continue; }
    if (token === "--dry-run") { flags.dryRun = true; continue; }
    if (token === "--inherit-keys") {
      const [v, j] = take(i);
      flags.inheritKeys = v;
      i = j;
      continue;
    }
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
    process.stderr.write(`${message}\n`);
    return 1;
  }

  try {
    // --from-json mode: extract bind fields from JSON, then populate flags
    if (flags.fromJson) {
      let raw: string;
      if (flags.fromJson === "-") {
        raw = fs.readFileSync(0, "utf8").trim();
      } else {
        raw = fs.readFileSync(flags.fromJson, "utf8").trim();
      }
      if (!raw) throw new Error("No JSON input provided.");
      const payload = JSON.parse(raw) as Record<string, unknown>;
      const bindFields = extractBindFields(payload);
      if (!bindFields.profile_name) bindFields.profile_name = bindFields.agent_name;
      const required: (keyof BindFields)[] = [
        "profile_name",
        "agent_name",
        "agent_id",
        "api_endpoint",
        "api_key",
      ];
      const missing = required.filter((key) => !bindFields[key]);
      if (missing.length > 0) {
        throw new Error(`Missing bind-local fields: ${missing.join(", ")}`);
      }
      // Merge: CLI flags take precedence over JSON-extracted fields
      if (!flags.agentName) flags.agentName = bindFields.agent_name;
      if (!flags.agentId) flags.agentId = bindFields.agent_id;
      if (!flags.apiEndpoint) flags.apiEndpoint = bindFields.api_endpoint;
      if (!flags.apiKey) flags.apiKey = bindFields.api_key;
      if (!flags.profileName) flags.profileName = bindFields.profile_name;
      if (!flags.isMain) flags.isMain = bindFields.is_main;
    }

    if (!flags.agentName || !flags.agentId || !flags.apiEndpoint || !flags.apiKey) {
      throw new Error(
        "Required: --agent-name, --agent-id, --api-endpoint, --api-key (or use --from-json).",
      );
    }

    if (/\.{3}/.test(flags.apiKey) && /^ak_\d+\.\.\.[A-Za-z]+$/.test(flags.apiKey)) {
      throw new Error(
        "GRIX_API_KEY appears to be a masked value. Use grix-egg --route existing with a plaintext bind JSON file, or rotate the key before binding.",
      );
    }
    if (!/^ak_\d+_[A-Za-z0-9]+$/.test(flags.apiKey)) {
      process.stderr.write(
        `[grix-hermes] Warning: GRIX_API_KEY does not match expected format (ak_<digits>_<alphanumeric>). Proceeding anyway.\n`,
      );
    }

    const runtimeHermesHome = resolveHermesHome(flags.hermesHome);
    const profileRoot = resolveProfileRoot(runtimeHermesHome);
    const plan = buildPlan(flags);
    const commandEnv: NodeJS.ProcessEnv = {
      ...process.env,
      HERMES_HOME: profileRoot,
    };
    let createdProfile = false;
    let envResult: EnvResult | null = null;
    let configResult: Record<string, unknown> | null = null;
    const commandResults: Array<{ cmd: string[]; stdout: string; stderr: string }> = [];

    if (!flags.dryRun) {
      for (const cmd of plan.commands) {
        const [bin, ...rest] = cmd;
        if (!bin) throw new Error("Empty command");
        const result = spawnSync(bin, rest, { encoding: "utf8", env: commandEnv });
        const code = result.status ?? -1;
        const stdout = (result.stdout || "").trim();
        const stderr = (result.stderr || "").trim();
        if (code !== 0) {
          throw new Error(stderr || stdout || `command failed: ${cmd.join(" ")}`);
        }
        commandResults.push({ cmd, stdout, stderr });
        if (cmd[0] === flags.hermes) {
          createdProfile = true;
          ensureBlankProfileState(plan.profile_dir);
        } else if (stdout) {
          configResult = JSON.parse(stdout) as Record<string, unknown>;
        }
      }

      envResult = applyEnvChanges(plan.env_path, plan.env_updates, new Set(plan.env_removals));

      const inheritSource = cleanText(flags.inheritKeys);
      if (inheritSource) {
        const inheritProfileName = inheritSource === "true" || inheritSource === "global"
          ? null
          : inheritSource;
        const inheritSourceLabel = inheritProfileName || "global";
        const inheritedConfigKeys = inheritLlmConfig(
          runtimeHermesHome,
          plan.config_path,
          inheritProfileName,
        );
        if (inheritedConfigKeys.length > 0) {
          commandResults.push({
            cmd: ["inherit-llm-config", "--source", inheritSourceLabel],
            stdout: `inherited: ${inheritedConfigKeys.join(", ")}`,
            stderr: "",
          });
        }

        const inheritedKeys = inheritLlmKeys(
          runtimeHermesHome,
          plan.env_path,
          inheritProfileName,
        );
        if (inheritedKeys.length > 0) {
          commandResults.push({
            cmd: ["inherit-llm-keys", "--source", inheritSourceLabel],
            stdout: `inherited: ${inheritedKeys.join(", ")}`,
            stderr: "",
          });
        }
      }
    }

    const payload = {
      ok: true,
      dry_run: flags.dryRun,
      created_profile: createdProfile,
      env_result: envResult,
      config_result: configResult,
      command_results: commandResults,
      ...maskPlanForOutput(plan),
    };
    if (flags.json) {
      process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
    } else {
      process.stdout.write(
        `profile=${plan.profile_name} agent=${plan.agent_name} dry_run=${flags.dryRun}\n`,
      );
      for (const cmd of plan.commands) {
        process.stdout.write(`$ ${cmd.join(" ")}\n`);
      }
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
