import pytest
from unittest.mock import patch
from pytoyoda.api import Api
import app.toyota_interceptor  # Ensures monkeypatch is applied


@pytest.mark.asyncio
async def test_patched_get_trips_filters_unsupported_kwargs():
    """Tests that _patched_get_trips filters out unsupported kwargs when underlying function doesn't accept them."""
    # Create a dummy function signature without 'route'
    async def dummy_get_trips(self, vin, from_date, to_date, summary=True):
        return {"vin": vin, "summary": summary}

    with patch.object(app.toyota_interceptor, "_orig_get_trips", autospec=dummy_get_trips) as mock_orig:
        mock_orig.side_effect = dummy_get_trips
        mock_api_instance = Api.__new__(Api)

        result = await Api.get_trips(
            mock_api_instance,
            vin="TESTVIN123",
            from_date="2026-01-01",
            to_date="2026-01-02",
            route=False,
            summary=True,
        )
        assert result == {"vin": "TESTVIN123", "summary": True}
