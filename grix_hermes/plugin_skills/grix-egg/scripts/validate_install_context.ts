#!/usr/bin/env node
import fs from "node:fs";
import {
  normalizeRoute,
  requiredForRoute,
  type InstallContext,
  type InstallRoute,
} from "../../shared/types/install-context.js";

function cleanText(value: unknown): string {
  return String(value ?? "").trim();
}

function readPayload(argv: string[]): InstallContext {
  const fileIndex = argv.indexOf("--from-file");
  if (fileIndex >= 0 && argv[fileIndex + 1]) {
    return JSON.parse(fs.readFileSync(argv[fileIndex + 1]!, "utf8")) as InstallContext;
  }
  const stdin = fs.readFileSync(0, "utf8").trim();
  if (!stdin) {
    throw new Error("No install context JSON provided.");
  }
  return JSON.parse(stdin) as InstallContext;
}

function buildSteps(route: InstallRoute): string[] {
  if (
    route === "create_new" ||
    route === "existing"
  ) {
    return [
      "准备安装上下文",
      "必要时创建远端 API agent",
      "创建或定位目标 Hermes profile",
      "下载或落位 grix-hermes 安装内容",
      "写入或替换 SOUL.md",
      "写入并校验 Hermes 绑定",
      "启动 Hermes gateway 并确认运行状态",
      "向当前对话发送开始状态卡",
      "创建测试群并保存准确 session_id",
      "向当前对话发送测试群会话卡片",
      "在测试群做身份验收并修到正确",
      "向当前对话发送最终文本总结和最终状态卡",
    ];
  }
  return ["识别路由并补齐上下文"];
}

try {
  const payload = readPayload(process.argv.slice(2));
  const install = (payload.install && typeof payload.install === "object"
    ? payload.install
    : {}) as Record<string, unknown>;
  const route = normalizeRoute(install.route ?? payload.route ?? payload.install_route);
  const required = requiredForRoute(route);
  const missing = required.filter(
    (key) => !cleanText((payload as Record<string, unknown>)[key] ?? install[key]),
  );
  const result = {
    ok: true,
    route,
    missing,
    is_ready: missing.length === 0 && Boolean(route),
    steps: buildSteps(route),
  };
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
} catch (error) {
  process.stderr.write(
    `${JSON.stringify(
      {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      },
      null,
      2,
    )}\n`,
  );
  process.exit(1);
}
