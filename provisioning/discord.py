"""Discord server lifecycle management via HTTP Bot API.

Uses aiohttp for REST calls. Does NOT use Gateway WebSocket —
that's the ingestor's job (ws_discord.py). This module handles
provisioning: create, configure, archive, delete.

Requires bot token with intents: Manage Guilds, Manage Channels,
Manage Roles, Create Instant Invite.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"

DEFAULT_CHANNELS = ["general", "standup", "sprint-log"]
DEFAULT_ROLES = ["pm", "developer", "ta", "read-only"]


@dataclass
class GuildResult:
    """Result of provisioning one team's Discord server."""

    guild_id: str
    team_id: str
    invite_url: str
    channel_ids: dict[str, str]  # channel_name -> channel_id
    role_ids: dict[str, str]     # role_name -> role_id
    created_at: str              # ISO 8601


class DiscordProvisioner:
    """Create/manage Discord servers for teams via HTTP Bot API.

    Separation of concerns:
    - This class: guild CRUD, channel/role setup, invite generation
    - ws_discord.py (ingestor): real-time message monitoring via Gateway
    - team_import.py: roster parsing (no Discord dependency)
    """

    def __init__(self, bot_token: str, template_id: str | None = None):
        self._token = bot_token
        self._template_id = template_id
        self._session = None  # aiohttp.ClientSession, lazy-init

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bot {self._token}"},
            )

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    # -- Lifecycle ----------------------------------------------------------

    async def provision_team(
        self,
        team_id: str,
        team_name: str,
        members: list[dict],
        channels: list[str] | None = None,
        roles: list[str] | None = None,
    ) -> GuildResult:
        """Create guild, channels, roles, invite link.

        Args:
            team_id: Internal team identifier (stored in guild metadata).
            team_name: Human-readable name (becomes guild name).
            members: [{"name": str, "email": str, "role": str}]
            channels: Override DEFAULT_CHANNELS.
            roles: Override DEFAULT_ROLES.

        Returns:
            GuildResult with guild_id, invite_url, channel_ids, role_ids.

        Rate limit: Discord caps unverified bots at 10 guilds.
        Beyond that, bot must be verified via Discord application review.
        """
        await self._ensure_session()
        channels = channels or DEFAULT_CHANNELS
        roles = roles or DEFAULT_ROLES

        # 1. Create guild
        guild = await self._create_guild(team_name)
        guild_id = guild["id"]
        log.info("Created guild %s for team %s", guild_id, team_id)

        # 2. Create roles
        role_ids = {}
        for role_name in roles:
            role = await self._create_role(guild_id, role_name)
            role_ids[role_name] = role["id"]

        # 3. Create category + channels
        category = await self._create_channel(
            guild_id, f"{team_name} — Sprint", ch_type=4,  # GUILD_CATEGORY
        )
        channel_ids = {}
        for ch_name in channels:
            ch = await self._create_channel(
                guild_id, ch_name, ch_type=0, parent_id=category["id"],
            )
            channel_ids[ch_name] = ch["id"]

        # 4. Generate invite (to first text channel)
        if not channel_ids:
            raise ValueError(f"No channels created for team {team_id}")
        first_channel = next(iter(channel_ids.values()))
        invite = await self._create_invite(first_channel)

        return GuildResult(
            guild_id=guild_id,
            team_id=team_id,
            invite_url=f"https://discord.gg/{invite['code']}",
            channel_ids=channel_ids,
            role_ids=role_ids,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    async def provision_batch(self, teams: list[dict]) -> list[GuildResult]:
        """Provision N teams sequentially (Discord rate limits).

        Args:
            teams: Output of parse_team_import() —
                   [{"team_id", "team_name", "members"}]

        Returns:
            List of GuildResult, one per team. Failed teams logged and skipped.
        """
        results = []
        for i, team in enumerate(teams):
            try:
                log.info("Provisioning %d/%d: %s", i + 1, len(teams), team["team_id"])
                result = await self.provision_team(
                    team_id=team["team_id"],
                    team_name=team.get("team_name", team["team_id"]),
                    members=team.get("members", []),
                )
                results.append(result)
                await asyncio.sleep(1.0)  # respect rate limits
            except Exception as e:
                log.error("Failed to provision %s: %s", team["team_id"], e)
        return results

    async def archive_team(self, guild_id: str) -> None:
        """Set all channels read-only, rename guild with [ARCHIVED] prefix."""
        await self._ensure_session()

        guild = await self._api("GET", f"/guilds/{guild_id}")
        if not guild["name"].startswith("[ARCHIVED]"):
            await self._api("PATCH", f"/guilds/{guild_id}", json={
                "name": f"[ARCHIVED] {guild['name']}",
            })

        # Deny SEND_MESSAGES for @everyone on all text/voice channels
        everyone_role = guild_id  # @everyone role ID == guild ID
        channels = await self._api("GET", f"/guilds/{guild_id}/channels")
        for ch in channels:
            if ch["type"] in (0, 2):  # text or voice
                await self._api(
                    "PUT",
                    f"/channels/{ch['id']}/permissions/{everyone_role}",
                    json={"deny": str(1 << 11), "allow": "0", "type": 0},
                )
        log.info("Archived guild %s", guild_id)

    async def delete_team(self, guild_id: str) -> None:
        """Delete guild entirely. Irreversible. Bot must be guild owner."""
        await self._ensure_session()
        await self._api("DELETE", f"/guilds/{guild_id}")
        log.info("Deleted guild %s", guild_id)

    async def sync_members(self, guild_id: str, members: list[dict]) -> dict:
        """Reconcile member list against guild. Returns summary of changes.

        Stub — full implementation requires OAuth2 flow for adding members,
        or generating invite links for manual join.
        """
        # TODO: Implement full member sync (OAuth2 add, kick removed)
        await self._ensure_session()
        channels = await self._api("GET", f"/guilds/{guild_id}/channels")
        text_channels = [c for c in channels if c["type"] == 0]
        if text_channels:
            invite = await self._create_invite(text_channels[0]["id"])
            return {"invite_url": f"https://discord.gg/{invite['code']}"}
        return {}

    # -- Templates (stub — style later) ------------------------------------

    async def apply_template(self, guild_id: str, template: dict) -> None:
        """Apply channel/role/permission template to guild.

        Template format TBD. For now, accepts:
        {"channels": ["general", ...], "roles": ["pm", ...]}
        """
        await self._ensure_session()
        # TODO: Full template application (icons, colors, permissions, topics)
        for ch_name in template.get("channels", []):
            await self._create_channel(guild_id, ch_name, ch_type=0)
        for role_name in template.get("roles", []):
            await self._create_role(guild_id, role_name)

    # -- Discord API helpers ------------------------------------------------

    async def _api(self, method: str, path: str, _retries: int = 0, **kwargs) -> Any:
        """Make Discord API request with rate limit handling."""
        if _retries > 5:
            raise RuntimeError(f"Discord API rate limit exceeded after 5 retries: {method} {path}")
        await self._ensure_session()
        url = f"{API_BASE}{path}"
        async with self._session.request(method, url, **kwargs) as resp:
            if resp.status == 429:
                retry_after = (await resp.json()).get("retry_after", 1.0)
                log.warning("Rate limited, retrying after %.1fs (attempt %d)", retry_after, _retries + 1)
                await asyncio.sleep(retry_after)
                return await self._api(method, path, _retries=_retries + 1, **kwargs)
            resp.raise_for_status()
            if resp.status == 204:
                return {}
            return await resp.json()

    async def _create_guild(self, name: str) -> dict:
        return await self._api("POST", "/guilds", json={"name": name})

    async def _create_channel(
        self, guild_id: str, name: str, ch_type: int = 0, parent_id: str | None = None,
    ) -> dict:
        payload: dict = {"name": name, "type": ch_type}
        if parent_id:
            payload["parent_id"] = parent_id
        return await self._api("POST", f"/guilds/{guild_id}/channels", json=payload)

    async def _create_role(self, guild_id: str, name: str) -> dict:
        return await self._api("POST", f"/guilds/{guild_id}/roles", json={"name": name})

    async def _create_invite(self, channel_id: str, max_age: int = 86400) -> dict:
        return await self._api("POST", f"/channels/{channel_id}/invites", json={
            "max_age": max_age, "max_uses": 0, "unique": True,
        })
