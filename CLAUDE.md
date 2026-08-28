# Project Guide

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

grix-hermes is a Python platform-adapter plugin that bridges the Grix/aibot protocol to the [Hermes Agent](https://github.com/nicobailon/hermes-agent). It is the **Python port of grix-connector** (`/Volumes/disk1/go/src/grix-connector`, Node.js/TypeScript): it speaks the same Grix WebSocket/aibot protocol — accepting chat events, dispatching to the agent, and streaming responses back — but packaged as a Hermes plugin (`plugin.yaml`, entry point `grix_hermes`).

Core code lives in `grix_hermes/`: `transport.py` / `protocol.py` / `http_client.py` (Grix protocol + I/O), `adapter.py` (platform adapter), and the various `*_tool.py` / card modules (skills and tools mirrored from the connector).

## Sync discipline with grix-connector (upstream reference)

**grix-connector is the reference implementation; grix-hermes must track it, not diverge from it.** When working in this repo:

1. **Treat grix-connector as the source of truth.** Before changing protocol handling, message flow, session control, adapter semantics, tools, or config, read how grix-connector does it and match that behavior. Functionality between the two must stay in sync.
2. **No destructive / behavior-breaking changes here on your own.** Bug fixes, Python-idiomatic refactors, and packaging changes that preserve behavior are fine. Do not unilaterally change or drop a feature's behavior in a way that breaks parity with the connector.
3. **Only request a connector-side change when genuinely necessary.** If parity can only be achieved by changing the connector (e.g. a shared protocol gap, or the connector itself is wrong), surface it and request the connector change so both sides stay in sync — rather than forking behavior locally.

The connector documents the reciprocal rule: any connector change must be mirrored here. The intent on both sides is one behavior, two implementations.
