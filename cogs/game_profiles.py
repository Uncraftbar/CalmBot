"""Game capability profiles for AMP-managed instances.

Keep game-specific behavior here so adding another game normally means adding one
profile (or a config override), not sprinkling application-name checks across cogs.
"""
from dataclasses import dataclass, replace
import re
from typing import Optional

import config


@dataclass(frozen=True)
class GameProfile:
    key: str
    label: str
    match: tuple[str, ...] = ()
    amp_management: bool = True
    player_metrics: bool = True
    console_events: bool = False
    chat_receive: bool = False
    chat_send: bool = False
    chat_command_template: Optional[str] = None
    minecraft: bool = False
    spark: bool = False


PROFILES = {
    "generic": GameProfile("generic", "Game server"),
    "minecraft": GameProfile(
        "minecraft", "Minecraft", ("minecraft", "paper", "purpur", "forge", "fabric", "neoforge"),
        console_events=True, chat_receive=True, chat_send=True,
        chat_command_template="tellraw @a {payload}", minecraft=True, spark=True,
    ),
    # AMP's Generic Module can manage Dune today. Chat is deliberately disabled:
    # neither AMP nor Dune exposes a stable, verified console chat protocol that
    # CalmBot can safely assume. It can be enabled later with a per-instance
    # template/parser once that protocol is confirmed.
    "dune_awakening": GameProfile(
        "dune_awakening", "Dune: Awakening", ("dune: awakening", "dune awakening", "duneawakening", "dune"),
        console_events=True,
    ),
    "hytale": GameProfile("hytale", "Hytale", ("hytale",)),
}


def _text(instance) -> str:
    fields = (
        getattr(instance, "instance_name", ""), getattr(instance, "friendly_name", ""),
        getattr(instance, "module_display_name", ""), getattr(instance, "module", ""),
        getattr(instance, "application_name", ""),
    )
    return " ".join(str(v) for v in fields if v).casefold()


def instance_key(instance) -> str:
    return str(getattr(instance, "instance_name", "") or getattr(instance, "friendly_name", "")).strip()


def _configured_profiles():
    profiles = dict(PROFILES)
    for key, raw in getattr(config, "GAME_PROFILES", {}).items():
        if not isinstance(raw, dict):
            continue
        base = profiles.get(raw.get("extends", key), profiles["generic"])
        allowed = {name for name in GameProfile.__dataclass_fields__ if name not in {"key"}}
        values = {name: value for name, value in raw.items() if name in allowed}
        if "match" in values:
            values["match"] = tuple(str(v).casefold() for v in values["match"])
        profiles[str(key)] = replace(base, key=str(key), **values)
    return profiles


def get_game_profile(instance) -> GameProfile:
    profiles = _configured_profiles()
    overrides = getattr(config, "GAME_INSTANCE_OVERRIDES", {})
    raw = overrides.get(instance_key(instance), {}) if isinstance(overrides, dict) else {}
    if isinstance(raw, str):
        raw = {"profile": raw}
    text = _text(instance)
    selected = raw.get("profile") if isinstance(raw, dict) else None
    if selected not in profiles:
        selected = "generic"
        for key, profile in profiles.items():
            if key != "generic" and any(token.casefold() in text for token in profile.match):
                selected = key
                break
    profile = profiles.get(selected, profiles["generic"])
    if isinstance(raw, dict):
        allowed = set(GameProfile.__dataclass_fields__) - {"key", "match"}
        profile = replace(profile, **{k: v for k, v in raw.items() if k in allowed})
    return profile


def plain_chat_command(profile: GameProfile, message: str) -> Optional[str]:
    """Build a configured non-Minecraft outbound chat command, if supported."""
    if not profile.chat_send or not profile.chat_command_template:
        return None
    safe = re.sub(r"[\r\n\x00-\x1f]", " ", str(message)).strip()[:1800]
    return profile.chat_command_template.format(message=safe, payload=safe)
