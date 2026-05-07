#!/usr/bin/env node
import { randomUUID } from "node:crypto";

const DEFAULT_BASE_URL = "https://grix.dhf.pub";
const DEFAULT_TIMEOUT_SECONDS = 15;

type Action =
  | "send-email-code"
  | "register"
  | "login"
  | "create-api-agent";

interface ParsedArgs {
  action: Action;
  baseUrl: string;
  flags: Record<string, string | boolean>;
}

class GrixAuthError extends Error {
  readonly status: number;
  readonly code: number;
  readonly payload: unknown;
  constructor(message: string, status = 0, code = -1, payload: unknown = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

function cleanText(value: unknown): string {
  return String(value ?? "").trim();
}

function resolveDefaultBaseUrl(): string {
  return cleanText(process.env.GRIX_WEB_BASE_URL) || DEFAULT_BASE_URL;
}

function normalizeBaseUrl(rawBaseUrl: string): string {
  const base = cleanText(rawBaseUrl) || resolveDefaultBaseUrl();
  let parsed: URL;
  try {
    parsed = new URL(base);
  } catch {
    throw new Error(`Invalid base URL: ${base}`);
  }
  if (!parsed.protocol || !parsed.host) {
    throw new Error(`Invalid base URL: ${base}`);
  }
  let pathPart = parsed.pathname.replace(/\/+$/, "");
  if (!pathPart) {
    pathPart = "/v1";
  } else if (!pathPart.endsWith("/v1")) {
    pathPart = `${pathPart}/v1`;
  }
  const normalized = new URL(parsed.toString());
  normalized.pathname = pathPart;
  normalized.search = "";
  normalized.hash = "";
  return normalized.toString().replace(/\/+$/, "");
}

function derivePortalUrl(rawBaseUrl: string): string {
  const base = cleanText(rawBaseUrl) || resolveDefaultBaseUrl();
  let parsed: URL;
  try {
    parsed = new URL(base);
  } catch {
    throw new Error(`Invalid base URL: ${base}`);
  }
  let pathPart = parsed.pathname.replace(/\/+$/, "");
  if (pathPart.endsWith("/v1")) {
    pathPart = pathPart.slice(0, -"/v1".length);
  }
  const normalized = new URL(parsed.toString());
  normalized.pathname = pathPart || "/";
  normalized.search = "";
  normalized.hash = "";
  return normalized.toString().replace(/\/+$/, "") + "/";
}

interface RequestResult {
  api_base_url: string;
  status: number;
  data: unknown;
  payload: Record<string, unknown>;
}

async function requestJson(
  method: string,
  pathPart: string,
  baseUrl: string,
  body?: unknown,
  headers?: Record<string, string>,
): Promise<RequestResult> {
  const apiBaseUrl = normalizeBaseUrl(baseUrl);
  const url = `${apiBaseUrl}${pathPart.startsWith("/") ? pathPart : `/${pathPart}`}`;
  const finalHeaders: Record<string, string> = { ...(headers || {}) };
  let data: string | undefined;
  if (body !== undefined && body !== null) {
    data = JSON.stringify(body);
    finalHeaders["Content-Type"] = "application/json";
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_SECONDS * 1000);
  let response: Response;
  let raw: string;
  try {
    response = await fetch(url, {
      method,
      headers: finalHeaders,
      body: data,
      signal: controller.signal,
    });
    raw = await response.text();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new GrixAuthError(`network error: ${message}`);
  } finally {
    clearTimeout(timer);
  }

  const status = response.status;
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    throw new GrixAuthError(`invalid json response: ${raw.slice(0, 256)}`, status);
  }

  const code = Number.parseInt(String(payload.code ?? -1), 10);
  const msg = cleanText(payload.msg) || "unknown error";
  if (status >= 400 || code !== 0) {
    throw new GrixAuthError(msg, status, code, payload);
  }

  return {
    api_base_url: apiBaseUrl,
    status,
    data: payload.data,
    payload,
  };
}

function printJson(payload: unknown): void {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
}

function parseOptionalBool(value: unknown, defaultValue: boolean): boolean {
  if (typeof value === "boolean") return value;
  const normalized = cleanText(value).toLowerCase();
  if (normalized === "true") return true;
  if (normalized === "false") return false;
  return defaultValue;
}

interface AuthResult {
  ok: true;
  action: string;
  api_base_url: string;
  access_token: string;
  refresh_token: string;
  expires_in: number;
  user_id: string;
  portal_url: string;
  data: unknown;
}

function buildAuthResult(action: string, result: RequestResult, baseUrl: string): AuthResult {
  const data = (result.data && typeof result.data === "object" ? result.data : {}) as Record<string, unknown>;
  const user = (data.user && typeof data.user === "object" ? data.user : {}) as Record<string, unknown>;
  return {
    ok: true,
    action,
    api_base_url: result.api_base_url,
    access_token: String(data.access_token ?? ""),
    refresh_token: String(data.refresh_token ?? ""),
    expires_in: Number(data.expires_in ?? 0),
    user_id: String(user.id ?? ""),
    portal_url: derivePortalUrl(baseUrl),
    data,
  };
}

interface AgentResult {
  ok: true;
  action: string;
  api_base_url: string;
  agent_id: string;
  agent_name: string;
  is_main: boolean;
  provider_type: number;
  api_endpoint: string;
  api_key: string;
  api_key_hint: string;
  session_id: string;
  handoff: {
    target_tool: string;
    task: string;
    bind_local: {
      profile_name: string;
      agent_name: string;
      agent_id: string;
      api_endpoint: string;
      api_key: string;
      is_main: boolean;
    };
  };
  data: unknown;
  source?: string;
  existing_agent?: unknown;
}

function buildAgentResult(action: string, result: RequestResult, isMain: boolean): AgentResult {
  const data = (result.data && typeof result.data === "object" ? result.data : {}) as Record<string, unknown>;
  const agentId = cleanText(data.id);
  const apiEndpoint = cleanText(data.api_endpoint);
  const apiKey = cleanText(data.api_key);
  const agentName = cleanText(data.agent_name);
  const bindLocalPayload = {
    profile_name: agentName,
    agent_name: agentName,
    agent_id: agentId,
    api_endpoint: apiEndpoint,
    api_key: apiKey,
    is_main: Boolean(isMain),
  };
  const handoffTask = [
    "grix-egg route=existing",
    `profile_name=${agentName}`,
    `agent_name=${agentName}`,
    `agent_id=${agentId}`,
    `api_endpoint=${apiEndpoint}`,
    `api_key=${apiKey}`,
    `is_main=${isMain ? "true" : "false"}`,
    "do_not_create_remote_agent=true",
  ].join("\n");
  return {
    ok: true,
    action,
    api_base_url: result.api_base_url,
    agent_id: agentId,
    agent_name: agentName,
    is_main: Boolean(isMain),
    provider_type: Number(data.provider_type ?? 0),
    api_endpoint: apiEndpoint,
    api_key: apiKey,
    api_key_hint: cleanText(data.api_key_hint),
    session_id: cleanText(data.session_id),
    handoff: {
      target_tool: "grix_egg",
      task: handoffTask,
      bind_local: bindLocalPayload,
    },
    data,
  };
}

async function loginWithCredentials(
  baseUrl: string,
  account: string,
  password: string,
  deviceId: string,
  platform: string,
): Promise<AuthResult> {
  const result = await requestJson("POST", "/auth/login", baseUrl, {
    account,
    password,
    device_id: deviceId,
    platform,
  });
  return buildAuthResult("login", result, baseUrl);
}

async function createApiAgent(
  baseUrl: string,
  accessToken: string,
  agentName: string,
  avatarUrl: string,
  isMain: boolean,
): Promise<AgentResult> {
  const requestBody: Record<string, unknown> = {
    agent_name: agentName.trim(),
    provider_type: 3,
    is_main: Boolean(isMain),
  };
  const normalizedAvatarUrl = cleanText(avatarUrl);
  if (normalizedAvatarUrl) {
    requestBody.avatar_url = normalizedAvatarUrl;
  }
  const result = await requestJson("POST", "/agents/create", baseUrl, requestBody, {
    Authorization: `Bearer ${accessToken.trim()}`,
  });
  return buildAgentResult("create-api-agent", result, Boolean(isMain));
}

async function listAgents(baseUrl: string, accessToken: string): Promise<Record<string, unknown>[]> {
  const result = await requestJson("GET", "/agents/list", baseUrl, undefined, {
    Authorization: `Bearer ${accessToken.trim()}`,
  });
  const data = (result.data && typeof result.data === "object" ? result.data : {}) as Record<string, unknown>;
  const items = Array.isArray(data.list) ? (data.list as Record<string, unknown>[]) : [];
  return items;
}

async function rotateApiAgentKey(
  baseUrl: string,
  accessToken: string,
  agentId: string,
  isMain: boolean,
): Promise<AgentResult> {
  const result = await requestJson(
    "POST",
    `/agents/${agentId.trim()}/api/key/rotate`,
    baseUrl,
    {},
    { Authorization: `Bearer ${accessToken.trim()}` },
  );
  return buildAgentResult("rotate-api-agent-key", result, Boolean(isMain));
}

function findExistingApiAgent(
  agents: Record<string, unknown>[],
  agentName: string,
): Record<string, unknown> | null {
  const normalizedName = cleanText(agentName);
  if (!normalizedName) return null;
  for (const item of agents) {
    if (!item || typeof item !== "object") continue;
    if (cleanText(item.agent_name) !== normalizedName) continue;
    if (Number.parseInt(String(item.provider_type ?? 0), 10) !== 3) continue;
    if (Number.parseInt(String(item.status ?? 0), 10) === 3) continue;
    return item;
  }
  return null;
}

async function createOrReuseApiAgent(
  baseUrl: string,
  accessToken: string,
  agentName: string,
  avatarUrl: string,
  preferExisting: boolean,
  rotateOnReuse: boolean,
  isMain: boolean,
): Promise<AgentResult> {
  if (preferExisting) {
    const agents = await listAgents(baseUrl, accessToken);
    const existing = findExistingApiAgent(agents, agentName);
    if (existing) {
      if (!rotateOnReuse) {
        throw new GrixAuthError(
          "existing provider_type=3 agent found but rotate-on-reuse is disabled; cannot obtain api_key safely",
          0,
          -1,
          { existing_agent: existing },
        );
      }
      const rotated = await rotateApiAgentKey(
        baseUrl,
        accessToken,
        cleanText(existing.id),
        parseOptionalBool(existing.is_main, Boolean(isMain)),
      );
      rotated.source = "reused_existing_agent_with_rotated_key";
      rotated.existing_agent = existing;
      return rotated;
    }
  }
  const created = await createApiAgent(baseUrl, accessToken, agentName, avatarUrl, Boolean(isMain));
  created.source = "created_new_agent";
  return created;
}

function defaultDeviceId(platform: string): string {
  const normalizedPlatform = cleanText(platform) || "web";
  return `${normalizedPlatform}_${randomUUID()}`;
}

function parseArgs(argv: string[]): ParsedArgs {
  if (argv.length === 0) {
    throw new Error("action is required");
  }
  const flags: Record<string, string | boolean> = {};
  let baseUrl = resolveDefaultBaseUrl();
  const positional: string[] = [];
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i]!;
    if (token === "--base-url") {
      baseUrl = argv[++i] ?? baseUrl;
      continue;
    }
    if (token.startsWith("--")) {
      const key = token.slice(2).replace(/-/g, "_");
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        flags[key] = next;
        i += 1;
      } else {
        flags[key] = true;
      }
      continue;
    }
    positional.push(token);
  }
  const action = positional[0] as Action | undefined;
  if (!action) throw new Error("action is required");
  return { action, baseUrl, flags };
}

