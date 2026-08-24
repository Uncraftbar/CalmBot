"""
Chat bridge for CalmBot.
Bridges chat between multiple Minecraft servers and Discord.
"""

import asyncio
import json
import re
import traceback
from collections import deque
from types import SimpleNamespace
from datetime import datetime, timezone
from urllib.parse import urlparse, quote

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands
from ampapi import Bridge as AMPBridge, AMPControllerInstance
from ampapi.dataclass import APIParams
from mcstatus import JavaServer

import config
from cogs.game_profiles import get_game_profile, plain_chat_command
from cogs.utils import (
    get_logger,
    load_json,
    save_json,
    admin_only,
    check_permissions,
    fetch_valid_instances,
    info_embed,
    success_embed,
    error_embed,
    CHAT_BRIDGE_FILE
)

log = get_logger("bridge")

class ChatBridge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Data structure: 
        # { "groups": { "group_name": { "servers": ["instance_1", "instance_2"], "active": True } } }
        self.bridge_data = {"groups": {}}
        
        self.amp_url = config.AMP_API_URL
        self.amp_user = config.AMP_USER
        self.amp_pass = config.AMP_PASS
        self.api_params = APIParams(url=self.amp_url, user=self.amp_user, password=self.amp_pass)
        self.amp_bridge = AMPBridge(api_params=self.api_params)
        self.ads = AMPControllerInstance()
        
        self.instances = {}
        # Stores the "High Water Mark" for each server: 
        # { "server_name": { "ts": datetime_obj, "hashes": set() } }
        self.high_water_marks = {}
        self.failure_counts = {}
        self.console_listeners = []
        # Bounded in-memory console history powers authorized, read-only LLM diagnostics.
        # It intentionally does not persist potentially sensitive server logs to disk.
        self.console_history = {}
        self.webhook_cache = {}
        # Webhook IDs owned by this bridge are the trust boundary for game-chat
        # LLM ingress. A normal Discord user cannot impersonate one.
        self.bridge_webhook_ids = set()
        self.send_locks = {}
        self.send_queues = {}
        self.send_workers = {}
        self.last_instance_refresh = datetime.min.replace(tzinfo=timezone.utc)

        # AMP pushes console events in real time. Each active instance gets one
        # socket; the existing GetUpdates path remains as an automatic fallback.
        self.ws_tasks = {}
        self.ws_connected = set()
        self.ws_entry_queues = {}
        self.ws_drop_counts = {}
        self.ws_drop_last_log = {}
        self.last_fallback_poll = {}
        self.ws_base_url = self._make_ws_base_url(self.amp_url)
        self.ws_alerted = set()
        self.ws_disconnect_since = {}
        self.last_health_alert = {}
        self.send_failed = set()
        self.discord_invite_cache = None

        self.sync_loop.start()

    async def cog_load(self):
        self.bridge_data = load_json(CHAT_BRIDGE_FILE, {"groups": {}, "instance_settings": {}})
        await self._refresh_instances()

    async def cog_unload(self):
        self.sync_loop.cancel()
        for task in self.ws_tasks.values():
            task.cancel()
        if self.ws_tasks:
            await asyncio.gather(*self.ws_tasks.values(), return_exceptions=True)
        self.ws_tasks.clear()
        self.ws_connected.clear()
        for task in self.send_workers.values():
            task.cancel()
        if self.send_workers:
            await asyncio.gather(*self.send_workers.values(), return_exceptions=True)
        self.send_workers.clear()
        self.send_queues.clear()

    @staticmethod
    def _make_ws_base_url(http_url):
        parsed = urlparse(http_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"

    def _active_instances(self):
        active = {}
        for group_data in self.bridge_data.get("groups", {}).values():
            if not group_data.get("active", True):
                continue
            for name in group_data.get("servers", []):
                if name in self.instances and self._profile(self.instances[name]).chat_receive:
                    active[name] = self.instances[name]
        return active

    def _reconcile_ws_tasks(self, active_instances):
        for name in list(self.ws_tasks):
            task = self.ws_tasks[name]
            if name not in active_instances or task.done():
                if not task.done():
                    task.cancel()
                self.ws_tasks.pop(name, None)
                self.ws_connected.discard(name)
        for name, instance in active_instances.items():
            if name not in self.ws_tasks:
                self.ws_entry_queues.setdefault(name, asyncio.Queue(maxsize=1000))
                self.ws_tasks[name] = asyncio.create_task(
                    self._amp_ws_worker(name, instance), name=f"amp-ws-{name}"
                )

    async def _amp_ws_worker(self, name, instance):
        """Maintain AMP's pushed-event stream, reconnecting with bounded backoff."""
        delay = 1
        while True:
            try:
                await instance._connect()
                session = instance._bridge._sessions.get(instance.instance_id)
                if not session or not getattr(session, "id", None) or session.id == "0":
                    raise RuntimeError("AMP did not issue a session for the event stream")

                # ADS-managed instances are streamed through the controller URL.
                ws_url = f"{self.ws_base_url}/stream/{instance.instance_id}/{session.id}"
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
                async with aiohttp.ClientSession(timeout=timeout) as client:
                    async with client.ws_connect(ws_url, heartbeat=30, autoping=True) as ws:
                        self.ws_connected.add(name)
                        self.failure_counts.pop(name, None)
                        self.ws_disconnect_since.pop(name, None)
                        if name in self.ws_alerted:
                            self.ws_alerted.discard(name)
                            asyncio.create_task(self._send_health_alert(
                                f"✅ AMP event stream recovered for **{name}**; real-time delivery is active again."
                            ))
                        delay = 1
                        log.info(f"AMP event stream connected for {name}")
                        async for message in ws:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    event = json.loads(message.data)
                                except (TypeError, json.JSONDecodeError):
                                    continue
                                if event.get("Message") == "ConsoleEntry":
                                    self._queue_ws_console_entry(name, event.get("Parameters"))
                            elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                raise ConnectionError("AMP event stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ws_connected.discard(name)
                self.ws_disconnect_since.setdefault(name, datetime.now(timezone.utc))
                log.warning(f"AMP event stream unavailable for {name}; polling fallback active: {exc}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
            finally:
                self.ws_connected.discard(name)

    async def _send_health_alert(self, message):
        settings = self.bridge_data.get("health_alerts", {})
        if not settings.get("enabled") or not settings.get("channel_id"):
            return
        channel = self.bot.get_channel(settings["channel_id"])
        if not channel:
            return
        key = message.split("**")[1] if "**" in message else message
        now = datetime.now(timezone.utc)
        if (now - self.last_health_alert.get(key, datetime.min.replace(tzinfo=timezone.utc))).total_seconds() < 60:
            return
        self.last_health_alert[key] = now
        try:
            await channel.send(message)
        except Exception as exc:
            log.warning(f"Failed to send bridge health alert: {exc}")

    async def _check_stream_health(self):
        now = datetime.now(timezone.utc)
        for name, since in list(self.ws_disconnect_since.items()):
            if name in self.ws_connected:
                self.ws_disconnect_since.pop(name, None)
                continue
            if name not in self.ws_alerted and (now - since).total_seconds() >= 60:
                self.ws_alerted.add(name)
                await self._send_health_alert(
                    f"⚠️ AMP event stream unavailable for **{name}** for 60 seconds; polling fallback remains active."
                )

    def _queue_ws_console_entry(self, name, data):
        if not isinstance(data, dict):
            return
        # AMP's websocket uses PascalCase; ampapi's dataclasses use snake_case.
        entry = SimpleNamespace(
            timestamp=data.get("Timestamp") or data.get("timestamp"),
            source=data.get("Source") or data.get("source") or "",
            contents=data.get("Contents") or data.get("contents") or "",
            type=data.get("Type") or data.get("type") or "",
        )
        queue = self.ws_entry_queues.setdefault(name, asyncio.Queue(maxsize=1000))
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            # A noisy server can send a historical console burst containing
            # hundreds of thousands of entries. Logging once per dropped entry
            # amplified that burst enough to starve Discord interaction handling.
            count = self.ws_drop_counts.get(name, 0) + 1
            self.ws_drop_counts[name] = count
            now = datetime.now(timezone.utc).timestamp()
            last = self.ws_drop_last_log.get(name, 0.0)
            if now - last >= 30:
                log.warning("Dropped %s stale queued AMP event(s) for %s in the last interval", count, name)
                self.ws_drop_counts[name] = 0
                self.ws_drop_last_log[name] = now
        queue.put_nowait(entry)

    @staticmethod
    def _console_timestamp(value):
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        raw = str(value or "")
        amp_epoch = re.fullmatch(r"/Date\((\d+)(?:[+-]\d+)?\)/", raw)
        try:
            return (datetime.fromtimestamp(int(amp_epoch.group(1)) / 1000, tz=timezone.utc)
                    if amp_epoch else datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except (ValueError, OSError, OverflowError):
            return datetime.now(timezone.utc)

    @staticmethod
    def _redact_console_line(text):
        """Remove common credentials and network identifiers before LLM exposure."""
        value = str(text or "").replace("\x00", "")[:2000]
        value = re.sub(r"(?i)\b(authorization|password|passwd|token|secret|api[_ -]?key)\s*[:=]\s*\S+",
                       r"\1=[REDACTED]", value)
        value = re.sub(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]+=*", r"\1 [REDACTED]", value)
        value = re.sub(r"(?<![\w:])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?", "[IP REDACTED]", value)
        return value

    def _remember_console_entries(self, source_name, entries):
        history = self.console_history.setdefault(source_name, deque(maxlen=2000))
        for entry in entries:
            contents = str(getattr(entry, "contents", "") or "").strip()
            if not contents:
                continue
            history.append({
                "timestamp": self._console_timestamp(getattr(entry, "timestamp", None)),
                "source": str(getattr(entry, "source", "") or "")[:80],
                "type": str(getattr(entry, "type", "") or "")[:40],
                "contents": contents[:2000],
            })

    def recent_console(self, source_name, *, minutes=15, limit=120):
        """Return a bounded, redacted snapshot; this never sends a console command."""
        minutes = max(1, min(int(minutes), 60))
        limit = max(1, min(int(limit), 200))
        cutoff = datetime.now(timezone.utc).timestamp() - minutes * 60
        result = []
        for item in reversed(self.console_history.get(source_name, ())):
            if item["timestamp"].timestamp() < cutoff:
                break
            result.append({
                "timestamp": item["timestamp"].isoformat(),
                "source": item["source"],
                "type": item["type"],
                "contents": self._redact_console_line(item["contents"]),
            })
            if len(result) >= limit:
                break
        result.reverse()
        return result

    async def run_console_command(self, instance, command, pattern, timeout=10.0, quiet_period=0.5):
        """Register a pushed-event listener before sending a console command."""
        source_name = instance.friendly_name or instance.instance_name
        collector = asyncio.create_task(
            self.collect_console(source_name, pattern, timeout=timeout, quiet_period=quiet_period)
        )
        await asyncio.sleep(0)  # allow collect_console to register before the command
        sent = await self._send_message_safe(instance, command, source_name)
        if not sent:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            raise ConnectionError(f"Failed to send console command to {source_name}")
        return await collector

    async def collect_console(self, source_name, pattern, timeout=10.0, quiet_period=0.5):
        """Collect pushed console lines until a match, then wait briefly for trailing lines."""
        regex = re.compile(pattern) if isinstance(pattern, str) else pattern
        queue = asyncio.Queue()
        future = asyncio.get_running_loop().create_future()
        listener = {"source": source_name, "regex": regex, "future": future, "queue": queue}
        self.console_listeners.append(listener)
        lines = []
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            while True:
                try:
                    lines.append(await asyncio.wait_for(queue.get(), timeout=quiet_period))
                except asyncio.TimeoutError:
                    break
            return lines
        finally:
            if listener in self.console_listeners:
                self.console_listeners.remove(listener)

    async def _refresh_instances(self):
        try:
            fetched_instances = await fetch_valid_instances()
            
            if not fetched_instances:
                log.debug("No instances returned from API")
                return

            self.instances = {}
            for inst in fetched_instances:
                name = inst.friendly_name or inst.instance_name
                self.instances[name] = inst
            self.last_instance_refresh = datetime.now(timezone.utc)
            self.failure_counts.clear()
            log.info(f"Refreshed {len(self.instances)} AMP instance(s)")
            
        except Exception as e:
            log.error(f"Error refreshing instances: {e}")

    async def _fetch_update_safe(self, name, instance):
        try:
            updates = await asyncio.wait_for(instance.get_updates(format_data=True), timeout=10.0)
            self.failure_counts.pop(name, None)
            return name, updates
        except asyncio.TimeoutError:
            self.failure_counts[name] = self.failure_counts.get(name, 0) + 1
            log.warning(f"Timeout fetching updates for {name} ({self.failure_counts[name]} consecutive)")
            return name, None
        except Exception as e:
            count = self.failure_counts.get(name, 0) + 1
            self.failure_counts[name] = count
            if count == 1 or count % 30 == 0:
                log.warning(f"Error fetching updates for {name} ({count} consecutive): {e}")
            return name, None

    def _sanitize_for_minecraft(self, text):
        if not text: return ""
        # 1. Remove newlines/returns to prevent console command injection
        text = text.replace('\n', ' ').replace('\r', '')
        # 2. Escape backslashes first, then quotes for valid JSON
        return text.replace('\\', '\\\\').replace('\"', '\\\"')


    def _extract_balanced_value(self, text, key):
        """Extract an SNBT value after `key:` while respecting nested braces/lists/strings."""
        m = re.search(r'(?<![\w:])' + re.escape(key) + r'\s*:', text)
        if not m:
            return None

        i = m.end()
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return None

        quote = None
        escape = False
        stack = []
        start = i
        while i < len(text):
            ch = text[i]
            if quote:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == quote:
                    quote = None
            else:
                if ch in ('"', "'"):
                    quote = ch
                elif ch in '{[':
                    stack.append('}' if ch == '{' else ']')
                elif ch in '}]':
                    if stack and ch == stack[-1]:
                        stack.pop()
                    elif not stack:
                        break
                elif ch == ',' and not stack:
                    break
            i += 1

        return text[start:i].strip()

    def _split_top_level_snbt(self, text):
        """Split a comma-delimited SNBT list/compound body without breaking nested values."""
        if not text:
            return []
        parts = []
        quote = None
        escape = False
        stack = []
        start = 0
        for i, ch in enumerate(text):
            if quote:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == quote:
                    quote = None
            else:
                if ch in ('"', "'"):
                    quote = ch
                elif ch in '{[':
                    stack.append('}' if ch == '{' else ']')
                elif ch in '}]':
                    if stack and ch == stack[-1]:
                        stack.pop()
                elif ch == ',' and not stack:
                    part = text[start:i].strip()
                    if part:
                        parts.append(part)
                    start = i + 1
        tail = text[start:].strip()
        if tail:
            parts.append(tail)
        return parts

    def _strip_numeric_suffixes_for_jsonish_snbt(self, text):
        """Make Mojang SNBT closer to JSON where needed: 1b/2s/3L/4.0f -> numbers."""
        if text is None:
            return None
        out = []
        quote = None
        escape = False
        i = 0
        while i < len(text):
            ch = text[i]
            if quote:
                out.append(ch)
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == quote:
                    quote = None
                i += 1
                continue
            if ch in ('"', "'"):
                quote = ch
                out.append(ch)
                i += 1
                continue
            m = re.match(r'[-+]?(?:\d+\.\d+|\d+)(?:[bBsSlLfFdD])(?=\s*[,}\]])', text[i:])
            if m:
                out.append(re.sub(r'[bBsSlLfFdD]$', '', m.group(0)))
                i += len(m.group(0))
                continue
            out.append(ch)
            i += 1
        return ''.join(out)

    def _quote_unquoted_snbt_keys_for_jsonish(self, text):
        """Quote bare SNBT keys so data-component compounds survive old JSON chat parsing."""
        if text is None:
            return None
        out = []
        quote = None
        escape = False
        i = 0
        while i < len(text):
            ch = text[i]
            if quote:
                out.append(ch)
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == quote:
                    quote = None
                i += 1
                continue
            if ch in ('"', "'"):
                quote = ch
                out.append(ch)
                i += 1
                continue
            if ch in '{,':
                out.append(ch)
                i += 1
                while i < len(text) and text[i].isspace():
                    out.append(text[i])
                    i += 1
                m = re.match(r'([A-Za-z0-9_+\-.]+(?::[A-Za-z0-9_+\-./]+)?)(\s*:)', text[i:])
                if m:
                    out.append(json.dumps(m.group(1), ensure_ascii=False))
                    out.append(m.group(2))
                    i += len(m.group(0))
                continue
            out.append(ch)
            i += 1
        return ''.join(out)

    def _component_value_for_json_chat(self, value):
        if value is None:
            return None
        v = self._strip_numeric_suffixes_for_jsonish_snbt(value.strip())
        v = self._quote_unquoted_snbt_keys_for_jsonish(v)
        return v

    def _extract_component_pairs(self, components):
        """Return [(component_id, value_snbt)] from a SelectedItem components compound."""
        if not components or not components.startswith('{') or not components.endswith('}'):
            return []
        inner = components[1:-1].strip()
        pairs = []
        key_pattern = re.compile(
            r'^"((?:[^"\\]|\\.)*)"\s*:|^([A-Za-z0-9_.+/-]+(?::[A-Za-z0-9_.+/-]+)?)\s*:'
        )
        for part in self._split_top_level_snbt(inner):
            stripped = part.strip()
            match = key_pattern.match(stripped)
            if not match:
                continue
            key = match.group(1) if match.group(1) is not None else match.group(2)
            if match.group(1) is not None:
                try:
                    key = json.loads('"' + key + '"')
                except Exception:
                    pass
            value = stripped[match.end():].strip()
            if key and value:
                pairs.append((key, value))
        return pairs

    def _components_for_json_chat(self, components):
        if not components:
            return None
        pairs = self._extract_component_pairs(components)
        if not pairs:
            return self._component_value_for_json_chat(components)
        body = []
        for key, value in pairs:
            body.append(json.dumps(key, ensure_ascii=False) + ':' + self._component_value_for_json_chat(value))
        return '{' + ','.join(body) + '}'

    def _components_for_modern_snbt_chat(self, components):
        if not components:
            return None
        pairs = self._extract_component_pairs(components)
        if not pairs:
            return components
        body = []
        for key, value in pairs:
            body.append(json.dumps(key, ensure_ascii=False) + ':' + value.strip())
        return '{' + ','.join(body) + '}'

    def _convert_selected_item_to_legacy_stack(self, raw):
        """Best-effort conversion from 1.20.5+ SelectedItem components to legacy item-stack SNBT."""
        item_id = self._extract_balanced_value(raw, 'id') or '"minecraft:air"'
        count = self._extract_balanced_value(raw, 'count') or self._extract_balanced_value(raw, 'Count') or '1'
        if count.endswith(('b', 's', 'l', 'f', 'd')):
            count_num = count[:-1]
        else:
            count_num = count

        components = self._extract_balanced_value(raw, 'components')
        tag_parts = []
        if components:
            custom_name = self._extract_balanced_value(components, 'minecraft:custom_name') or self._extract_balanced_value(components, 'custom_name')
            if custom_name:
                tag_parts.append('display:{Name:' + json.dumps(custom_name, ensure_ascii=False) + '}')

            lore = self._extract_balanced_value(components, 'minecraft:lore') or self._extract_balanced_value(components, 'lore')
            if lore:
                # Lore in old hover SNBT wants a list of JSON-text strings.
                # Modern component lore is already a component list, so stringify each rough element.
                lore_inner = lore[1:-1].strip() if lore.startswith('[') and lore.endswith(']') else lore
                entries = []
                depth = 0
                quote = None
                escape = False
                part_start = 0
                for i, ch in enumerate(lore_inner):
                    if quote:
                        if escape:
                            escape = False
                        elif ch == '\\':
                            escape = True
                        elif ch == quote:
                            quote = None
                    else:
                        if ch in ('"', "'"):
                            quote = ch
                        elif ch in '{[':
                            depth += 1
                        elif ch in '}]':
                            depth -= 1
                        elif ch == ',' and depth == 0:
                            part = lore_inner[part_start:i].strip()
                            if part:
                                entries.append(json.dumps(part, ensure_ascii=False))
                            part_start = i + 1
                part = lore_inner[part_start:].strip()
                if part:
                    entries.append(json.dumps(part, ensure_ascii=False))
                if entries:
                    tag_parts.append('display:{Lore:[' + ','.join(entries) + ']}')

            custom_data = self._extract_balanced_value(components, 'minecraft:custom_data') or self._extract_balanced_value(components, 'custom_data')
            if custom_data and custom_data.startswith('{'):
                tag_parts.append(custom_data[1:-1])

        tag = ''
        if tag_parts:
            tag = ',tag:{' + ','.join(tag_parts) + '}'
        return '{id:' + item_id + ',Count:' + count_num + 'b' + tag + '}'

    def _extract_selected_item(self, data_text):
        """Parse vanilla `data get entity <player> SelectedItem` console output."""
        if not data_text:
            return None

        raw = data_text.strip()
        # AMP usually gives: Player has the following entity data: {id:"minecraft:stone",Count:1b,...}
        marker = " has the following entity data: "
        if marker in raw:
            raw = raw.split(marker, 1)[1].strip()

        if raw in ('{}', 'null') or 'No entity was found' in raw or 'Found no elements' in raw:
            return None

        item_id_snbt = self._extract_balanced_value(raw, 'id') or '"minecraft:air"'
        item_id = item_id_snbt.strip().strip('"\'') or "minecraft:air"
        short_id = item_id.split(":", 1)[-1]
        item_name = short_id.replace("_", " ").title()

        # Prefer custom display name when it is stored as plain JSON text in classic NBT.
        name_match = re.search(r'Name\s*:\s*\'({.*?})\'|Name\s*:\s*"(\\?\{.*?\\?\})"', raw)
        if name_match:
            try:
                name_json = (name_match.group(1) or name_match.group(2) or "").replace('\\"', '"')
                parsed = json.loads(name_json)
                if isinstance(parsed, dict) and parsed.get("text"):
                    item_name = str(parsed["text"])
            except Exception:
                pass

        custom_name = None
        components = self._extract_balanced_value(raw, 'components')
        if components:
            custom_name = self._extract_balanced_value(components, 'minecraft:custom_name') or self._extract_balanced_value(components, 'custom_name')
        if custom_name:
            fallback_name = custom_name.strip().strip('"\'')
            try:
                encoded_name = custom_name.strip()
                # Component values are commonly single-quoted JSON/SNBT strings.
                if len(encoded_name) >= 2 and encoded_name[0] == encoded_name[-1] == "'":
                    encoded_name = encoded_name[1:-1]
                parsed_name = json.loads(encoded_name)
                if isinstance(parsed_name, dict):
                    item_name = str(parsed_name.get("text") or parsed_name.get("translate") or fallback_name)
                elif isinstance(parsed_name, str):
                    item_name = parsed_name
                else:
                    item_name = fallback_name
            except Exception:
                item_name = fallback_name
            item_name = item_name[:80]

        count_raw = self._extract_balanced_value(raw, 'Count') or self._extract_balanced_value(raw, 'count') or "1"
        count_match = re.search(r'\d+', count_raw)
        count = count_match.group(0) if count_match else "1"

        legacy_stack = raw if 'Count' in raw and 'tag' in raw else self._convert_selected_item_to_legacy_stack(raw)

        if len(raw) > 1800:
            hover_text = raw[:1800] + "…"
        else:
            hover_text = raw

        return {"raw": raw, "hover_text": hover_text, "id": item_id, "name": item_name, "count": count, "components": components, "legacy_stack": legacy_stack}

    def _text_component_literal(self, text, **style):
        comp = {"text": text}
        comp.update({k: v for k, v in style.items() if v is not None})
        return json.dumps(comp, ensure_ascii=False)

    def _build_item_share_tellraw(self, source, color, user, item, hover_mode="show_item"): 
        """Build a tellraw command for sharing the held item.

        The bridge may talk to mixed Minecraft versions/modpacks. Build the same
        visible text with both hover syntaxes available:
        - modern 1.20.5-1.21.4 JSON: hoverEvent.contents{id,count,components}
        - legacy JSON: hoverEvent.value = full item stack SNBT string
        - 1.21.5+ SNBT: hover_event with id/count/components inlined
        """
        count = int(item.get("count") or 1)
        prefix = [
            json.dumps("", ensure_ascii=False),
            self._text_component_literal(f"[{source}] ", color=color),
            self._text_component_literal(f"<{user}> ", color="white"),
        ]
        visible = f"[{item['name']} x{count}]"
        legacy_stack = item.get("legacy_stack") or item.get("raw") or f'{{id:"{item["id"]}",Count:{count}b}}'

        legacy_hover = {
            "text": visible,
            "color": "light_purple",
            "hoverEvent": {"action": "show_item", "value": legacy_stack},
        }

        modern_hover = {
            "text": visible,
            "color": "light_purple",
            "hoverEvent": {"action": "show_item", "contents": {"id": item["id"], "count": count}},
        }
        json_components = self._components_for_json_chat(item.get("components"))
        if json_components:
            modern_json = json.dumps({"text": visible, "color": "light_purple"}, ensure_ascii=False)
            modern_json = modern_json[:-1] + (
                ',"hoverEvent":{"action":"show_item","contents":'
                f'{{"id":{json.dumps(item["id"], ensure_ascii=False)},'
                f'"count":{count},'
                f'"components":{json_components}}}'
                '}'
                '}'
            )
        else:
            modern_json = json.dumps(modern_hover, ensure_ascii=False)

        snbt_components = self._components_for_modern_snbt_chat(item.get("components"))

        legacy_cmd = "tellraw @a " + "[" + ",".join(prefix + [json.dumps(legacy_hover, ensure_ascii=False)]) + "]"
        modern_cmd = "tellraw @a " + "[" + ",".join(prefix + [modern_json]) + "]"

        modern_snbt_item = '{text:' + json.dumps(visible, ensure_ascii=False) + ',color:"light_purple",hover_event:{action:"show_item",id:' + json.dumps(item["id"], ensure_ascii=False) + ',count:' + str(count)
        if snbt_components:
            modern_snbt_item += ',components:' + snbt_components
        modern_snbt_item += '}}'
        snbt_cmd = 'tellraw @a ["",' + ",".join([
            '{text:' + json.dumps(f"[{source}] ", ensure_ascii=False) + ',color:' + json.dumps(color, ensure_ascii=False) + '}',
            '{text:' + json.dumps(f"<{user}> ", ensure_ascii=False) + ',color:"white"}',
            modern_snbt_item,
        ]) + "]"

        if hover_mode == "legacy":
            return [legacy_cmd]
        if hover_mode == "modern":
            return [modern_cmd]
        if hover_mode == "snbt":
            return [snbt_cmd]
        return [modern_cmd, legacy_cmd, snbt_cmd]

    def _profile(self, instance):
        return get_game_profile(instance)

    def _is_minecraft(self, instance):
        return bool(instance and self._profile(instance).minecraft)

    @staticmethod
    def _discord_text_for_minecraft(text):
        return re.sub(r"<a?:([A-Za-z0-9_]+):\d+>", r":\1:", text or "")

    def _minecraft_chat_json(self, safe_user, text):
        url_regex = r"(https?://[^\s<>]+?)(?=[.,!?;:)]*(?:\s|$))"
        components = ["", {"text": "[Discord] ", "color": "blue"},
                      {"text": f"<{safe_user}> ", "color": "white"}]
        for part in re.split(url_regex, text):
            if not part:
                continue
            component = {"text": part, "color": "white"}
            if re.fullmatch(url_regex, part):
                component.update({"color": "blue", "underlined": True,
                                  "clickEvent": {"action": "open_url", "value": part}})
            components.append(component)
        return "tellraw @a " + json.dumps(components, ensure_ascii=False)

    async def _discord_invite_url(self, channel):
        if self.discord_invite_cache:
            return self.discord_invite_cache
        configured = getattr(config, "DISCORD_INVITE_URL", None)
        if configured:
            self.discord_invite_cache = configured
            return configured
        try:
            guild = channel.guild
            vanity = await guild.vanity_invite() if getattr(guild, "vanity_url_code", None) else None
            invite = vanity or await channel.create_invite(max_age=0, max_uses=0, unique=False,
                                                           reason="CalmBot in-game !discord command")
            self.discord_invite_cache = invite.url
            return invite.url
        except Exception as exc:
            log.warning(f"Unable to obtain Discord invite: {exc}")
            return None

    def is_game_bridge_message(self, message) -> bool:
        """Return true only for messages created by this cog's owned webhook."""
        webhook_id = getattr(message, "webhook_id", None)
        if not webhook_id or int(webhook_id) not in self.bridge_webhook_ids:
            return False
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        return any(
            data.get("active", True) and data.get("channel_id") == channel_id
            for data in self.bridge_data.get("groups", {}).values()
        )

    async def broadcast_llm_response(self, source_message, text: str) -> int:
        """Send an LLM answer back to the game group that produced its prompt."""
        if not self.is_game_bridge_message(source_message):
            return 0
        channel_id = source_message.channel.id
        count = 0
        for group_data in self.bridge_data.get("groups", {}).values():
            if not group_data.get("active", True) or group_data.get("channel_id") != channel_id:
                continue
            for target_name in group_data.get("servers", []):
                target = self.instances.get(target_name)
                if not target:
                    continue
                safe_text = self._sanitize_for_minecraft(text)
                if self._is_minecraft(target):
                    cmd = self._minecraft_chat_json("CalmBot", text)
                else:
                    cmd = plain_chat_command(self._profile(target), f"[Discord] <CalmBot> {safe_text}")
                if cmd:
                    self._enqueue_send(target, cmd, target_name)
                    count += 1
            break
        return count

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if not self.bridge_data.get("groups"): return

        for group_name, group_data in self.bridge_data["groups"].items():
            if not group_data.get("active", True): continue
            
            # Check if this channel is linked to the group
            linked_channel_id = group_data.get("channel_id")
            if not linked_channel_id or message.channel.id != linked_channel_id: continue

            # Broadcast to all servers in the group
            instance_names = group_data.get("servers", [])
            if not instance_names: continue

            user = message.author.display_name
            parts = []
            reference = getattr(message, "reference", None)
            resolved = getattr(reference, "resolved", None) if reference else None
            if resolved and getattr(resolved, "author", None):
                quoted = self._discord_text_for_minecraft(getattr(resolved, "content", ""))
                quoted = re.sub(r"\s+", " ", quoted).strip()[:80]
                parts.append(f"↪ {resolved.author.display_name}: {quoted or '[attachment]'} | ")
            content = self._discord_text_for_minecraft(message.content)
            if content:
                parts.append(content)
            attachments = list(getattr(message, "attachments", []))[:3]
            if attachments:
                parts.append("Attachments: " + " ".join(a.url for a in attachments))
            msg = " ".join(parts).strip()
            if not msg:
                continue

            safe_user = self._sanitize_for_minecraft(user)
            safe_msg = self._sanitize_for_minecraft(msg)

            for target_name in instance_names:
                target = self.instances.get(target_name)
                if target:
                    if self._is_minecraft(target):
                        cmd = self._minecraft_chat_json(safe_user, msg)
                    else:
                        cmd = plain_chat_command(self._profile(target), f"[Discord] <{safe_user}> {safe_msg}")
                    if cmd:
                        self._enqueue_send(target, cmd, target_name)
            
            # We found the group, no need to check others (assuming 1:1 mapping preference)
            break

    async def broadcast_system_message(self, message: str, group_name: str = None) -> int:
        """Broadcasts a system message to bridged servers. Returns count of targets."""
        if not self.bridge_data.get("groups"): return 0

        unique_targets = set()
        for name, group_data in self.bridge_data["groups"].items():
            if not group_data.get("active", True): continue
            
            # Filter by group if specified
            if group_name and name.lower() != group_name.lower():
                continue
                
            for server_name in group_data.get("servers", []):
                unique_targets.add(server_name)
        
        if not unique_targets:
            return 0
        
        # Parse for links
        # Regex for URL
        url_regex = r'(https?://[^\s<>]+?)(?=[.,!?;:)]*(?:\s|$))'
        parts = re.split(url_regex, message)
        
        json_components = ['["",{"text":"[System] ", "color": "gold"}']
        
        for part in parts:
            if not part: continue
            if re.match(url_regex, part):
                # It's a link
                safe_url = self._sanitize_for_minecraft(part)
                json_components.append(f', {{ "text": "{safe_url}", "color": "blue", "underlined": true, "clickEvent": {{ "action": "open_url", "value": "{safe_url}" }} }}')
            else:
                # Normal text
                safe_text = self._sanitize_for_minecraft(part)
                json_components.append(f', {{ "text": "{safe_text}", "color": "yellow" }}')
        
        json_components.append(']')
        json_cmd = "".join(json_components)
        
        safe_plain_message = self._sanitize_for_minecraft(message)
        plain_cmd = f'tellraw @a "[System] {safe_plain_message}"'  # Fallback for non-MC
        
        count = 0
        for target_name in unique_targets:
            target = self.instances.get(target_name)
            if target:
                if self._is_minecraft(target):
                    self._enqueue_send(target, f"tellraw @a {json_cmd}", target_name)
                    count += 1
                else:
                    command = plain_chat_command(self._profile(target), f"[System] {safe_plain_message}")
                    if command:
                        self._enqueue_send(target, command, target_name)
                        count += 1
        return count

    async def _get_online_players(self, group_data):
        instance_names = group_data.get("servers", [])
        if not instance_names: return {}
        
        # Parse AMP URL for hostname
        try:
            parsed_url = urlparse(self.amp_url)
            hostname = parsed_url.hostname
            if not hostname: hostname = "localhost"
        except:
            hostname = "localhost"

        online_data = {} # { "Server Alias": [player1, player2] }

        async def fetch_server_status(server_name):
            inst = self.instances.get(server_name)
            if not inst or not inst.running: return None
            
            # Get Display Name
            settings = self.bridge_data.get("instance_settings", {}).get(server_name, {})
            display_name = settings.get("alias", server_name)
            
            # Only check Minecraft servers via mcstatus
            if not self._is_minecraft(inst):
                # Try to get from AMP status for non-Minecraft servers
                try:
                    status = await inst.get_instance_status()
                    # AMP's active_users can be list of structs or names
                    if status and hasattr(status, 'active_users'):
                         raw_users = status.active_users
                         users = []
                         if isinstance(raw_users, list):
                             # Ensure we get strings
                             users = [str(u.user_name if hasattr(u, 'user_name') else u) for u in raw_users]
                         elif isinstance(raw_users, dict):
                             users = list(raw_users.keys())
                         
                         if users:
                             users.sort()
                             return display_name, users
                except Exception:
                    pass
                return None
            
            mc_port = None
            if hasattr(inst, 'application_endpoints'):
                 for ep in inst.application_endpoints:
                     if ep.get('display_name') == 'Minecraft Server Address':
                         endpoint_str = ep.get('endpoint', '')
                         if ':' in endpoint_str:
                             try:
                                 mc_port = int(endpoint_str.split(':')[-1])
                             except: pass
                         break
            
            if not mc_port: return None

            try:
                address = f"{hostname}:{mc_port}"
                server = await JavaServer.async_lookup(address)
                status = await server.async_status()
                
                players = []
                if status.players.sample:
                    players = [p.name for p in status.players.sample]
                
                # Sort players
                players.sort()
                return display_name, players
                
            except Exception:
                return None

        # Fetch all statuses in parallel
        tasks = [fetch_server_status(name) for name in instance_names]
        results = await asyncio.gather(*tasks)

        for result in results:
            if result:
                display_name, players = result
                online_data[display_name] = players
        
        return online_data

    async def _update_channel_topic(self, group_name, group_data):
        linked_channel_id = group_data.get("channel_id")
        if not linked_channel_id: return
        
        channel = self.bot.get_channel(linked_channel_id)
        if not channel or not isinstance(channel, discord.TextChannel): return

        # Claim the interval before the first await so concurrent loops cannot all edit.
        last_update = group_data.get("last_topic_update", 0)
        current_time = datetime.now().timestamp()
        if current_time - last_update < 300:
            return
        group_data["last_topic_update"] = current_time

        online_data = await self._get_online_players(group_data)
        
        total_players = 0
        all_player_names = set()

        for players in online_data.values():
            total_players += len(players)
            for p in players:
                all_player_names.add(p)

        # Construct Topic
        topic = f"Online Players ({total_players})"
        if all_player_names:
             sorted_names = sorted(list(all_player_names))
             names_str = ", ".join(sorted_names)
             topic += f": {names_str}"
        
        if len(topic) > 1000:
            topic = topic[:1000] + "..."

        if channel.topic != topic:
             try:
                 await channel.edit(topic=topic)
             except Exception as e:
                 log.warning(f"Failed to update topic for {channel.name}: {e}")

    async def handle_minecraft_command(self, source_name, user, msg, group_data):
        command = msg.split(" ")[0].lower()
        
        target = self.instances.get(source_name)
        if not target: return
        
        is_minecraft = self._is_minecraft(target)
        
        if command == "!online":
            online_data = await self._get_online_players(group_data)
            
            if is_minecraft:
                # Construct Tellraw Message
                # Header
                json_msg = ['["",{"text":"[System] ", "color": "gold"}, {"text": "Online Players:", "color": "yellow"}']
                
                if not online_data:
                    json_msg.append(',{"text":"\\nNo players online.", "color": "gray"}]')
                else:
                    for server_alias, players in online_data.items():
                        p_list = ", ".join(players) if players else "None"
                        json_msg.append(f', {{"text": "\\n{server_alias}: ", "color": "aqua"}}, {{"text": "{p_list}", "color": "white"}}')
                    json_msg.append(']')
                
                full_cmd = "".join(json_msg)
                # Target the specific user
                final_cmd = f"tellraw {user} {full_cmd}"
            else:
                # Plain text for Hytale
                lines = ["[System] Online Players:"]
                if not online_data:
                    lines.append("No players online.")
                else:
                    for server_alias, players in online_data.items():
                        p_list = ", ".join(players) if players else "None"
                        lines.append(f"{server_alias}: {p_list}")
                
                full_msg = self._sanitize_for_minecraft(" | ".join(lines))
                final_cmd = f'tellraw @a "{full_msg}"'

            await self._send_message_safe(target, final_cmd, source_name)

        elif command == "!help":
            if is_minecraft:
                help_msg = '["",{"text":"[System] ", "color": "gold"}, {"text": "Available Commands:\\n", "color": "yellow"}, {"text": "!online ", "color": "aqua"}, {"text": "- List online players", "color": "white"}, {"text": "\\n!item ", "color": "aqua"}, {"text": "- Show held item", "color": "white"}]'
                final_cmd = f"tellraw {user} {help_msg}"
            else:
                final_cmd = 'tellraw @a "[System] Available Commands: !online, !discord"'
            
            await self._send_message_safe(target, final_cmd, source_name)

        elif command == "!discord":
            channel_id = group_data.get("channel_id")
            channel = self.bot.get_channel(channel_id) if channel_id else None
            invite = await self._discord_invite_url(channel) if channel else None
            if not invite:
                final_cmd = f'tellraw {user} ["",{{"text":"[System] Discord invite is unavailable. Please ask a moderator.","color":"red"}}]'
            elif is_minecraft:
                payload = ["", {"text": "[System] ", "color": "gold"},
                           {"text": "Join our Discord: ", "color": "yellow"},
                           {"text": invite, "color": "blue", "underlined": True,
                            "clickEvent": {"action": "open_url", "value": invite}}]
                final_cmd = f"tellraw {user} " + json.dumps(payload, ensure_ascii=False)
            else:
                final_cmd = f'tellraw @a "[System] Join our Discord: {self._sanitize_for_minecraft(invite)}"'
            await self._send_message_safe(target, final_cmd, source_name)

        elif command == "!item":
            if not is_minecraft: return # Not supported on non-minecraft

            target_inst = self.instances.get(source_name)
            if not target_inst: return

            # 1. Ask Minecraft for the full held-item SNBT, not just SelectedItem.id.
            cmd_check = f"data get entity {user} SelectedItem"
            
            # 2. Setup listener before sending so we cannot miss fast console output.
            # Pattern: PlayerName has the following entity data: {id:"minecraft:stone",Count:1b,...}
            pattern_str = f"{re.escape(user)} has the following entity data: (.+)$"
            regex = re.compile(pattern_str)
            
            fut = asyncio.Future()
            listener = {'source': source_name, 'regex': regex, 'future': fut}
            self.console_listeners.append(listener)
            await self._send_message_safe(target_inst, cmd_check, source_name)
            
            try:
                # Wait for response (4 seconds to cover 2 sync loops)
                match = await asyncio.wait_for(fut, timeout=4.0)
                item = self._extract_selected_item(match.group(0))
                if not item:
                    return
                
                # 3. Broadcast to all servers in the group (including source)
                settings = self.bridge_data.get("instance_settings", {}).get(source_name, {})
                display_name = settings.get("alias", source_name)
                color = settings.get("color", "aqua")

                instance_names = group_data.get("servers", [])
                for target_name in instance_names:
                    target = self.instances.get(target_name)
                    if target:
                        # Send exactly one hover syntax per target server. The old diagnostic
                        # behavior sent every compatible variant, which made servers that accept
                        # multiple JSON formats display duplicate !item lines. Default to the
                        # modern JSON hover because it preserves 1.20.5+ components such as
                        # damage/durability; per-instance overrides can still use legacy/snbt/all.
                        target_settings = self.bridge_data.get("instance_settings", {}).get(target_name, {})
                        hover_mode = target_settings.get("item_hover_mode") or "modern"
                        cmds = self._build_item_share_tellraw(display_name, color, user, item, hover_mode=hover_mode)
                        for cmd in cmds:
                            self._enqueue_send(target, cmd, target_name)
                
                # Send to Discord too if linked. Discord cannot do Minecraft-style hover,
                # so include the NBT/components in an embed field instead.
                discord_channel_id = group_data.get("channel_id")
                if discord_channel_id:
                    channel = self.bot.get_channel(discord_channel_id)
                    if channel:
                         embed = discord.Embed(
                             title=f"{user} Shared an Item", 
                             description=f"**{item['name']}** x{item['count']}\n`{item['id']}`", 
                             color=discord.Color.blue()
                         )
                         embed.add_field(name="NBT / Components", value=f"```snbt\n{item['hover_text'][:1000]}\n```", inline=False)
                         asyncio.create_task(self._send_discord_message_webhook(channel, user, None, display_name, avatar_url=f"https://mc-heads.net/avatar/{quote(user, safe='')}", embed=embed))

            except asyncio.TimeoutError:
                pass
            except Exception:
                traceback.print_exc()
            finally:
                if listener in self.console_listeners:
                    self.console_listeners.remove(listener)

    @app_commands.command(name="online", description="List online players across the bridged servers.")
    async def online_command(self, interaction: discord.Interaction):
        # Determine group based on channel
        target_group = None
        target_group_name = None
        
        if self.bridge_data.get("groups"):
            for name, data in self.bridge_data["groups"].items():
                if data.get("channel_id") == interaction.channel_id:
                    target_group = data
                    target_group_name = name
                    break
        
        if not target_group:
            await interaction.response.send_message("This channel is not linked to any bridge group.", ephemeral=True)
            return

        await interaction.response.defer()
        
        online_data = await self._get_online_players(target_group)
        
        total_count = sum(len(p) for p in online_data.values())
        
        embed = discord.Embed(title=f"Online Players - {target_group_name}", description=f"**Total:** {total_count}", color=discord.Color.green())
        
        if not online_data:
            embed.description += "\nNo players online."
        else:
            for alias, players in online_data.items():
                if players:
                    # Discord fields have 1024 char limit
                    p_str = ", ".join(players)
                    if len(p_str) > 1000: p_str = p_str[:1000] + "..."
                    embed.add_field(name=f"{alias} ({len(players)})", value=f"`{p_str}`", inline=False)
                else:
                    embed.add_field(name=f"{alias} (0)", value="*No players*", inline=False)
        
        await interaction.followup.send(embed=embed)

    @staticmethod
    def _classify_server_event(message):
        text = re.sub(r"^\[[^]]+\]\s*", "", message or "").strip()
        if re.search(r"\b(joined the game|logged in with entity id)\b", text, re.I):
            return "join"
        if re.search(r"\b(left the game|lost connection:)\b", text, re.I):
            return "leave"
        if re.search(r"\b(has made the advancement|has completed the challenge|has reached the goal)\b", text, re.I):
            return "advancement"
        death_patterns = (r" was slain by ", r" was shot by ", r" fell ", r" drowned$", r" burned to death$",
                          r" blew up$", r" hit the ground too hard$", r" tried to swim in lava$", r" died$")
        if any(re.search(pattern, text, re.I) for pattern in death_patterns):
            return "death"
        return None

    @tasks.loop(seconds=0.25)
    async def sync_loop(self):
        if not self.bridge_data.get("groups"): return

        # 1. Keep one pushed-event stream per unique active server.
        active_instances = self._active_instances()
        if not active_instances:
            return
        # Core/GetUpdates and /stream share AMP's per-instance session cursor.
        # On the deployed AMP version the socket emits Metrics but not ConsoleEntry,
        # while still consuming the update stream. Do not attach that socket to chat
        # instances: keep one authoritative GetUpdates consumer for console/chat.
        for task in self.ws_tasks.values():
            task.cancel()
        self.ws_tasks.clear()
        self.ws_connected.clear()

        # 2. Drain any already-queued entries, then poll the authoritative console
        # feed. A single consumer avoids AMP cursor races and missing player chat.
        updates_map = {}
        for name in active_instances:
            queue = self.ws_entry_queues.setdefault(name, asyncio.Queue(maxsize=1000))
            entries = []
            while len(entries) < 500:
                try:
                    entries.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if entries:
                updates_map[name] = SimpleNamespace(console_entries=entries)

        now = datetime.now(timezone.utc)
        # AMP's event stream carries metrics reliably, but current AMP versions do
        # not emit ConsoleEntry events on every game instance. Core/GetUpdates is
        # the authoritative per-session console feed, so poll it even while the
        # socket is connected. Treating a metrics-only socket as a complete chat
        # feed silently drops all Minecraft -> Discord messages.
        console_polls = []
        for name, instance in active_instances.items():
            last = self.last_fallback_poll.get(name, 0.0)
            interval = 0.75
            if now.timestamp() - last >= interval:
                self.last_fallback_poll[name] = now.timestamp()
                console_polls.append(self._fetch_update_safe(name, instance))
        if console_polls:
            for name, updates in await asyncio.gather(*console_polls):
                if updates and getattr(updates, "console_entries", None):
                    existing = updates_map.get(name)
                    if existing:
                        existing.console_entries.extend(updates.console_entries)
                    else:
                        updates_map[name] = updates

        # AMP handles go stale after instance restarts; periodically reacquire them.
        if any(count >= 3 for count in self.failure_counts.values()) and (now - self.last_instance_refresh).total_seconds() >= 60:
            for task in self.ws_tasks.values():
                task.cancel()
            self.ws_tasks.clear()
            self.ws_connected.clear()
            await self._refresh_instances()

        # 3. First Pass: Identify TRULY NEW messages for each server and update watermarks
        new_messages_per_server = {} # { "server_name": [(user, msg), ...] }

        for source_name, updates in updates_map.items():
            if not updates.console_entries: continue
            self._remember_console_entries(source_name, updates.console_entries)

            # Pre-process and sort entries by timestamp
            parsed_entries = []
            for entry in updates.console_entries:
                ts = getattr(entry, 'timestamp', None)
                if not ts: continue
                
                # If timestamp is already a datetime (converted by ampapi), use it.
                if not isinstance(ts, datetime):
                    raw_ts = str(ts)
                    amp_epoch = re.fullmatch(r"/Date\((\d+)(?:[+-]\d+)?\)/", raw_ts)
                    try:
                        ts = (datetime.fromtimestamp(int(amp_epoch.group(1)) / 1000, tz=timezone.utc)
                              if amp_epoch else datetime.fromisoformat(raw_ts.replace("Z", "+00:00")))
                    except (ValueError, OSError, OverflowError):
                        continue
                
                if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
                parsed_entries.append((ts, entry))
            
            parsed_entries.sort(key=lambda x: x[0])

            # Initialization (High-Water Mark)
            if source_name not in self.high_water_marks:
                # GetUpdates is session-scoped. The first response can contain chat
                # that arrived after this bot session was established, so process a
                # small recent window instead of discarding the entire first batch.
                # The age limit still suppresses historical console backlog.
                startup_cutoff = datetime.now(timezone.utc).timestamp() - 15
                self.high_water_marks[source_name] = {
                    'ts': datetime.fromtimestamp(startup_cutoff, tz=timezone.utc),
                    'hashes': set(),
                }

            watermark = self.high_water_marks[source_name]
            valid_new = []

            for ts, entry in parsed_entries:
                if ts < watermark['ts']: continue
                
                msg = str(getattr(entry, 'contents', ''))
                user = str(getattr(entry, 'source', ''))
                msg_hash = hash(f"{user}:{msg}")

                if ts == watermark['ts']:
                    if msg_hash in watermark['hashes']: continue
                    watermark['hashes'].add(msg_hash)
                elif ts > watermark['ts']:
                    watermark['ts'] = ts
                    watermark['hashes'] = {msg_hash}

                # Check Listeners
                for listener in self.console_listeners[:]:
                    if listener['source'] == source_name:
                        listener_queue = listener.get('queue')
                        if listener_queue is not None:
                            listener_queue.put_nowait(msg)
                        match = listener['regex'].search(msg)
                        if match and not listener['future'].done():
                            listener['future'].set_result(match)
                            if listener_queue is None and listener in self.console_listeners:
                                self.console_listeners.remove(listener)

                # Filters
                msg_type = str(getattr(entry, 'type', '')).lower()
                entry_source = str(getattr(entry, 'source', ''))
                if not entry_source or not msg: continue

                # Optional low-noise server event relay. Never classify actual player chat
                # as an event merely because somebody typed "joined the game".
                event_kind = None if "chat" in msg_type else self._classify_server_event(msg)
                if event_kind:
                    for group_name, group_data in self.bridge_data.get("groups", {}).items():
                        enabled = group_data.get("events", {})
                        if source_name in group_data.get("servers", []) and enabled.get(event_kind, False):
                            channel = self.bot.get_channel(group_data.get("channel_id"))
                            if channel:
                                alias = self.bridge_data.get("instance_settings", {}).get(source_name, {}).get("alias", source_name)
                                asyncio.create_task(channel.send(f"**[{alias}]** {msg}"))
                            break
                    continue

                # Check Comp Mode
                settings = self.bridge_data.get("instance_settings", {}).get(source_name, {})
                comp_mode = settings.get("comp_mode", False)
                
                is_chat = "chat" in msg_type
                is_info = "info" in msg_type or "info" in entry_source.lower()

                if not is_chat:
                    if comp_mode and is_info:
                        # Regex check for specific format: <[title]: username> message
                        # Example: <[Member]: Player1> Hello World
                        # Also supports optional "[Not Secure]: " prefix
                        # Support both "<[Rank]: Name>" and "<[Rank] Name>"
                        match = re.match(r"^(?:\[Not Secure\]: )?<\[.+?\](?: |: )(.+?)> (.+)$", msg)
                        if match:
                            user = match.group(1)
                            msg = match.group(2)
                        else:
                            continue # Info message didn't match pattern
                    else:
                        continue # Not chat, and not (comp_mode + info)
                else:
                    user = entry_source

                if re.match(r"^\\[.+?\\] <.+?> .+", msg): continue
                if msg.startswith("[") and "]" in msg: continue
                if len(user) < 1 or len(user) > 32: continue
                
                msg_lower = msg.lower()
                if "tps" in msg_lower and "ms/tick" in msg_lower: continue
                if msg_lower.startswith("private_for_"): continue
                
                system_users = {"server", "console", "rcon", "tip", "ftbteambases", "dimdungeons", "compactmachines", "storage", "twilight", "the", "overworld", "nether", "end", "irons_spellbooks", "ftb", "irregular_implements", "spatial"}
                if user.lower() in system_users: continue

                valid_new.append((user, msg))
            
            if valid_new:
                new_messages_per_server[source_name] = valid_new

        # 3.5 Intercept Commands
        for source_name, messages in list(new_messages_per_server.items()):
            # Find the group for this server
            parent_group = None
            for group_name, group_data in self.bridge_data["groups"].items():
                if source_name in group_data.get("servers", []):
                    parent_group = group_data
                    break
            
            if not parent_group: continue

            filtered_messages = []
            valid_commands = {"!online", "!help", "!item", "!discord"}
            
            for user, msg in messages:
                first_word = msg.split(" ")[0].lower()
                if msg.startswith("!") and first_word in valid_commands:
                    # It's a valid command
                    asyncio.create_task(self.handle_minecraft_command(source_name, user, msg, parent_group))
                else:
                    filtered_messages.append((user, msg))
            
            if filtered_messages:
                new_messages_per_server[source_name] = filtered_messages
            else:
                del new_messages_per_server[source_name]

        # 4. Second Pass: Dispatch messages to groups AND Discord
        for group_name, group_data in self.bridge_data["groups"].items():
            if not group_data.get("active", True): continue
            
            instance_names = group_data.get("servers", [])
            discord_channel_id = group_data.get("channel_id")
            
            # Skip if no servers and no discord (need at least 2 endpoints effectively)
            if len(instance_names) < 1: continue

            discord_channel = None
            if discord_channel_id:
                discord_channel = self.bot.get_channel(discord_channel_id)

            for source_name in instance_names:
                messages = new_messages_per_server.get(source_name)
                if not messages: continue
                
                # Get Instance Settings
                settings = self.bridge_data.get("instance_settings", {}).get(source_name, {})
                display_name = settings.get("alias", source_name)
                color = settings.get("color", "aqua")

                # Determine Source Type for Avatar
                source_inst = self.instances.get(source_name)
                is_minecraft_source = self._is_minecraft(source_inst)

                for user, msg in messages:
                    # Queue endpoint deliveries independently so an unavailable AMP target
                    # cannot stall the polling loop or delay Minecraft -> Discord delivery.
                    for target_name in instance_names:
                        if target_name == source_name:
                            continue
                        target = self.instances.get(target_name)
                        if target:
                            safe_user = self._sanitize_for_minecraft(user)
                            safe_msg = self._sanitize_for_minecraft(msg)
                            safe_source = self._sanitize_for_minecraft(display_name)
                            if self._is_minecraft(target):
                                cmd = f'tellraw @a ["",{{"text":"[{safe_source}] ", "color": "{color}"}}, {{ "text": "<{safe_user}> ", "color": "white" }}, {{ "text": "{safe_msg}", "color": "white" }}]'
                            else:
                                cmd = plain_chat_command(self._profile(target), f"[{safe_source}] <{safe_user}> {safe_msg}")
                            if cmd:
                                self._enqueue_send(target, cmd, target_name)

                    if discord_channel:
                        avatar_url = f"https://mc-heads.net/avatar/{quote(user, safe='')}" if is_minecraft_source else None
                        asyncio.create_task(self._send_discord_message_webhook(discord_channel, user, msg, display_name, avatar_url=avatar_url))

            # Throttling is claimed before the first await inside this method, preventing
            # topic-update storms without blocking the chat polling loop.
            asyncio.create_task(self._update_channel_topic(group_name, group_data))

    async def _send_discord_message_webhook(self, channel, user, msg, source_name, avatar_url=None, embed=None):
        try:
            # 1. Clean content (Escape Markdown first to prevent formatting exploits)
            safe_msg = discord.utils.escape_markdown(msg) if msg else None
            
            # --- NEW: Mention Resolver ---
            # Parses @Username -> <@UserID>
            # Only runs if we have text and are in a guild channel
            if safe_msg and "@" in safe_msg and hasattr(channel, "guild"):
                def replace_match(match):
                    # We strip backslashes because escape_markdown turns "User_Name" into "User\_Name"
                    # We need the clean name to find the user.
                    name_query = match.group(1).replace("\\", "")
                    
                    # 1. Try exact username (new discord system)
                    member = discord.utils.get(channel.guild.members, name=name_query)
                    
                    # 2. If not found, try Display Name / Nickname
                    if not member:
                        member = discord.utils.get(channel.guild.members, display_name=name_query)
                        
                    # Return the ping syntax if found, otherwise return original text
                    return member.mention if member else match.group(0)

                # Regex: Matches @ followed by letters, numbers, dots, or backslashes (for escaped chars)
                # We limit the character set to prevent it from eating up the rest of the sentence.
                safe_msg = re.sub(r"@([a-zA-Z0-9_\\.]+)", replace_match, safe_msg)
            # -----------------------------

            # Try to get or create a webhook
            webhook = await self._get_or_create_webhook(channel)
            
            if webhook:
                # Use webhook to impersonate player
                kwargs = {
                    "content": safe_msg,
                    "embed": embed,
                    "username": f"{user} [{source_name}]",
                    # ENABLE MENTIONS: Allow "users" to be pinged, but block "everyone" and "roles"
                    "allowed_mentions": discord.AllowedMentions(users=True, roles=False, everyone=False)
                }
                if avatar_url:
                    kwargs["avatar_url"] = avatar_url

                await webhook.send(**kwargs)
                log.info("Relayed game chat to Discord source=%s user=%s channel=%s", source_name, user, channel.id)
            else:
                # Fallback if webhook creation failed
                safe_user = discord.utils.escape_markdown(user)
                prefix = f"**[{source_name}]** <{safe_user}>"
                if embed:
                    await channel.send(content=prefix, embed=embed)
                else:
                    await channel.send(f"{prefix} {safe_msg}")

        except Exception:
            # Fallback for any other error (permissions, rate limits)
            try:
                log.debug(f"Webhook failed for {channel.id}, falling back to standard message")
                safe_user = discord.utils.escape_markdown(user)
                prefix = f"**[{source_name}]** <{safe_user}>"
                if embed:
                    await channel.send(content=prefix, embed=embed)
                else:
                    safe_msg = discord.utils.escape_markdown(msg) if msg else ""
                    await channel.send(f"{prefix} {safe_msg}")
            except Exception:
                log.warning(f"Failed to send message to Discord channel {channel.id}")

    async def _get_or_create_webhook(self, channel):
        if not isinstance(channel, discord.TextChannel):
            return None
        cached = self.webhook_cache.get(channel.id)
        if cached:
            self.bridge_webhook_ids.add(int(cached.id))
            return cached
        try:
            webhooks = await channel.webhooks()
            for wh in webhooks:
                if wh.user == self.bot.user or wh.name == "CalmBot Bridge":
                    self.webhook_cache[channel.id] = wh
                    self.bridge_webhook_ids.add(int(wh.id))
                    return wh
            webhook = await channel.create_webhook(name="CalmBot Bridge")
            self.webhook_cache[channel.id] = webhook
            self.bridge_webhook_ids.add(int(webhook.id))
            return webhook
        except discord.Forbidden:
            log.warning(f"Missing Manage Webhooks permission in {channel.name}")
            return None
        except Exception as e:
            log.warning(f"Unable to obtain webhook for {channel.name}: {e}")
            return None

    def _enqueue_send(self, target, cmd, target_name):
        """Queue an AMP send without allowing an unavailable target to grow tasks forever."""
        queue = self.send_queues.get(target_name)
        if queue is None:
            queue = asyncio.Queue(maxsize=100)
            self.send_queues[target_name] = queue
        worker = self.send_workers.get(target_name)
        if worker is None or worker.done():
            self.send_workers[target_name] = asyncio.create_task(
                self._send_worker(target_name), name=f"bridge-send-{target_name}"
            )
        try:
            queue.put_nowait((target, cmd))
            return True
        except asyncio.QueueFull:
            log.warning(f"Outgoing bridge queue full for {target_name}; dropping newest message")
            return False

    async def _send_worker(self, target_name):
        queue = self.send_queues[target_name]
        try:
            while True:
                target, cmd = await queue.get()
                try:
                    await self._send_message_safe(target, cmd, target_name)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _send_message_safe(self, target, cmd, target_name):
        # Serialize each target and retry one transient AMP failure.
        lock = self.send_locks.setdefault(("amp", target_name), asyncio.Lock())
        async with lock:
            for attempt in range(1, 3):
                try:
                    await asyncio.wait_for(target.send_console_message(cmd), timeout=15.0)
                    if target_name in self.send_failed:
                        self.send_failed.discard(target_name)
                        asyncio.create_task(self._send_health_alert(
                            f"✅ AMP message delivery recovered for **{target_name}**."
                        ))
                    return True
                except Exception as e:
                    if attempt == 2:
                        log.warning(f"Failed to send message to {target_name} after {attempt} attempts: {e}")
                        if target_name not in self.send_failed:
                            self.send_failed.add(target_name)
                            asyncio.create_task(self._send_health_alert(
                                f"⚠️ AMP message delivery failed twice for **{target_name}**."
                            ))
                        return False
                    await asyncio.sleep(0.75)
        return False

    @sync_loop.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()
        await self._refresh_instances()

    async def group_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        groups = list(self.bridge_data.get("groups", {}).keys())
        return [
            app_commands.Choice(name=g, value=g)
            for g in groups if current.lower() in g.lower()
        ][:25]

    @app_commands.command(name="broadcast", description="Broadcast a system message to bridged servers.")
    @app_commands.describe(message="The message to broadcast", group="Target bridge group")
    @app_commands.autocomplete(group=group_autocomplete)
    @admin_only()
    async def broadcast_command(self, interaction: discord.Interaction, message: str, group: str):
        await interaction.response.defer(ephemeral=True)
        
        # Validate group
        if group not in self.bridge_data.get("groups", {}):
             # Try case-insensitive lookup
             found = False
             for g in self.bridge_data.get("groups", {}):
                 if g.lower() == group.lower():
                     group = g
                     found = True
                     break
             if not found:
                 await interaction.followup.send(f"❌ Group '{group}' not found.", ephemeral=True)
                 return

        count = await self.broadcast_system_message(message, group)
        
        if count > 0:
            await interaction.followup.send(f"✅ Broadcast sent to **{count}** servers in group '{group}': {message}")
        else:
            await interaction.followup.send(f"⚠️ No active servers found for group '{group}'.")

    @app_commands.command(name="bridge_events", description="Configure optional Minecraft event relays")
    @app_commands.describe(group="Bridge group", joins="Relay joins", leaves="Relay leaves", deaths="Relay deaths", advancements="Relay advancements")
    @app_commands.autocomplete(group=group_autocomplete)
    @admin_only()
    async def bridge_events(self, interaction: discord.Interaction, group: str, joins: bool, leaves: bool, deaths: bool, advancements: bool):
        actual = next((name for name in self.bridge_data.get("groups", {}) if name.lower() == group.lower()), None)
        if not actual:
            await interaction.response.send_message(f"Unknown bridge group: {group}", ephemeral=True)
            return
        self.bridge_data["groups"][actual]["events"] = {
            "join": joins, "leave": leaves, "death": deaths, "advancement": advancements
        }
        save_json(CHAT_BRIDGE_FILE, self.bridge_data)
        await interaction.response.send_message(
            f"Event relay updated for **{actual}** — joins: {joins}, leaves: {leaves}, deaths: {deaths}, advancements: {advancements}",
            ephemeral=True)

    @app_commands.command(name="bridge_alerts", description="Configure staff alerts for bridge failures and recovery")
    @admin_only()
    async def bridge_alerts(self, interaction: discord.Interaction, channel: discord.TextChannel, enabled: bool = True):
        self.bridge_data["health_alerts"] = {"enabled": enabled, "channel_id": channel.id}
        save_json(CHAT_BRIDGE_FILE, self.bridge_data)
        await interaction.response.send_message(
            f"Bridge health alerts {'enabled' if enabled else 'disabled'} in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="bridge", description="Open the Chat Bridge Control Center")
    @admin_only()
    async def bridge_control(self, interaction: discord.Interaction):
        
        embed = discord.Embed(title="🌉 Chat Bridge Control Center", color=discord.Color.blue())
        embed.description = "Manage your cross-server chat links here."
        view = BridgeControlView(self)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# --- Views ---

class BridgeControlView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog
        self.add_item(BCC_CreateGroupButton(cog))
        self.add_item(BCC_InstanceSettingsButton(cog))
        self.add_item(BCC_GroupSelect(cog))
        self.add_item(BCC_StatusButton(cog))

class BCC_CreateGroupButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Create Group", style=discord.ButtonStyle.success, row=0)
        self.cog = cog
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CreateGroupModal(self.cog))

class BCC_InstanceSettingsButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Instance Settings", style=discord.ButtonStyle.secondary, row=0)
        self.cog = cog
    async def callback(self, interaction: discord.Interaction):
        await self.cog._refresh_instances()
        options = [discord.SelectOption(label=name[:100], value=name) for name in self.cog.instances.keys()]
        if not options:
            await interaction.response.send_message("No instances found.", ephemeral=True)
            return
        await interaction.response.send_message("Select an instance to configure:", view=InstanceSettingsSelector(self.cog, options[:25]), ephemeral=True)

class BCC_StatusButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Global Status", style=discord.ButtonStyle.secondary, row=0)
        self.cog = cog
    async def callback(self, interaction: discord.Interaction):
        data = self.cog.bridge_data["groups"]
        embed = discord.Embed(title="Global Bridge Status", color=discord.Color.gold())
        if not data: embed.description = "No bridge groups created."
        for name, info in data.items():
            status = "🟢 Active" if info.get("active", True) else "🔴 Disabled"
            servers = info.get("servers", [])
            server_text = ", ".join(servers) if servers else "*No servers linked*"
            
            # Add Discord channel status
            channel_id = info.get("channel_id")
            channel_text = ""
            if channel_id:
                channel = self.cog.bot.get_channel(channel_id)
                channel_name = channel.mention if channel else f"Unknown Channel ({channel_id})"
                channel_text = f"\n**Discord:** {channel_name}"
            
            embed.add_field(name=f"{name} ({status})", value=f"**Servers:** {server_text}{channel_text}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class BCC_GroupSelect(discord.ui.Select):
    def __init__(self, cog):
        self.cog = cog
        options = []
        for name in cog.bridge_data["groups"].keys():
            options.append(discord.SelectOption(label=name, value=name))
        if not options: options.append(discord.SelectOption(label="No groups available", value="none"))
        super().__init__(placeholder="Manage a Group...", options=options, disabled=len(options)==0 or options[0].value=="none", row=1)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none": return
        group_name = self.values[0]
        await interaction.response.send_message(f"Managing Group: **{group_name}**", view=GroupManageView(self.cog, group_name), ephemeral=True)

class GroupManageView(discord.ui.View):
    def __init__(self, cog, group_name):
        super().__init__(timeout=300)
        self.cog = cog
        self.group_name = group_name
        group_data = self.cog.bridge_data["groups"].get(group_name, {})
        
        active = group_data.get("active", True)
        label = "Disable Bridge" if active else "Enable Bridge"
        style = discord.ButtonStyle.danger if active else discord.ButtonStyle.success
        
        self.add_item(GM_ToggleActiveButton(cog, group_name, label, style))
        self.add_item(GM_LinkServerButton(cog, group_name))
        self.add_item(GM_UnlinkServerButton(cog, group_name))
        
        # Add Link/Unlink Discord Channel Buttons
        self.add_item(GM_LinkChannelButton(cog, group_name))
        if group_data.get("channel_id"):
            self.add_item(GM_UnlinkChannelButton(cog, group_name))
            
        self.add_item(GM_DeleteGroupButton(cog, group_name))

class GM_ToggleActiveButton(discord.ui.Button):
    def __init__(self, cog, group_name, label, style):
        super().__init__(label=label, style=style, row=0)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        group_data = self.cog.bridge_data["groups"][self.group_name]
        current = group_data.get("active", True)
        group_data["active"] = not current
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        
        new_state = "Enabled" if not current else "Disabled"
        await interaction.response.edit_message(
            content=f"Bridge **{self.group_name}** is now **{new_state}**.",
            view=GroupManageView(self.cog, self.group_name)
        )

class GM_LinkServerButton(discord.ui.Button):
    def __init__(self, cog, group_name):
        super().__init__(label="Link Server", style=discord.ButtonStyle.primary, row=0)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        await self.cog._refresh_instances()
        options = []
        current_links = self.cog.bridge_data["groups"][self.group_name].get("servers", [])
        for name in self.cog.instances.keys():
            if name not in current_links:
                options.append(discord.SelectOption(label=name[:100], value=name))
        if not options:
            await interaction.response.send_message("No unlinked servers available.", ephemeral=True)
            return
        await interaction.response.send_message(f"Add server to **{self.group_name}**:", view=LinkInstanceView(self.cog, self.group_name, options[:25]), ephemeral=True)

class GM_UnlinkServerButton(discord.ui.Button):
    def __init__(self, cog, group_name):
        super().__init__(label="Unlink Server", style=discord.ButtonStyle.secondary, row=1)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        current_links = self.cog.bridge_data["groups"][self.group_name].get("servers", [])
        if not current_links:
            await interaction.response.send_message("No servers linked to this group.", ephemeral=True)
            return
        options = [discord.SelectOption(label=name[:100], value=name) for name in current_links]
        await interaction.response.send_message(f"Remove server from **{self.group_name}**:", view=UnlinkInstanceView(self.cog, self.group_name, options[:25]), ephemeral=True)

class GM_LinkChannelButton(discord.ui.Button):
    def __init__(self, cog, group_name):
        super().__init__(label="Link Discord Channel", style=discord.ButtonStyle.blurple, row=1)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Select a text channel to link:", view=LinkChannelView(self.cog, self.group_name), ephemeral=True)

class GM_UnlinkChannelButton(discord.ui.Button):
    def __init__(self, cog, group_name):
        super().__init__(label="Unlink Discord Channel", style=discord.ButtonStyle.secondary, row=1)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        if "channel_id" in self.cog.bridge_data["groups"][self.group_name]:
            del self.cog.bridge_data["groups"][self.group_name]["channel_id"]
            save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
            await interaction.response.edit_message(content=f"Unlinked Discord channel from **{self.group_name}**.", view=GroupManageView(self.cog, self.group_name))
        else:
            await interaction.response.send_message("No channel linked.", ephemeral=True)

class GM_DeleteGroupButton(discord.ui.Button):
    def __init__(self, cog, group_name):
        super().__init__(label="Delete Group", style=discord.ButtonStyle.danger, row=2)
        self.cog = cog
        self.group_name = group_name
    async def callback(self, interaction: discord.Interaction):
        if self.group_name in self.cog.bridge_data["groups"]:
            del self.cog.bridge_data["groups"][self.group_name]
            save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
            await interaction.response.send_message(f"🗑️ Deleted group **{self.group_name}**.", ephemeral=True)
        else:
            await interaction.response.send_message("Group already deleted.", ephemeral=True)

# --- Modals & Select Views ---

class LinkChannelView(discord.ui.View):
    def __init__(self, cog, group_name):
        super().__init__(timeout=60)
        self.add_item(LinkChannelSelect(cog, group_name))

class LinkChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, cog, group_name):
        self.cog = cog
        self.group_name = group_name
        # Only allow text channels
        super().__init__(placeholder="Select a channel...", channel_types=[discord.ChannelType.text])

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        self.cog.bridge_data["groups"][self.group_name]["channel_id"] = channel.id
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        await interaction.response.send_message(f"✅ Linked {channel.mention} to **{self.group_name}**.", ephemeral=True)
        # We can't easily refresh the previous view here without passing the original interaction, but users can re-open or click other buttons.

class CreateGroupModal(discord.ui.Modal, title="Create Bridge Group"):
    name = discord.ui.TextInput(label="Group Name", placeholder="e.g. Survival", required=True)
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
    async def on_submit(self, interaction: discord.Interaction):
        name = self.name.value.strip()
        if name in self.cog.bridge_data["groups"]:
            await interaction.response.send_message("Group already exists.", ephemeral=True)
            return
        self.cog.bridge_data["groups"][name] = {"servers": [], "active": True}
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        # Refresh the main view to show the new group in the dropdown
        await interaction.response.edit_message(embed=interaction.message.embeds[0], view=BridgeControlView(self.cog))

class LinkInstanceView(discord.ui.View):
    def __init__(self, cog, group_name, options):
        super().__init__(timeout=60)
        self.add_item(LinkInstanceSelect(cog, group_name, options))

class LinkInstanceSelect(discord.ui.Select):
    def __init__(self, cog, group_name, options):
        self.cog = cog
        self.group_name = group_name
        super().__init__(placeholder="Select server to add...", options=options)
    async def callback(self, interaction: discord.Interaction):
        server = self.values[0]
        if server not in self.cog.bridge_data["groups"][self.group_name]["servers"]:
            self.cog.bridge_data["groups"][self.group_name]["servers"].append(server)
            save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
            await interaction.response.send_message(f"✅ Linked **{server}**.", ephemeral=True)
        else:
            await interaction.response.send_message("Server already linked.", ephemeral=True)

class UnlinkInstanceView(discord.ui.View):
    def __init__(self, cog, group_name, options):
        super().__init__(timeout=60)
        self.add_item(UnlinkInstanceSelect(cog, group_name, options))

class UnlinkInstanceSelect(discord.ui.Select):
    def __init__(self, cog, group_name, options):
        self.cog = cog
        self.group_name = group_name
        super().__init__(placeholder="Select server to remove...", options=options)
    async def callback(self, interaction: discord.Interaction):
        server = self.values[0]
        if server in self.cog.bridge_data["groups"][self.group_name]["servers"]:
            self.cog.bridge_data["groups"][self.group_name]["servers"].remove(server)
            save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
            await interaction.response.send_message(f"✅ Unlinked **{server}**.", ephemeral=True)
        else:
            await interaction.response.send_message("Server was not linked.", ephemeral=True)
        self.view.stop()

class InstanceSettingsSelector(discord.ui.View):
    def __init__(self, cog, options):
        super().__init__(timeout=60)
        self.add_item(InstanceSelect(cog, options))

class InstanceSelect(discord.ui.Select):
    def __init__(self, cog, options):
        self.cog = cog
        super().__init__(placeholder="Select instance...", options=options)
    async def callback(self, interaction: discord.Interaction):
        server_name = self.values[0]
        await interaction.response.send_message(f"Configuring **{server_name}**:", view=InstanceEditView(self.cog, server_name), ephemeral=True)

class InstanceEditView(discord.ui.View):
    def __init__(self, cog, server_name):
        super().__init__(timeout=180)
        self.cog = cog
        self.server_name = server_name
        
        # Get current settings
        settings = self.cog.bridge_data.get("instance_settings", {}).get(server_name, {})
        alias = settings.get("alias", server_name)
        color = settings.get("color", "aqua")
        comp_mode = settings.get("comp_mode", False)
        
        self.add_item(IE_SetAliasButton(cog, server_name, alias))
        self.add_item(IE_CompModeButton(cog, server_name, comp_mode))
        self.add_item(IE_ColorSelect(cog, server_name, color))

class IE_SetAliasButton(discord.ui.Button):
    def __init__(self, cog, server_name, current_alias):
        super().__init__(label=f"Alias: {current_alias}", style=discord.ButtonStyle.primary, row=0)
        self.cog = cog
        self.server_name = server_name
        self.current_alias = current_alias
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AliasModal(self.cog, self.server_name, self.current_alias))

class IE_CompModeButton(discord.ui.Button):
    def __init__(self, cog, server_name, current_state):
        style = discord.ButtonStyle.success if current_state else discord.ButtonStyle.secondary
        label = f"Comp Mode: {'ON' if current_state else 'OFF'}"
        super().__init__(label=label, style=style, row=0)
        self.cog = cog
        self.server_name = server_name
        self.current_state = current_state

    async def callback(self, interaction: discord.Interaction):
        if "instance_settings" not in self.cog.bridge_data:
            self.cog.bridge_data["instance_settings"] = {}
        if self.server_name not in self.cog.bridge_data["instance_settings"]:
            self.cog.bridge_data["instance_settings"][self.server_name] = {}
        
        new_state = not self.current_state
        self.cog.bridge_data["instance_settings"][self.server_name]["comp_mode"] = new_state
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        
        await interaction.response.edit_message(
            content=f"Configuring **{self.server_name}**:",
            view=InstanceEditView(self.cog, self.server_name)
        )

class IE_ColorSelect(discord.ui.Select):
    def __init__(self, cog, server_name, current_color):
        self.cog = cog
        self.server_name = server_name
        
        colors = ["black", "dark_blue", "dark_green", "dark_aqua", "dark_red", "dark_purple", "gold", "gray", "dark_gray", "blue", "green", "aqua", "red", "light_purple", "yellow", "white"]
        options = []
        for c in colors:
            options.append(discord.SelectOption(label=c.replace("_", " ").title(), value=c, default=(c==current_color)))
            
        super().__init__(placeholder="Select Name Color...", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        color = self.values[0]
        if "instance_settings" not in self.cog.bridge_data:
            self.cog.bridge_data["instance_settings"] = {}
        if self.server_name not in self.cog.bridge_data["instance_settings"]:
            self.cog.bridge_data["instance_settings"][self.server_name] = {}
            
        self.cog.bridge_data["instance_settings"][self.server_name]["color"] = color
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        
        await interaction.response.edit_message(
            content=f"Configuring **{self.server_name}** (Color set to `{color}`):",
            view=InstanceEditView(self.cog, self.server_name)
        )

class AliasModal(discord.ui.Modal, title="Set Instance Alias"):
    alias = discord.ui.TextInput(label="Display Name", required=True, max_length=20)
    
    def __init__(self, cog, server_name, current_alias):
        super().__init__()
        self.cog = cog
        self.server_name = server_name
        self.alias.default = current_alias
        
    async def on_submit(self, interaction: discord.Interaction):
        alias = self.alias.value.strip()
        if "instance_settings" not in self.cog.bridge_data:
            self.cog.bridge_data["instance_settings"] = {}
        if self.server_name not in self.cog.bridge_data["instance_settings"]:
            self.cog.bridge_data["instance_settings"][self.server_name] = {}
            
        self.cog.bridge_data["instance_settings"][self.server_name]["alias"] = alias
        save_json(CHAT_BRIDGE_FILE, self.cog.bridge_data)
        await interaction.response.edit_message(
            content=f"Configuring **{self.server_name}** (Alias set to **{alias}**):",
            view=InstanceEditView(self.cog, self.server_name)
        )

async def setup(bot):
    await bot.add_cog(ChatBridge(bot))