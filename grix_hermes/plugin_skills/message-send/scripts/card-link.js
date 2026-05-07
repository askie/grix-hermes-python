#!/usr/bin/env node
import { runCli } from "../../shared/cli/card-links.js";
try {
    runCli(process.argv.slice(2));
}
catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`${message}\n`);
    process.exit(1);
}