function flagString(flags: Record<string, string | boolean>, key: string, fallback = ""): string {
  const value = flags[key];
  if (typeof value === "string") return value;
  return fallback;
}

function flagRequired(flags: Record<string, string | boolean>, key: string): string {
  const value = flags[key];
  if (typeof value !== "string" || !value) {
    throw new Error(`--${key.replace(/_/g, "-")} is required`);
  }
  return value;
}

async function handleSendEmailCode(baseUrl: string, flags: Record<string, string | boolean>): Promise<void> {
  const scene = cleanText(flagRequired(flags, "scene"));
  const body: Record<string, string> = {
    email: cleanText(flagRequired(flags, "email")),
    scene,
  };

  const result = await requestJson("POST", "/auth/send-code", baseUrl, body);
  printJson({
    ok: true,
    action: "send-email-code",
    api_base_url: result.api_base_url,
    data: result.data,
  });
}

async function handleRegister(baseUrl: string, flags: Record<string, string | boolean>): Promise<void> {
  const platform = cleanText(flagString(flags, "platform", "web")) || "web";
  const deviceId = cleanText(flagString(flags, "device_id")) || defaultDeviceId(platform);
  const result = await requestJson("POST", "/auth/register", baseUrl, {
    email: cleanText(flagRequired(flags, "email")),
    password: cleanText(flagRequired(flags, "password")),
    email_code: cleanText(flagRequired(flags, "email_code")),
    device_id: deviceId,
    platform,
  });
  printJson(buildAuthResult("register", result, baseUrl));
}

