#!/usr/bin/env python3
"""
OpenAI Responses API -> Chat Completions API Proxy

Converts the OpenAI Responses API format to Chat Completions format
so that Codex can work with multiple LLM providers (GLM, Kimi, etc.).
"""

import json
import http.server
import socketserver
import http.client
import urllib.request
import urllib.error
import urllib.parse
import os
import sys
import logging
import io
import threading
import time

# Unbuffered stdout for logging
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

# Backend configuration
BACKEND = os.environ.get("BACKEND", "glm")  # "glm" or "kimi"
PROXY_PORT = int(os.environ.get("PROXY_PORT", 18765))
# Per-event SSE logging. Set SSE_LOG=0 to keep only the per-request summary.
SSE_LOG = os.environ.get("SSE_LOG", "1").strip().lower() not in ("0", "false", "no", "off", "")

BACKENDS = {
    "glm": {
        "api_base": os.environ.get("GLM_API_BASE", "https://open.bigmodel.cn/api/coding/paas/v4"),
        "api_key": os.environ.get("GLM_API_KEY", ""),
        "model_mapping": {
            "glm-5": "glm-5",
            "gpt-4": "glm-4",
            "gpt-4-turbo": "glm-4",
            "gpt-4o": "glm-5",
            "gpt-4o-mini": "glm-4-flash",
            "gpt-3.5-turbo": "glm-4-flash",
            "gpt-5.2-codex": "glm-5",
            "gpt-5.3-codex": "glm-5",
        },
        "default_model": "glm-5",
    },
    "kimi": {
        "api_base": os.environ.get("KIMI_API_BASE", "https://api.kimi.com/coding/v1"),
        "api_key": os.environ.get("KIMI_API_KEY", ""),
        "model_mapping": {
            "kimi-for-coding": "kimi-for-coding",
            "gpt-4": "kimi-for-coding",
            "gpt-4-turbo": "kimi-for-coding",
            "gpt-4o": "kimi-for-coding",
            "gpt-4o-mini": "kimi-for-coding",
            "gpt-3.5-turbo": "kimi-for-coding",
            "gpt-5.2-codex": "kimi-for-coding",
            "gpt-5.3-codex": "kimi-for-coding",
        },
        "default_model": "kimi-for-coding",
    },
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("codex-llm-proxy")


def get_backend():
    """Get current backend configuration."""
    if BACKEND not in BACKENDS:
        log.warning(f"Unknown backend '{BACKEND}', falling back to 'glm'")
        return BACKENDS["glm"]
    return BACKENDS[BACKEND]




def _flatten_responses_tools(tools, _warned_types=None):
    """Flatten Responses API tools (namespace, custom, etc.) to Chat Completions format.

    Codex CLI wraps MCP tools in type:'namespace' containers; this recursively
    flattens them so the backend sees plain function tools.
    """
    if _warned_types is None:
        _warned_types = set()
    result = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        ttype = tool.get("type", "")
        if ttype == "function":
            if "function" in tool:
                result.append(tool)
            else:
                chat_tool = {"type": "function", "function": {}}
                if "name" in tool:
                    chat_tool["function"]["name"] = tool["name"]
                if "description" in tool:
                    chat_tool["function"]["description"] = tool["description"]
                if "parameters" in tool:
                    chat_tool["function"]["parameters"] = tool["parameters"]
                result.append(chat_tool)
        elif ttype == "custom":
            chat_tool = {"type": "function", "function": {}}
            chat_tool["function"]["name"] = tool.get("name", "custom_tool")
            chat_tool["function"]["description"] = tool.get("description", "")
            chat_tool["function"]["parameters"] = {
                "type": "object",
                "properties": {
                    "input": {"type": "string", "description": "Input to the tool"}
                },
                "required": ["input"]
            }
            result.append(chat_tool)
        elif ttype == "namespace":
            ns_name = tool.get("name", "")
            inner_tools = tool.get("tools", [])
            for inner in inner_tools:
                if not isinstance(inner, dict):
                    continue
                inner_type = inner.get("type", "")
                if inner_type == "function":
                    chat_tool = {"type": "function", "function": {}}
                    inner_name = inner.get("name", "")
                    if ns_name:
                        chat_tool["function"]["name"] = f"{ns_name}{inner_name}"
                    else:
                        chat_tool["function"]["name"] = inner_name
                    if "description" in inner:
                        chat_tool["function"]["description"] = inner["description"]
                    if "parameters" in inner:
                        chat_tool["function"]["parameters"] = inner["parameters"]
                    result.append(chat_tool)
                elif inner_type == "namespace":
                    result.extend(_flatten_responses_tools([inner], _warned_types))
                else:
                    flattened = _flatten_responses_tools([inner], _warned_types)
                    for ft in flattened:
                        if ns_name and "function" in ft:
                            orig_name = ft["function"].get("name", "")
                            if orig_name:
                                ft["function"]["name"] = f"{ns_name}{orig_name}"
                        result.append(ft)
        elif ttype in ("web_search", "web_search_preview"):
            log.info(f"Skipping web_search tool (provider-specific): {ttype}")
        elif ttype in ("computer_use", "computer_use_preview", "computer_call",
                       "code_interpreter", "code_interpreter_call",
                       "file_search", "file_search_call",
                       "image_generation_call", "local_shell", "mcp"):
            if ttype not in _warned_types:
                _warned_types.add(ttype)
                log.info(f"Skipping unsupported tool type: {ttype}")
        else:
            if "function" in tool:
                result.append(tool)
            elif ttype not in _warned_types:
                _warned_types.add(ttype)
                log.info(f"Skipping unknown tool type: {ttype}")
    return result


def _restore_tool_namespace(tool_name, original_tools):
    """Restore original (name, namespace) from a flattened tool name.

    When namespace tools are flattened, the name becomes {ns}{name}.
    This reverses the mapping so Codex CLI can route the call correctly.
    """
    if not original_tools or not tool_name:
        return tool_name, None
    for tool in original_tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace":
            ns_name = tool.get("name", "")
            inner_tools = tool.get("tools", [])
            for inner in inner_tools:
                if not isinstance(inner, dict):
                    continue
                if inner.get("type") == "function":
                    inner_name = inner.get("name", "")
                    expected = f"{ns_name}{inner_name}" if ns_name else inner_name
                    if expected == tool_name:
                        return inner_name, ns_name
        elif tool.get("type") == "function":
            if tool.get("name") == tool_name:
                return tool_name, None
    return tool_name, None


def _fix_tool_call_gaps(messages):
    """Ensure every assistant tool_call has a corresponding tool response message.

    Kimi strictly validates that each tool_call_id in an assistant message
    must be followed by a tool message, with no other messages in between,
    and that every tool message corresponds to a preceding tool_call.
    """
    # Pass 1: drop orphan tool messages and dedupe duplicate tool responses.
    # - Orphan: tool_call_id has no matching assistant tool_call anywhere earlier.
    # - Duplicate: a second `tool` message for a tool_call_id we've already
    #   kept. Codex re-sends each tool result a second time after the assistant
    #   text turn that follows it, which strands the duplicate after a plain
    #   assistant message and trips Kimi's validation
    #   ("messages with role 'tool' must be a response to a preceding message
    #   with 'tool_calls'").
    valid_call_ids = set()
    seen_tool_responses = set()
    cleaned = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                cid = tc.get("id")
                if cid:
                    valid_call_ids.add(cid)
            cleaned.append(msg)
        elif msg.get("role") == "tool":
            tcid = msg.get("tool_call_id")
            if tcid not in valid_call_ids:
                log.warning(
                    f"_fix_tool_call_gaps: dropping orphan tool message "
                    f"(tool_call_id={tcid!r}, no preceding assistant tool_call)"
                )
            elif tcid in seen_tool_responses:
                log.warning(
                    f"_fix_tool_call_gaps: dropping duplicate tool message "
                    f"(tool_call_id={tcid!r}, already responded to earlier)"
                )
            else:
                cleaned.append(msg)
                seen_tool_responses.add(tcid)
        else:
            cleaned.append(msg)
    messages = cleaned

    # Pass 2: place each tool message immediately after its matching assistant
    # tool_call. We can't just walk linearly and break on the next assistant,
    # because Codex can interleave a plain assistant text message between the
    # tool_call and its tool response (same logical turn). Instead, index all
    # tool messages by tool_call_id and emit them right after their owning
    # assistant. After Pass 1 each id has at most one tool message.
    tools_by_id = {}
    non_tool_msgs = []
    for msg in messages:
        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id")
            if tcid is not None:
                tools_by_id[tcid] = msg
        else:
            non_tool_msgs.append(msg)

    result = []
    placed = set()
    for msg in non_tool_msgs:
        result.append(msg)
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                cid = tc.get("id")
                if cid is None or cid in placed:
                    continue
                placed.add(cid)
                if cid in tools_by_id:
                    result.append(tools_by_id[cid])
                else:
                    log.info(
                        f"_fix_tool_call_gaps: inserting placeholder for "
                        f"missing tool_call_id: {cid}"
                    )
                    result.append({
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": "<no output>"
                    })

    return result


def _extract_content_text(content):
    """Convert Responses API content blocks to Chat Completions format.

    Returns one of:
    - A plain string (single text block only — backward compatible)
    - A list of content parts [{"type":"text",...}, {"type":"image_url",...}]
    - None if no usable content

    Responses `input_image` blocks are converted to Chat Completions
    `image_url` parts so Kimi (and other vision-capable backends) can
    process them.  `input_file` / `output_image` are still skipped.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if not isinstance(c, dict):
                continue
            ctype = c.get("type", "")
            if ctype in ("input_text", "output_text"):
                parts.append({"type": "text", "text": c.get("text", "")})
            elif ctype == "input_image":
                image_url = c.get("image_url")
                if image_url:
                    parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": image_url,
                            "detail": c.get("detail", "auto"),
                        },
                    })
            elif ctype == "image_url":
                # Already in Chat Completions format (e.g. from dict path)
                parts.append(c)
            elif ctype in ("output_image", "input_file"):
                pass
        if not parts:
            return None
        # Single text part → keep as string for backward compatibility
        if len(parts) == 1 and parts[0]["type"] == "text":
            return parts[0]["text"]
        return parts
    return str(content) if content is not None else None


def convert_responses_to_chat(body: dict) -> dict:
    """Convert Responses API format to Chat Completions API format."""
    chat_body = {}

    # Model mapping (uses current backend's mapping table)
    model = body.get("model", "gpt-4")
    backend = get_backend()
    model_mapping = backend["model_mapping"]
    chat_body["model"] = model_mapping.get(model, backend["default_model"])

    messages = []

    # Convert instructions to system message
    if "instructions" in body and body["instructions"]:
        messages.append({"role": "system", "content": body["instructions"]})

    # Convert input to messages
    if "input" in body:
        inp = body["input"]
        if isinstance(inp, str):
            messages.append({"role": "user", "content": inp})
        elif isinstance(inp, list):
            # Responses API format: list of message objects.
            # `pending_reasoning` captures `reasoning` items so they can be
            # attached to the next assistant message — Kimi's coding endpoint
            # has thinking enabled and rejects assistant tool_call messages
            # that lack `reasoning_content`.
            pending_reasoning = None
            for item in inp:
                if isinstance(item, dict) and "type" in item:
                    if item["type"] == "reasoning":
                        # Capture summary text for the next assistant message.
                        # `encrypted_content` is OpenAI-specific and opaque to
                        # other providers, so we ignore it.
                        summary = item.get("summary", [])
                        parts = []
                        if isinstance(summary, list):
                            for s in summary:
                                if isinstance(s, dict) and s.get("type") == "summary_text":
                                    parts.append(s.get("text", ""))
                        pending_reasoning = "\n".join(parts)

                    elif item["type"] == "message":
                        role = item.get("role", "user")
                        # Map "developer" to "system" for GLM compatibility
                        if role == "developer":
                            role = "system"

                        content = item.get("content", [])
                        extracted = _extract_content_text(content)
                        msg_dict = {"role": role, "content": extracted} if extracted is not None else None

                        if msg_dict is not None:
                            if role == "assistant" and pending_reasoning:
                                msg_dict["reasoning_content"] = pending_reasoning
                            messages.append(msg_dict)
                        pending_reasoning = None

                    elif item["type"] == "function_call":
                        # This is a historical tool call from the model
                        # Convert to assistant message with tool_calls
                        call_id = item.get("call_id", item.get("id", ""))
                        name = item.get("name", "")
                        arguments = item.get("arguments", "{}")

                        # If previous message was also an assistant with tool_calls,
                        # merge into it instead of creating a new message
                        if (messages and messages[-1].get("role") == "assistant"
                                and messages[-1].get("tool_calls")):
                            messages[-1]["tool_calls"].append({
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": arguments
                                }
                            })
                            if "reasoning_content" not in messages[-1]:
                                messages[-1]["reasoning_content"] = pending_reasoning or ""
                        else:
                            messages.append({
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": pending_reasoning or "",
                                "tool_calls": [{
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": arguments
                                    }
                                }]
                            })
                        pending_reasoning = None

                    elif item["type"] == "function_call_output":
                        # This is the result of a tool call
                        # Convert to tool message
                        # Codex may send either `call_id` or `id` - try both
                        call_id = item.get("call_id", item.get("id", ""))
                        output = item.get("output", "")

                        # Tool output may be a content array (e.g. browser tool
                        # returning input_image blocks). Extract text, or fall
                        # back to empty string so content is always a string.
                        extracted = _extract_content_text(output)
                        if isinstance(extracted, list):
                            # Tool messages can't have array content;
                            # pull out any text parts and join them.
                            text_parts = [
                                p.get("text", "")
                                for p in extracted
                                if isinstance(p, dict) and p.get("type") == "text"
                            ]
                            extracted = " ".join(text_parts) if text_parts else ""
                        elif extracted is None:
                            extracted = ""

                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": extracted
                        })
                        pending_reasoning = None

                    elif item["type"] in ("computer_call", "code_interpreter_call",
                                           "file_search_call", "web_search_call",
                                           "image_generation_call"):
                        # Responses-native tool calls that the backend doesn't
                        # support. Convert to a placeholder user message so the
                        # conversation history stays coherent.
                        placeholder = f"[{item['type']}]"
                        status = item.get("status", "")
                        if status:
                            placeholder += f" status={status}"
                        messages.append({"role": "user", "content": placeholder})
                        pending_reasoning = None

        elif isinstance(inp, dict):
            if "messages" in inp:
                for msg in inp["messages"]:
                    role = msg.get("role", "user")
                    if role == "developer":
                        role = "system"
                    # Content may be a string or an array of content blocks
                    # (e.g. from Codex Desktop's browser tool sending input_image).
                    extracted = _extract_content_text(msg.get("content", ""))
                    msg_dict = {"role": role}
                    if extracted is not None:
                        msg_dict["content"] = extracted
                    # Preserve tool_calls / tool_call_id if already in Chat
                    # Completions format (Codex Desktop may send them directly).
                    if msg.get("tool_calls"):
                        msg_dict["tool_calls"] = msg["tool_calls"]
                    if msg.get("tool_call_id"):
                        msg_dict["tool_call_id"] = msg["tool_call_id"]
                    messages.append(msg_dict)
            elif "content" in inp:
                extracted = _extract_content_text(inp["content"])
                if extracted is not None:
                    messages.append({"role": "user", "content": extracted})

    # Validate message sequence: ensure every tool_call has a matching tool response.
    # Some providers (Kimi) strictly require this. If a tool_call lacks a response,
    # insert a placeholder tool message.
    messages = _fix_tool_call_gaps(messages)

    chat_body["messages"] = messages

    # Pass through other fields
    for key in ["temperature", "top_p", "max_tokens", "stream", "frequency_penalty", "presence_penalty", "stop"]:
        if key in body:
            chat_body[key] = body[key]

    # Handle tools - convert Responses API format to Chat Completions format
    if "tools" in body:
        chat_tools = _flatten_responses_tools(body["tools"])
        if chat_tools:
            chat_body["tools"] = chat_tools
            log.info(f"Converted tools: {len(chat_tools)} tools (from {len(body['tools'])} original)")

    if "tool_choice" in body:
        chat_body["tool_choice"] = body["tool_choice"]

    # Handle reasoning/extended thinking
    if "reasoning" in body:
        # GLM may not support this, but pass it through
        chat_body["reasoning"] = body["reasoning"]

    return chat_body


def convert_chat_to_responses(response_body: dict, is_stream: bool, original_tools=None) -> dict:
    """Convert Chat Completions response back to Responses format."""
    if is_stream:
        # For streaming, the format is similar but with different event types
        return response_body

    # Responses API format:
    # {
    #   "id": "resp_xxx",
    #   "object": "response",
    #   "output": [
    #     {
    #       "type": "message",
    #       "id": "msg_xxx",
    #       "status": "completed",
    #       "role": "assistant",
    #       "content": [
    #         {"type": "output_text", "text": "..."}
    #       ]
    #     }
    #   ],
    #   "usage": {...}
    # }
    
    outputs = []
    if "choices" in response_body:
        for choice in response_body["choices"]:
            msg = choice.get("message", {})
            content_text = msg.get("content", "")
            
            # Build content array
            content = []
            if content_text:
                content.append({
                    "type": "output_text",
                    "text": content_text
                })
            
            # Handle tool calls
            if "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    raw_name = tc.get("function", {}).get("name", "")
                    restored_name, ns = _restore_tool_namespace(raw_name, original_tools)
                    tool_call_item = {
                        "type": "tool_call",
                        "id": tc.get("id", ""),
                        "call_id": tc.get("id", ""),
                        "name": restored_name,
                        "arguments": tc.get("function", {}).get("arguments", "{}")
                    }
                    if ns:
                        tool_call_item["namespace"] = ns
                    content.append(tool_call_item)
            
            output_item = {
                "type": "message",
                "id": f"msg_{response_body.get('id', '')}",
                "status": "completed",
                "role": msg.get("role", "assistant"),
                "content": content,
            }
            
            outputs.append(output_item)
    
    responses_body = {
        "id": response_body.get("id", ""),
        "object": "response",
        "created": response_body.get("created", 0),
        "model": response_body.get("model", ""),
        "output": outputs,
        "usage": response_body.get("usage", {}),
        "status": "completed",
    }

    return responses_body


def convert_stream_line(line: bytes) -> bytes:
    """Convert a single SSE line from Chat to Responses format."""
    if not line.startswith(b"data: "):
        return line

    data = line[6:].strip()
    if data == b"[DONE]":
        return b"data: [DONE]\n\n"

    try:
        chunk = json.loads(data)

        # Transform the chunk format
        response_chunk = {
            "id": chunk.get("id", ""),
            "object": "response.chunk",
            "created": chunk.get("created", 0),
            "model": chunk.get("model", ""),
            "output": []
        }

        if "choices" in chunk:
            for choice in chunk["choices"]:
                delta = choice.get("delta", {})
                response_chunk["output"].append({
                    "index": choice.get("index", 0),
                    "delta": delta,
                    "finish_reason": choice.get("finish_reason"),
                })

        return f"data: {json.dumps(response_chunk)}\n\n".encode()
    except json.JSONDecodeError:
        return line


def _extract_event_type(sse_bytes: bytes) -> str:
    """Extract the SSE event name from a raw event payload for logging."""
    if sse_bytes.startswith(b"event: "):
        nl = sse_bytes.find(b"\n")
        if nl > 0:
            return sse_bytes[7:nl].decode("utf-8", errors="replace").strip()
    if sse_bytes.startswith(b"data: [DONE]"):
        return "[DONE]"
    if sse_bytes.startswith(b"data:"):
        return "data"
    if sse_bytes.startswith(b":"):
        return "comment"
    return "unknown"


def _message_summary(messages: list) -> str:
    """One-line role/tool_call_id summary of a chat-completions messages array,
    for log scanning when full bodies are truncated.
    """
    parts = []
    for m in messages:
        role = m.get("role", "?")
        if role == "assistant" and m.get("tool_calls"):
            ids = ",".join(tc.get("id", "?") for tc in m["tool_calls"])
            parts.append(f"[asst tools={ids}]")
        elif role == "tool":
            parts.append(f"[tool {m.get('tool_call_id', '?')}]")
        else:
            parts.append(f"[{role}]")
    return " ".join(parts)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Thread-per-request HTTP server."""
    daemon_threads = True
    allow_reuse_address = True


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        log.info(format, *args)

    def handle_error(self):
        """Suppress scary tracebacks for routine client disconnects."""
        cls, exc = sys.exc_info()[:2]
        if cls in (BrokenPipeError, ConnectionResetError):
            log.info(f"Client disconnected ({exc})")
            return
        # Everything else falls back to the default handler (prints traceback).
        super().handle_error()

    def do_GET(self):
        """Handle health checks."""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        elif self.path == "/v4/models" or self.path == "/v1/models":
            self.handle_models()
        else:
            self.send_response(404)
            self.send_header("Connection", "close")
            self.end_headers()

    def do_POST(self):
        """Handle POST requests - main proxy logic."""
        if self.path.endswith("/responses"):
            self.handle_responses()
        elif self.path.endswith("/chat/completions"):
            self.forward_request("POST")
        else:
            self.forward_request("POST")

    def handle_responses(self):
        """Convert Responses API to Chat Completions and proxy."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body_data = self.rfile.read(content_length)
            body = json.loads(body_data)

            # Convert to Chat Completions format
            chat_body = convert_responses_to_chat(body)
            is_stream = body.get("stream", False)
            self._original_tools = body.get("tools", [])  # Save for namespace restore

            log.info(f"Stream mode: {is_stream}")
            log.info(f"Message structure: {_message_summary(chat_body.get('messages', []))}")
            log.info(f"Converted chat_body: {json.dumps(chat_body, ensure_ascii=False, indent=2)[:2000]}")

            # Forward to backend
            backend = get_backend()
            api_base = backend["api_base"]
            api_key = backend["api_key"]

            # Build headers.  We intentionally strip Codex CLI identity headers
            # (originator, x-codex-*, x-openai-*, chatgpt-account-id, session_id,
            # thread_id) so the backend sees a neutral third-party request.
            # Kimi's coding endpoint gates on a coding-agent User-Agent, so we
            # keep a minimal one only for that backend.
            req_headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Accept": "text/event-stream" if is_stream else "application/json",
            }
            if BACKEND == "kimi":
                req_headers["User-Agent"] = "KimiCLI/1.40.0"

            url_parts = urllib.parse.urlparse(api_base)
            conn = http.client.HTTPSConnection(url_parts.netloc, timeout=120)

            log.info(f"Forwarding to {BACKEND}: {api_base}/chat/completions (stream={is_stream})")
            
            try:
                conn.request("POST", f"{url_parts.path}/chat/completions", 
                            body=json.dumps(chat_body).encode(), headers=req_headers)
                glm_resp = conn.getresponse()
                log.info(f"Backend response status: {glm_resp.status} {glm_resp.reason}")

                if glm_resp.status != 200:
                    error_body = glm_resp.read().decode()
                    log.error(f"Backend error response: {glm_resp.status} - {error_body}")
                    self.send_response(glm_resp.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(error_body.encode())
                    return

                if is_stream:
                    self.stream_response(glm_resp)
                else:
                    response_body = json.loads(glm_resp.read())
                    log.info(f"Backend response: {json.dumps(response_body, ensure_ascii=False)[:2000]}")
                    converted = convert_chat_to_responses(response_body, False, self._original_tools)
                    log.info(f"Converted response: {json.dumps(converted, ensure_ascii=False)[:2000]}")

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(json.dumps(converted).encode())
            finally:
                conn.close()

        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            log.error(f"Backend API error: {e.code} - {error_body}")
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(error_body.encode())

        except (BrokenPipeError, ConnectionResetError) as e:
            log.info(f"Client disconnected during request handling: {e}")

        except Exception as e:
            log.error(f"Proxy error: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def stream_response(self, glm_response):
        """Handle streaming SSE response from GLM and convert to Responses format."""
        # Reset state for this request
        self.sequence_number = 0
        self.item_id = None
        self.response_id = None
        self.created_at = None
        self.model = None
        self.full_content = ""
        self.content_part_id = None
        self.tool_calls = {}  # Track tool calls by index
        self.current_tool_index = 0
        self._done_events_sent = False  # Track if finish events were already sent
        
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        log.info("Starting streaming response...")
        upstream_count = 0
        downstream_count = 0
        event_type_counts = {}
        stream_done = threading.Event()

        # Start a keep-alive thread that sends SSE comments every 3 seconds
        # to prevent client-side idle timeouts. Some clients (Codex CLI) have
        # short SSE idle timeouts; keep-alive comments keep the connection alive.
        def keepalive():
            while not stream_done.is_set():
                try:
                    time.sleep(3)
                    if stream_done.is_set():
                        break
                    self.wfile.write(b":\n\n")
                    self.wfile.flush()
                except Exception:
                    break

        keepalive_thread = threading.Thread(target=keepalive, daemon=True)
        keepalive_thread.start()

        try:
            # Wrap the raw response in a BufferedReader for efficient line reading
            raw = glm_response
            if not hasattr(raw, 'readline'):
                raw = io.BufferedReader(raw)

            wfile = self.wfile
            while True:
                line = raw.readline()
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                upstream_count += 1
                if SSE_LOG:
                    log.info(
                        f"SSE upstream #{upstream_count}: "
                        f"{line[:1500].decode('utf-8', errors='replace')}"
                    )

                converted_lines = self.convert_stream_line(line)
                for converted in converted_lines:
                    wfile.write(converted)
                    downstream_count += 1
                    event_type = _extract_event_type(converted)
                    event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
                    if SSE_LOG:
                        log.info(
                            f"SSE downstream #{downstream_count} [{event_type}]: "
                            f"{converted[:800].decode('utf-8', errors='replace').rstrip()}"
                        )
                wfile.flush()

            log.info(
                f"Streaming complete: {upstream_count} upstream chunks -> "
                f"{downstream_count} downstream events; type breakdown={event_type_counts}"
            )

        except (BrokenPipeError, ConnectionResetError) as e:
            log.info(f"Client disconnected during stream: {e}")
        except Exception as e:
            log.error(f"Streaming error: {e}")
        finally:
            stream_done.set()
            keepalive_thread.join(timeout=2)

    def convert_stream_line(self, line: bytes) -> list:
        """Convert a single SSE line from Chat Completions to Responses format.
        
        Returns a list of SSE lines to send.
        """
        results = []
        
        line_str = line.decode() if isinstance(line, bytes) else line
        if not line_str.startswith("data:"):
            return [line + b"\n"]

        data = line_str[5:].strip()
        if data == "[DONE]":
            # If done events were never sent (no finish_reason in chunks), send them now
            if not self._done_events_sent and self.item_id:
                self._done_events_sent = True
                # Send output_text.done
                done_event = {
                    "type": "response.output_text.done",
                    "sequence_number": self.sequence_number,
                    "output_index": 0,
                    "content_index": 0,
                    "item_id": self.item_id,
                    "text": self.full_content
                }
                self.sequence_number += 1
                results.append(f"event: response.output_text.done\ndata: {json.dumps(done_event)}\n\n".encode())
                # Send content_part.done
                cp_done = {
                    "type": "response.content_part.done",
                    "sequence_number": self.sequence_number,
                    "output_index": 0,
                    "content_index": 0,
                    "item_id": self.item_id,
                    "content_part": {"type": "output_text", "text": self.full_content}
                }
                self.sequence_number += 1
                results.append(f"event: response.content_part.done\ndata: {json.dumps(cp_done)}\n\n".encode())
                # Send output_item.done for message
                item_done = {
                    "type": "response.output_item.done",
                    "sequence_number": self.sequence_number,
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "id": self.item_id,
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": self.full_content}] if self.full_content else []
                    }
                }
                self.sequence_number += 1
                results.append(f"event: response.output_item.done\ndata: {json.dumps(item_done)}\n\n".encode())
                # Send tool done events if any
                for tc_index, tc_data in self.tool_calls.items():
                    tc_id = tc_data["id"]
                    # function_call_arguments.done
                    fca_done = {
                        "type": "response.function_call_arguments.done",
                        "sequence_number": self.sequence_number,
                        "output_index": tc_index + 1,
                        "item_id": f"fc_{tc_id}",
                        "arguments": tc_data["arguments"],
                        "call_id": tc_id
                    }
                    self.sequence_number += 1
                    results.append(f"event: response.function_call_arguments.done\ndata: {json.dumps(fca_done)}\n\n".encode())
                    # output_item.done for function_call
                    fc_done = {
                        "type": "response.output_item.done",
                        "sequence_number": self.sequence_number,
                        "output_index": tc_index + 1,
                        "item": {
                            "type": "function_call",
                            "id": f"fc_{tc_id}",
                            "call_id": tc_id,
                            "name": tc_data["name"],
                            "arguments": tc_data["arguments"],
                            "status": "completed"
                        }
                    }
                    self.sequence_number += 1
                    results.append(f"event: response.output_item.done\ndata: {json.dumps(fc_done)}\n\n".encode())

            # Build output array for completed event
            # Always include a message item, even if content is empty
            outputs = []
            if self.item_id:
                outputs.append({
                    "type": "message",
                    "id": self.item_id,
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": self.full_content}] if self.full_content else []
                })
            for tc_index, tc_data in self.tool_calls.items():
                fc_out = {
                    "type": "function_call",
                    "id": f"fc_{tc_data['id']}",
                    "call_id": tc_data["id"],
                    "name": tc_data["name"],
                    "arguments": tc_data["arguments"],
                    "status": "completed"
                }
                if tc_data.get("namespace"):
                    fc_out["namespace"] = tc_data["namespace"]
                outputs.append(fc_out)

            if self.response_id:
                completed_event = {
                    "type": "response.completed",
                    "sequence_number": self.sequence_number,
                    "response": {
                        "id": self.response_id,
                        "object": "response",
                        "created_at": self.created_at or 0,
                        "model": self.model or "",
                        "output": outputs,
                        "status": "completed"
                    }
                }
                self.sequence_number += 1
                results.append(f"event: response.completed\ndata: {json.dumps(completed_event)}\n\n".encode())

            results.append(b"data: [DONE]\n\n")
            return results

        try:
            chunk = json.loads(data)
            
            # Store response metadata from first chunk
            if not self.item_id:
                self.response_id = chunk.get("id", "")
                # Ensure ID format matches OpenAI's format
                if not self.response_id.startswith("resp_"):
                    self.response_id = f"resp_{self.response_id}"
                self.created_at = chunk.get("created", 0)
                self.model = chunk.get("model", "")
                self.item_id = f"msg_{self.response_id}"
                self.content_part_id = f"cp_{self.response_id}"
                
                # Send response.created event
                created_event = {
                    "type": "response.created",
                    "sequence_number": self.sequence_number,
                    "response": {
                        "id": self.response_id,
                        "object": "response",
                        "created_at": self.created_at,
                        "model": self.model,
                        "output": [],
                        "status": "in_progress"
                    }
                }
                self.sequence_number += 1
                results.append(f"event: response.created\ndata: {json.dumps(created_event)}\n\n".encode())

                # Send response.in_progress event
                in_progress_event = {
                    "type": "response.in_progress",
                    "sequence_number": self.sequence_number,
                    "response": {
                        "id": self.response_id,
                        "object": "response",
                        "created_at": self.created_at,
                        "model": self.model,
                        "output": [],
                        "status": "in_progress"
                    }
                }
                self.sequence_number += 1
                results.append(f"event: response.in_progress\ndata: {json.dumps(in_progress_event)}\n\n".encode())

                # Send output_item.added event
                item_added_event = {
                    "type": "response.output_item.added",
                    "sequence_number": self.sequence_number,
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "id": self.item_id,
                        "status": "in_progress",
                        "role": "assistant",
                        "content": []
                    }
                }
                self.sequence_number += 1
                results.append(f"event: response.output_item.added\ndata: {json.dumps(item_added_event)}\n\n".encode())
                
                # Send content_part.added event
                content_part_event = {
                    "type": "response.content_part.added",
                    "sequence_number": self.sequence_number,
                    "output_index": 0,
                    "content_index": 0,
                    "item_id": self.item_id,
                    "content_part": {
                        "type": "output_text",
                        "text": ""
                    }
                }
                self.sequence_number += 1
                results.append(f"event: response.content_part.added\ndata: {json.dumps(content_part_event)}\n\n".encode())

            if "choices" in chunk:
                for choice in chunk["choices"]:
                    delta = choice.get("delta", {})
                    content = delta.get("content", "")
                    finish_reason = choice.get("finish_reason")
                    
                    if content:
                        # Send response.output_text.delta event
                        self.full_content += content
                        delta_event = {
                            "type": "response.output_text.delta",
                            "sequence_number": self.sequence_number,
                            "output_index": 0,
                            "content_index": 0,
                            "item_id": self.item_id,
                            "delta": content,
                            "logprobs": []  # Required field
                        }
                        self.sequence_number += 1
                        results.append(f"event: response.output_text.delta\ndata: {json.dumps(delta_event)}\n\n".encode())
                    
                    # Handle tool calls in delta
                    if "tool_calls" in delta:
                        for tc in delta["tool_calls"]:
                            tc_index = tc.get("index", 0)
                            tc_id = tc.get("id", "")
                            tc_function = tc.get("function", {})
                            tc_name = tc_function.get("name", "")
                            tc_args = tc_function.get("arguments", "")

                            # Restore namespace before storing
                            ns = None
                            if getattr(self, "_original_tools", None):
                                tc_name, ns = _restore_tool_namespace(tc_name, self._original_tools)

                            # If this is a new tool call, send output_item.added event
                            if tc_index not in self.tool_calls:
                                self.tool_calls[tc_index] = {
                                    "id": tc_id,
                                    "name": tc_name,
                                    "arguments": "",
                                    "namespace": ns
                                }

                                # Send function_call item added event
                                fc_item = {
                                    "type": "function_call",
                                    "id": f"fc_{tc_id}",
                                    "call_id": tc_id,
                                    "name": tc_name,
                                    "arguments": "",
                                    "status": "in_progress"
                                }
                                if ns:
                                    fc_item["namespace"] = ns
                                tool_item_event = {
                                    "type": "response.output_item.added",
                                    "sequence_number": self.sequence_number,
                                    "output_index": tc_index + 1,  # After text output
                                    "item": fc_item
                                }
                                self.sequence_number += 1
                                results.append(f"event: response.output_item.added\ndata: {json.dumps(tool_item_event)}\n\n".encode())
                            
                            # Send function_call_arguments.delta event
                            if tc_args:
                                self.tool_calls[tc_index]["arguments"] += tc_args
                                tool_delta_event = {
                                    "type": "response.function_call_arguments.delta",
                                    "sequence_number": self.sequence_number,
                                    "output_index": tc_index + 1,
                                    "item_id": f"fc_{tc_id}",
                                    "delta": tc_args,
                                    "call_id": tc_id
                                }
                                self.sequence_number += 1
                                results.append(f"event: response.function_call_arguments.delta\ndata: {json.dumps(tool_delta_event)}\n\n".encode())
                    
                    if finish_reason:
                        self._done_events_sent = True
                        # If there are tool calls, send done events for them
                        if finish_reason == "tool_calls" and self.tool_calls:
                            for tc_index, tc_data in self.tool_calls.items():
                                tc_id = tc_data["id"]
                                tc_name = tc_data["name"]
                                tc_args = tc_data["arguments"]
                                
                                # Send function_call_arguments.done event
                                tool_done_event = {
                                    "type": "response.function_call_arguments.done",
                                    "sequence_number": self.sequence_number,
                                    "output_index": tc_index + 1,
                                    "item_id": f"fc_{tc_id}",
                                    "arguments": tc_args,
                                    "call_id": tc_id
                                }
                                self.sequence_number += 1
                                results.append(f"event: response.function_call_arguments.done\ndata: {json.dumps(tool_done_event)}\n\n".encode())
                                
                                # Send output_item.done for function_call
                                fc_done_item = {
                                    "type": "function_call",
                                    "id": f"fc_{tc_id}",
                                    "call_id": tc_id,
                                    "name": tc_name,
                                    "arguments": tc_args,
                                    "status": "completed"
                                }
                                if tc_data.get("namespace"):
                                    fc_done_item["namespace"] = tc_data["namespace"]
                                tool_item_done = {
                                    "type": "response.output_item.done",
                                    "sequence_number": self.sequence_number,
                                    "output_index": tc_index + 1,
                                    "item": fc_done_item
                                }
                                self.sequence_number += 1
                                results.append(f"event: response.output_item.done\ndata: {json.dumps(tool_item_done)}\n\n".encode())
                        
                        # Send output_text.done event (always, even if empty)
                        done_event = {
                            "type": "response.output_text.done",
                            "sequence_number": self.sequence_number,
                            "output_index": 0,
                            "content_index": 0,
                            "item_id": self.item_id,
                            "text": self.full_content
                        }
                        self.sequence_number += 1
                        results.append(f"event: response.output_text.done\ndata: {json.dumps(done_event)}\n\n".encode())

                        # Send content_part.done event (always, even if empty)
                        content_done_event = {
                            "type": "response.content_part.done",
                            "sequence_number": self.sequence_number,
                            "output_index": 0,
                            "content_index": 0,
                            "item_id": self.item_id,
                            "content_part": {
                                "type": "output_text",
                                "text": self.full_content
                            }
                        }
                        self.sequence_number += 1
                        results.append(f"event: response.content_part.done\ndata: {json.dumps(content_done_event)}\n\n".encode())
                        
                        # Send output_item.done event for message
                        # Always send this, even if content is empty (e.g. tool-only response)
                        if self.item_id:
                            item_done_event = {
                                "type": "response.output_item.done",
                                "sequence_number": self.sequence_number,
                                "output_index": 0,
                                "item": {
                                    "type": "message",
                                    "id": self.item_id,
                                    "status": "completed",
                                    "role": "assistant",
                                    "content": [{
                                        "type": "output_text",
                                        "text": self.full_content
                                    }] if self.full_content else []
                                }
                            }
                            self.sequence_number += 1
                            results.append(f"event: response.output_item.done\ndata: {json.dumps(item_done_event)}\n\n".encode())

            return results
            
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse chunk: {e}, line: {line}")
            return [line + b"\n"]

    def handle_models(self):
        """Return a model list that exposes the real backend model names."""
        backend = get_backend()
        model_mapping = backend["model_mapping"]
        default_model = backend["default_model"]

        seen = set()
        data = []

        # First, expose the real backend model names so Codex can select them
        for backend_name in sorted(set(model_mapping.values())):
            if backend_name not in seen:
                seen.add(backend_name)
                data.append({
                    "id": backend_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": BACKEND,
                })

        # Then add OpenAI aliases with mapping info in owned_by
        for openai_name, backend_name in sorted(model_mapping.items()):
            if openai_name not in seen:
                seen.add(openai_name)
                data.append({
                    "id": openai_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": f"{BACKEND}:{backend_name}",
                })

        # Ensure default model is listed
        if default_model not in seen:
            data.append({
                "id": default_model,
                "object": "model",
                "created": 0,
                "owned_by": BACKEND,
            })

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(json.dumps({"object": "list", "data": data}).encode())

    def forward_request(self, method):
        """Forward request directly without conversion."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            backend = get_backend()
            api_base = backend["api_base"]
            api_key = backend["api_key"]

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }

            path = self.path
            if path.startswith("/v4/"):
                path = path[3:]  # Remove /v4 prefix

            req = urllib.request.Request(
                f"{api_base}{path}",
                data=body,
                headers=headers,
                method=method,
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                response_body = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.end_headers()
                self.wfile.write(response_body)

        except Exception as e:
            log.error(f"Forward error: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())


def main():
    backend = get_backend()
    if not backend["api_key"]:
        key_env = "GLM_API_KEY" if BACKEND == "glm" else "KIMI_API_KEY"
        log.error(f"{key_env} environment variable is required (backend={BACKEND})")
        sys.exit(1)

    with ThreadingHTTPServer(("", PROXY_PORT), ProxyHandler) as httpd:
        log.info(f"Codex LLM proxy running on port {PROXY_PORT}")
        log.info(f"Backend: {BACKEND} | API base: {backend['api_base']}")
        log.info("Press Ctrl+C to stop")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("Shutting down...")


if __name__ == "__main__":
    main()
