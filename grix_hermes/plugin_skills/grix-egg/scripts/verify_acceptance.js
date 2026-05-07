#!/usr/bin/env node
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";
function cleanText(value) {
    return String(value ?? "").trim();
}
function asRecord(value) {
    return value && typeof value === "object" && !Array.isArray(value)
        ? value
        : {};
}
function runCommand(cmd, env, check = true) {
    const [bin, ...rest] = cmd;
    if (!bin)
        throw new Error("runCommand received empty cmd");
    const result = spawnSync(bin, rest, { encoding: "utf8", env });
    const output = {
        code: result.status ?? -1,
        stdout: (result.stdout || "").trim(),
        stderr: (result.stderr || "").trim(),
    };
    if (check && output.code !== 0) {
        throw new Error(output.stderr || output.stdout || `command failed: ${cmd.join(" ")}`);
    }
    return output;
}
function parseJsonOutput(result) {
    const raw = cleanText(result.stdout);
    if (!raw)
        return {};
    return JSON.parse(raw);
}
function extractSessionId(payload) {
    for (const key of ["session_id", "sessionId"]) {
        const value = cleanText(payload[key]);
        if (value)
            return value;
    }
    for (const nestedKey of ["data", "ack", "resolvedTarget"]) {
        const nested = asRecord(payload[nestedKey]);
        if (Object.keys(nested).length > 0) {
            const sessionId = extractSessionId(nested);
            if (sessionId)
                return sessionId;
        }
    }
    return "";
}
function parseArgs(argv) {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const root = path.resolve(here, "..", "..");
    const flags = {
        sessionId: "",
        probeMessage: "",
        expectedSubstring: "",
        timeoutSeconds: 15,
        pollIntervalSeconds: 1,
        historyLimit: 10,
        node: "node",
        sendScript: path.join(root, "message-send", "scripts", "send.js"),
        queryScript: path.join(root, "grix-query", "scripts", "query.js"),
        json: false,
    };
    const take = (i) => {
        const next = argv[i + 1];
        if (next === undefined)
            throw new Error(`Missing value after ${argv[i]}`);
        return [next, i + 1];
    };
    for (let i = 0; i < argv.length; i += 1) {
        const token = argv[i];
        if (token === "--session-id") {
            const [v, j] = take(i);
            flags.sessionId = v;
            i = j;
            continue;
        }
        if (token === "--probe-message") {
            const [v, j] = take(i);
            flags.probeMessage = v;
            i = j;
            continue;
        }
        if (token === "--expected-substring") {
            const [v, j] = take(i);
            flags.expectedSubstring = v;
            i = j;
            continue;
        }
        if (token === "--timeout-seconds") {
            const [v, j] = take(i);
            flags.timeoutSeconds = Number.parseInt(v, 10);
            i = j;
            continue;
        }
        if (token === "--poll-interval-seconds") {
            const [v, j] = take(i);
            flags.pollIntervalSeconds = Number.parseFloat(v);
            i = j;
            continue;
        }
        if (token === "--history-limit") {
            const [v, j] = take(i);
            flags.historyLimit = Number.parseInt(v, 10);
            i = j;
            continue;
        }
        if (token === "--node") {
            const [v, j] = take(i);
            flags.node = v;
            i = j;
            continue;
        }
        if (token === "--send-script") {
            const [v, j] = take(i);
            flags.sendScript = v;
            i = j;
            continue;
        }
        if (token === "--query-script") {
            const [v, j] = take(i);
            flags.queryScript = v;
            i = j;
            continue;
        }
        if (token === "--json") {
            flags.json = true;
            continue;
        }
    }
    return flags;
}
async function main() {
    let flags;
    try {
        flags = parseArgs(process.argv.slice(2));
    }
    catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        process.stderr.write(`${message}\n`);
        return 1;
    }
    try {
        if (!flags.sessionId)
            throw new Error("--session-id is required");
        if (!flags.probeMessage)
            throw new Error("--probe-message is required");
        if (!flags.expectedSubstring)
            throw new Error("--expected-substring is required");
        const env = process.env;
        const sendResult = runCommand([flags.node, flags.sendScript, "--to", flags.sessionId, "--message", flags.probeMessage], env);
        const sendPayload = parseJsonOutput(sendResult);
        const expectedLower = flags.expectedSubstring.toLowerCase();
        const deadline = Date.now() + Math.max(flags.timeoutSeconds, 1) * 1000;
        let lastQuery = {};
        while (Date.now() < deadline) {
            const queryResult = runCommand([
                flags.node,
                flags.queryScript,
                "--action",
                "message_history",
                "--session-id",
                flags.sessionId,
                "--limit",
                String(flags.historyLimit),
            ], env);
            lastQuery = parseJsonOutput(queryResult);
            const haystack = JSON.stringify(lastQuery).toLowerCase();
            if (haystack.includes(expectedLower)) {
                const payload = {
                    ok: true,
                    verified: true,
                    session_id: flags.sessionId,
                    probe_send: sendPayload,
                    query_result: lastQuery,
                };
                if (flags.json) {
                    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
                }
                else {
                    process.stdout.write(`verified=true session_id=${flags.sessionId}\n`);
                }
                return 0;
            }
            await sleep(Math.max(flags.pollIntervalSeconds, 0.1) * 1000);
        }
        throw new Error("Acceptance verification did not observe the expected identity text.\n" +
            `expected: ${flags.expectedSubstring}\n` +
            `last_query: ${JSON.stringify(lastQuery)}`);
    }
    catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (flags.json) {
            process.stderr.write(`${JSON.stringify({ ok: false, error: message }, null, 2)}\n`);
        }
        else {
            process.stderr.write(`${message}\n`);
        }
        return 1;
    }
}
process.exit(await main());
