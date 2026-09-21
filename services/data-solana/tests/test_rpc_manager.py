import httpx
import pytest

from yonixalpha_core.solana.rpc import RpcAllEndpointsFailedError, RpcManager

pytestmark = pytest.mark.asyncio


def _client_with_responses(responder) -> httpx.AsyncClient:
    transport = httpx.MockTransport(responder)
    return httpx.AsyncClient(transport=transport)


async def test_primary_success():
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    async with _client_with_responses(responder) as client:
        manager = RpcManager.create(client=client, primary_url="https://primary.example/rpc")
        result = await manager.call("getHealth")
        assert result == "ok"
        assert manager.health_snapshot()[0]["consecutive_failures"] == 0


async def test_failover_to_backup_on_primary_failure():
    def responder(request: httpx.Request) -> httpx.Response:
        if "primary" in str(request.url):
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "from-backup"})

    async with _client_with_responses(responder) as client:
        manager = RpcManager.create(
            client=client,
            primary_url="https://primary.example/rpc",
            backup_url="https://backup.example/rpc",
        )
        result = await manager.call("getHealth")
        assert result == "from-backup"

        snapshot = {e["label"]: e for e in manager.health_snapshot()}
        assert snapshot["primary"]["consecutive_failures"] == 1
        assert snapshot["backup"]["consecutive_failures"] == 0


async def test_endpoint_disabled_after_failure_threshold():
    def responder(request: httpx.Request) -> httpx.Response:
        if "primary" in str(request.url):
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "from-backup"})

    async with _client_with_responses(responder) as client:
        manager = RpcManager.create(
            client=client,
            primary_url="https://primary.example/rpc",
            backup_url="https://backup.example/rpc",
            failure_threshold=2,
            cooldown_seconds=60,
        )
        await manager.call("getHealth")
        await manager.call("getHealth")

        snapshot = {e["label"]: e for e in manager.health_snapshot()}
        assert snapshot["primary"]["disabled"] is True
        # Once disabled, subsequent calls should skip straight to backup —
        # verify by making the mock fail loudly if primary is hit again.
        calls_to_primary = 0

        def responder2(request: httpx.Request) -> httpx.Response:
            nonlocal calls_to_primary
            if "primary" in str(request.url):
                calls_to_primary += 1
                return httpx.Response(500, json={"error": "still down"})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "from-backup"})

        manager.client = httpx.AsyncClient(transport=httpx.MockTransport(responder2))
        await manager.call("getHealth")
        assert calls_to_primary == 0
        await manager.client.aclose()


async def test_all_endpoints_failed_raises():
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    async with _client_with_responses(responder) as client:
        manager = RpcManager.create(client=client, primary_url="https://primary.example/rpc")
        with pytest.raises(RpcAllEndpointsFailedError):
            await manager.call("getHealth")


async def test_emergency_fallback_still_attempts_when_all_disabled():
    """Once every endpoint is in cooldown, the manager should still attempt
    a call (primary-first) rather than short-circuit to a hard failure —
    that's the "emergency fallback" behavior from spec section 32.
    """
    call_log: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        call_log.append(str(request.url))
        return httpx.Response(500, json={"error": "down"})

    async with _client_with_responses(responder) as client:
        manager = RpcManager.create(
            client=client,
            primary_url="https://primary.example/rpc",
            backup_url="https://backup.example/rpc",
            failure_threshold=1,
            cooldown_seconds=9999,
        )
        with pytest.raises(RpcAllEndpointsFailedError):
            await manager.call("getHealth")  # disables both after this round

        call_log.clear()
        with pytest.raises(RpcAllEndpointsFailedError):
            await manager.call("getHealth")  # both disabled, but should still be attempted

        assert len(call_log) == 2  # both endpoints were still tried despite cooldown
