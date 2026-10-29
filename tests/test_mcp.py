"""MCP tool tests via the in-memory FastMCP client."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from sapdlm.api.mcp import create_mcp_server
from sapdlm.api.service import Service
from sapdlm.config import Settings, save_settings
from sapdlm.credentials import CredentialStore
from tests.test_api import portal_handler

EXPECTED_TOOLS = {
    "get_status",
    "list_basket_items",
    "refresh_basket",
    "remove_from_basket",
    "start_download",
    "pause_download",
    "resume_download",
    "cancel_download",
    "delete_local_data",
    "get_download_protocol",
    "get_download_history",
    "get_task",
    "schedule_download",
    "get_config",
    "update_config",
}


def make_service(tmp_path: Path) -> Service:
    data_dir = tmp_path / "data"
    download_dir = tmp_path / "store"
    data_dir.mkdir(parents=True, exist_ok=True)
    save_settings(
        data_dir,
        Settings(
            sap_url="https://launchpad.test",
            username="jane",
            download_store=str(download_dir),
        ),
    )
    CredentialStore(data_dir).set("sap_password", "s3cret")
    transport = httpx.MockTransport(portal_handler)
    return Service(
        data_dir=data_dir,
        download_dir=download_dir,
        http_client=httpx.Client(transport=transport),
        component_client_factory=lambda: httpx.Client(transport=transport),
    )


def test_mcp_tool_inventory(tmp_path):
    service = make_service(tmp_path)
    mcp = create_mcp_server(lambda: service)

    async def run():
        from fastmcp import Client

        async with Client(mcp) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            missing = EXPECTED_TOOLS - names
            assert not missing, f"missing MCP tools: {missing}"
            # adding basket items is not part of the client protocol
            assert "add_to_basket" not in names
            status = await client.call_tool("get_status", {})
            assert status.data["configured"] is True

    asyncio.run(run())


def test_mcp_full_download_chain(tmp_path):
    service = make_service(tmp_path)
    mcp = create_mcp_server(lambda: service)

    async def run():
        from fastmcp import Client

        service.events.set_loop(asyncio.get_running_loop())
        async with Client(mcp) as client:
            basket = await client.call_tool("refresh_basket", {})
            assert basket.data["refreshed"] is True
            assert len(basket.data["items"]) == 1

            started = await client.call_tool("start_download", {"mode": "all"})
            assert started.data["keys"] == ["K1"]
            task_id = started.data["task_id"]

            for _ in range(200):
                task = await client.call_tool("get_task", {"task_id": task_id})
                if task.data["state"] != "running":
                    break
                await asyncio.sleep(0.05)
            assert task.data["state"] == "done", task.data["error"]

            history = await client.call_tool("get_download_history", {})
            assert history.data["entries"][0]["dockey"] == "K1"

            protocol = await client.call_tool("get_download_protocol", {"dockey": "K1"})
            messages = [e["message"] for e in protocol.data["entries"]]
            assert any("completed successfully" in m for m in messages)

            # Config tool never exposes secrets
            config = await client.call_tool("get_config", {})
            assert "password" not in str(config.data).lower() or "s3cret" not in str(config.data)

    asyncio.run(run())
    assert (tmp_path / "store" / "comp.bin").exists()


def test_mcp_update_config_roundtrip(tmp_path):
    service = make_service(tmp_path)
    mcp = create_mcp_server(lambda: service)

    async def run():
        from fastmcp import Client

        async with Client(mcp) as client:
            result = await client.call_tool(
                "update_config",
                {"settings": {"create_subdir": True, "save_description": False}},
            )
            assert result.data["create_subdir"] is True
            assert service.settings.create_subdir is True

    asyncio.run(run())
