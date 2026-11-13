import asyncio
import json
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.edge_firewall_service import EdgeFirewallSync, ban_all, build_edge_firewalls


def settings(nodes):
    return SimpleNamespace(EDGE_FIREWALL_NODES=json.dumps(nodes), EDGE_FIREWALL_HOST='',
                           EDGE_FIREWALL_PORT=2053, EDGE_FIREWALL_TOKEN='', EDGE_FIREWALL_CA_FILE=None)


def test_destinations_use_distinct_addresses_and_shared_sni():
    nodes = build_edge_firewalls(settings([
        {'host': '192.0.2.1', 'server_name': 'fw.example.com', 'token': 'a'},
        {'host': '192.0.2.2', 'server_name': 'fw.example.com', 'token': 'b'},
    ]))
    assert [(n.host, n.port, n.server_name) for n in nodes] == [
        ('192.0.2.1', 443, 'fw.example.com'), ('192.0.2.2', 443, 'fw.example.com')]


def test_failure_does_not_prevent_other_node():
    first = SimpleNamespace(host='first', ban=AsyncMock(side_effect=RuntimeError('offline')))
    second = SimpleNamespace(host='second', ban=AsyncMock(return_value=True))
    assert asyncio.run(ban_all([first, second], '192.0.2.3', 60)) == [False, True]
    second.ban.assert_awaited_once_with('192.0.2.3', 60)


def test_persisted_bans_survive_restart_and_do_not_extend(tmp_path):
    node = SimpleNamespace(host='first', ban=AsyncMock(return_value=True))
    path = str(tmp_path / 'state.sqlite3')
    async def run():
        sync = EdgeFirewallSync([node], path, 30)
        await sync.enqueue('192.0.2.4', 120)
        original = sync._active()[0][1]
        await sync.enqueue('192.0.2.4', 60)
        assert sync._active()[0][1] == original
        restarted = EdgeFirewallSync([node], path, 30)
        await restarted.start()
        for _ in range(100):
            if node.ban.await_count:
                break
            await asyncio.sleep(0.01)
        await restarted.stop()
        assert node.ban.await_count == 1
        ip, ttl = node.ban.await_args.args
        assert ip == '192.0.2.4' and 0 < ttl <= 120
        assert original <= time.time() + 120
    asyncio.run(run())
