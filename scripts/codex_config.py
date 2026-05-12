#!/usr/bin/env python3
"""
Snapshot / apply / restore ~/.codex/config.toml lifecycle for codex-llm-proxy.

Mimics the behavior of `codex-app-transfer` (autoApplyOnStart + restoreCodexOnExit):
- apply  : take a snapshot of ~/.codex/config.toml, generate a model catalog,
           and rewrite the config so Codex talks to our proxy (default :18765).
- restore: copy the snapshot back over ~/.codex/config.toml.

If `Codex App Transfer.app` is detected running, `apply` is skipped so the two
tools don't fight over the same file. `restore` still proceeds (idempotent).

stdlib only — no third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
CODEX_CONFIG = HOME / ".codex" / "config.toml"
STATE_DIR = HOME / ".codex-llm-proxy"
SNAPSHOT = STATE_DIR / "codex-config.snapshot.toml"
CATALOG = STATE_DIR / "model-catalog.json"
MARKER = STATE_DIR / "applied.txt"

MANAGED_TOP_KEYS = ("model_provider", "model", "openai_base_url", "model_catalog_json")
# Sections we strip on every apply. We never write [model_providers.openai] —
# Codex Desktop reserves the built-in `openai` provider ID and refuses to start
# any conversation if a user [model_providers.openai] section exists. We also
# strip our own managed [model_providers.codex-llm-proxy] so re-applies are
# idempotent.
MANAGED_SECTIONS = ("model_providers.openai", "model_providers.codex-llm-proxy")
PROVIDER_NAME = "codex-llm-proxy"


def codex_app_transfer_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "Codex App Transfer"],
            capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


REASONING_LEVELS_FULL = [
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
    {"effort": "high", "description": "Greater reasoning depth for complex problems"},
    {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
]


def _model_entry(*, slug, display_name, priority, default_reasoning, default_verbosity,
                 apply_patch_tool_type, web_search_tool_type,
                 additional_speed_tiers=None, availability_nux=None, upgrade=None,
                 shell_type="shell_command", truncation_mode="tokens",
                 truncation_limit=10000, supports_image_detail_original=True,
                 supports_reasoning_summaries=True, support_verbosity=True,
                 supports_search_tool=True, supports_parallel_tool_calls=True,
                 default_reasoning_summary="none"):
    return {
        "slug": slug,
        "display_name": display_name,
        "description": f"Routed through codex-llm-proxy as {display_name}.",
        "default_reasoning_level": default_reasoning,
        "supported_reasoning_levels": REASONING_LEVELS_FULL,
        "shell_type": shell_type,
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "additional_speed_tiers": additional_speed_tiers or [],
        "availability_nux": availability_nux,
        "upgrade": upgrade,
        "base_instructions": "",
        "supports_reasoning_summaries": supports_reasoning_summaries,
        "default_reasoning_summary": default_reasoning_summary,
        "support_verbosity": support_verbosity,
        "default_verbosity": default_verbosity,
        "apply_patch_tool_type": apply_patch_tool_type,
        "web_search_tool_type": web_search_tool_type,
        "truncation_policy": {"mode": truncation_mode, "limit": truncation_limit},
        "supports_parallel_tool_calls": supports_parallel_tool_calls,
        "supports_image_detail_original": supports_image_detail_original,
        "context_window": 258400,
        "max_context_window": 258400,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"],
        "supports_search_tool": supports_search_tool,
        "auto_compact_token_limit": 193800,
    }


def build_model_catalog(backend: str) -> dict:
    if backend == "glm":
        display_name = "GLM / glm-5.1"
        native_slug = "glm-5.1"
    elif backend == "kimi":
        display_name = "Kimi Code / kimi-for-coding"
        native_slug = "kimi-for-coding"
    else:
        display_name = f"codex-llm-proxy / {backend}"
        native_slug = backend

    gpt55_nux = {
        "message": (
            "GPT-5.5 is now available in Codex. It's our strongest agentic coding "
            "model yet, built to reason through large codebases, check assumptions "
            "with tools, and keep going until the work is done.\n\n"
            "Routed via codex-llm-proxy.\n"
        )
    }
    upgrade_to_54 = {
        "model": "gpt-5.4",
        "migration_markdown": "Introducing GPT-5.4 (routed via codex-llm-proxy).\n",
    }

    models = [
        _model_entry(slug="gpt-5.5", display_name=display_name, priority=0,
                     default_reasoning="medium", default_verbosity="low",
                     apply_patch_tool_type="freeform",
                     web_search_tool_type="text_and_image",
                     additional_speed_tiers=["fast"], availability_nux=gpt55_nux),
        _model_entry(slug="gpt-5.4", display_name=display_name, priority=2,
                     default_reasoning="xhigh", default_verbosity="low",
                     apply_patch_tool_type="freeform",
                     web_search_tool_type="text_and_image",
                     additional_speed_tiers=["fast"]),
        _model_entry(slug="gpt-5.4-mini", display_name=display_name, priority=4,
                     default_reasoning="medium", default_verbosity="medium",
                     apply_patch_tool_type="freeform",
                     web_search_tool_type="text_and_image"),
        _model_entry(slug="gpt-5.3-codex", display_name=display_name, priority=6,
                     default_reasoning="medium", default_verbosity="low",
                     apply_patch_tool_type="freeform",
                     web_search_tool_type="text",
                     upgrade=upgrade_to_54),
        _model_entry(slug="gpt-5.2", display_name=display_name, priority=10,
                     default_reasoning="medium", default_verbosity="low",
                     apply_patch_tool_type="freeform",
                     web_search_tool_type="text",
                     truncation_mode="bytes",
                     supports_image_detail_original=False,
                     default_reasoning_summary="auto",
                     upgrade=upgrade_to_54),
        _model_entry(slug=native_slug, display_name=display_name, priority=10,
                     default_reasoning="high", default_verbosity=None,
                     shell_type="default",
                     apply_patch_tool_type=None,
                     web_search_tool_type="text",
                     supports_reasoning_summaries=False, support_verbosity=False,
                     supports_search_tool=False, supports_parallel_tool_calls=False,
                     supports_image_detail_original=False,
                     truncation_mode="bytes", truncation_limit=4_000_000,
                     default_reasoning_summary="auto"),
    ]
    return {"models": models}


_SECTION_RE = re.compile(r"^\s*\[\[?([^\]]+)\]\]?")


def rewrite_config_toml(original: str, *, port: int, catalog_path: str,
                        default_model: str) -> str:
    lines = original.splitlines(keepends=True)

    first_section_idx = None
    for i, line in enumerate(lines):
        if _SECTION_RE.match(line):
            first_section_idx = i
            break

    if first_section_idx is None:
        root_lines, section_lines = lines, []
    else:
        root_lines = lines[:first_section_idx]
        section_lines = lines[first_section_idx:]

    managed_key_re = re.compile(
        r"^\s*(?:" + "|".join(re.escape(k) for k in MANAGED_TOP_KEYS) + r")\s*="
    )

    def is_managed_marker(line: str) -> bool:
        return line.lstrip().startswith("#") and "codex-llm-proxy managed" in line

    new_root: list[str] = []
    for line in root_lines:
        if is_managed_marker(line):
            continue
        if managed_key_re.match(line):
            continue
        new_root.append(line)

    new_sections: list[str] = []
    skipping = False
    for line in section_lines:
        m = _SECTION_RE.match(line)
        if m:
            section_name = m.group(1).strip()
            skipping = (section_name in MANAGED_SECTIONS)
            if skipping:
                continue
        if skipping:
            continue
        if is_managed_marker(line):
            continue
        new_sections.append(line)

    if new_root and not new_root[-1].endswith("\n"):
        new_root[-1] += "\n"

    # Trim trailing blank lines in both regions so the managed-block separator
    # is deterministic. Without this, repeated apply calls would accumulate
    # blank lines (each apply's strip leaves behind one blank from the previous
    # managed block's surrounding whitespace).
    while new_root and new_root[-1].strip() == "":
        new_root.pop()
    while new_sections and new_sections[-1].strip() == "":
        new_sections.pop()

    managed_top = (
        "\n"
        "# vvv codex-llm-proxy managed (regenerated each start; restored on stop) vvv\n"
        f'model_provider = "{PROVIDER_NAME}"\n'
        f'model = "{default_model}"\n'
        f'model_catalog_json = "{catalog_path}"\n'
        "# ^^^ codex-llm-proxy managed (top-level) ^^^\n"
        "\n"
    )

    managed_section = (
        "\n"
        "# vvv codex-llm-proxy managed section vvv\n"
        f"[model_providers.{PROVIDER_NAME}]\n"
        'name = "Codex LLM Proxy"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\n'
        'wire_api = "responses"\n'
        "# ^^^ codex-llm-proxy managed section ^^^\n"
    )

    return "".join(new_root) + managed_top + "".join(new_sections) + managed_section


def cmd_apply(args) -> int:
    if codex_app_transfer_running():
        print("Warning: 'Codex App Transfer' is running; skipping config rewrite "
              "to avoid conflict.", file=sys.stderr)
        return 0
    if not CODEX_CONFIG.exists():
        print(f"Warning: {CODEX_CONFIG} not found; nothing to apply.", file=sys.stderr)
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if not MARKER.exists():
        shutil.copy2(CODEX_CONFIG, SNAPSHOT)

    catalog = build_model_catalog(args.backend)
    CATALOG.write_text(json.dumps(catalog, indent=2, ensure_ascii=False))

    original = CODEX_CONFIG.read_text()
    updated = rewrite_config_toml(
        original, port=args.port, catalog_path=str(CATALOG),
        default_model=args.default_model,
    )

    tmp = CODEX_CONFIG.with_suffix(".toml.tmp")
    tmp.write_text(updated)
    os.replace(tmp, CODEX_CONFIG)

    MARKER.write_text(datetime.now().isoformat() + "\n")
    print(f"Applied codex-llm-proxy config "
          f"(port={args.port}, backend={args.backend}, model={args.default_model})")
    print(f"  snapshot: {SNAPSHOT}")
    print(f"  catalog : {CATALOG}")
    return 0


def cmd_restore(args) -> int:
    if not MARKER.exists():
        return 0
    if not SNAPSHOT.exists():
        print(f"Warning: snapshot missing at {SNAPSHOT}; cannot restore. "
              "User's config.toml has been left as-is.", file=sys.stderr)
        MARKER.unlink(missing_ok=True)
        return 1
    shutil.copy2(SNAPSHOT, CODEX_CONFIG)
    MARKER.unlink(missing_ok=True)
    print(f"Restored {CODEX_CONFIG} from snapshot.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    apply_p = sub.add_parser("apply", help="Snapshot config.toml and rewrite for the proxy")
    apply_p.add_argument("--port", type=int,
                         default=int(os.environ.get("PROXY_PORT", 18765)))
    apply_p.add_argument("--backend", default=os.environ.get("BACKEND", "glm"))
    apply_p.add_argument("--default-model", default="gpt-5.4")
    apply_p.set_defaults(func=cmd_apply)

    restore_p = sub.add_parser("restore", help="Restore config.toml from the snapshot")
    restore_p.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
