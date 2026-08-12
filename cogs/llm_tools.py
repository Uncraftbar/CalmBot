"""Bounded tools for CalmBot's LLM mode.

Read tools are available to every LLM user. Mutating AMP actions are never run by
model output: the model can only prepare a confirmation button. The callback
re-fetches the invoking member and requires guild owner or Discord Administrator.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import discord

from cogs.utils import fetch_valid_instances, get_instance_state, get_logger

log = get_logger("llm_tools")
MODPACK_INDEX_URL = "https://www.modpackindex.com/api/v1"

READ_TOOL_NAMES = {"server_status", "online_players", "search_modpacks", "get_modpack"}
WRITE_TOOL_NAMES = {"request_amp_action"}

TOOL_DEFINITIONS = [
    {
        "name": "server_status",
        "description": "Get live read-only state and player counts for CalmBot's AMP game servers.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "online_players",
        "description": "Get live read-only online-player information reported by AMP.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "search_modpacks",
        "description": "Search the public Modpack Index catalog by modpack name. Returns up to five matches.",
        "parameters": {
            "type": "object", "properties": {
                "name": {"type": "string", "description": "Modpack name to search for"},
            }, "required": ["name"], "additionalProperties": False,
        },
    },
    {
        "name": "get_modpack",
        "description": "Get public Modpack Index details for one numeric modpack ID.",
        "parameters": {
            "type": "object", "properties": {
                "id": {"type": "integer", "minimum": 1},
            }, "required": ["id"], "additionalProperties": False,
        },
    },
    {
        "name": "request_amp_action",
        "description": (
            "Prepare an administrator confirmation for starting, stopping, or restarting an AMP server. "
            "This does not perform the action. Use only when the user explicitly asks for that exact action."
        ),
        "parameters": {
            "type": "object", "properties": {
                "action": {"type": "string", "enum": ["start", "stop", "restart"]},
                "server": {"type": "string", "description": "Exact or unambiguous server name"},
                "reason": {"type": "string", "minLength": 3, "maxLength": 200},
            }, "required": ["action", "server", "reason"], "additionalProperties": False,
        },
    },
]


def openai_tools(include_write: bool) -> list[dict]:
    names = READ_TOOL_NAMES | (WRITE_TOOL_NAMES if include_write else set())
    return [{"type": "function", "function": item} for item in TOOL_DEFINITIONS if item["name"] in names]


def responses_tools(include_write: bool) -> list[dict]:
    names = READ_TOOL_NAMES | (WRITE_TOOL_NAMES if include_write else set())
    return [{"type": "function", **item, "strict": False} for item in TOOL_DEFINITIONS if item["name"] in names]


def is_administrator(member: Any) -> bool:
    """Deliberately stricter than general CalmBot moderation checks for writes."""
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    return bool(guild.owner_id == member.id or getattr(member.guild_permissions, "administrator", False))


@dataclass(frozen=True)
class PendingAMPAction:
    action: str
    instance_name: str
    display_name: str
    reason: str


class AMPActionConfirmView(discord.ui.View):
    def __init__(self, action: PendingAMPAction, actor_id: int, guild_id: int):
        super().__init__(timeout=120)
        self.action = action
        self.actor_id = actor_id
        self.guild_id = guild_id
        self.used = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This confirmation belongs to another administrator.", ephemeral=True)
            return False
        if self.used:
            await interaction.response.send_message("This action has already been handled.", ephemeral=True)
            return False
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            await interaction.response.send_message("AMP actions cannot be confirmed outside the originating server.", ephemeral=True)
            return False
        try:
            member = await guild.fetch_member(self.actor_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            await interaction.response.send_message("I could not verify your current permissions; action denied.", ephemeral=True)
            return False
        if not is_administrator(member):
            log.warning("LLM AMP permission denied at confirmation: guild=%s user=%s", guild.id, self.actor_id)
            await interaction.response.send_message("Discord Administrator permission is required; action denied.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm AMP action", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.used = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        instances = await fetch_valid_instances()
        instance = next((x for x in instances if x.instance_name == self.action.instance_name), None)
        if instance is None:
            await interaction.followup.send("The selected AMP instance is no longer available; nothing was changed.", ephemeral=True)
            return
        method = {"start": instance.start_application, "stop": instance.stop_application,
                  "restart": instance.restart_application}[self.action.action]
        try:
            await asyncio.wait_for(method(), timeout=20)
            log.warning("LLM AMP AUDIT: guild=%s user=%s action=%s instance=%s reason=%r",
                        interaction.guild_id, self.actor_id, self.action.action,
                        self.action.instance_name, self.action.reason)
            await interaction.followup.send(
                f"AMP **{self.action.action}** requested for **{self.action.display_name}**. "
                f"Reason: {self.action.reason}", ephemeral=True)
        except Exception as exc:
            log.error("Confirmed LLM AMP action failed for %s: %s", self.action.instance_name, exc)
            await interaction.followup.send("AMP rejected or failed the action; no success was assumed.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.used = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="AMP action cancelled.", view=self)


class LLMToolRuntime:
    def __init__(self, cog: Any, message: discord.Message):
        self.cog = cog
        self.message = message
        self.pending_action: PendingAMPAction | None = None

    @property
    def actor_is_admin(self) -> bool:
        # This is only used to decide which schema the model sees. Confirmation performs
        # a fresh REST fetch and checks again; this cached object never authorizes a write.
        return is_administrator(self.message.author)

    async def execute(self, name: str, arguments: Any) -> str:
        try:
            args = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except (TypeError, ValueError, json.JSONDecodeError):
            return json.dumps({"error": "Invalid tool arguments"})

        try:
            if name == "server_status":
                return json.dumps(await self._server_status(), ensure_ascii=False)
            if name == "online_players":
                return json.dumps(await self._online_players(), ensure_ascii=False)
            if name == "search_modpacks":
                return json.dumps(await self._search_modpacks(args), ensure_ascii=False)
            if name == "get_modpack":
                return json.dumps(await self._get_modpack(args), ensure_ascii=False)
            if name == "request_amp_action":
                return json.dumps(await self._request_amp_action(args), ensure_ascii=False)
            return json.dumps({"error": "Unknown or unavailable tool"})
        except Exception as exc:
            log.warning("LLM read tool %s failed: %s", name, exc)
            return json.dumps({"error": f"{name} is temporarily unavailable"})

    async def _instances(self):
        return await asyncio.wait_for(fetch_valid_instances(), timeout=15)

    async def _server_status(self):
        result = []
        for instance in await self._instances():
            name = instance.friendly_name or instance.instance_name
            try:
                status = await asyncio.wait_for(instance.get_instance_status(), timeout=8)
                users = getattr(status, "active_users", None)
                count = len(users) if isinstance(users, (list, dict)) else users if isinstance(users, int) else None
                result.append({"server": name, "state": get_instance_state(status), "players": count})
            except Exception:
                result.append({"server": name, "state": "Unknown", "players": None})
        return {"servers": result}

    async def _online_players(self):
        result = []
        for instance in await self._instances():
            name = instance.friendly_name or instance.instance_name
            try:
                status = await asyncio.wait_for(instance.get_instance_status(), timeout=8)
                users = getattr(status, "active_users", None)
                if isinstance(users, dict):
                    players = [str(x) for x in users.keys()]
                elif isinstance(users, list):
                    players = [str(getattr(x, "name", x)) for x in users]
                else:
                    players = []
                result.append({"server": name, "players": players[:100], "count": len(players)})
            except Exception:
                result.append({"server": name, "error": "status unavailable"})
        return {"servers": result}

    async def _mpi_get(self, path: str, params: dict | None = None):
        session = await self.cog._http()
        url = f"{MODPACK_INDEX_URL}/{path.lstrip('/')}"
        async with session.get(url, params=params, headers={"Accept": "application/json"}) as response:
            if response.status != 200:
                raise RuntimeError(f"Modpack Index HTTP {response.status}")
            return await response.json(content_type=None)

    async def _search_modpacks(self, args):
        name = str(args.get("name", "")).strip()[:100]
        if len(name) < 2:
            return {"error": "Search name must contain at least two characters"}
        payload = await self._mpi_get("modpacks", {"name": name})
        packs = payload.get("data", []) if isinstance(payload, dict) else []
        return {"query": name, "results": [self._pack_summary(x) for x in packs[:5]]}

    async def _get_modpack(self, args):
        try:
            pack_id = int(args.get("id"))
        except (TypeError, ValueError):
            return {"error": "A valid numeric modpack ID is required"}
        if pack_id < 1:
            return {"error": "A valid numeric modpack ID is required"}
        payload = await self._mpi_get(f"modpack/{pack_id}")
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        return self._pack_summary(data, detailed=True) if isinstance(data, dict) else {"error": "Unexpected API response"}

    @staticmethod
    def _pack_summary(pack: dict, detailed: bool = False):
        keys = ("id", "name", "summary", "download_count", "latest_release_date", "page_url", "url")
        result = {key: pack.get(key) for key in keys if pack.get(key) is not None}
        if detailed:
            for key in ("links", "thumbnail_url", "primary_language", "last_modified"):
                if pack.get(key) is not None:
                    result[key] = pack[key]
        return result

    async def _request_amp_action(self, args):
        # Hard authorization gate in ordinary code, independent of model instructions.
        if not self.actor_is_admin:
            log.warning("LLM AMP permission denied at request: guild=%s user=%s",
                        self.message.guild.id, self.message.author.id)
            return {"error": "Denied: Discord Administrator permission is required"}
        action = str(args.get("action", "")).lower()
        requested = str(args.get("server", "")).strip()
        reason = str(args.get("reason", "")).strip()[:200]
        if action not in {"start", "stop", "restart"} or len(reason) < 3 or not requested:
            return {"error": "Invalid action, server, or reason"}
        instances = await self._instances()
        exact = [x for x in instances if requested.casefold() in {
            x.instance_name.casefold(), str(x.friendly_name or "").casefold()}]
        if len(exact) != 1:
            return {"error": "Server name must exactly identify one current AMP instance"}
        instance = exact[0]
        display = instance.friendly_name or instance.instance_name
        self.pending_action = PendingAMPAction(action, instance.instance_name, display, reason)
        return {"confirmation_required": True, "action": action, "server": display,
                "message": "No action has run. The administrator must use the confirmation button."}

# main.py auto-loads every Python module in cogs/. This module is a support
# library rather than a Cog, so provide an intentional no-op extension hook.
async def setup(bot):
    return None
