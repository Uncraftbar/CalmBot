"""Bounded tools for CalmBot's LLM mode.

Read tools are available to every LLM user. Mutating AMP actions are never run by
model output: the model can only prepare a confirmation button. The callback
re-fetches the invoking member and requires guild owner or Discord Administrator.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any

import config
import discord

from cogs.utils import fetch_valid_instances, get_instance_state, get_logger, get_player_data

log = get_logger("llm_tools")
MODPACK_INDEX_URL = "https://www.modpackindex.com/api/v1"

READ_TOOL_NAMES = {"server_status", "online_players", "search_modpacks", "get_modpack", "query_modpack_index", "check_modpack_contains_mod", "search_community_docs", "connection_diagnostic", "stay_silent", "end_conversation"}
ADMIN_READ_TOOL_NAMES = {"read_server_console"}
WRITE_TOOL_NAMES = {"request_amp_action"}

TOOL_DEFINITIONS = [
    {
        "name": "stay_silent",
        "description": ("Send no Discord response to this message while keeping the active conversation open. "
                        "Use when the message is not directed at CalmBot, asks CalmBot not to reply, or a reply would be intrusive."),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "end_conversation",
        "description": ("Send no Discord response and completely end the current channel's automatic conversation. "
                        "Use only when someone clearly dismisses CalmBot, asks it to stop/shut up/go away, or the conversation is clearly over."),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
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
        "name": "query_modpack_index",
        "description": (
            "Query any public Modpack Index API resource. Supports listing and details for authors, "
            "categories, launchers, Minecraft versions, modpacks, and mods; category/launcher/version "
            "mod and modpack memberships; mods in a modpack; and modpacks containing a mod."
        ),
        "parameters": {
            "type": "object", "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "list_authors", "get_author", "list_categories", "get_category",
                        "category_mods", "category_modpacks", "list_launchers", "get_launcher",
                        "launcher_mods", "launcher_modpacks", "list_minecraft_versions",
                        "get_minecraft_version", "minecraft_version_mods",
                        "minecraft_version_modpacks", "list_modpacks", "get_modpack",
                        "modpack_mods", "list_mods", "get_mod", "mod_modpacks"
                    ]
                },
                "id": {"type": "integer", "minimum": 1, "description": "Required for get/relationship operations"},
                "name": {"type": "string", "maxLength": 100, "description": "Optional name search for list_mods/list_modpacks"},
                "page": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10}
            }, "required": ["operation"], "additionalProperties": False
        },
    },
    {
        "name": "check_modpack_contains_mod",
        "description": (
            "Check whether a named modpack contains a named mod. This performs the pack lookup and "
            "searches its complete Modpack Index mod list in one call. Use this instead of chaining "
            "search_modpacks/get_modpack/query_modpack_index for membership questions."
        ),
        "parameters": {
            "type": "object", "properties": {
                "modpack": {"type": "string", "minLength": 2, "maxLength": 100},
                "mod": {"type": "string", "minLength": 2, "maxLength": 100},
            }, "required": ["modpack", "mod"], "additionalProperties": False,
        },
    },
    {
        "name": "search_community_docs",
        "description": "Search CalmBot's local public README/help documentation. Returns quoted excerpts and source names.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 100}
        }, "required": ["query"], "additionalProperties": False},
    },
    {
        "name": "connection_diagnostic",
        "description": "Run a composite read-only connection diagnostic across public servers using uncached live AMP state and player metrics.",
        "parameters": {"type": "object", "properties": {
            "server": {"type": "string", "maxLength": 100, "description": "Optional exact public server name"}
        }, "additionalProperties": False},
    },
    {
        "name": "read_server_console",
        "description": (
            "Read recent, redacted AMP console history for one public server to diagnose an earlier "
            "connection failure. This is passive and sends no console command. Administrator-only."
        ),
        "parameters": {
            "type": "object", "properties": {
                "server": {"type": "string", "maxLength": 100, "description": "Public server name; spaces and punctuation are ignored when matching"},
                "minutes": {"type": "integer", "minimum": 1, "maximum": 60, "default": 15},
            }, "required": ["server"], "additionalProperties": False,
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
    names = READ_TOOL_NAMES | ((ADMIN_READ_TOOL_NAMES | WRITE_TOOL_NAMES) if include_write else set())
    return [{"type": "function", "function": item} for item in TOOL_DEFINITIONS if item["name"] in names]


def responses_tools(include_write: bool) -> list[dict]:
    names = READ_TOOL_NAMES | ((ADMIN_READ_TOOL_NAMES | WRITE_TOOL_NAMES) if include_write else set())
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
        self.tool_calls = 0
        self.image_data_urls: list[str] = []
        self.conversation_control: str | None = None

    @property
    def actor_is_admin(self) -> bool:
        # This is only used to decide which schema the model sees. Confirmation performs
        # a fresh REST fetch and checks again; this cached object never authorizes a write.
        return is_administrator(self.message.author)

    async def execute(self, name: str, arguments: Any) -> str:
        self.tool_calls += 1
        try:
            args = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except (TypeError, ValueError, json.JSONDecodeError):
            return json.dumps({"error": "Invalid tool arguments"})
        if name == "stay_silent":
            self.conversation_control = "silent"
            return json.dumps({"ok": True, "result": "No Discord message will be sent; conversation remains active."})
        if name == "end_conversation":
            self.conversation_control = "end"
            return json.dumps({"ok": True, "result": "No Discord message will be sent; conversation will be ended."})

        try:
            if name == "server_status":
                return json.dumps(await self._server_status(), ensure_ascii=False)
            if name == "online_players":
                return json.dumps(await self._online_players(), ensure_ascii=False)
            if name == "search_modpacks":
                return json.dumps(await self._search_modpacks(args), ensure_ascii=False)
            if name == "get_modpack":
                return json.dumps(await self._get_modpack(args), ensure_ascii=False)
            if name == "query_modpack_index":
                return json.dumps(await self._query_modpack_index(args), ensure_ascii=False)
            if name == "check_modpack_contains_mod":
                return json.dumps(await self._check_modpack_contains_mod(args), ensure_ascii=False)
            if name == "search_community_docs":
                return json.dumps(await self._search_community_docs(args), ensure_ascii=False)
            if name == "connection_diagnostic":
                return json.dumps(await self._connection_diagnostic(args), ensure_ascii=False)
            if name == "read_server_console":
                return json.dumps(await self._read_server_console(args), ensure_ascii=False)
            if name == "request_amp_action":
                return json.dumps(await self._request_amp_action(args), ensure_ascii=False)
            return json.dumps({"error": "Unknown or unavailable tool"})
        except Exception as exc:
            log.warning("LLM read tool %s failed: %s", name, exc)
            return json.dumps({"error": f"{name} is temporarily unavailable"})

    async def _instances(self):
        return await asyncio.wait_for(fetch_valid_instances(), timeout=15)

    async def _public_instances(self):
        """Apply the same public-server allowlist used by /servers."""
        instances = await self._instances()
        allowlist = {
            str(name).strip().casefold()
            for name in getattr(config, "PUBLIC_SERVER_ALLOWLIST", [])
            if str(name).strip()
        }
        if not allowlist:
            return instances
        return [
            instance for instance in instances
            if str(instance.instance_name).casefold() in allowlist
            or str(instance.friendly_name or "").casefold() in allowlist
        ]

    async def _server_status(self):
        result = []
        for instance in await self._public_instances():
            name = instance.friendly_name or instance.instance_name
            try:
                status = await asyncio.wait_for(instance.get_instance_status(), timeout=8)
                _names, count = get_player_data(status)
                result.append({"server": name, "state": get_instance_state(status), "players": count})
            except Exception:
                result.append({"server": name, "state": "Unknown", "players": None})
        return {"servers": result, "source": "AMP live status", "fresh_at": int(time.time()), "cached": False}

    async def _online_players(self):
        result = []
        for instance in await self._public_instances():
            name = instance.friendly_name or instance.instance_name
            try:
                status = await asyncio.wait_for(instance.get_instance_status(), timeout=8)
                players, count = get_player_data(status)
                item = {"server": name, "players": players[:100], "count": count}
                if count is None:
                    item["error"] = "player count unavailable"
                elif count and not players:
                    item["note"] = "AMP reports a count but not player names"
                result.append(item)
            except Exception:
                result.append({"server": name, "error": "status unavailable"})
        return {"servers": result, "source": "AMP live status", "fresh_at": int(time.time()), "cached": False}

    async def _mpi_get(self, path: str, params: dict | None = None):
        cache = getattr(self.cog, "_public_tool_cache", None)
        if cache is None:
            cache = self.cog._public_tool_cache = {}
        key = (path, json.dumps(params or {}, sort_keys=True))
        now = time.monotonic()
        cached = cache.get(key)
        if cached and now - cached[0] < 120:
            return cached[1]
        session = await self.cog._http()
        url = f"{MODPACK_INDEX_URL}/{path.lstrip('/')}"
        async with session.get(url, params=params, headers={"Accept": "application/json", "User-Agent": "CalmBot/1.0 (Discord integration; github.com/Uncraftbar/CalmBot)"}) as response:
            if response.status != 200:
                raise RuntimeError(f"Modpack Index HTTP {response.status}")
            payload = await response.json(content_type=None)
        cache[key] = (now, payload)
        if len(cache) > 200:
            for stale in list(cache)[:50]: cache.pop(stale, None)
        return payload

    @staticmethod
    def _bounded_api_value(value, *, depth: int = 0):
        """Keep API results useful without letting huge relationships consume the LLM context."""
        if depth >= 5:
            return "[nested data omitted]"
        if isinstance(value, dict):
            return {str(k)[:80]: LLMToolRuntime._bounded_api_value(v, depth=depth + 1)
                    for k, v in list(value.items())[:60]}
        if isinstance(value, list):
            items = [LLMToolRuntime._bounded_api_value(v, depth=depth + 1) for v in value[:25]]
            if len(value) > 25:
                items.append({"omitted_items": len(value) - 25})
            return items
        if isinstance(value, str):
            return value[:1000]
        return value

    async def _query_modpack_index(self, args):
        operations = {
            "list_authors": ("authors", False, False),
            "get_author": ("author/{id}", True, False),
            "list_categories": ("categories", False, False),
            "get_category": ("category/{id}", True, False),
            "category_mods": ("category/{id}/mods", True, True),
            "category_modpacks": ("category/{id}/modpacks", True, True),
            "list_launchers": ("launchers", False, False),
            "get_launcher": ("launcher/{id}", True, False),
            "launcher_mods": ("launcher/{id}/mods", True, True),
            "launcher_modpacks": ("launcher/{id}/modpacks", True, True),
            "list_minecraft_versions": ("minecraft/versions", False, False),
            "get_minecraft_version": ("minecraft/version/{id}", True, False),
            "minecraft_version_mods": ("minecraft/version/{id}/mods", True, True),
            "minecraft_version_modpacks": ("minecraft/version/{id}/modpacks", True, True),
            "list_modpacks": ("modpacks", False, True),
            "get_modpack": ("modpack/{id}", True, False),
            "modpack_mods": ("modpack/{id}/mods", True, False),
            "list_mods": ("mods", False, True),
            "get_mod": ("mod/{id}", True, False),
            "mod_modpacks": ("mod/{id}/modpacks", True, True),
        }
        operation = str(args.get("operation", ""))
        spec = operations.get(operation)
        if spec is None:
            return {"error": "Unsupported Modpack Index operation"}
        path, needs_id, paginated = spec
        if needs_id:
            try:
                item_id = int(args.get("id"))
            except (TypeError, ValueError):
                return {"error": "This operation requires a valid numeric id"}
            if item_id < 1:
                return {"error": "This operation requires a valid numeric id"}
            path = path.format(id=item_id)
        params = None
        if paginated or operation.startswith("list_"):
            try:
                page = max(1, int(args.get("page", 1)))
                limit = min(25, max(1, int(args.get("limit", 10))))
            except (TypeError, ValueError):
                return {"error": "page and limit must be integers"}
            params = {"page": page, "limit": limit}
            if operation in {"list_mods", "list_modpacks"}:
                query = str(args.get("name", "")).strip()[:100]
                if query:
                    params["name"] = query
        payload = await self._mpi_get(path, params)
        return {"operation": operation, "result": self._bounded_api_value(payload)}

    @staticmethod
    def _catalog_items(payload):
        if isinstance(payload, dict):
            data = payload.get("data", [])
            return data if isinstance(data, list) else []
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _normalized_name(value):
        return "".join(ch for ch in str(value).casefold() if ch.isalnum())

    async def _check_modpack_contains_mod(self, args):
        pack_query = str(args.get("modpack", "")).strip()[:100]
        mod_query = str(args.get("mod", "")).strip()[:100]
        if len(pack_query) < 2 or len(mod_query) < 2:
            return {"error": "Both modpack and mod names must contain at least two characters"}

        packs = self._catalog_items(await self._mpi_get("modpacks", {"name": pack_query, "limit": 10}))
        wanted_pack = self._normalized_name(pack_query)
        exact = [p for p in packs if self._normalized_name(p.get("name")) == wanted_pack]
        candidates = exact or packs
        if not candidates:
            return {"verified": False, "reason": "modpack_not_found", "modpack_query": pack_query}
        if len(candidates) > 1 and not exact:
            return {"verified": False, "reason": "ambiguous_modpack", "candidates": [self._pack_summary(x) for x in candidates[:10]]}
        pack = candidates[0]

        # This relationship endpoint currently ignores pagination/name parameters and returns
        # the complete list. Filter locally so the model gets a small, decisive result.
        mods = self._catalog_items(await self._mpi_get(f"modpack/{int(pack['id'])}/mods"))
        wanted_mod = self._normalized_name(mod_query)
        exact_mods = [m for m in mods if self._normalized_name(m.get("name")) == wanted_mod]
        partial_mods = [m for m in mods if wanted_mod in self._normalized_name(m.get("name"))
                        or self._normalized_name(m.get("name")) in wanted_mod]
        matches = exact_mods or partial_mods
        return {
            "verified": True,
            "contains": bool(matches),
            "modpack": {"id": pack.get("id"), "name": pack.get("name"), "page_url": pack.get("page_url")},
            "mod_query": mod_query,
            "matches": [self._pack_summary(item) for item in matches[:10]],
            "mods_checked": len(mods),
        }

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
        return self._bounded_api_value(data) if isinstance(data, dict) else {"error": "Unexpected API response"}

    @staticmethod
    def _pack_summary(pack: dict, detailed: bool = False):
        keys = ("id", "name", "summary", "download_count", "latest_release_date", "page_url", "url")
        result = {key: pack.get(key) for key in keys if pack.get(key) is not None}
        if detailed:
            for key in ("links", "thumbnail_url", "primary_language", "last_modified"):
                if pack.get(key) is not None:
                    result[key] = pack[key]
        return result

    async def _search_community_docs(self, args):
        query = str(args.get("query", "")).strip()[:100]
        terms = [term.casefold() for term in re.findall(r"[A-Za-z0-9_.-]{2,}", query)]
        if not terms:
            return {"error": "A searchable query is required"}
        path = Path("README.md")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {"error": "Community documentation is unavailable"}
        matches = []
        for index, line in enumerate(lines):
            folded = line.casefold()
            score = sum(term in folded for term in terms)
            if score:
                excerpt = " ".join(part.strip() for part in lines[max(0,index-1):min(len(lines),index+2)] if part.strip())
                matches.append((score, index + 1, excerpt[:700]))
        matches.sort(key=lambda item: (-item[0], item[1]))
        return {"query": query, "results": [{"source": "README.md", "line": line, "excerpt": text} for _, line, text in matches[:6]], "fresh_at": int(time.time()), "cached": False}

    async def _connection_diagnostic(self, args):
        requested = str(args.get("server", "")).strip().casefold()
        status = await self._server_status()
        servers = status["servers"]
        if requested:
            exact = [item for item in servers if str(item.get("server", "")).casefold() == requested]
            if not exact:
                return {"verified": False, "reason": "public_server_not_found", "public_servers": [item.get("server") for item in servers]}
            servers = exact
        findings = []
        for item in servers:
            state = str(item.get("state", "unknown")).casefold()
            if state != "running": findings.append(f"{item['server']} is {item.get('state', 'Unknown')}")
            elif item.get("players") is None: findings.append(f"{item['server']} is running, but AMP player metrics are unavailable")
            else: findings.append(f"{item['server']} is running and AMP reports {item['players']} player(s)")
        return {"verified": True, "findings": findings, "limits": ["This checks AMP state only; it cannot inspect the player's client, DNS path, firewall, account, or mod mismatch."], "source": "AMP live status", "fresh_at": int(time.time()), "cached": False}

    @staticmethod
    def _server_key(value):
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    async def _read_server_console(self, args):
        # Console output can contain private operational data. Authorization is
        # enforced here in ordinary code; model output alone can never bypass it.
        if not self.actor_is_admin:
            log.warning("LLM console permission denied: guild=%s user=%s",
                        getattr(self.message.guild, "id", None), self.message.author.id)
            return {"error": "Denied: Discord Administrator permission is required"}
        requested = self._server_key(str(args.get("server", ""))[:100])
        if not requested:
            return {"error": "A public server name is required"}
        public_instances = await self._public_instances()
        matches = []
        for instance in public_instances:
            aliases = {self._server_key(instance.instance_name), self._server_key(instance.friendly_name)}
            if requested in aliases:
                matches.append(instance)
        if len(matches) != 1:
            public = [x.friendly_name or x.instance_name for x in public_instances]
            return {"error": "Server name did not identify exactly one public server", "public_servers": public}
        instance = matches[0]
        display = instance.friendly_name or instance.instance_name
        bridge = self.cog.bot.get_cog("ChatBridge")
        if bridge is None or not hasattr(bridge, "recent_console"):
            return {"error": "Recent console history is unavailable"}
        try:
            minutes = max(1, min(int(args.get("minutes", 15)), 60))
        except (TypeError, ValueError):
            minutes = 15
        history_name = next((name for name, value in bridge.instances.items()
                             if value.instance_name == instance.instance_name), display)
        entries = bridge.recent_console(history_name, minutes=minutes, limit=120)
        return {
            "verified": True,
            "server": display,
            "minutes": minutes,
            "entries": entries,
            "entry_count": len(entries),
            "limits": [
                "History is in-memory and only covers console events observed since CalmBot started.",
                "Sensitive-looking credentials and IP addresses are redacted before model access.",
                "No console command was sent.",
            ],
            "source": "AMP real-time console event stream with polling fallback",
            "fresh_at": int(time.time()),
            "cached": False,
        }

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
