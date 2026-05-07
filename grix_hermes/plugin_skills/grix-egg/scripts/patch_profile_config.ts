#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import YAML from "yaml";

const RESTRICTED_MANAGEMENT_SKILLS = [
  "grix-admin",
  "grix-register",
  "grix-update",
  "grix-egg",
];

type ManagementPolicy = "preserve" | "main" | "restricted";

interface Flags {
  config?: string;
  externalDirs: string[];
  managementPolicy?: string;
  channelGrixWsUrl?: string;
  dryRun?: boolean;
  json?: boolean;
}

function parseArgs(argv: string[]): Flags {
  const flags: Flags = { externalDirs: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--config" && argv[index + 1]) {
      flags.config = argv[index + 1];
      index += 1;
      continue;
    }
    if (token === "--external-dir" && argv[index + 1]) {
      flags.externalDirs.push(argv[index + 1]!);
      index += 1;
      continue;
    }
    if (token === "--management-policy" && argv[index + 1]) {
      flags.managementPolicy = argv[index + 1];
      index += 1;
      continue;
    }
    if (token === "--channel-grix-ws-url" && argv[index + 1]) {
      flags.channelGrixWsUrl = argv[index + 1];
      index += 1;
      continue;
    }
    if (token === "--dry-run") {
      flags.dryRun = true;
      continue;
    }
    if (token === "--json") {
      flags.json = true;
      continue;
    }
  }
  return flags;
}

function normalizeExternalDirs(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((entry) => String(entry ?? "").trim()).filter(Boolean);
  }
  if (typeof value === "string" && value.trim()) {
    return [value.trim()];
  }
  return [];
}

function normalizeSkillList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((entry) => String(entry ?? "").trim()).filter(Boolean);
  }
  if (typeof value === "string" && value.trim()) {
    return value.split(",").map((entry) => entry.trim()).filter(Boolean);
  }
  return [];
}

function looksLikeGrixHermesRoot(candidate: string): boolean {
  return (
    fs.existsSync(path.join(candidate, "bin", "grix-hermes.js")) &&
    fs.existsSync(path.join(candidate, "lib", "manifest.js")) &&
    fs.existsSync(path.join(candidate, "grix-admin", "SKILL.md"))
  );
}

function isManagedGrixHermesPath(candidate: string): boolean {
  const base = path.basename(candidate);
  return base.startsWith("grix-hermes") || looksLikeGrixHermesRoot(candidate);
}

function applyManagementPolicy(
  currentDisabledSkills: string[],
  policy: ManagementPolicy,
): string[] {
  if (policy === "preserve") {
    return [...currentDisabledSkills];
  }
  const nextDisabledSkills = currentDisabledSkills.filter(
    (skillName) => !RESTRICTED_MANAGEMENT_SKILLS.includes(skillName),
  );
  if (policy === "restricted") {
    for (const skillName of RESTRICTED_MANAGEMENT_SKILLS) {
      if (!nextDisabledSkills.includes(skillName)) {
        nextDisabledSkills.push(skillName);
      }
    }
  }
  return nextDisabledSkills;
}

const flags = parseArgs(process.argv.slice(2));

if (!flags.config) {
  process.stderr.write(
    `${JSON.stringify({ ok: false, error: "Missing --config." }, null, 2)}\n`,
  );
  process.exit(1);
}

const configPath = path.resolve(flags.config);
const configDir = path.dirname(configPath);
const requestedExternalDirs = flags.externalDirs
  .map((entry) => path.resolve(entry))
  .filter(Boolean);

let config: Record<string, unknown> = {};
if (fs.existsSync(configPath)) {
  const raw = fs.readFileSync(configPath, "utf8");
  if (raw.trim()) {
    const parsed = YAML.parse(raw);
    if (parsed && typeof parsed === "object") {
      config = parsed as Record<string, unknown>;
    }
  }
}

const skills = config.skills;
const skillsObj =
  skills && typeof skills === "object" && !Array.isArray(skills)
    ? (skills as Record<string, unknown>)
    : {};
config.skills = skillsObj;

const managementPolicyRaw = String(flags.managementPolicy || "preserve").trim() || "preserve";
if (!["preserve", "main", "restricted"].includes(managementPolicyRaw)) {
  process.stderr.write(
    `${JSON.stringify(
      { ok: false, error: `Unsupported --management-policy: ${managementPolicyRaw}` },
      null,
      2,
    )}\n`,
  );
  process.exit(1);
}
const managementPolicy = managementPolicyRaw as ManagementPolicy;

const currentExternalDirs = normalizeExternalDirs(skillsObj.external_dirs);
const currentDisabledSkills = normalizeSkillList(skillsObj.disabled);
const requestedSet = new Set(requestedExternalDirs);
const nextExternalDirs = currentExternalDirs.filter((entry) => {
  const resolved = path.resolve(entry);
  if (!isManagedGrixHermesPath(resolved)) return true;
  return requestedSet.has(resolved);
});
for (const externalDir of requestedExternalDirs) {
  if (!nextExternalDirs.includes(externalDir)) {
    nextExternalDirs.push(externalDir);
  }
}
const nextDisabledSkills = applyManagementPolicy(currentDisabledSkills, managementPolicy);
skillsObj.external_dirs = nextExternalDirs;
skillsObj.disabled = nextDisabledSkills;

const requestedGrixWsUrl = String(flags.channelGrixWsUrl || "").trim();
if (requestedGrixWsUrl) {
  const channels =
    config.channels && typeof config.channels === "object" && !Array.isArray(config.channels)
      ? (config.channels as Record<string, unknown>)
      : {};
  config.channels = channels;
  const grix =
    channels.grix && typeof channels.grix === "object" && !Array.isArray(channels.grix)
      ? (channels.grix as Record<string, unknown>)
      : {};
  grix.wsUrl = requestedGrixWsUrl;
  channels.grix = grix;
}

const payload = {
  ok: true,
  dry_run: Boolean(flags.dryRun),
  changed:
    JSON.stringify(currentExternalDirs) !== JSON.stringify(nextExternalDirs) ||
    JSON.stringify(currentDisabledSkills) !== JSON.stringify(nextDisabledSkills),
  config_path: configPath,
  external_dirs: nextExternalDirs,
  management_policy: managementPolicy,
  disabled_skills: nextDisabledSkills,
};

if (!flags.dryRun) {
  fs.mkdirSync(configDir, { recursive: true });
  fs.writeFileSync(configPath, YAML.stringify(config), "utf8");
}

if (flags.json) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
} else {
  process.stdout.write(`${configPath}\n`);
}
