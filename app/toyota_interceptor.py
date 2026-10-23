import functools
import inspect
from pytoyoda.controller import Controller
from pytoyoda.api import Api

# Patch Api.get_trips to handle signature mismatch (e.g. unexpected 'route' keyword argument) across pytoyoda versions
_orig_get_trips = Api.get_trips

@functools.wraps(_orig_get_trips)
async def _patched_get_trips(self, *args, **kwargs):
    sig = inspect.signature(_orig_get_trips)
    has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    if not has_var_keyword:
        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return await _orig_get_trips(self, *args, **kwargs)

Api.get_trips = _patched_get_trips

class PatchedController(Controller):
    """Intercepts raw API responses to fix missing keys before Pydantic parsing."""

    async def request_json(self, method: str, endpoint: str, **kwargs) -> dict:
        response = await super().request_json(method, endpoint, **kwargs)

        try:
            from app.fetcher import save_raw_response
            await save_raw_response(endpoint, response)
        except Exception:
            pass

        # Intercept trips endpoint to patch missing summary keys
        if "trips" in endpoint and isinstance(response, dict) and "payload" in response:
            payload = response.get("payload")
            if payload and "trips" in payload and isinstance(payload["trips"], list):
                for trip in payload["trips"]:
                    summary = trip.get("summary")
                    if isinstance(summary, dict):
                        # Inject missing keys required by pytoyoda's strict Pydantic model
                        missing_keys = [
                            "durationIdle", 
                            "countries", 
                            "maxSpeed", 
                            "lengthOverspeed", 
                            "durationOverspeed", 
                            "lengthHighway", 
                            "durationHighway"
                        ]
                        for key in missing_keys:
                            summary.setdefault(key, None)

        # Intercept vehicle/guid endpoint to patch missing remoteDisplay key
        if "vehicle/guid" in endpoint and isinstance(response, dict) and "payload" in response:
            payload = response.get("payload")
            if isinstance(payload, list):
                for vehicle in payload:
                    if isinstance(vehicle, dict):
                        vehicle.setdefault("remoteDisplay", None)

        return response
