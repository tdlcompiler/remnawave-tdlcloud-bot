from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import sqlite3
import ssl
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class EdgeFirewallError(Exception):
    pass


class EdgeFirewallService:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        ca_file: str | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 15.0,
        server_name: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.ca_file = ca_file
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.server_name = server_name or host

    async def ban(self, ip: str, ttl: int) -> bool:
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError as exc:
            raise EdgeFirewallError(f"Invalid IP: {ip!r}") from exc

        ttl = int(ttl)

        if ttl <= 0:
            raise EdgeFirewallError(
                f"Invalid ban TTL: {ttl}"
            )

        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None

        try:
            ssl_context = ssl.create_default_context(
                cafile=self.ca_file,
            )

            logger.info(
                "Edge firewall: connecting",
                host=self.host,
                port=self.port,
            )

            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=self.host,
                    port=self.port,
                    ssl=ssl_context,
                    server_hostname=self.server_name,
                ),
                timeout=self.connect_timeout,
            )

            hello = {
                "type": "hello",
                "token": self.token,
            }

            await self._send(writer, hello)

            response = await self._receive(
                reader,
                self.read_timeout,
            )

            if response.get("ok") is not True:
                raise EdgeFirewallError(
                    f"Frontend rejected hello: {response}"
                )

            payload = {
                "type": "ban",
                "token": self.token,
                "ip": ip,
                "ttl": ttl,
            }

            logger.warning(
                "Edge firewall: sending ban",
                ip=ip,
                ttl=ttl,
            )

            await self._send(
                writer,
                payload,
            )

            response = await self._receive(
                reader,
                self.read_timeout,
            )

            if response.get("ok") is not True:
                raise EdgeFirewallError(
                    f"Frontend rejected ban: {response}"
                )

            logger.warning(
                "Edge firewall: ban applied",
                ip=ip,
                ttl=ttl,
            )

            return True

        except Exception:
            logger.exception(
                "Edge firewall: ban failed",
                ip=ip,
                ttl=ttl,
            )
            return False

        finally:
            if writer is not None:
                writer.close()

                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    @staticmethod
    async def _send(
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
    ) -> None:
        import json

        raw = (
            json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")

        writer.write(raw)
        await writer.drain()

    @staticmethod
    async def _receive(
        reader: asyncio.StreamReader,
        timeout: float,
    ) -> dict[str, Any]:
        import json

        raw = await asyncio.wait_for(
            reader.readline(),
            timeout=timeout,
        )

        if not raw:
            raise EdgeFirewallError(
                "Frontend closed connection"
            )

        if len(raw) > 64 * 1024:
            raise EdgeFirewallError(
                "Frontend response too large"
            )

        data = json.loads(
            raw.decode("utf-8")
        )

        if not isinstance(data, dict):
            raise EdgeFirewallError(
                "Invalid frontend response"
            )

        return data


def build_edge_firewalls(settings: Any) -> list[EdgeFirewallService]:
    """Parse explicit destinations; retain the original single-node settings."""
    if not settings.EDGE_FIREWALL_NODES.strip():
        if not settings.EDGE_FIREWALL_HOST:
            return []
        return [EdgeFirewallService(settings.EDGE_FIREWALL_HOST, settings.EDGE_FIREWALL_PORT,
                                    settings.EDGE_FIREWALL_TOKEN, settings.EDGE_FIREWALL_CA_FILE)]
    nodes = json.loads(settings.EDGE_FIREWALL_NODES)
    if not isinstance(nodes, list) or not nodes:
        raise ValueError('EDGE_FIREWALL_NODES must be a non-empty JSON array')
    result = []
    for node in nodes:
        if not isinstance(node, dict) or not all(isinstance(node.get(k), str) and node[k].strip()
                                                for k in ('host', 'server_name', 'token')):
            raise ValueError('Each firewall node requires host, server_name and token')
        port = int(node.get('port', 443))
        if not 1 <= port <= 65535:
            raise ValueError('Invalid firewall node port')
        result.append(EdgeFirewallService(node['host'], port, node['token'],
                                          node.get('ca_file') or settings.EDGE_FIREWALL_CA_FILE,
                                          server_name=node['server_name']))
    return result


async def ban_all(nodes: list[EdgeFirewallService], ip: str, ttl: int) -> list[bool]:
    results = await asyncio.gather(*(node.ban(ip, ttl) for node in nodes), return_exceptions=True)
    for node, result in zip(nodes, results, strict=True):
        if isinstance(result, BaseException):
            logger.error('Edge firewall delivery failed', host=node.host, error=str(result))
        elif not result:
            logger.error('Edge firewall delivery failed', host=node.host)
    return [result is True for result in results]


class EdgeFirewallSync:
    """Persist active bans and replay remaining TTL to all frontends periodically."""

    def __init__(self, nodes: list[EdgeFirewallService], path: str, interval: int) -> None:
        self.nodes, self.path, self.interval = nodes, path, max(5, interval)
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def _connect(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS bans (ip TEXT PRIMARY KEY, expires REAL NOT NULL)')
        return db

    def _save(self, ip: str, ttl: int) -> None:
        with closing(self._connect()) as db, db:
            db.execute('INSERT INTO bans VALUES (?, ?) ON CONFLICT(ip) DO UPDATE SET '
                       'expires = MAX(expires, excluded.expires)', (ip, time.time() + ttl))

    def _active(self) -> list[tuple[str, float]]:
        with closing(self._connect()) as db, db:
            db.execute('DELETE FROM bans WHERE expires <= ?', (time.time(),))
            return db.execute('SELECT ip, expires FROM bans').fetchall()

    async def enqueue(self, ip: str, ttl: int) -> None:
        ip = str(ipaddress.ip_address(ip))
        if not 0 < ttl <= 7 * 86400:
            raise ValueError('Firewall ban duration must be between 1 second and 7 days')
        if not self.nodes:
            raise ValueError('Edge firewall enabled but no destinations configured')
        await asyncio.to_thread(self._save, ip, ttl)
        self._wake.set()

    async def start(self) -> None:
        if self._task is None:
            await asyncio.to_thread(self._active)
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                for ip, expires in await asyncio.to_thread(self._active):
                    ttl = math.ceil(expires - time.time())
                    if ttl > 0:
                        await ban_all(self.nodes, ip, ttl)
            except Exception:
                logger.exception('Edge firewall synchronization failed')
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except TimeoutError:
                pass
