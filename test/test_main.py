# tests/test_main.py
import pytest
from unittest.mock import patch, AsyncMock
import yaml

@pytest.mark.asyncio
async def test_read_root(client):
    """Tests if the main page loads successfully."""
    response = await client.get("/")
    assert response.status_code == 200
    assert "MyVehicle Dashboard" in response.text

@pytest.mark.asyncio
async def test_get_config(client):
    """Tests the API endpoint for retrieving the configuration."""
    response = await client.get("/api/config")
    assert response.status_code == 200
    config = response.json()
    assert "web_server" in config
    assert "polling" in config["web_server"]

@pytest.mark.asyncio
async def test_force_poll_mocked(client):
    """Tests the force_poll endpoint by mocking the actual fetcher."""
    # We patch 'app.fetcher.run_fetch_cycle' to avoid real API calls.
    # The patch is an AsyncMock because the original function is async.
    with patch("app.fetcher.run_fetch_cycle", new_callable=AsyncMock) as mock_fetch:
        # We can define a return value for our mock if needed.
        mock_fetch.return_value = [{"vin": "TESTVIN123"}]

        response = await client.post("/api/force_poll")
        
        assert response.status_code == 200
        assert response.json() == {"message": "Data poll completed successfully."}
        mock_fetch.assert_awaited_once()

@pytest.mark.asyncio
async def test_update_config(client, test_data_dir):
    """
    Tests updating the configuration via the API.
    Uses the test_data_dir fixture to ensure file operations are isolated.
    """
    new_settings = {
        "web_server": {
            "polling": {
                "mode": "fixed_time",
                "fixed_time": "10:00"
            }
        }
    }
    response = await client.post("/api/config", json=new_settings)
    assert response.status_code == 200
    assert response.json() == {"message": "Settings saved successfully."}

    # Verify the config was reloaded into memory
    from app.config import config_manager
    assert config_manager.settings["web_server"]["polling"]["mode"] == "fixed_time"

    # Verify the user_config.yaml file was written correctly
    user_config_path = test_data_dir / "user_config.yaml"
    assert user_config_path.exists()
    with open(user_config_path, 'r') as f:
        saved_config = yaml.safe_load(f)
    assert saved_config["web_server"]["polling"]["mode"] == "fixed_time"


@pytest.mark.asyncio
async def test_log_stream_per_client_subscribers():
    """Tests that logs only queue when subscribers are connected and clean up on disconnect."""
    import asyncio
    import logging
    from unittest.mock import MagicMock
    import app.main as main_mod

    main_mod.log_subscribers.clear()
    main_mod.update_log_history_size(50)
    assert main_mod.log_history.maxlen == 50

    # Emit log with no subscribers connected -> should only update log_history
    logging.getLogger("test_logger").info("Test log without subscribers")
    assert any("Test log without subscribers" in item["message"] for item in main_mod.log_history)
    assert len(main_mod.log_subscribers) == 0

    # Simulate a connected SSE client
    mock_request = MagicMock()
    disconnected = False

    async def is_disconnected():
        return disconnected

    mock_request.is_disconnected = is_disconnected

    gen = main_mod.log_stream_generator(mock_request)
    first_chunk = await gen.__anext__()
    assert first_chunk.startswith("event: history\n")
    assert len(main_mod.log_subscribers) == 1

    # Emit a live log while subscriber is active
    logging.getLogger("test_logger").warning("Live streamed warning")
    await asyncio.sleep(0)  # Let call_soon_threadsafe callback run

    second_chunk = await gen.__anext__()
    assert second_chunk.startswith("event: message\n")
    assert "Live streamed warning" in second_chunk

    # Disconnect client and ensure subscriber is removed
    disconnected = True
    await gen.aclose()
    assert len(main_mod.log_subscribers) == 0