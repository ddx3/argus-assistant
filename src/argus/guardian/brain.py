"""The model, behind one interface so that tests can replace it.

`ClaudeBrain` runs the Claude Agent SDK. The SDK's own tools are all switched off: the model is
given only the `ToolSpec`s of `tools.py`, served from this process as an MCP server, and a
deny-by-default permission callback turns away anything else (three independent layers, because the
cost of one of them being misconfigured is a model with a shell).

`ScriptedBrain` plays a fixed list of tool calls against the real toolbox. It is what the tests
use: the model is the only thing replaced, so the policy, the validation, and the audit log are
exercised for real.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from argus.guardian.config import Config
from argus.guardian.tools import ToolSpec

log = logging.getLogger("argus.guardian")

SERVER = "guardian"
# Everything the SDK ships with that could read, write, run, or fetch. Named so a reader can see that
# the answer to "can it run a command?" is a list, not a hope.
BUILTIN_TOOLS = [
    "Bash", "BashOutput", "KillBash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "WebFetch", "WebSearch",
    "Task", "NotebookEdit", "TodoWrite", "ToolSearch", "SlashCommand", "ExitPlanMode",
]


@dataclass
class AgentRun:
    ok: bool
    text: str = ""
    tool_calls: int = 0
    cost_usd: float = 0.0       # 0 on a subscription, which is not billed per call: the token count is the cap there
    error: str = ""
    tokens: int = 0
    # When the model cannot be used before a given time (a usage limit, a billing or login problem), the unix time
    # to try again. The cycle stops spending effort on it until then instead of failing every problem in turn.
    blocked_until: float = 0.0


class Brain(Protocol):
    async def run(self, *, system: str, prompt: str, tools: list[ToolSpec], call: Callable[[str, dict], str],
                  max_turns: int, timeout_s: float, budget_usd: float) -> AgentRun: ...


class ClaudeBrain:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _env(self) -> dict[str, str]:
        # Subscription mode must stay on the subscription: an exported key would silently switch the child
        # process to paid API calls. The SDK overlays `env` on the inherited environment, so the variable is
        # blanked rather than removed.
        if self.cfg.auth == "api":
            return {}
        return {"ANTHROPIC_API_KEY": ""}

    def _options(self, system: str, server, allowed: list[str], max_turns: int, budget_usd: float):
        from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny

        allowed_set = set(allowed)

        async def can_use_tool(tool_name, input_data, context):
            if tool_name in allowed_set:
                return PermissionResultAllow()
            log.warning("guardian: the model asked for a tool that is not allowed: %s", tool_name)
            return PermissionResultDeny(message=f"{tool_name} is not available")

        options = {
            "system_prompt": system,
            "mcp_servers": {SERVER: server},
            "strict_mcp_config": True,
            "tools": [],                        # layer 1: no built-in tools exist
            "allowed_tools": allowed,           # layer 2: only ours are pre-approved
            "disallowed_tools": BUILTIN_TOOLS,  # layer 3: and the built-in ones are named and refused
            "can_use_tool": can_use_tool,
            "permission_mode": "dontAsk",
            "setting_sources": [],              # do not load the user's hooks, plugins, or CLAUDE.md
            "max_turns": max_turns,
            "max_budget_usd": budget_usd,
            "env": self._env(),
        }
        if self.cfg.model:
            options["model"] = self.cfg.model
        with warnings.catch_warnings():
            # The SDK notes that the callback is skipped for pre-approved tools. That is the intended layering: the
            # callback exists for everything that is NOT pre-approved.
            warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")
            return ClaudeAgentOptions(**options)

    async def run(self, *, system: str, prompt: str, tools: list[ToolSpec], call: Callable[[str, dict], str],
                  max_turns: int, timeout_s: float, budget_usd: float) -> AgentRun:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeSDKClient,
            RateLimitEvent,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            create_sdk_mcp_server,
            tool,
        )

        sdk_tools = []
        for spec in tools:
            def make(spec: ToolSpec):
                @tool(spec.name, spec.description, spec.schema)
                async def handler(args: dict) -> dict:
                    text = await asyncio.to_thread(call, spec.name, args)
                    return {"content": [{"type": "text", "text": text}], "is_error": text.startswith(("refused:", "error:"))}
                return handler
            sdk_tools.append(make(spec))
        server = create_sdk_mcp_server(name=SERVER, version="1.0.0", tools=sdk_tools)
        allowed = [f"mcp__{SERVER}__{spec.name}" for spec in tools]
        options = self._options(system, server, allowed, max_turns, budget_usd)

        texts: list[str] = []
        state = {"calls": 0, "cost": 0.0, "tokens": 0, "final": None, "error": "", "blocked_until": 0.0}

        async def drive() -> None:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, RateLimitEvent):
                        info = message.rate_limit_info
                        if getattr(info, "status", "allowed") != "allowed":
                            state["error"] = f"the model is rate limited ({getattr(info, 'rate_limit_type', '')})"
                            state["blocked_until"] = float(getattr(info, "resets_at", 0) or 0) or time.time() + 3600
                    elif isinstance(message, AssistantMessage):
                        if getattr(message, "error", None):
                            state["error"] = f"the model answered with an error: {message.error}"
                            state["blocked_until"] = state["blocked_until"] or time.time() + 3600
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                texts.append(block.text)
                            elif isinstance(block, ToolUseBlock):
                                state["calls"] += 1
                    elif isinstance(message, ResultMessage):
                        state["cost"] = float(getattr(message, "total_cost_usd", 0.0) or 0.0)
                        state["final"] = getattr(message, "result", None)
                        if getattr(message, "is_error", False):
                            state["error"] = state["error"] or f"the run ended in an error: {str(state['final'])[:200]}"
                            state["blocked_until"] = state["blocked_until"] or time.time() + 3600
                        usage = getattr(message, "usage", None) or {}
                        state["tokens"] = sum(int(usage.get(k) or 0) for k in
                                              ("input_tokens", "output_tokens", "cache_creation_input_tokens"))

        try:
            with warnings.catch_warnings():
                # See `_options`: the SDK warns that the permission callback is skipped for pre-approved tools.
                warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")
                await asyncio.wait_for(drive(), timeout=timeout_s)
        except TimeoutError:
            return AgentRun(False, "\n".join(texts), state["calls"], state["cost"], f"timed out after {timeout_s:.0f}s", state["tokens"])
        except Exception as e:  # noqa: BLE001 - SDK and CLI failures are many; the cycle only needs "it did not work"
            return AgentRun(False, "\n".join(texts), state["calls"], state["cost"], f"{type(e).__name__}: {e}", state["tokens"])
        text = str(state["final"] or "\n".join(texts))
        return AgentRun(not state["error"], text, state["calls"], state["cost"], state["error"], state["tokens"], state["blocked_until"])


class ScriptedBrain:
    """Plays prepared tool calls against the real tools; for tests and dry runs."""

    def __init__(self, script: list[tuple[str, dict]] | Callable[[Callable[[str, dict], str]], None]) -> None:
        self.script = script
        self.prompts: list[str] = []
        self.results: list[str] = []

    async def run(self, *, system: str, prompt: str, tools: list[ToolSpec], call: Callable[[str, dict], str],
                  max_turns: int, timeout_s: float, budget_usd: float) -> AgentRun:
        self.prompts.append(prompt)
        calls = 0
        if callable(self.script):
            self.script(call)
        else:
            for name, args in self.script:
                self.results.append(call(name, args))
                calls += 1
        return AgentRun(True, "scripted", calls, 0.0)


def claude_available() -> bool:
    import shutil

    return bool(shutil.which("claude") or shutil.which("claude.cmd") or os.environ.get("CLAUDE_BIN"))
