"""Deterministic, actor-bound gateway for high-impact moderator actions.

The gateway intentionally exposes a small, fixed action set.  It never accepts a
Discord actor ID from model/user payloads: authorization is derived from the
``Interaction.user`` that confirms the action.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Optional
from uuid import uuid4

import discord
from discord import app_commands
from discord.ext import commands

from cogs.utils import (
    error_embed,
    fetch_valid_instances,
    get_instance_state,
    get_logger,
    info_embed,
    success_embed,
)

log = get_logger("moderator_actions")

MAX_TIMEOUT_MINUTES = 28 * 24 * 60
AUDIT_PATH = Path("data/moderator_actions.jsonl")

# This is deliberately code-owned rather than supplied by an LLM/tool payload.
ACTION_PERMISSIONS = MappingProxyType(
    {
        "discord.timeout": "moderate_members",
        "discord.remove_timeout": "moderate_members",
        "amp.start": "manage_guild",
        "amp.stop": "manage_guild",
        "amp.restart": "manage_guild",
    }
)
AMP_METHODS = MappingProxyType(
    {
        "amp.start": "start_application",
        "amp.stop": "stop_application",
        "amp.restart": "restart_application",
    }
)
ALLOWED_ACTIONS = frozenset(ACTION_PERMISSIONS)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE = re.compile(r"\s+")


def sanitize_text(value: object, *, limit: int = 300) -> str:
    """Make user/service text safe for one-line audit records and embeds."""
    text = _CONTROL_CHARS.sub(" ", str(value or ""))
    text = _WHITESPACE.sub(" ", text).strip()
    return text[:limit]


def validate_reason(reason: object) -> str:
    clean = sanitize_text(reason, limit=300)
    if len(clean) < 3:
        raise GatewayError("A reason of at least 3 characters is required.", "invalid_reason")
    return clean


def select_amp_instance(instances: list, requested: str, *, internal_only: bool = False):
    """Resolve one exact AMP instance; never use partial/fuzzy matching."""
    needle = sanitize_text(requested, limit=150).casefold()
    if not needle:
        raise GatewayError("An AMP instance name is required.", "invalid_instance")

    matches = []
    for instance in instances:
        internal = sanitize_text(getattr(instance, "instance_name", ""), limit=150)
        friendly = sanitize_text(getattr(instance, "friendly_name", ""), limit=150)
        if internal.casefold() == needle or (
            not internal_only and friendly and friendly.casefold() == needle
        ):
            matches.append(instance)

    # A duplicated friendly name is unsafe: require the unique internal name.
    unique = {id(instance): instance for instance in matches}
    if len(unique) != 1:
        if not unique:
            raise GatewayError("That AMP instance is not in the managed instance scope.", "instance_out_of_scope")
        raise GatewayError("That name is ambiguous; use the exact AMP instance name.", "ambiguous_instance")
    return next(iter(unique.values()))


class GatewayError(Exception):
    """Expected, user-safe gateway rejection."""

    def __init__(self, message: str, code: str = "rejected"):
        super().__init__(message)
        self.message = sanitize_text(message, limit=300)
        self.code = sanitize_text(code, limit=80)


@dataclass(frozen=True)
class ActionRequest:
    request_id: str
    action: str
    guild_id: int
    actor_id: int
    actor_label: str
    reason: str
    target_id: Optional[int] = None
    target_label: str = ""
    timeout_minutes: Optional[int] = None
    amp_instance_name: str = ""
    channel_id: Optional[int] = None
    origin: str = "slash_command"


@dataclass(frozen=True)
class ActionResult:
    success: bool
    title: str
    message: str


class ModeratorAuditLog:
    """Append-only JSONL audit sink.  Failure is reported so actions can fail closed."""

    def __init__(self, path: Path = AUDIT_PATH):
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def append(self, request: ActionRequest, status: str, **extra: object) -> bool:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request.request_id,
            "status": sanitize_text(status, limit=40),
            "action": request.action,
            "guild_id": request.guild_id,
            "channel_id": request.channel_id,
            "actor_id": request.actor_id,
            "actor": request.actor_label,
            "target_id": request.target_id,
            "target": request.target_label,
            "amp_instance": request.amp_instance_name,
            "timeout_minutes": request.timeout_minutes,
            "reason": request.reason,
            "origin": request.origin,
        }
        for key, value in extra.items():
            # Never persist raw exception messages; callers provide stable codes/types.
            event[sanitize_text(key, limit=60)] = (
                sanitize_text(value, limit=300) if isinstance(value, str) else value
            )
        line = json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n"
        try:
            async with self._lock:
                await asyncio.to_thread(self._append_sync, line)
            return True
        except Exception as exc:  # Audit failure must not expose raw path/service details.
            log.error("Moderator audit write failed (%s)", type(exc).__name__)
            return False

    def _append_sync(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


class ConfirmActionView(discord.ui.View):
    """One-use confirmation bound to the initiating Discord actor."""

    def __init__(self, gateway: "ModeratorActions", request: ActionRequest):
        super().__init__(timeout=60)
        self.gateway = gateway
        self.request = request
        self._consumed = False
        self._lock = asyncio.Lock()
        self.message: Optional[discord.InteractionMessage] = None

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.request.actor_id:
            await interaction.response.send_message(
                "Only the moderator who requested this action can confirm it.", ephemeral=True
            )
            return

        async with self._lock:
            if self._consumed:
                await interaction.response.send_message("This action request is already closed.", ephemeral=True)
                return
            self._consumed = True
            self._disable()
            await interaction.response.edit_message(
                embed=info_embed("Executing moderator action", "Revalidating scope and permissions…"),
                view=self,
            )
            result = await self.gateway.execute_request(interaction, self.request)
            embed = (
                success_embed(result.title, result.message)
                if result.success
                else error_embed(result.title, result.message)
            )
            await interaction.edit_original_response(embed=embed, view=self)
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.request.actor_id:
            await interaction.response.send_message(
                "Only the moderator who requested this action can cancel it.", ephemeral=True
            )
            return

        async with self._lock:
            if self._consumed:
                await interaction.response.send_message("This action request is already closed.", ephemeral=True)
                return
            self._consumed = True
            self._disable()
            await self.gateway.audit.append(self.request, "cancelled")
            await interaction.response.edit_message(
                embed=info_embed("Action cancelled", f"Request `{self.request.request_id}` was not executed."),
                view=self,
            )
            self.stop()

    async def on_timeout(self) -> None:
        if self._consumed:
            return
        self._consumed = True
        self._disable()
        await self.gateway.audit.append(self.request, "expired")
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=info_embed("Action expired", "No action was executed."), view=self
                )
            except discord.HTTPException:
                pass


class ModeratorActions(commands.Cog):
    """Actor-bound commands plus a programmatic gateway for existing cogs."""

    mod_action = app_commands.Group(
        name="mod_action", description="Request a reviewed moderator action"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        configured_ids = getattr(getattr(bot, "config", None), "guild_ids", ())
        # Snapshot the configured guild allowlist for the lifetime of this cog.
        self.allowed_guild_ids = frozenset(
            int(value) for value in configured_ids if str(value).isdigit()
        )
        self.audit = ModeratorAuditLog()
        log.info(
            "Moderator action gateway initialized for %d guild(s); actions=%s",
            len(self.allowed_guild_ids),
            ",".join(sorted(ALLOWED_ACTIONS)),
        )

    def _actual_actor(self, interaction: discord.Interaction, action: str) -> discord.Member:
        if action not in ALLOWED_ACTIONS:
            raise GatewayError("That action is not allowlisted.", "action_not_allowlisted")
        guild = interaction.guild
        if guild is None or interaction.guild_id is None:
            raise GatewayError("Moderator actions are only available inside a guild.", "guild_required")
        if interaction.guild_id not in self.allowed_guild_ids:
            raise GatewayError("This guild is outside the configured action scope.", "guild_out_of_scope")
        actor = interaction.user
        if not isinstance(actor, discord.Member) or actor.guild.id != guild.id:
            raise GatewayError("The confirming Discord member could not be verified.", "actor_unverified")

        permissions = actor.guild_permissions
        allowed = (
            guild.owner_id == actor.id
            or permissions.administrator
            or bool(getattr(permissions, ACTION_PERMISSIONS[action], False))
        )
        if not allowed:
            required = ACTION_PERMISSIONS[action].replace("_", " ")
            raise GatewayError(f"This action requires the Discord `{required}` permission.", "permission_denied")
        return actor

    def _validate_member_scope(
        self, interaction: discord.Interaction, actor: discord.Member, target: discord.Member
    ) -> discord.Member:
        guild = interaction.guild
        if target.guild.id != guild.id:
            raise GatewayError("The target member is outside this guild.", "target_out_of_scope")
        if target.id == actor.id:
            raise GatewayError("You cannot apply this action to yourself.", "self_target")
        if target.id == guild.owner_id:
            raise GatewayError("The guild owner cannot be moderated by this action.", "owner_target")
        if target.bot:
            raise GatewayError("Bot accounts are outside the timeout scope.", "bot_target")
        if actor.id != guild.owner_id and actor.top_role <= target.top_role:
            raise GatewayError("Your highest role must be above the target member.", "actor_hierarchy")

        bot_member = guild.me
        if bot_member is None:
            raise GatewayError("The bot's guild member could not be verified.", "bot_unverified")
        if not (bot_member.guild_permissions.administrator or bot_member.guild_permissions.moderate_members):
            raise GatewayError("The bot lacks the Discord `moderate members` permission.", "bot_permission")
        if bot_member.top_role <= target.top_role:
            raise GatewayError("The bot's highest role must be above the target member.", "bot_hierarchy")
        return target

    def _new_request(
        self,
        interaction: discord.Interaction,
        action: str,
        reason: str,
        **kwargs: object,
    ) -> ActionRequest:
        actor = self._actual_actor(interaction, action)
        return ActionRequest(
            request_id=uuid4().hex[:12],
            action=action,
            guild_id=interaction.guild_id,
            actor_id=actor.id,
            actor_label=sanitize_text(str(actor), limit=120),
            reason=validate_reason(reason),
            channel_id=getattr(interaction, "channel_id", None),
            **kwargs,
        )

    async def _deny(self, interaction: discord.Interaction, exc: GatewayError) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed("Action rejected", exc.message), ephemeral=True)
        else:
            await interaction.response.send_message(
                embed=error_embed("Action rejected", exc.message), ephemeral=True
            )

    async def _send_confirmation(
        self, interaction: discord.Interaction, request: ActionRequest, detail: str
    ) -> None:
        if not await self.audit.append(request, "pending_confirmation"):
            await self._deny(
                interaction,
                GatewayError("The audit log is unavailable, so the action was not opened.", "audit_unavailable"),
            )
            return
        view = ConfirmActionView(self, request)
        embed = info_embed(
            "Confirm moderator action",
            f"**Action:** `{request.action}`\n**Target:** {request.target_label or request.amp_instance_name}"
            f"\n**Reason:** {request.reason}\n{detail}\n\nRequest `{request.request_id}` expires in 60 seconds.",
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @mod_action.command(name="timeout", description="Request a reversible Discord member timeout")
    @app_commands.describe(member="Member to timeout", minutes="Duration (maximum 28 days)", reason="Required audit reason")
    async def timeout_member(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES],
        reason: str,
    ):
        try:
            actor = self._actual_actor(interaction, "discord.timeout")
            self._validate_member_scope(interaction, actor, member)
            request = self._new_request(
                interaction,
                "discord.timeout",
                reason,
                target_id=member.id,
                target_label=sanitize_text(str(member), limit=120),
                timeout_minutes=int(minutes),
            )
        except GatewayError as exc:
            await self._deny(interaction, exc)
            return
        await self._send_confirmation(interaction, request, f"**Duration:** {minutes} minute(s)")

    @mod_action.command(name="remove_timeout", description="Request removal of a Discord member timeout")
    @app_commands.describe(member="Member whose timeout should be removed", reason="Required audit reason")
    async def remove_timeout_member(
        self, interaction: discord.Interaction, member: discord.Member, reason: str
    ):
        try:
            actor = self._actual_actor(interaction, "discord.remove_timeout")
            self._validate_member_scope(interaction, actor, member)
            request = self._new_request(
                interaction,
                "discord.remove_timeout",
                reason,
                target_id=member.id,
                target_label=sanitize_text(str(member), limit=120),
            )
        except GatewayError as exc:
            await self._deny(interaction, exc)
            return
        await self._send_confirmation(interaction, request, "This clears the current timeout.")

    @mod_action.command(name="amp", description="Request a scoped AMP start, stop, or restart")
    @app_commands.describe(instance="Exact AMP instance or friendly name", action="Allowlisted AMP action", reason="Required audit reason")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Start", value="start"),
            app_commands.Choice(name="Stop", value="stop"),
            app_commands.Choice(name="Restart", value="restart"),
        ]
    )
    async def amp_action(
        self,
        interaction: discord.Interaction,
        instance: str,
        action: app_commands.Choice[str],
        reason: str,
    ):
        canonical = f"amp.{action.value}"
        try:
            self._actual_actor(interaction, canonical)
            instances = await fetch_valid_instances()
            selected = select_amp_instance(instances, instance)
            internal = sanitize_text(selected.instance_name, limit=150)
            label = sanitize_text(selected.friendly_name or internal, limit=120)
            request = self._new_request(
                interaction,
                canonical,
                reason,
                target_label=label,
                amp_instance_name=internal,
            )
        except GatewayError as exc:
            await self._deny(interaction, exc)
            return
        await self._send_confirmation(interaction, request, "The instance state is rechecked at confirmation.")

    async def execute_confirmed_amp_action(
        self,
        interaction: discord.Interaction,
        action: str,
        instance_name: str,
        reason: str,
        *,
        origin: str = "amp_modal",
    ) -> ActionResult:
        """Gateway entry point for the existing AMP reason/confirmation modal."""
        canonical = f"amp.{sanitize_text(action, limit=20).casefold()}"
        try:
            request = self._new_request(
                interaction,
                canonical,
                reason,
                target_label=sanitize_text(instance_name, limit=120),
                amp_instance_name=sanitize_text(instance_name, limit=150),
                origin=origin,
            )
        except GatewayError as exc:
            return ActionResult(False, "Action rejected", exc.message)
        if not await self.audit.append(request, "confirmed"):
            return ActionResult(False, "Action rejected", "The audit log is unavailable; no action was executed.")
        return await self.execute_request(interaction, request, preaudited=True)

    async def execute_request(
        self, interaction: discord.Interaction, request: ActionRequest, *, preaudited: bool = False
    ) -> ActionResult:
        """Revalidate the actual confirmer and execute exactly one allowlisted action."""
        try:
            actor = self._actual_actor(interaction, request.action)
            if actor.id != request.actor_id or interaction.guild_id != request.guild_id:
                raise GatewayError("The confirming actor or guild does not match this request.", "confirmation_scope_changed")

            if request.action.startswith("discord."):
                target = interaction.guild.get_member(request.target_id)
                if target is None:
                    try:
                        target = await interaction.guild.fetch_member(request.target_id)
                    except (discord.HTTPException, discord.NotFound):
                        raise GatewayError("The target member is no longer available.", "target_missing")
                self._validate_member_scope(interaction, actor, target)
            else:
                target = None

            if not preaudited and not await self.audit.append(request, "confirmed"):
                raise GatewayError("The audit log is unavailable; no action was executed.", "audit_unavailable")
            if not await self.audit.append(request, "execution_started"):
                raise GatewayError("The audit log is unavailable; no action was executed.", "audit_unavailable")

            if request.action == "discord.timeout":
                minutes = int(request.timeout_minutes or 0)
                if not 1 <= minutes <= MAX_TIMEOUT_MINUTES:
                    raise GatewayError("The timeout duration is outside Discord's 28-day limit.", "invalid_duration")
                await target.timeout(
                    timedelta(minutes=minutes),
                    reason=f"CalmBot request {request.request_id}: {request.reason}"[:512],
                )
                message = f"{request.target_label} was timed out for {minutes} minute(s). Use `/mod_action remove_timeout` to reverse it."
            elif request.action == "discord.remove_timeout":
                await target.timeout(
                    None,
                    reason=f"CalmBot request {request.request_id}: {request.reason}"[:512],
                )
                message = f"The timeout for {request.target_label} was removed."
            else:
                instances = await fetch_valid_instances()
                instance = select_amp_instance(
                    instances, request.amp_instance_name, internal_only=True
                )
                try:
                    status = await asyncio.wait_for(instance.get_instance_status(), timeout=8)
                except (asyncio.TimeoutError, Exception) as exc:
                    # Catching here yields a stable, sanitized error instead of invoking blind.
                    raise GatewayError(
                        "AMP state could not be verified; no action was executed.",
                        f"amp_status_{type(exc).__name__}",
                    )
                state = get_instance_state(status).casefold()
                if request.action == "amp.start" and state != "stopped":
                    raise GatewayError(f"Start requires Stopped state; current state is {state or 'unknown'}.", "invalid_amp_state")
                if request.action in {"amp.stop", "amp.restart"} and state != "running":
                    raise GatewayError(f"{request.action.split('.')[-1].title()} requires Running state; current state is {state or 'unknown'}.", "invalid_amp_state")
                method_name = AMP_METHODS[request.action]
                method = getattr(instance, method_name, None)
                if not callable(method):
                    raise GatewayError("This AMP API does not expose the requested operation.", "amp_api_unsupported")
                try:
                    await asyncio.wait_for(method(), timeout=20)
                except asyncio.TimeoutError:
                    raise GatewayError("AMP did not acknowledge the action before the timeout.", "amp_action_timeout")
                message = f"AMP {request.action.split('.')[-1]} was requested for {request.target_label}."

            await self.audit.append(request, "succeeded")
            log.warning(
                "MODERATOR AUDIT succeeded request=%s actor_id=%s guild_id=%s action=%s target_id=%s instance=%s",
                request.request_id,
                request.actor_id,
                request.guild_id,
                request.action,
                request.target_id,
                request.amp_instance_name,
            )
            return ActionResult(True, "Moderator action completed", message)
        except GatewayError as exc:
            await self.audit.append(request, "rejected", error_code=exc.code)
            return ActionResult(False, "Action rejected", exc.message)
        except (discord.Forbidden, discord.NotFound) as exc:
            await self.audit.append(request, "failed", error_type=type(exc).__name__)
            return ActionResult(False, "Action failed", "Discord rejected the action because the target or permissions changed.")
        except discord.HTTPException as exc:
            await self.audit.append(request, "failed", error_type=type(exc).__name__)
            return ActionResult(False, "Action failed", "Discord did not accept the action. No raw API details were exposed.")
        except Exception as exc:
            await self.audit.append(request, "failed", error_type=type(exc).__name__)
            log.error("Moderator action %s failed (%s)", request.request_id, type(exc).__name__)
            return ActionResult(False, "Action failed", "The action failed safely; see the sanitized audit event.")


async def setup(bot: commands.Bot):
    await bot.add_cog(ModeratorActions(bot))
