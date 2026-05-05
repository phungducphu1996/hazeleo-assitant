from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from typing import Any

from app.config import Settings
from app.schemas import SkylightAction


class SkylightMCPError(RuntimeError):
    """Raised when the Skylight MCP server cannot complete a request."""


SKYLIGHT_ALLOWED_TOOLS = {
    "get_tasks",
    "create_chore",
    "get_events",
    "get_meals",
    "create_meal",
    "update_meal_recipe",
    "delete_meal_sitting",
}


class SkylightMCPClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.skylight_enabled and self.settings.skylight_mcp_command.strip())

    async def health(self) -> dict[str, Any]:
        if not self.configured:
            return {"ok": False, "configured": False, "tools": []}
        tools = await self.list_tools()
        return {"ok": True, "configured": True, "tools": tools}

    async def list_tools(self) -> list[dict[str, Any]]:
        response = await self._with_session(lambda session: session.list_tools())
        tools = response.get("tools") if isinstance(response, dict) else None
        if not isinstance(tools, list):
            raise SkylightMCPError("invalid_tools_list")
        return [tool for tool in tools if isinstance(tool, dict) and tool.get("name") in SKYLIGHT_ALLOWED_TOOLS]

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise SkylightMCPError("skylight_not_configured")
        if tool not in SKYLIGHT_ALLOWED_TOOLS:
            raise SkylightMCPError(f"tool_not_allowed: {tool}")
        return await self._with_session(lambda session: session.call_tool(tool, _clean_arguments(arguments or {})))

    async def execute_actions(self, actions: list[SkylightAction]) -> list[dict[str, Any]]:
        if not actions:
            return []
        if not self.configured:
            return [
                {
                    "tool": action.tool,
                    "arguments": action.arguments,
                    "ok": False,
                    "error": "skylight_not_configured",
                }
                for action in actions
            ]

        results: list[dict[str, Any]] = []
        for action in actions:
            try:
                result = await self.call_tool(action.tool, action.arguments)
                results.append(
                    {
                        "tool": action.tool,
                        "arguments": action.arguments,
                        "ok": not bool(result.get("isError")),
                        "result": result,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "tool": action.tool,
                        "arguments": action.arguments,
                        "ok": False,
                        "error": str(exc),
                    }
                )
        return results

    async def _with_session(self, callback):
        command = self.settings.skylight_mcp_command.strip()
        args = _command_args(self.settings.skylight_mcp_args or "")
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        session = _MCPStdioSession(process, timeout=max(1.0, self.settings.skylight_mcp_timeout_seconds))
        try:
            await session.initialize()
            return await callback(session)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


class _MCPStdioSession:
    def __init__(self, process: asyncio.subprocess.Process, *, timeout: float) -> None:
        self.process = process
        self.timeout = timeout
        self.next_id = 1

    async def initialize(self) -> dict[str, Any]:
        return await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "family-assistant-ver2", "version": "0.1.0"},
            },
        )

    async def list_tools(self) -> dict[str, Any]:
        return await self.request("tools/list", {})

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await self.request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(response, dict):
            return {"raw": response}
        return _decode_tool_result(response)

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        if self.process.stdin is None or self.process.stdout is None:
            raise SkylightMCPError("mcp_process_not_available")
        message_id = self.next_id
        self.next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": method,
            "params": params,
        }
        await self._write_message(payload)
        response = await asyncio.wait_for(self._read_message(), timeout=self.timeout)
        if response.get("id") != message_id:
            raise SkylightMCPError("mcp_response_id_mismatch")
        if "error" in response:
            error = response.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise SkylightMCPError(str(message or "mcp_error"))
        return response.get("result")

    async def _write_message(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise SkylightMCPError("mcp_stdin_closed")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.process.stdin.write(header + body)
        await self.process.stdin.drain()

    async def _read_message(self) -> dict[str, Any]:
        if self.process.stdout is None:
            raise SkylightMCPError("mcp_stdout_closed")
        header = await self.process.stdout.readuntil(b"\r\n\r\n")
        length: int | None = None
        for line in header.decode("ascii", errors="ignore").split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
                break
        if length is None:
            raise SkylightMCPError("missing_content_length")
        body = await self.process.stdout.readexactly(length)
        return json.loads(body.decode("utf-8"))


def _decode_tool_result(response: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(response)
    content = response.get("content")
    if not isinstance(content, list) or not content:
        return decoded
    first = content[0]
    if not isinstance(first, dict):
        return decoded
    text = first.get("text")
    if not isinstance(text, str):
        return decoded
    try:
        decoded["json"] = json.loads(text)
    except ValueError:
        decoded["text"] = text
    return decoded


def _clean_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        if value is None:
            continue
        cleaned[key] = value
    return cleaned


def _command_args(raw_args: str) -> list[str]:
    cleaned = raw_args.strip()
    if not cleaned:
        return []
    if Path(cleaned).exists():
        return [cleaned]
    return shlex.split(cleaned)
