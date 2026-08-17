from __future__ import annotations

import asyncio
import ipaddress
import ssl
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
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.ca_file = ca_file
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

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
                    server_hostname=self.host,
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