async function handleLogin(baseUrl: string, flags: Record<string, string | boolean>): Promise<void> {
  const account = cleanText(flagString(flags, "email") || flagString(flags, "account"));
  if (!account) throw new GrixAuthError("either --email or --account is required");
  const platform = cleanText(flagString(flags, "platform", "web")) || "web";
  const deviceId = cleanText(flagString(flags, "device_id")) || defaultDeviceId(platform);
  const result = await loginWithCredentials(
    baseUrl,
    account,
    cleanText(flagRequired(flags, "password")),
    deviceId,
    platform,
  );
  printJson(result);
}

async function handleCreateApiAgent(baseUrl: string, flags: Record<string, string | boolean>): Promise<void> {
  const requestedIsMain = parseOptionalBool(flagString(flags, "is_main"), true);
  const result = await createOrReuseApiAgent(
    baseUrl,
    cleanText(flagRequired(flags, "access_token")),
    cleanText(flagRequired(flags, "agent_name")),
    flagString(flags, "avatar_url"),
    !Boolean(flags.no_reuse_existing_agent),
    !Boolean(flags.no_rotate_key_on_reuse),
    Boolean(requestedIsMain),
  );
  printJson(result);
}

async function main(): Promise<void> {
  let parsed: ParsedArgs;
  try {
    parsed = parseArgs(process.argv.slice(2));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`${message}\n`);
    process.exit(1);
    return;
  }
  try {
    switch (parsed.action) {
      case "send-email-code":
        await handleSendEmailCode(parsed.baseUrl, parsed.flags);
        break;
      case "register":
        await handleRegister(parsed.baseUrl, parsed.flags);
        break;
      case "login":
        await handleLogin(parsed.baseUrl, parsed.flags);
        break;
      case "create-api-agent":
        await handleCreateApiAgent(parsed.baseUrl, parsed.flags);
        break;
      default:
        throw new Error(`unknown action: ${parsed.action}`);
    }
  } catch (error) {
    if (error instanceof GrixAuthError) {
      printJson({
        ok: false,
        action: parsed.action,
        status: error.status,
        code: error.code,
        error: error.message,
        payload: error.payload,
      });
    } else {
      const message = error instanceof Error ? error.message : String(error);
      printJson({
        ok: false,
        action: parsed.action,
        status: 0,
        code: -1,
        error: message,
      });
    }
    process.exit(1);
  }
}

await main();
