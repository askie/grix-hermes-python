#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomBytes } from "node:crypto";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";
import {
  formatWsCredentialDiagnostics,
  getWsCredentialDiagnostics,
  hasWsCredentials,
} from "../../shared/cli/config.js";
import { buildConversationCard, buildEggStatusCard } from "../../shared/cli/card-links.js";
import type { InstallContext } from "../../shared/types/install-context.js";

// ---------------------------------------------------------------------------
// Section 1: Utility functions
// ---------------------------------------------------------------------------

function cleanText(value: unknown): string {
  return String(value ?? "").trim();
}

function expandHome(value: string): string {
  if (!value) return value;
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
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

function resolveProfileDir(hermesHome: string, profileName: string): string {
  const normalized = cleanText(profileName);
  if (!normalized || normalized === "default") return hermesHome;
  return path.resolve(path.join(hermesHome, "profiles", normalized));
}

function projectRoot(): string {
  const here = path.dirname(fileURLToPath(import.meta.url));
  return path.resolve(here, "..", "..");
}

function defaultInstallDir(hermesHome: string): string {
  return path.join(hermesHome, "skills", "grix-hermes");
}

function isoNow(): string {
  return new Date().toISOString();
}

const PROFILE_NAME_PATTERN = /^(default|[a-z0-9][a-z0-9_-]{0,63})$/;

function generateInstallId(): string {
  return `egg-${randomBytes(4).toString("hex")}`;
}

function isValidProfileName(profileName: string): boolean {
  return PROFILE_NAME_PATTERN.test(profileName);
}

function slugifyProfileCandidate(value: string): string {
  return cleanText(value)
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+/, "")
    .replace(/-+$/, "")
    .slice(0, 64);
}

function resolveProfileNameSuffix(value: string): string {
  const normalized = slugifyProfileCandidate(value);
  return normalized === "default" ? "" : normalized;
}

function buildProfileNameWithSuffix(profileNameSuffix: string): string {
  const suffix = resolveProfileNameSuffix(profileNameSuffix);
  if (!suffix) return "";
  const base = "egg";
  const maxSuffixLength = Math.max(0, 64 - base.length - 1);
  const trimmedSuffix = suffix.slice(0, maxSuffixLength).replace(/-+$/, "");
  return trimmedSuffix ? `${base}-${trimmedSuffix}` : base;
}

function resolveBootstrapProfileName(
  explicitProfileName: string,
  agentName: string,
  profileNameSuffix: string,
): string {
  const requested = cleanText(explicitProfileName);
  if (requested) return requested;

  const requestedAgentName = cleanText(agentName);
  if (isValidProfileName(requestedAgentName)) return requestedAgentName;

  const requestedProfileSuffix = cleanText(profileNameSuffix);
  if (requestedProfileSuffix) {
    return buildProfileNameWithSuffix(requestedProfileSuffix);
  }
  return "";
}

function validateProfileNameSuffix(profileNameSuffix: string): void {
  const requested = cleanText(profileNameSuffix);
  if (!requested) return;
  const normalized = resolveProfileNameSuffix(requested);
  if (!normalized) {
    throw new Error(
      "Invalid --profile-name-suffix: must contain at least one ASCII letter or number after normalization.",
    );
  }
}

// ---------------------------------------------------------------------------
// Section 2: Types
// ---------------------------------------------------------------------------

const STEP_NAMES = ["detect", "install", "create", "bind", "soul", "gateway", "accept"] as const;
type StepName = (typeof STEP_NAMES)[number];
type CreatePath = "host" | "http" | "existing" | "";
type InteractionStatus = "full" | "degraded" | "none";
type DeliveryTargetSource =
  | "status_target"
  | "home_channel"
  | "install_context.current_chat_session_id"
  | "install_context.session_id"
  | "none";
type DeliveryMessageKind =
  | "running_card"
  | "acceptance_card"
  | "final_text"
  | "final_card"
  | "failure_text"
  | "failure_card";

function setStatePath(state: StateFile, pathUsed: CreatePath): void {
  state.path = pathUsed;
}

interface StepCreateResult {
  credentials: RuntimeCredentials | null;
  pathUsed: CreatePath;
}

type StepStatus = "pending" | "done" | "failed" | "skipped";

interface StepState {
  status: StepStatus;
  at: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
}

interface DeliveryAttemptState {
  kind: DeliveryMessageKind;
  at: string;
  ok: boolean;
  target: string;
  target_source: DeliveryTargetSource;
  message_preview: string;
  ack: Record<string, unknown> | null;
  error: string | null;
}

interface DeliveryState {
  target: string;
  target_source: DeliveryTargetSource;
  attempts: DeliveryAttemptState[];
}

interface ResolvedDeliveryTarget {
  target: string;
  source: DeliveryTargetSource;
}

interface StateFile {
  version: 2;
  install_id: string;
  agent_name: string;
  profile_name: string;
  route: string;
  path: CreatePath;
  started_at: string;
  updated_at: string;
  completed_at: string | null;
  interaction_status: InteractionStatus;
  delivery: DeliveryState;
  steps: Record<StepName, StepState>;
}

interface Flags {
  installId: string;
  installContext: string;
  agentName: string;
  profileNameSuffix: string;
  statusTarget: string;
  soulContent: string;
  soulFile: string;
  accessToken: string;
  account: string;
  email: string;
  password: string;
  baseUrl: string;
  avatarUrl: string;
  categoryName: string;
  isMain: string;
  route: string;
  profileName: string;
  installDir: string;
  hermesHome: string;
  hermes: string;
  node: string;
  probeMessage: string;
  expectedSubstring: string;
  memberIds: string;
  memberTypes: string;
  agentId: string;
  apiEndpoint: string;
  apiKey: string;
  bindJson: string;
  accountId: string;
  allowedUsers: string;
  allowAllUsers: string;
  homeChannel: string;
  homeChannelName: string;
  acceptTimeoutSeconds: string;
  acceptPollIntervalSeconds: string;
  resume: boolean;
  dryRun: boolean;
  json: boolean;
}

interface CommandResult {
  code: number;
  stdout: string;
  stderr: string;
}

interface RuntimeCredentials {
  agentId: string;
  agentName: string;
  apiEndpoint: string;
  apiKey: string;
  profileName: string;
}

class BootstrapError extends Error {
  step: StepName;
  stepNumber: number;
  suggestion: string;
  rawError: string;
  constructor(step: StepName, stepNumber: number, reason: string, suggestion: string, rawError: string) {
    super(reason);
    this.name = "BootstrapError";
    this.step = step;
    this.stepNumber = stepNumber;
    this.suggestion = suggestion;
    this.rawError = rawError;
  }
}

// ---------------------------------------------------------------------------
// Section 3: Command execution
// ---------------------------------------------------------------------------

function runCommand(
  cmd: string[],
  options?: { env?: NodeJS.ProcessEnv; cwd?: string; inputText?: string; check?: boolean },
): CommandResult {
  const [bin, ...rest] = cmd;
  if (!bin) throw new Error("runCommand received empty cmd");
  const result = spawnSync(bin, rest, {
    encoding: "utf8",
    env: options?.env,
    cwd: options?.cwd,
    input: options?.inputText,
  });
  const output: CommandResult = {
    code: result.status ?? -1,
    stdout: (result.stdout || "").trim(),
    stderr: (result.stderr || "").trim(),
  };
  if (options?.check !== false && output.code !== 0) {
    throw new Error(output.stderr || output.stdout || `command failed: ${cmd.join(" ")}`);
  }
  return output;
}

function parseJsonOutput(result: CommandResult): Record<string, unknown> {
  const raw = cleanText(result.stdout);
  if (!raw) return {};
  return JSON.parse(raw) as Record<string, unknown>;
}

function appendTextFlag(cmd: string[], flag: string, value: unknown): void {
  const text = cleanText(value);
  if (text) cmd.push(flag, text);
}

function cleanList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => cleanText(item)).filter(Boolean);
  return cleanText(value).split(",").map((item) => item.trim()).filter(Boolean);
}

