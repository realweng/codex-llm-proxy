# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A single-file Python proxy (`proxy.py`, stdlib only) that lets the OpenAI Codex CLI talk to **GLM (智谱 AI)** or **Kimi (月之暗面)** by translating between OpenAI's **Responses API** (what Codex sends) and **Chat Completions API** (what GLM/Kimi accept). No build step, no dependencies, no test suite.

## Common commands

```bash
# Run (foreground, for debugging)
python3 proxy.py

# Run (managed; writes /tmp/codex-llm-proxy.{log,pid})
./scripts/start.sh                 # GLM (default)
./scripts/start.sh -p kimi         # Kimi
./scripts/stop.sh

# Health check / live logs
curl http://localhost:18765/health
tail -f /tmp/codex-llm-proxy.log
```

Required env vars: `GLM_API_KEY` or `KIMI_API_KEY` (matching the chosen backend). Optional: `BACKEND` (`glm`|`kimi`), `PROXY_PORT` (default `18765`), `GLM_API_BASE`, `KIMI_API_BASE`.

## Architecture

The proxy is **stateful per request** in streaming mode and **format-translating in both directions**. Two flows:

1. **POST `/v?/responses`** → `handle_responses` → `convert_responses_to_chat` → forward to `<api_base>/chat/completions` → `convert_chat_to_responses` (non-stream) **or** `stream_response` (stream).
2. **Anything else** → `forward_request` (passthrough; strips a `/v4` prefix from the path).

### Backend selection

`BACKENDS` dict in `proxy.py` keys all per-provider state: `api_base`, `api_key`, `model_mapping`, `default_model`. `get_backend()` reads the `BACKEND` env var at request time. Adding a provider means adding a key here and ensuring its `/chat/completions` endpoint is OpenAI-compatible — no other changes needed.

### Request conversion (Responses → Chat)

`convert_responses_to_chat` walks the Responses `input` list and emits Chat `messages`. Three input item types matter:
- `message` → role-mapped message (`developer` → `system` for GLM compatibility); `input_text` blocks are joined, `input_image` blocks are dropped.
- `function_call` → assistant message with `tool_calls`. **Consecutive `function_call` items are merged into a single assistant message** — splitting them breaks Kimi's validation.
- `function_call_output` → `tool` message with matching `tool_call_id`.

Tools of type `web_search` / `code_interpreter` / `file_search` / `computer_use` are dropped — backends don't support them.

### `_fix_tool_call_gaps` — load-bearing workaround

Kimi rejects requests where any `tool_call_id` in an assistant message lacks a following `tool` response. When Codex sends a partial tool-call history, this helper inserts empty placeholder `tool` messages so the conversation validates. Don't remove this without checking against Kimi.

### Streaming response (Chat SSE → Responses SSE)

`stream_response` + `convert_stream_line` (the **method**, not the module-level function — there are two with that name) emit the Responses event sequence Codex expects. Per-request state lives on the handler instance: `sequence_number`, `item_id`, `response_id`, `full_content`, `tool_calls` (keyed by index). Order matters:

```
response.created
  → response.output_item.added (message)
  → response.content_part.added
  → response.output_text.delta (×N)
  [if tool_calls:
    → response.output_item.added (function_call, output_index = tc_index + 1)
    → response.function_call_arguments.delta (×N)
    → response.function_call_arguments.done
    → response.output_item.done (function_call)]
  → response.output_text.done
  → response.content_part.done
  → response.output_item.done (message)
  → response.completed
data: [DONE]
```

`response_id` must be prefixed with `resp_` and `sequence_number` must increment monotonically — Codex validates both. Tool-call function_call items use `output_index = tc_index + 1` to sit after the message at index 0.

### Model mapping

Each backend has its own `model_mapping`. Requests with unmapped model names fall through to `default_model`. `gpt-4o` is the recommended Codex-side model for both backends.

## Things to know before editing

- **No tests.** Verify changes by starting the proxy and running `codex exec "..."` end-to-end, watching `/tmp/codex-llm-proxy.log`. Both streaming and tool-calling paths need exercising.
- The module-level `convert_stream_line` is dead code — the streaming path uses `ProxyHandler.convert_stream_line`. Don't confuse them.
- The `User-Agent: claude-cli/...` header in `handle_responses` is intentional — Kimi's coding endpoint gates on a coding-agent UA. Removing it will 4xx Kimi requests.
- Logs include full request/response bodies (truncated to 2–10 KB). Be aware when sharing them.