function truncateText(value: unknown, maxLength = 160): string {
  const normalized = cleanText(value).replace(/\s+/g, " ");
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, Math.max(0, maxLength - 3)).trimEnd()}...`;
}

let stdinCache: string | null = null;

function readStdinText(): string {
  if (stdinCache === null) {
    stdinCache = fs.readFileSync(0, "utf8");
  }
  return stdinCache;
}

function ensurePrivateDir(dirPath: string): void {
  fs.mkdirSync(dirPath, { recursive: true, mode: 0o700 });
  try {
    fs.chmodSync(dirPath, 0o700);
  } catch {
    // Best-effort on filesystems that do not support POSIX permissions.
  }
}

function writePrivateFileAtomic(filePath: string, contents: string): void {
  const tmpPath = filePath + ".tmp";
  fs.writeFileSync(tmpPath, contents, { encoding: "utf8", mode: 0o600 });
  try {
    fs.chmodSync(tmpPath, 0o600);
  } catch {
    // Best-effort on filesystems that do not support POSIX permissions.
  }
  fs.renameSync(tmpPath, filePath);
  try {
    fs.chmodSync(filePath, 0o600);
  } catch {
    // Best-effort on filesystems that do not support POSIX permissions.
  }
}

function redactStateValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => redactStateValue(item));
  if (!value || typeof value !== "object") return value;

  const result: Record<string, unknown> = {};
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, "");
    if (normalized === "apikey" || normalized.endsWith("apikey")) {
      result[key] = cleanText(child) ? "ak_***" : "";
      continue;
    }
    result[key] = redactStateValue(child);
  }
  return result;
}

function extractInstallContextMainAgent(installContext: InstallContext | null): Record<string, unknown> {
  const mainAgent = installContext?.main_agent;
  return mainAgent && typeof mainAgent === "object" && !Array.isArray(mainAgent)
    ? (mainAgent as Record<string, unknown>)
    : {};
}

function loadInstallContext(rawSource: string): InstallContext | null {
  const source = cleanText(rawSource);
  if (!source) return null;
  const text = source === "-"
    ? readStdinText()
    : fs.readFileSync(expandHome(source), "utf8");
  const normalized = text.trim();
  if (!normalized) {
    throw new Error(`Install context is empty: ${source}`);
  }
  return JSON.parse(normalized) as InstallContext;
}

function resolveHomeChannelName(flags: Flags, installContext: InstallContext | null): string {
  const explicit = cleanText(flags.homeChannelName);
  if (explicit) return explicit;
  return cleanText(extractInstallContextMainAgent(installContext).home_channel_name);
}

function resolveHomeChannel(flags: Flags, installContext: InstallContext | null): string {
  const explicit = cleanText(flags.homeChannel);
  if (explicit) return explicit;
  const currentChatSessionId = cleanText(installContext?.current_chat_session_id);
  if (currentChatSessionId) return currentChatSessionId;
  return cleanText(installContext?.session_id);
}

function resolveDeliveryTarget(flags: Flags, installContext: InstallContext | null): ResolvedDeliveryTarget {
  const statusTarget = cleanText(flags.statusTarget);
  if (statusTarget) return { target: statusTarget, source: "status_target" };

  const homeChannel = cleanText(flags.homeChannel);
  if (homeChannel) return { target: homeChannel, source: "home_channel" };

  const currentChatSessionId = cleanText(installContext?.current_chat_session_id);
  if (currentChatSessionId) {
    return { target: currentChatSessionId, source: "install_context.current_chat_session_id" };
  }

  const sessionId = cleanText(installContext?.session_id);
  if (sessionId) {
    return { target: sessionId, source: "install_context.session_id" };
  }

  return { target: "", source: "none" };
}

// ---------------------------------------------------------------------------
// Section 4: State file management
// ---------------------------------------------------------------------------

function getStateFilePath(hermesHome: string, installId: string): string {
  return path.join(hermesHome, "tmp", `grix-egg-${installId}.json`);
}

function emptyStepState(): StepState {
  return { status: "pending", at: null, result: null, error: null };
}

function buildEmptySteps(): Record<StepName, StepState> {
  const steps: Record<StepName, StepState> = {} as Record<StepName, StepState>;
  for (const name of STEP_NAMES) steps[name] = emptyStepState();
  return steps;
}

function emptyDeliveryState(): DeliveryState {
  return {
    target: "",
    target_source: "none",
    attempts: [],
  };
}

function normalizeInteractionStatus(value: unknown): InteractionStatus {
  const normalized = cleanText(value);
  if (normalized === "full" || normalized === "degraded" || normalized === "none") {
    return normalized;
  }
  return "none";
}

function normalizeCreatePath(value: unknown): CreatePath {
  const normalized = cleanText(value);
  if (normalized === "host" || normalized === "http" || normalized === "existing") {
    return normalized;
  }
  return "";
}

function normalizeDeliveryTargetSource(value: unknown): DeliveryTargetSource {
  const normalized = cleanText(value);
  if (
    normalized === "status_target" ||
    normalized === "home_channel" ||
    normalized === "install_context.current_chat_session_id" ||
    normalized === "install_context.session_id" ||
    normalized === "none"
  ) {
    return normalized;
  }
  return "none";
}

function refreshInteractionStatus(state: StateFile): void {
  const target = cleanText(state.delivery.target);
  if (!target) {
    state.interaction_status = "none";
    return;
  }
  if (state.delivery.attempts.some((attempt) => !attempt.ok)) {
    state.interaction_status = "degraded";
    return;
  }
  const hasSuccessFinal = ["final_text", "final_card"].every((kind) =>
    state.delivery.attempts.some((attempt) => attempt.kind === kind && attempt.ok),
  );
  const hasFailureFinal = ["failure_text", "failure_card"].every((kind) =>
    state.delivery.attempts.some((attempt) => attempt.kind === kind && attempt.ok),
  );
  state.interaction_status = hasSuccessFinal || hasFailureFinal ? "full" : "none";
}

function setResolvedDeliveryTarget(state: StateFile, target: ResolvedDeliveryTarget): void {
  state.delivery.target = target.target;
  state.delivery.target_source = target.source;
  refreshInteractionStatus(state);
}

function normalizeDeliveryState(raw: unknown): DeliveryState {
  const fallback = emptyDeliveryState();
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return fallback;

  const record = raw as Record<string, unknown>;
  const attempts = Array.isArray(record.attempts)
    ? record.attempts.flatMap((item) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) return [];
        const attempt = item as Record<string, unknown>;
        const kind = cleanText(attempt.kind) as DeliveryMessageKind;
        const allowedKinds: DeliveryMessageKind[] = [
          "running_card",
          "acceptance_card",
          "final_text",
          "final_card",
          "failure_text",
          "failure_card",
        ];
        if (!allowedKinds.includes(kind)) return [];
        return [{
          kind,
          at: cleanText(attempt.at) || isoNow(),
          ok: Boolean(attempt.ok),
          target: cleanText(attempt.target),
          target_source: normalizeDeliveryTargetSource(attempt.target_source),
          message_preview: truncateText(attempt.message_preview),
          ack: attempt.ack && typeof attempt.ack === "object" && !Array.isArray(attempt.ack)
            ? (attempt.ack as Record<string, unknown>)
            : null,
          error: cleanText(attempt.error) || null,
        } satisfies DeliveryAttemptState];
      })
    : [];

  return {
    target: cleanText(record.target),
    target_source: normalizeDeliveryTargetSource(record.target_source),
    attempts,
  };
}

function makeFreshState(flags: Flags): StateFile {
  return {
    version: 2,
    install_id: flags.installId,
    agent_name: flags.agentName,
    profile_name: flags.profileName || flags.agentName,
    route: flags.route,
    path: "",
    started_at: isoNow(),
    updated_at: isoNow(),
    completed_at: null,
    interaction_status: "none",
    delivery: emptyDeliveryState(),
    steps: buildEmptySteps(),
  };
}

function loadState(filePath: string): StateFile | null {
  if (!fs.existsSync(filePath)) return null;
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const steps = buildEmptySteps();
    const rawSteps = parsed.steps && typeof parsed.steps === "object" && !Array.isArray(parsed.steps)
      ? (parsed.steps as Record<string, unknown>)
      : {};
    for (const name of STEP_NAMES) {
      const rawStep = rawSteps[name];
      if (!rawStep || typeof rawStep !== "object" || Array.isArray(rawStep)) continue;
      const step = rawStep as Record<string, unknown>;
      const status = cleanText(step.status);
      steps[name] = {
        status: status === "done" || status === "failed" || status === "skipped" ? status : "pending",
        at: cleanText(step.at) || null,
        result: step.result && typeof step.result === "object" && !Array.isArray(step.result)
          ? (step.result as Record<string, unknown>)
          : null,
        error: cleanText(step.error) || null,
      };
    }

    const state: StateFile = {
      version: 2,
      install_id: cleanText(parsed.install_id),
      agent_name: cleanText(parsed.agent_name),
      profile_name: cleanText(parsed.profile_name),
      route: cleanText(parsed.route),
      path: normalizeCreatePath(parsed.path),
      started_at: cleanText(parsed.started_at) || isoNow(),
      updated_at: cleanText(parsed.updated_at) || isoNow(),
      completed_at: cleanText(parsed.completed_at) || null,
      interaction_status: normalizeInteractionStatus(parsed.interaction_status),
      delivery: normalizeDeliveryState(parsed.delivery),
      steps,
    };
    refreshInteractionStatus(state);
    return state;
  } catch {
    return null;
  }
}

function saveState(filePath: string, state: StateFile): void {
  const dir = path.dirname(filePath);
  ensurePrivateDir(dir);
  state.updated_at = isoNow();
  writePrivateFileAtomic(filePath, JSON.stringify(redactStateValue(state), null, 2));
}

function markStepDone(state: StateFile, step: StepName, result: Record<string, unknown>): void {
  state.steps[step] = { status: "done", at: isoNow(), result, error: null };
}

function markStepFailed(state: StateFile, step: StepName, error: string): void {
  state.steps[step] = { status: "failed", at: isoNow(), result: null, error };
}

function markStepSkipped(state: StateFile, step: StepName): void {
  state.steps[step] = { status: "skipped", at: isoNow(), result: null, error: null };
}

function stepIsDone(state: StateFile, step: StepName): boolean {
  return state.steps[step]?.status === "done";
}

// ---------------------------------------------------------------------------
// Section 5: Argument parsing
// ---------------------------------------------------------------------------

function parseArgs(argv: string[]): Flags {
  const flags: Flags = {
    installId: "",
    installContext: "",
    agentName: "",
    profileNameSuffix: "",
    statusTarget: "",
    soulContent: "",
    soulFile: "",
    accessToken: "",
    account: "",
    email: "",
    password: "",
    baseUrl: "",
    avatarUrl: "",
    categoryName: "",
    isMain: "true",
    route: "create_new",
    profileName: "",
    installDir: "",
    hermesHome: "",
    hermes: "hermes",
    node: "node",
    probeMessage: "probe",
    expectedSubstring: "",
    memberIds: "",
    memberTypes: "",
    agentId: "",
    apiEndpoint: "",
    apiKey: "",
    bindJson: "",
    accountId: "",
    allowedUsers: "",
    allowAllUsers: "",
    homeChannel: "",
    homeChannelName: "",
    acceptTimeoutSeconds: "15",
    acceptPollIntervalSeconds: "1",
    resume: false,
    dryRun: false,
    json: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i]!;
    const next = argv[i + 1];
    if (token === "--install-id" && next !== undefined) { flags.installId = next; i += 1; continue; }
    if (token === "--install-context" && next !== undefined) { flags.installContext = next; i += 1; continue; }
    if (token === "--agent-name" && next !== undefined) { flags.agentName = next; i += 1; continue; }
    if (token === "--profile-name-suffix" && next !== undefined) { flags.profileNameSuffix = next; i += 1; continue; }
    if (token === "--status-target" && next !== undefined) { flags.statusTarget = next; i += 1; continue; }
    if (token === "--soul-content" && next !== undefined) { flags.soulContent = next; i += 1; continue; }
    if (token === "--soul-file" && next !== undefined) { flags.soulFile = next; i += 1; continue; }
    if (token === "--access-token" && next !== undefined) { flags.accessToken = next; i += 1; continue; }
    if (token === "--account" && next !== undefined) { flags.account = next; i += 1; continue; }
    if (token === "--email" && next !== undefined) { flags.email = next; i += 1; continue; }
    if (token === "--password" && next !== undefined) { flags.password = next; i += 1; continue; }
    if (token === "--base-url" && next !== undefined) { flags.baseUrl = next; i += 1; continue; }
    if (token === "--avatar-url" && next !== undefined) { flags.avatarUrl = next; i += 1; continue; }
    if (token === "--category-name" && next !== undefined) { flags.categoryName = next; i += 1; continue; }
    if (token === "--is-main" && next !== undefined) { flags.isMain = next; i += 1; continue; }
    if (token === "--route" && next !== undefined) { flags.route = next; i += 1; continue; }
    if (token === "--profile-name" && next !== undefined) { flags.profileName = next; i += 1; continue; }
    if (token === "--install-dir" && next !== undefined) { flags.installDir = next; i += 1; continue; }
    if (token === "--hermes-home" && next !== undefined) { flags.hermesHome = next; i += 1; continue; }
    if (token === "--hermes" && next !== undefined) { flags.hermes = next; i += 1; continue; }
    if (token === "--node" && next !== undefined) { flags.node = next; i += 1; continue; }
    if (token === "--probe-message" && next !== undefined) { flags.probeMessage = next; i += 1; continue; }
    if (token === "--expected-substring" && next !== undefined) { flags.expectedSubstring = next; i += 1; continue; }
    if (token === "--member-ids" && next !== undefined) { flags.memberIds = next; i += 1; continue; }
    if (token === "--member-types" && next !== undefined) { flags.memberTypes = next; i += 1; continue; }
    if (token === "--agent-id" && next !== undefined) { flags.agentId = next; i += 1; continue; }
    if (token === "--api-endpoint" && next !== undefined) { flags.apiEndpoint = next; i += 1; continue; }
    if (token === "--api-key" && next !== undefined) { flags.apiKey = next; i += 1; continue; }
    if (token === "--bind-json" && next !== undefined) { flags.bindJson = next; i += 1; continue; }
    if (token === "--account-id" && next !== undefined) { flags.accountId = next; i += 1; continue; }
    if (token === "--allowed-users" && next !== undefined) { flags.allowedUsers = next; i += 1; continue; }
    if (token === "--allow-all-users" && next !== undefined) { flags.allowAllUsers = next; i += 1; continue; }
    if (token === "--home-channel" && next !== undefined) { flags.homeChannel = next; i += 1; continue; }
    if (token === "--home-channel-name" && next !== undefined) { flags.homeChannelName = next; i += 1; continue; }
    if (token === "--accept-timeout-seconds" && next !== undefined) { flags.acceptTimeoutSeconds = next; i += 1; continue; }
    if (token === "--accept-poll-interval-seconds" && next !== undefined) { flags.acceptPollIntervalSeconds = next; i += 1; continue; }
    if (token === "--resume") { flags.resume = true; continue; }
    if (token === "--dry-run") { flags.dryRun = true; continue; }
    if (token === "--json") { flags.json = true; continue; }
  }
  return flags;
}

// ---------------------------------------------------------------------------
// Section 6: Error suggestion mapping
// ---------------------------------------------------------------------------

function suggestForError(step: StepName, errorMessage: string): string {
  const lower = errorMessage.toLowerCase();
  switch (step) {
    case "detect":
      if (lower.includes("no") && lower.includes("access-token")) {
        return "当前宿主 Grix 主路径不可用，且还没有可用于 HTTP fallback 的 access token。请向用户获取邮箱/账号和密码，通过 grix-register 登录换取 token 后再继续；或显式提供已有 agent 凭证走 --route existing。";
      }
      return "检查当前 Hermes 会话是否具备可用的 Grix 宿主能力；若宿主主路径不可用，则向用户获取邮箱/账号和密码，通过 grix-register 登录换取 token 后再走 HTTP fallback，或提供已有 agent 凭证走 --route existing。";
    case "install":
      return "确认 npm 包已安装，或指定正确的 --install-dir。";
    case "create":
      if (lower.includes("already") || lower.includes("已存在") || lower.includes("duplicate")) {
        return "Agent 已存在。使用 --route existing 继续安装，或用不同的 --agent-name 重试。";
      }
      if (lower.includes("401") || lower.includes("unauthorized") || lower.includes("auth")) {
        return "认证失败。检查当前宿主 Grix 会话是否具备可用权限，或确认已有 agent 凭证是否有效。";
      }
      if (lower.includes("unsupported cmd for hermes") || lower.includes("agent_invoke failed")) {
        return "当前运行时没有暴露可复用的宿主 Grix create 能力，也不应退回要求用户提供 access token。请补宿主侧 create bridge，或改走 --route existing。";
      }
      return "创建远端 agent 失败。检查网络连通性和认证凭证。";
    case "bind":
      if (lower.includes("***") || lower.includes("mask") || lower.includes("遮掩")) {
        return "API key 被遮掩。这通常是因为 Hermes 输出过滤。确保 --inherit-keys global 已自动传入。";
      }
      if (lower.includes("profile") && (lower.includes("exist") || lower.includes("已存在"))) {
        return "Profile 已存在。脚本自动使用 create-or-reuse 模式。如需全新创建，先删除旧 profile。";
      }
      return "绑定失败。检查 .env 文件权限和 profile 目录是否可写。";
    case "soul":
      return "SOUL.md 写入失败。检查 profile 目录是否存在且有写权限。";
    case "gateway":
      if (lower.includes("not found") || lower.includes("not available")) {
        return "Hermes CLI 未找到。确保 hermes 在 PATH 中，或用 --hermes 指定完整路径。";
      }
      return "网关启动失败。检查 SOUL.md 和 .env 内容是否正确，查看 Hermes 日志获取详情。";
    case "accept":
      if (lower.includes("timeout") || lower.includes("超时")) {
        return "Agent 未在超时时间内回复预期内容。检查：(1) SOUL.md 内容，(2) 网关是否在线，(3) agent 是否已连接。";
      }
      if (lower.includes("session_id") || lower.includes("session")) {
        return "测试群创建失败。检查 Grix 连接和 WS session 是否有效。";
      }
      return "验收测试失败。检查 agent 是否在线并能正常回复。";
  }
}

// ---------------------------------------------------------------------------
// Section 7: Script path resolution
// ---------------------------------------------------------------------------

interface ScriptPaths {
  adminScript: string;
  bindScript: string;
  createAndBindScript: string;
  grixAuthScript: string;
  groupScript: string;
  installBin: string;
  queryScript: string;
  sendScript: string;
  startScript: string;
}

function resolveScripts(root: string): ScriptPaths {
  return {
    adminScript: path.join(root, "grix-admin", "scripts", "admin.js"),
    bindScript: path.join(root, "grix-egg", "scripts", "bind_local.js"),
    createAndBindScript: path.join(root, "grix-register", "scripts", "create_api_agent_and_bind.js"),
    grixAuthScript: path.join(root, "grix-register", "scripts", "grix_auth.js"),
    groupScript: path.join(root, "grix-group", "scripts", "group.js"),
    installBin: path.join(root, "bin", "grix-hermes.js"),
    queryScript: path.join(root, "grix-query", "scripts", "query.js"),
    sendScript: path.join(root, "message-send", "scripts", "send.js"),
    startScript: path.join(root, "grix-egg", "scripts", "start_gateway.js"),
  };
}

// ---------------------------------------------------------------------------
// Section 8: Step executors
// ---------------------------------------------------------------------------

function stepDetect(
  flags: Flags,
  state: StateFile,
  _scripts: ScriptPaths,
  hermesHome: string,
): void {
  if (stepIsDone(state, "detect")) return;

  const route = cleanText(flags.route) || "create_new";
  if (route !== "create_new" && route !== "existing") {
    throw new BootstrapError(
      "detect", 1,
      `不支持的 route: ${route}`,
      "使用 --route create_new 创建新 agent，或 --route existing 绑定已有 agent 凭证。",
      `unsupported route=${route}`,
    );
  }
  if (route === "existing") {
    if (!cleanText(flags.bindJson) && (!cleanText(flags.agentId) || !cleanText(flags.apiEndpoint) || !cleanText(flags.apiKey))) {
      throw new BootstrapError(
        "detect", 1,
        "--route existing 需要提供 --agent-id、--api-endpoint、--api-key，或提供 --bind-json",
        "已有 agent 绑定必须显式提供完整凭证；不要从 checkpoint 或终端脱敏输出里恢复 API key。",
        "missing existing bind credentials",
      );
    }
    setStatePath(state, "existing");
    markStepDone(state, "detect", { path: "existing" });
    return;
  }

  const wsDiagnostics = getWsCredentialDiagnostics({
    hermesHome,
    profileName: cleanText(flags.profileName) || cleanText(flags.agentName),
  });
  const useWs = hasWsCredentials({
    hermesHome,
    profileName: cleanText(flags.profileName) || cleanText(flags.agentName),
  });
  if (useWs) {
    setStatePath(state, "host");
    markStepDone(state, "detect", {
      path: "host",
      ws_source: wsDiagnostics.selectedSource,
      ws_source_path: wsDiagnostics.selectedSourcePath,
      ws_profile_name: wsDiagnostics.selectedProfileName || "",
      transport: "host_grix_session",
    });
    return;
  }

  if (cleanText(flags.accessToken) || cleanText(flags.email) || cleanText(flags.account)) {
    setStatePath(state, "http");
    markStepDone(state, "detect", {
      path: "http",
      transport: "http_login_token",
      token_source: cleanText(flags.accessToken)
        ? "cli_flag_access_token"
        : "login_with_account_password",
    });
    return;
  }

  throw new BootstrapError(
    "detect", 1,
    "未检测到可复用的 Grix 宿主会话凭证，且也没有可用于 HTTP fallback 的 access token / 登录账号信息",
    "请先检查当前 Hermes 会话是否已连接 Grix；如果宿主链路不可用，则向用户获取邮箱/账号和密码，通过 grix-register login/register 换取 access token 后再重试；或显式提供已有 agent 凭证走 --route existing。",
    formatWsCredentialDiagnostics(wsDiagnostics),
  );
}

function backupExistingState(
  hermesHome: string,
  route: string,
  profileDir: string,
  installDir: string,
): string {
  if (route !== "existing") return "";
  const candidates = [
    path.join(profileDir, ".env"),
    path.join(profileDir, "config.yaml"),
    path.join(profileDir, "SOUL.md"),
    installDir,
  ].filter((candidate) => fs.existsSync(candidate));
  if (candidates.length === 0) return "";

  const now = new Date();
  const timestamp = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
    "-",
    String(now.getHours()).padStart(2, "0"),
    String(now.getMinutes()).padStart(2, "0"),
    String(now.getSeconds()).padStart(2, "0"),
  ].join("");
  const backupRoot = path.join(hermesHome, "backups", "grix-egg", timestamp);
  fs.mkdirSync(backupRoot, { recursive: true });

  for (const source of candidates) {
    const dest = path.join(backupRoot, path.basename(source));
    const stat = fs.statSync(source);
    if (stat.isDirectory()) {
      copyDirRecursive(source, dest);
    } else {
      fs.copyFileSync(source, dest);
    }
  }
  return backupRoot;
}

function copyDirRecursive(src: string, dest: string): void {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src)) {
    const srcPath = path.join(src, entry);
    const destPath = path.join(dest, entry);
    const stat = fs.statSync(srcPath);
    if (stat.isDirectory()) {
      copyDirRecursive(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

function normalizeBindPayload(payload: Record<string, unknown>, flags: Flags): RuntimeCredentials | null {
  const handoff = extractNested(payload, "handoff");
  const bindHermes = extractNested(payload, "bind_hermes");
  const handoffBindHermes = extractNested(handoff, "bind_hermes");
  const bindLocal = extractNested(payload, "bind_local");
  const handoffBindLocal = extractNested(handoff, "bind_local");
  const data = extractNested(payload, "data");
  const createdAgent = extractNested(payload, "createdAgent");
  const source = Object.keys(bindHermes).length > 0
    ? bindHermes
    : Object.keys(handoffBindHermes).length > 0
      ? handoffBindHermes
      : Object.keys(bindLocal).length > 0
        ? bindLocal
        : Object.keys(handoffBindLocal).length > 0
          ? handoffBindLocal
          : Object.keys(createdAgent).length > 0
            ? createdAgent
            : Object.keys(data).length > 0
              ? data
              : payload;

  const agentName = cleanText(source.agent_name || source.name || flags.agentName);
  const profileName = cleanText(source.profile_name || agentName || flags.profileName);
  const credentials: RuntimeCredentials = {
    agentId: cleanText(source.agent_id || source.id),
    agentName,
    apiEndpoint: cleanText(source.api_endpoint || source.endpoint),
    apiKey: cleanText(source.api_key),
    profileName,
  };
  return credentials.agentId && credentials.apiEndpoint && credentials.apiKey
    ? credentials
    : null;
}

function credentialsFromFlags(flags: Flags): RuntimeCredentials | null {
  const bindJson = cleanText(flags.bindJson);
  if (bindJson) {
    const raw = bindJson === "-"
      ? readStdinText()
      : fs.readFileSync(expandHome(bindJson), "utf8");
    const payload = JSON.parse(raw) as Record<string, unknown>;
    return normalizeBindPayload(payload, flags);
  }

  const agentId = cleanText(flags.agentId);
  const apiEndpoint = cleanText(flags.apiEndpoint);
  const apiKey = cleanText(flags.apiKey);
  if (!agentId && !apiEndpoint && !apiKey) return null;

  return agentId && apiEndpoint && apiKey
    ? {
        agentId,
        agentName: cleanText(flags.agentName),
        apiEndpoint,
        apiKey,
        profileName: cleanText(flags.profileName) || cleanText(flags.agentName),
      }
    : null;
}

function requireExistingCredentials(flags: Flags): RuntimeCredentials {
  const credentials = credentialsFromFlags(flags);
  if (!credentials) {
    throw new BootstrapError(
      "detect", 1,
      "--route existing 需要提供 --agent-id、--api-endpoint、--api-key，或提供 --bind-json",
      "已有 agent 绑定必须显式提供完整凭证；不要从 checkpoint 或终端脱敏输出里恢复 API key。",
      "missing existing bind credentials",
    );
  }
  return credentials;
}

function stepInstall(
  flags: Flags,
  state: StateFile,
  scripts: ScriptPaths,
  hermesHome: string,
  env: NodeJS.ProcessEnv,
): void {
  if (stepIsDone(state, "install")) return;

  const installDir = cleanText(flags.installDir) || defaultInstallDir(hermesHome);
  const cmd = [flags.node, scripts.installBin, "install", "--dest", installDir, "--force"];
  const root = projectRoot();
  const result = runCommand(cmd, { env, cwd: root });
  markStepDone(state, "install", {
    install_dir: installDir,
    stdout: cleanText(result.stdout),
  });
}

async function stepCreate(
  flags: Flags,
  state: StateFile,
  scripts: ScriptPaths,
  hermesHome: string,
  env: NodeJS.ProcessEnv,
): Promise<StepCreateResult> {
  if (stepIsDone(state, "create")) {
    return {
      credentials: credentialsFromFlags(flags),
      pathUsed: cleanText(state.steps.create?.result?.["path"]) as CreatePath,
    };
  }

  const detectedPath = state.steps.detect?.result?.["path"];
  if (detectedPath === "host") {
    const credentials = await stepCreateWs(flags, state, scripts, env);
    setStatePath(state, "host");
    markStepDone(state, "create", {
      ...(state.steps.create?.result || {}),
      path: "host",
      transport: "host_grix_session",
    });
    return { credentials, pathUsed: "host" };
  }
  if (detectedPath === "existing") {
    const credentials = stepCreateExisting(flags, state);
    setStatePath(state, "existing");
    return { credentials, pathUsed: "existing" };
  }
  stepCreateHttp(flags, state, scripts, hermesHome, env);
  setStatePath(state, "http");
  return { credentials: null, pathUsed: "http" };
}

async function stepCreateWs(
  flags: Flags,
  state: StateFile,
  scripts: ScriptPaths,
  env: NodeJS.ProcessEnv,
): Promise<RuntimeCredentials> {
  const cmd = [flags.node, scripts.adminScript, "--action", "create_grix"];
  appendTextFlag(cmd, "--agent-name", flags.agentName);
  appendTextFlag(cmd, "--introduction", flags.agentName);
  appendTextFlag(cmd, "--is-main", flags.isMain);
  appendTextFlag(cmd, "--category-name", flags.categoryName);

  const result = runCommand(cmd, { env });
  const payload = parseJsonOutput(result);

  const createdAgent = extractNested(payload, "createdAgent");
  const createdAgentSource = Object.keys(createdAgent).length > 0
    ? createdAgent
    : extractNested(payload, "data") || payload;
  const agentId = cleanText(createdAgentSource.agent_id || createdAgentSource.id);
  const apiEndpoint = cleanText(createdAgentSource.api_endpoint || createdAgentSource.endpoint);
  const apiKey = cleanText(createdAgentSource.api_key);
  const agentName = cleanText(createdAgentSource.agent_name || createdAgentSource.name || flags.agentName);

  if (!agentId || !apiEndpoint || !apiKey) {
    throw new BootstrapError(
      "create", 3,
      `WS 创建 agent 未返回有效凭证。agent_id=${agentId}, api_endpoint=${apiEndpoint}, api_key=${apiKey ? "ak_***" : ""}`,
      "检查 WS 连接和 GRIX_API_KEY 是否有效。确认 agent_api_create 接口正常。",
      result.stderr || result.stdout,
    );
  }

  markStepDone(state, "create", {
    path: "host",
    agent_id: agentId,
    agent_name: agentName,
    api_endpoint: apiEndpoint,
    api_key: apiKey ? "ak_***" : "",
    transport: "host_grix_session",
  });

  return {
    agentId,
    agentName,
    apiEndpoint,
    apiKey,
    profileName: cleanText(flags.profileName) || flags.agentName,
  };
}

function stepCreateExisting(flags: Flags, state: StateFile): RuntimeCredentials {
  const credentials = requireExistingCredentials(flags);
  markStepDone(state, "create", {
    path: "existing",
    agent_id: credentials.agentId,
    agent_name: credentials.agentName,
    api_endpoint: credentials.apiEndpoint,
    api_key: credentials.apiKey ? "ak_***" : "",
    profile_name: credentials.profileName,
  });
  state.profile_name = credentials.profileName;
  return credentials;
}

function stepCreateHttp(
  flags: Flags,
  state: StateFile,
  scripts: ScriptPaths,
  hermesHome: string,
  env: NodeJS.ProcessEnv,
): void {
  const installDir = cleanText(flags.installDir) || defaultInstallDir(hermesHome);
  const profileName = cleanText(flags.profileName) || flags.agentName;
  let accessToken = cleanText(flags.accessToken);
  if (!accessToken) {
    const account = cleanText(flags.email) || cleanText(flags.account);
    const password = cleanText(flags.password);
    if (!account || !password) {
      throw new BootstrapError(
        "create", 3,
        "HTTP fallback 缺少 access token，且也没有可用于登录的账号/邮箱和密码",
        "请先向用户获取邮箱/账号和密码，通过 grix-register login 换取 access token；如用户还没有账号，则先走 send-email-code/register/login，再重试。",
        `missing HTTP auth inputs: access_token=${accessToken ? "present" : "missing"}, account=${account ? "present" : "missing"}, password=${password ? "present" : "missing"}`,
      );
    }
    const loginCmd = [
      flags.node,
      scripts.grixAuthScript,
      "login",
      "--password", password,
    ];
    if (cleanText(flags.email)) loginCmd.push("--email", cleanText(flags.email));
    else loginCmd.push("--account", cleanText(flags.account));
    appendTextFlag(loginCmd, "--base-url", flags.baseUrl);
    const loginResult = runCommand(loginCmd, { env });
    const loginPayload = parseJsonOutput(loginResult);
    accessToken = cleanText(loginPayload.access_token);
    if (!accessToken) {
      throw new BootstrapError(
        "create", 3,
        "grix-register login 未返回 access token，无法继续 HTTP fallback 创建 agent",
        "检查邮箱/账号密码是否正确，并确认 grix-register login 对当前 Grix 环境可用。",
        loginResult.stderr || loginResult.stdout,
      );
    }
  }
  const cmd = [
    flags.node,
    scripts.createAndBindScript,
    "--access-token", accessToken,
    "--agent-name", flags.agentName,
    "--profile-mode", "create-or-reuse",
    "--install-dir", installDir,
    "--profile-name", profileName,
    "--is-main", flags.isMain,
    "--account-id", cleanText(flags.accountId) || cleanText(env.GRIX_ACCOUNT_ID) || "main",
    "--allowed-users", cleanText(flags.allowedUsers),
    "--allow-all-users", flags.allowAllUsers,
    "--home-channel", cleanText(flags.homeChannel),
    "--home-channel-name", cleanText(flags.homeChannelName),
    "--inherit-keys", "global",
    "--json",
  ];
  appendTextFlag(cmd, "--base-url", flags.baseUrl);
  appendTextFlag(cmd, "--avatar-url", flags.avatarUrl);

  const result = runCommand(cmd, { env });
  const payload = parseJsonOutput(result);

  const bindResult = extractNested(payload, "bind_result") || payload;
  const agentId = cleanText(bindResult.agent_id);
  const apiEndpoint = cleanText(bindResult.api_endpoint);
  const apiKey = cleanText(bindResult.api_key);
  const agentName = cleanText(bindResult.agent_name || flags.agentName);
  const resolvedProfileName = cleanText(bindResult.profile_name || flags.profileName);

  if (!agentId || !apiEndpoint) {
    throw new BootstrapError(
      "create", 3,
      `HTTP 创建 agent 未返回有效凭证。agent_id=${agentId}, api_endpoint=${apiEndpoint}`,
      "检查是否已先通过 grix-register login/register 获取有效 access token，并确认网络可访问 Grix API。",
      result.stderr || result.stdout,
    );
  }

  markStepDone(state, "create", {
    path: "http",
    agent_id: agentId,
    agent_name: agentName,
    api_endpoint: apiEndpoint,
    api_key: apiKey ? "ak_***" : "",
    profile_name: resolvedProfileName,
    transport: "http_login_token",
    token_source: cleanText(flags.accessToken)
      ? "cli_flag_access_token"
      : "login_with_account_password",
  });
}

function stepBind(
  flags: Flags,
  state: StateFile,
  scripts: ScriptPaths,
  hermesHome: string,
  env: NodeJS.ProcessEnv,
  credentials: RuntimeCredentials | null,
): void {
  if (stepIsDone(state, "bind")) return;
  const profileRoot = resolveProfileRoot(hermesHome);

  const createResult = state.steps.create?.result;
  const pathUsed = state.path || (state.steps.create?.result?.["path"] as CreatePath) || (state.steps.detect?.result?.["path"] as CreatePath);

  if (pathUsed === "http") {
    // HTTP path: create_api_agent_and_bind already did binding
    // Extract bind result from create step
    const profileName = cleanText(createResult?.profile_name) || flags.profileName || flags.agentName;
    markStepDone(state, "bind", {
      profile_name: profileName,
      profile_dir: resolveProfileDir(profileRoot, profileName),
      via: "http_create_and_bind",
    });
    state.profile_name = profileName;
    return;
  }

  // WS and existing paths bind with credentials kept in memory for this process.
  const runtimeCredentials = credentials || credentialsFromFlags(flags);
  const agentId = cleanText(runtimeCredentials?.agentId || createResult?.agent_id);
  const agentName = cleanText(runtimeCredentials?.agentName || createResult?.agent_name) || flags.agentName;
  const apiEndpoint = cleanText(runtimeCredentials?.apiEndpoint || createResult?.api_endpoint);
  const apiKey = cleanText(runtimeCredentials?.apiKey);
  const installDir = cleanText(flags.installDir) || defaultInstallDir(hermesHome);
  const profileName = cleanText(runtimeCredentials?.profileName || flags.profileName) || flags.agentName;

  if (!agentId || !apiEndpoint || !apiKey) {
    throw new BootstrapError(
      "bind", 4,
      "绑定需要 agent_id、api_endpoint 和未脱敏 api_key",
      "如果是在 create 后中断再 --resume，请改用 --route existing 并显式提供 --agent-id、--api-endpoint、--api-key 或 --bind-json。",
      `agent_id=${agentId}, api_endpoint=${apiEndpoint}, api_key=${apiKey ? "ak_***" : ""}`,
    );
  }

  const bindInput = JSON.stringify({
    agent_name: agentName,
    agent_id: agentId,
    api_endpoint: apiEndpoint,
    api_key: apiKey,
    is_main: flags.isMain,
    profile_name: profileName,
  });

  const defaultAllowedUsers = cleanText(flags.allowedUsers);
  const defaultAllowAllUsers = cleanText(flags.allowAllUsers) || (defaultAllowedUsers ? "" : "true");

  const cmd = [
    flags.node, scripts.bindScript,
    "--from-json", "-",
    "--profile-mode", "create-or-reuse",
    "--install-dir", installDir,
    "--hermes-home", hermesHome,
    "--hermes", flags.hermes,
    "--node", flags.node,
    "--inherit-keys", "global",
    "--is-main", flags.isMain,
    "--profile-name", profileName,
    "--account-id", cleanText(flags.accountId) || cleanText(env.GRIX_ACCOUNT_ID) || "main",
    "--home-channel", cleanText(flags.homeChannel),
    "--home-channel-name", cleanText(flags.homeChannelName),
    "--allowed-users", defaultAllowedUsers,
    "--allow-all-users", defaultAllowAllUsers,
    "--json",
  ];

  const result = runCommand(cmd, { env, inputText: bindInput });
  const payload = parseJsonOutput(result);
  const bindResult = extractNested(payload, "bind_result") || payload;
  const resolvedProfile = cleanText(bindResult.profile_name) || profileName;
  const resolvedProfileDir = cleanText(bindResult.profile_dir) || resolveProfileDir(profileRoot, resolvedProfile);

  markStepDone(state, "bind", {
    profile_name: resolvedProfile,
    profile_dir: resolvedProfileDir,
    env_path: cleanText(bindResult.env_path),
    config_path: cleanText(bindResult.config_path),
  });
  state.profile_name = resolvedProfile;
}

function stepSoul(
  flags: Flags,
  state: StateFile,
  _scripts: ScriptPaths,
  hermesHome: string,
): void {
  if (stepIsDone(state, "soul")) return;

  const profileName = state.profile_name || flags.profileName || flags.agentName;
  const boundProfileDir = cleanText(state.steps.bind?.result?.profile_dir);
  const profileRoot = resolveProfileRoot(hermesHome);
  const profileDir = boundProfileDir || resolveProfileDir(profileRoot, profileName);

  let content: string;
  const soulFile = cleanText(flags.soulFile);
  const soulContent = cleanText(flags.soulContent);

  if (soulFile) {
    if (!fs.existsSync(soulFile)) {
      throw new BootstrapError(
        "soul", 5,
        `SOUL 文件不存在: ${soulFile}`,
        "检查 --soul-file 路径是否正确。",
        `file not found: ${soulFile}`,
      );
    }
    content = fs.readFileSync(soulFile, "utf8");
  } else if (soulContent) {
    content = soulContent;
  } else {
    markStepSkipped(state, "soul");
    return;
  }

  const target = path.join(profileDir, "SOUL.md");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, `${content.trimEnd()}\n`, "utf8");

  markStepDone(state, "soul", { soul_path: target });
}

function stepGateway(
  flags: Flags,
  state: StateFile,
  scripts: ScriptPaths,
  hermesHome: string,
  env: NodeJS.ProcessEnv,
): void {
  if (stepIsDone(state, "gateway")) return;

  const profileName = state.profile_name || flags.profileName || flags.agentName;
  const cmd = [
    flags.node, scripts.startScript,
    "--profile-name", profileName,
    "--hermes-home", hermesHome,
    "--hermes", flags.hermes,
    "--json",
  ];

  const result = runCommand(cmd, { env });
  const payload = parseJsonOutput(result);

  markStepDone(state, "gateway", {
    profile_name: profileName,
    start_result: payload,
  });
}

async function stepAccept(
  flags: Flags,
  state: StateFile,
  scripts: ScriptPaths,
  deliveryTarget: ResolvedDeliveryTarget,
  hermesHome: string,
  env: NodeJS.ProcessEnv,
): Promise<void> {
  if (stepIsDone(state, "accept")) return;

  const probeMessage = "probe";
  const expectedSubstring = cleanText(flags.expectedSubstring);

  const createResult = state.steps.create?.result;
  const targetAgentId = cleanText(createResult?.agent_id || flags.agentId);
  if (!targetAgentId) {
    throw new BootstrapError(
      "accept", 7,
      "验收需要目标 agent_id，但 create 步骤没有记录 agent_id",
      "检查 create 步骤是否成功；已有 agent 路径请提供 --agent-id。",
      "missing target agent_id",
    );
  }

  const profileName = state.profile_name || cleanText(flags.profileName) || flags.agentName;
  const cmdEnv = { ...env, HERMES_PROFILE: profileName };

  const groupCmd = [flags.node, scripts.groupScript, "--action", "create", "--name", `验收测试-${state.agent_name}`];
  const acceptanceMembers = buildAcceptanceMembers(targetAgentId, flags.memberIds, flags.memberTypes);
  if (acceptanceMembers.memberIds.length > 0) {
    groupCmd.push("--member-ids", acceptanceMembers.memberIds.join(","));
    groupCmd.push("--member-types", acceptanceMembers.memberTypes.join(","));
  }
  const groupResult = runCommand(groupCmd, { env: cmdEnv });
  const groupPayload = parseJsonOutput(groupResult);
  const groupData = extractNested(groupPayload, "data") || groupPayload;
  const acceptanceSessionId = extractSessionId(groupData);

  if (!acceptanceSessionId) {
    throw new BootstrapError(
      "accept", 7,
      `测试群创建未返回 session_id: ${JSON.stringify(groupData)}`,
      "检查 Grix WS 连接是否正常，session 是否有效。",
      groupResult.stderr || groupResult.stdout,
    );
  }

  if (deliveryTarget.target) {
    const cardText = buildConversationCard({
      sessionId: acceptanceSessionId,
      sessionType: "group",
      title: `验收测试-${state.agent_name}`,
    });
    sendDeliveryMessage(
      flags,
      state,
      scripts,
      deliveryTarget,
      "acceptance_card",
      cardText,
      hermesHome,
    );
  }

  const acceptanceProbeMessage = buildAcceptanceProbeMessage(targetAgentId, probeMessage);
  const probeSentAtMs = Date.now();
  const probeSendResult = runCommand(
    [flags.node, scripts.sendScript, "--to", acceptanceSessionId, "--message", acceptanceProbeMessage],
    { env: cmdEnv },
  );
  const probePayload = parseJsonOutput(probeSendResult);
  const probeMessageId = extractMessageId(probePayload);

  const timeoutSeconds = parsePositiveFloat(flags.acceptTimeoutSeconds, 15);
  const pollInterval = parsePositiveFloat(flags.acceptPollIntervalSeconds, 1);
  const expectedLower = expectedSubstring.toLowerCase();
  const requireExpectedSubstring = Boolean(expectedLower);
  const deadline = Date.now() + timeoutSeconds * 1000;

  let lastQuery: Record<string, unknown> = {};
  let lastCandidateCount = 0;

  while (Date.now() < deadline) {
    const queryResult = runCommand([
      flags.node,
      scripts.queryScript,
      "--action", "message_history",
      "--session-id", acceptanceSessionId,
      "--limit", "10",
    ], { env: cmdEnv });
    const queryPayload = parseJsonOutput(queryResult);
    const extractedData = extractNested(queryPayload, "data");
    lastQuery = Object.keys(extractedData).length > 0 ? extractedData : queryPayload;
    const messages = extractMessageRecords(lastQuery);
    lastCandidateCount = messages.length;
    const verifiedMessage = messages.find((message) =>
      messageMatchesAcceptance({
        message,
        targetAgentId,
        expectedLower,
        requireExpectedSubstring,
        probeMessageId,
        probeSentAtMs,
      }),
    );
    if (verifiedMessage) {
      markStepDone(state, "accept", {
        session_id: acceptanceSessionId,
        verified: true,
        target_agent_id: targetAgentId,
        probe_message: acceptanceProbeMessage,
        probe_message_id: probeMessageId,
        expected_substring: expectedSubstring,
        reply_msg_id: extractMessageId(verifiedMessage),
        reply_sender_id: extractSenderIds(verifiedMessage)[0] || "",
        reply_content: extractMessageText(verifiedMessage),
        verified_message: verifiedMessage,
      });
      return;
    }
    await sleep(pollInterval * 1000);
  }

  throw new BootstrapError(
    "accept", 7,
    requireExpectedSubstring
      ? `验收超时：agent 未在 ${timeoutSeconds} 秒内回复包含「${expectedSubstring}」的内容`
      : `验收超时：agent 未在 ${timeoutSeconds} 秒内给出 probe 之后的首条非空回复`,
    "检查：(1) SOUL.md 内容是否正确，(2) 网关是否在线（hermes --profile <name> gateway status），(3) agent 是否已连接到 Grix。",
    `target_agent_id=${targetAgentId}, probe_message_id=${probeMessageId}, candidates=${lastCandidateCount}, last_query=${JSON.stringify(lastQuery)}`,
  );
}

// ---------------------------------------------------------------------------
// Section 9: Helpers
// ---------------------------------------------------------------------------

function buildAcceptanceProbeMessage(targetAgentId: string, probeMessage: string): string {
  const mention = `@${targetAgentId}`;
  const bracketMention = `@[${targetAgentId}]`;
  const message = cleanText(probeMessage);
  if (message.startsWith(mention)) return message;
  if (message.startsWith(bracketMention)) {
    return `${mention}${message.slice(bracketMention.length)}`.trim();
  }
  return `${mention} ${message}`.trim();
}

function extractNested(payload: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = payload[key];
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

function extractSessionId(payload: Record<string, unknown>): string {
  for (const key of ["session_id", "sessionId"]) {
    const value = cleanText(payload[key]);
    if (value) return value;
  }
  for (const nestedKey of ["data", "ack", "resolvedTarget"]) {
    const nested = extractNested(payload, nestedKey);
    if (Object.keys(nested).length > 0) {
      const sessionId = extractSessionId(nested);
      if (sessionId) return sessionId;
    }
  }
  return "";
}

function buildAcceptanceMembers(
  targetAgentId: string,
  memberIdsText: string,
  memberTypesText: string,
): { memberIds: string[]; memberTypes: string[] } {
  const memberIds = cleanList(memberIdsText);
  const memberTypes = cleanList(memberTypesText);
  if (memberTypes.length > 0 && memberTypes.length !== memberIds.length) {
    throw new BootstrapError(
      "accept", 7,
      "--member-types 数量必须和 --member-ids 一致",
      "批量验收成员请同时提供一一对应的 --member-ids 和 --member-types；不提供 member-types 时默认用户类型为 1。",
      `member_ids=${memberIds.length}, member_types=${memberTypes.length}`,
    );
  }

  const pairs = memberIds.map((id, index) => ({
    id,
    type: memberTypes[index] || "1",
  }));
  const existingTarget = pairs.find((pair) => pair.id === targetAgentId);
  if (existingTarget) {
    existingTarget.type = existingTarget.type || "2";
  } else {
    pairs.push({ id: targetAgentId, type: "2" });
  }

  return {
    memberIds: pairs.map((pair) => pair.id),
    memberTypes: pairs.map((pair) => pair.type),
  };
}

function parsePositiveFloat(value: unknown, fallback: number): number {
  const parsed = Number.parseFloat(cleanText(value));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function validateProfileName(profileName: string): void {
  if (!isValidProfileName(profileName)) {
    throw new Error(
      `Invalid Hermes profile name: ${profileName}. Must match [a-z0-9][a-z0-9_-]{0,63}`,
    );
  }
}

function extractMessageId(payload: unknown): string {
  const record = payload && typeof payload === "object" && !Array.isArray(payload)
    ? (payload as Record<string, unknown>)
    : null;
  if (!record) return "";
  for (const key of ["message_id", "messageId", "msg_id", "msgId", "id"]) {
    const value = cleanText(record[key]);
    if (value) return value;
  }
  for (const value of Object.values(record)) {
    if (value && typeof value === "object") {
      const nested = extractMessageId(value);
      if (nested) return nested;
    }
  }
  return "";
}

function extractMessageRecords(payload: unknown): Record<string, unknown>[] {
  const results: Record<string, unknown>[] = [];
  collectMessageRecords(payload, results, 0);
  return results;
}

function collectMessageRecords(value: unknown, results: Record<string, unknown>[], depth: number): void {
  if (depth > 8 || !value) return;
  if (Array.isArray(value)) {
    for (const item of value) {
      if (item && typeof item === "object" && !Array.isArray(item)) {
        const record = item as Record<string, unknown>;
        if (looksLikeMessageRecord(record)) results.push(record);
        collectMessageRecords(record, results, depth + 1);
      }
    }
    return;
  }
  if (typeof value !== "object") return;
  for (const child of Object.values(value as Record<string, unknown>)) {
    collectMessageRecords(child, results, depth + 1);
  }
}

function looksLikeMessageRecord(record: Record<string, unknown>): boolean {
  return Boolean(
    extractMessageText(record) ||
      extractMessageId(record) ||
      extractSenderIds(record).length > 0,
  );
}

function extractMessageText(record: Record<string, unknown>): string {
  for (const key of ["content", "text", "message", "body", "raw_text", "rawText", "msg_content", "msgContent"]) {
    const value = record[key];
    if (typeof value === "string" || typeof value === "number") {
      const text = cleanText(value);
      if (text) return text;
    }
  }
  for (const key of ["content", "message", "payload"]) {
    const nested = extractNested(record, key);
    if (Object.keys(nested).length > 0) {
      const text = extractMessageText(nested);
      if (text) return text;
    }
  }
  return "";
}

function extractSenderIds(record: Record<string, unknown>): string[] {
  const ids: string[] = [];
  const push = (value: unknown): void => {
    const text = cleanText(value);
    if (text && !ids.includes(text)) ids.push(text);
  };
  for (const key of [
    "sender_id",
    "senderId",
    "from_id",
    "fromId",
    "author_id",
    "authorId",
    "agent_id",
    "agentId",
    "member_id",
    "memberId",
    "user_id",
    "userId",
  ]) {
    push(record[key]);
  }
  for (const key of ["sender", "from", "author", "agent", "member", "user"]) {
    const nested = extractNested(record, key);
    if (Object.keys(nested).length === 0) continue;
    for (const idKey of ["id", "agent_id", "agentId", "user_id", "userId", "member_id", "memberId"]) {
      push(nested[idKey]);
    }
  }
  return ids;
}

function parseNumericId(value: string): bigint | null {
  const text = cleanText(value);
  return /^\d+$/.test(text) ? BigInt(text) : null;
}

function extractMessageTimeMs(record: Record<string, unknown>): number | null {
  for (const key of ["created_at", "createdAt", "timestamp", "time", "msg_time", "msgTime", "send_time", "sendTime"]) {
    const value = record[key];
    if (typeof value === "number") {
      return value > 9999999999 ? value : value * 1000;
    }
    const text = cleanText(value);
    if (!text) continue;
    if (/^\d+$/.test(text)) {
      const numeric = Number.parseInt(text, 10);
      return numeric > 9999999999 ? numeric : numeric * 1000;
    }
    const parsed = Date.parse(text);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function messageMatchesAcceptance(params: {
  message: Record<string, unknown>;
  targetAgentId: string;
  expectedLower: string;
  requireExpectedSubstring: boolean;
  probeMessageId: string;
  probeSentAtMs: number;
}): boolean {
  const text = extractMessageText(params.message).trim();
  if (!text) return false;
  if (params.requireExpectedSubstring && !text.toLowerCase().includes(params.expectedLower)) return false;
  if (!extractSenderIds(params.message).includes(params.targetAgentId)) return false;

  const messageId = parseNumericId(extractMessageId(params.message));
  const probeId = parseNumericId(params.probeMessageId);
  if (messageId !== null && probeId !== null) return messageId > probeId;

  const messageTime = extractMessageTimeMs(params.message);
  if (messageTime !== null) return messageTime >= params.probeSentAtMs - 1000;

  return false;
}

function wasDeliveryAttemptSuccessful(state: StateFile, kind: DeliveryMessageKind): boolean {
  return state.delivery.attempts.some((attempt) => attempt.kind === kind && attempt.ok);
}

function buildSuccessSummary(state: StateFile): string {
  const agentName = cleanText(state.agent_name);
  const profileName = cleanText(state.profile_name);
  const acceptResult = state.steps.accept?.result || {};
  const replyContent = cleanText(acceptResult.reply_content);

  const head = profileName
    ? `agent「${agentName}」已创建完成，本地 profile 为「${profileName}」`
    : `agent「${agentName}」已创建完成`;

  let summary = replyContent ? `${head}，验收回复为：${replyContent}` : `${head}，验收已通过`;
  if (wasDeliveryAttemptSuccessful(state, "acceptance_card")) {
    summary = `${summary}，测试群入口已发送`;
  }
  return summary;
}

function buildFailureSummary(agentName: string, step: string, reason: string, suggestion: string): string {
  const shortReason = truncateText(reason, 120);
  const shortSuggestion = truncateText(suggestion.split(/[。\n]/)[0] || suggestion, 80);
  return `agent「${agentName}」孵化失败，停在 ${step}：${shortReason}。建议：${shortSuggestion}`;
}

function buildSuccessOutput(state: StateFile, backupDir: string): Record<string, unknown> {
  const acceptResult = state.steps.accept?.result || {};
  const acceptanceSessionId = cleanText(acceptResult.session_id);
  const targetAgentId = cleanText(acceptResult.target_agent_id || state.steps.create?.result?.agent_id);

  const acceptance: Record<string, unknown> = {
    session_id: acceptanceSessionId,
    target_agent_id: targetAgentId,
    probe_message: cleanText(acceptResult.probe_message),
    probe_message_id: cleanText(acceptResult.probe_message_id),
    reply_msg_id: cleanText(acceptResult.reply_msg_id),
    reply_sender_id: cleanText(acceptResult.reply_sender_id),
    reply_content: cleanText(acceptResult.reply_content),
    expected_substring: cleanText(acceptResult.expected_substring),
  };

  const links: Record<string, unknown> = {};
  if (acceptanceSessionId) {
    links.acceptance_conversation = buildConversationCard({
      sessionId: acceptanceSessionId,
      sessionType: "group",
      title: `验收测试-${state.agent_name}`,
    });
  }
  if (cleanText(state.delivery.target)) {
    links.status_target = cleanText(state.delivery.target);
  }

  const output: Record<string, unknown> = {
    ok: true as const,
    install_id: state.install_id,
    agent_name: state.agent_name,
    profile_name: state.profile_name,
    route: state.route,
    path: state.path,
    interaction_status: state.interaction_status,
    delivery: state.delivery,
    summary: buildSuccessSummary(state),
    acceptance,
    steps: Object.fromEntries(
      STEP_NAMES.map((name) => [name, { status: state.steps[name].status }]),
    ),
  };

  if (Object.keys(links).length > 0) output.links = links;
  if (backupDir) output.backup_dir = backupDir;
  return output;
}

function recordDeliveryAttempt(
  state: StateFile,
  deliveryTarget: ResolvedDeliveryTarget,
  kind: DeliveryMessageKind,
  message: string,
  result: { ok: boolean; ack: Record<string, unknown> | null; error: string | null },
): void {
  if (!deliveryTarget.target) return;
  state.delivery.attempts.push({
    kind,
    at: isoNow(),
    ok: result.ok,
    target: deliveryTarget.target,
    target_source: deliveryTarget.source,
    message_preview: truncateText(message),
    ack: result.ack,
    error: result.error,
  });
  refreshInteractionStatus(state);
}

function sendDeliveryMessage(
  flags: Flags,
  state: StateFile,
  scripts: ScriptPaths,
  deliveryTarget: ResolvedDeliveryTarget,
  kind: DeliveryMessageKind,
  message: string,
  hermesHome: string,
): void {
  if (!deliveryTarget.target) return;

  const profileName = cleanText(state.profile_name) || cleanText(flags.profileName) || flags.agentName;
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    HERMES_HOME: hermesHome,
    HERMES_PROFILE: profileName,
  };

  try {
    const result = runCommand(
      [flags.node, scripts.sendScript, "--to", deliveryTarget.target, "--message", message],
      { env, check: false },
    );
    if (result.code === 0) {
      const payload = parseJsonOutput(result);
      recordDeliveryAttempt(state, deliveryTarget, kind, message, {
        ok: true,
        ack: payload,
        error: null,
      });
      return;
    }
    recordDeliveryAttempt(state, deliveryTarget, kind, message, {
      ok: false,
      ack: null,
      error: truncateText(result.stderr || result.stdout || "message-send failed"),
    });
  } catch (error) {
    const messageText = error instanceof Error ? error.message : String(error);
    recordDeliveryAttempt(state, deliveryTarget, kind, message, {
      ok: false,
      ack: null,
      error: truncateText(messageText),
    });
  }
}

function sendStatusCard(
  flags: Flags,
  state: StateFile,
  scripts: ScriptPaths,
  deliveryTarget: ResolvedDeliveryTarget,
  kind: DeliveryMessageKind,
  status: string,
  step: string,
  summary: string,
  hermesHome: string,
): void {
  const cardText = buildEggStatusCard({
    installId: flags.installId,
    status,
    step,
    summary,
  });
  sendDeliveryMessage(flags, state, scripts, deliveryTarget, kind, cardText, hermesHome);
}

// ---------------------------------------------------------------------------
// Section 10: Main orchestration
// ---------------------------------------------------------------------------

function buildResumeCommand(flags: Flags): string {
  const parts = ["node", "scripts/bootstrap.js"];
  parts.push("--install-id", flags.installId);
  parts.push("--agent-name", flags.agentName);
  if (flags.installContext && flags.installContext !== "-") parts.push("--install-context", flags.installContext);
  if (flags.route) parts.push("--route", flags.route);
  if (flags.profileName) parts.push("--profile-name", flags.profileName);
  else if (flags.profileNameSuffix) parts.push("--profile-name-suffix", flags.profileNameSuffix);
  if (flags.soulFile) parts.push("--soul-file", flags.soulFile);
  if (flags.soulContent) parts.push("--soul-content", `'${flags.soulContent.slice(0, 30)}...'`);
  if (flags.statusTarget) parts.push("--status-target", flags.statusTarget);
  if (flags.homeChannel) parts.push("--home-channel", flags.homeChannel);
  if (flags.homeChannelName) parts.push("--home-channel-name", flags.homeChannelName);
  if (flags.expectedSubstring) parts.push("--expected-substring", flags.expectedSubstring);
  if (flags.bindJson && flags.bindJson !== "-") parts.push("--bind-json", flags.bindJson);
  parts.push("--resume", "--json");
  return parts.join(" ");
}

async function main(): Promise<number> {
  let flags: Flags;
  try {
    flags = parseArgs(process.argv.slice(2));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`${message}\n`);
    return 1;
  }

  if (!cleanText(flags.agentName)) {
    process.stderr.write("Missing required flag: --agent-name\n");
    return 1;
  }
  const requestedProfileName = cleanText(flags.profileName);
  if (flags.resume && !cleanText(flags.installId)) {
    process.stderr.write("Missing required flag for resume: --install-id\n");
    return 1;
  }
  if (cleanText(flags.installContext) === "-" && cleanText(flags.bindJson) === "-") {
    process.stderr.write("--install-context - cannot be combined with --bind-json - because both require stdin\n");
    return 1;
  }
  try {
    validateProfileNameSuffix(flags.profileNameSuffix);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`${message}\n`);
    return 1;
  }
  if (!requestedProfileName && !isValidProfileName(cleanText(flags.agentName)) && !cleanText(flags.profileNameSuffix)) {
    process.stderr.write(
      "Non-ASCII or non-profile-safe --agent-name requires --profile-name-suffix or an explicit --profile-name\n",
    );
    return 1;
  }
  let installContext: InstallContext | null;
  try {
    installContext = loadInstallContext(flags.installContext);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`${message}\n`);
    return 1;
  }
  const deliveryTarget = resolveDeliveryTarget(flags, installContext);
  flags.homeChannel = resolveHomeChannel(flags, installContext);
  flags.homeChannelName = resolveHomeChannelName(flags, installContext);
  flags.installId = cleanText(flags.installId) || generateInstallId();
  flags.profileName = resolveBootstrapProfileName(
    flags.profileName,
    flags.agentName,
    flags.profileNameSuffix,
  );
  try {
    validateProfileName(flags.profileName);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`${message}\n`);
    return 1;
  }

  const root = projectRoot();
  const scripts = resolveScripts(root);
  const hermesHome = resolveHermesHome(flags.hermesHome);
  const env: NodeJS.ProcessEnv = { ...process.env, HERMES_HOME: hermesHome };
  const stateFile = getStateFilePath(hermesHome, flags.installId);

  // Load or create state
  let state: StateFile;
    if (flags.resume) {
      const loaded = loadState(stateFile);
      if (!loaded) {
        process.stderr.write(`No checkpoint found for --install-id ${flags.installId}. Starting fresh.\n`);
        state = makeFreshState(flags);
      } else {
        state = loaded;
        // Allow flag overrides on resume
        if (cleanText(flags.agentName)) state.agent_name = flags.agentName;
        if (requestedProfileName) state.profile_name = flags.profileName;
      }
    } else {
      state = makeFreshState(flags);
  }
  state.delivery.attempts = [];
  state.interaction_status = "none";
  setResolvedDeliveryTarget(state, deliveryTarget);

  // Dry-run mode
  if (flags.dryRun) {
    const dryRunPayload = {
      ok: true,
      dry_run: true,
      install_id: flags.installId,
      agent_name: flags.agentName,
      profile_name: state.profile_name,
      route: flags.route,
      hermes_home: hermesHome,
      interaction_status: state.interaction_status,
      delivery: state.delivery,
      steps: Object.fromEntries(STEP_NAMES.map((name) => [name, state.steps[name]?.status || "pending"])),
    };
    process.stdout.write(`${JSON.stringify(dryRunPayload, null, 2)}\n`);
    return 0;
  }

  // Track current step for error reporting
  let currentStep: StepName = "detect";

  try {
    sendStatusCard(flags, state, scripts, deliveryTarget, "running_card", "running", "preparing", "开始孵化 agent", hermesHome);
    saveState(stateFile, state);

    // Backup for existing route
    const profileName = state.profile_name || flags.profileName || flags.agentName;
    const profileDir = resolveProfileDir(resolveProfileRoot(hermesHome), profileName);
    const installDir = cleanText(flags.installDir) || defaultInstallDir(hermesHome);
    const backupDir = backupExistingState(hermesHome, flags.route, profileDir, installDir);

    // Step 1: detect
    currentStep = "detect";
    stepDetect(flags, state, scripts, hermesHome);
    saveState(stateFile, state);

    // Step 2: install
    currentStep = "install";
    stepInstall(flags, state, scripts, hermesHome, env);
    saveState(stateFile, state);

    // Step 3: create
    currentStep = "create";
    const { credentials: runtimeCredentials } = await stepCreate(flags, state, scripts, hermesHome, env);
    saveState(stateFile, state);

    // Step 4: bind
    currentStep = "bind";
    stepBind(flags, state, scripts, hermesHome, env, runtimeCredentials);
    saveState(stateFile, state);

    // Step 5: soul
    currentStep = "soul";
    stepSoul(flags, state, scripts, hermesHome);
    saveState(stateFile, state);

    // Step 6: gateway
    currentStep = "gateway";
    stepGateway(flags, state, scripts, hermesHome, env);
    saveState(stateFile, state);

    // Step 7: accept (async due to polling)
    currentStep = "accept";
    await stepAccept(flags, state, scripts, deliveryTarget, hermesHome, env);
    saveState(stateFile, state);

    // Success
    state.completed_at = isoNow();
    const successSummary = buildSuccessSummary(state);
    sendDeliveryMessage(flags, state, scripts, deliveryTarget, "final_text", successSummary, hermesHome);
    sendStatusCard(flags, state, scripts, deliveryTarget, "final_card", "success", "complete", "Agent 孵化完成", hermesHome);
    saveState(stateFile, state);

    const output = buildSuccessOutput(state, backupDir);
    process.stdout.write(`${JSON.stringify(output, null, 2)}\n`);
    return 0;
  } catch (error) {
    const stepName = error instanceof BootstrapError ? error.step : currentStep;
    const stepNumber = error instanceof BootstrapError ? error.stepNumber : (STEP_NAMES.indexOf(currentStep) + 1);
    const reason = error instanceof Error ? error.message : String(error);
    const suggestion = error instanceof BootstrapError ? error.suggestion : suggestForError(currentStep, reason);
    const rawError = error instanceof BootstrapError ? error.rawError : reason;

    if (error instanceof BootstrapError) {
      markStepFailed(state, error.step, reason);
    } else {
      markStepFailed(state, currentStep, reason);
    }
    const failureSummary = buildFailureSummary(state.agent_name || flags.agentName, stepName, reason, suggestion);
    sendDeliveryMessage(flags, state, scripts, deliveryTarget, "failure_text", failureSummary, hermesHome);
    sendStatusCard(flags, state, scripts, deliveryTarget, "failure_card", "failed", stepName, reason.slice(0, 100), hermesHome);
    saveState(stateFile, state);

    const errorPayload = {
      ok: false as const,
      step: stepName,
      step_number: stepNumber,
      reason,
      suggestion,
      state_file: stateFile,
      resume_command: buildResumeCommand(flags),
      raw_error: rawError,
      interaction_status: state.interaction_status,
      delivery: state.delivery,
    };

    if (flags.json) {
      process.stderr.write(`${JSON.stringify(errorPayload, null, 2)}\n`);
    } else {
      process.stderr.write(`${JSON.stringify(errorPayload)}\n`);
    }
    return 1;
  }
}

process.exit(await main());
