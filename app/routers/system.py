# app/routers/system.py
import logging
import yaml
import io
import zipfile
import tempfile
from typing import Optional

import os
from fastapi import APIRouter, HTTPException, Request, Body, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .. import fetcher
from .. import database
from ..credentials_manager import get_username, save_credentials
from ..config import config_manager
from ..logging_config import setup_logging
from .vehicles import get_cached_vehicle_data

_LOGGER = logging.getLogger(__name__)
_REDACTED_VALUE = "***REDACTED***"
_SENSITIVE_CONFIG_KEY_PARTS = {
    "api_key",
    "apikey",
    "key",
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "credentials",
}


def _sanitize_config(value, parent_key: str = ""):
    """Return a copy of the config with sensitive values redacted."""
    if isinstance(value, dict):
        sanitized = {}
        for key, child_value in value.items():
            key_str = str(key).lower()
            if any(part in key_str for part in _SENSITIVE_CONFIG_KEY_PARTS):
                sanitized[key] = _REDACTED_VALUE
            else:
                sanitized[key] = _sanitize_config(child_value, key_str)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_config(item, parent_key) for item in value]
    return value


router = APIRouter(prefix="/api", tags=["system"])

@router.get("/geocode_status")
def get_geocode_status():
    """API endpoint to get the number of trips pending geocoding."""
    db = database.SessionLocal()
    try:
        pending_count = (
            db.query(database.Trip)
            .filter(database.Trip.start_address == "Geocoding...")
            .count()
        )
        total_count = db.query(database.Trip).count()
        return {"pending": pending_count, "total": total_count}
    finally:
        db.close()

@router.post("/force_poll")
async def force_poll(request: Request):
    """Manually triggers a data fetch."""
    try:
        _LOGGER.info("Manual poll triggered via API.")
        all_vehicles_data = await fetcher.run_fetch_cycle()
        if (
            hasattr(request.app.state, "mqtt_handler")
            and request.app.state.mqtt_handler
            and all_vehicles_data
        ):
            _LOGGER.info(
                f"Publishing data for {len(all_vehicles_data)} vehicles to MQTT..."
            )
            for vehicle_data in all_vehicles_data:
                request.app.state.mqtt_handler.publish(vehicle_data, autodiscovery=True)
        return {"message": "Data poll completed successfully."}
    except Exception as e:
        _LOGGER.error(f"Error during manual poll: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="An internal error occurred during the data poll."
        )

class MqttTestRequest(BaseModel):
    enabled: bool
    host: str
    port: int
    username: Optional[str] = ""
    password: Optional[str] = ""
    base_topic: str
    discovery_prefix: str
    enabled_sensors: dict

@router.post("/mqtt/test")
async def mqtt_test(request: Request, test_config: MqttTestRequest):
    """
    Sends the latest cached data to the MQTT broker for testing purposes using the provided config.
    """
    _LOGGER.info("MQTT test message triggered via API.")

    if not hasattr(request.app.state, "mqtt_handler") or not request.app.state.mqtt_handler:
        raise HTTPException(
            status_code=400,
            detail="MQTT handler is not initialized.",
        )

    vehicles = await get_cached_vehicle_data()
    if not vehicles:
        raise HTTPException(
            status_code=404,
            detail="No cached vehicle data found. Please run a poll first.",
        )

    config_dict = test_config.dict()
    
    # Handle redacted password
    if config_dict.get("password") == "***REDACTED***":
        saved_config = config_manager.settings.get("mqtt", {})
        config_dict["password"] = saved_config.get("password", "")

    try:
        for vehicle in vehicles:
            request.app.state.mqtt_handler.publish(vehicle, autodiscovery=True, override_config=config_dict)
        return {"message": "Test message sent successfully to MQTT broker."}
    except Exception as e:
        _LOGGER.error(f"Error during MQTT test publish: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to connect or publish to MQTT broker: {str(e)}"
        )

@router.get("/credentials")
def get_stored_username():
    """API endpoint to get the stored username."""
    username = get_username()
    return {"username": username or ""}

@router.post("/credentials")
def update_credentials(creds: dict = Body(...)):
    """API endpoint to update and save credentials."""
    username = creds.get("username")
    password = creds.get("password")
    if not username or not password:
        raise HTTPException(
            status_code=400, detail="Username and password are required."
        )
    try:
        save_credentials(username, password)
        return {"message": "Credentials saved successfully."}
    except Exception as e:
        logging.error(f"Error saving credentials: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to save credentials.")

@router.get("/raw_responses")
def list_raw_responses():
    import json
    from ..config import DATA_DIR
    raw_dir = DATA_DIR / "raw_responses"
    if not raw_dir.exists():
        return []
    
    polls = []
    for p in raw_dir.iterdir():
        if p.is_dir():
            files = []
            trip_count = None
            metadata_obj = None
            
            # Check for metadata.json first
            meta_path = p / "metadata.json"
            if meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8") as mf:
                        raw_meta = json.load(mf)
                        if isinstance(raw_meta, dict) and "updates" in raw_meta:
                            updates = raw_meta.get("updates") or {}
                            if "trips" in updates and isinstance(updates["trips"], list):
                                trip_count = len(updates["trips"])
                                metadata_obj = {"updates": {"trips": True if trip_count > 0 else []}}
                            else:
                                metadata_obj = {"updates": {}}
                except Exception:
                    pass

            for f in p.glob("*.json"):
                if f.is_file():
                    stat = f.stat()
                    files.append({
                        "filename": f.name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime
                    })
            files.sort(key=lambda x: x["filename"])
            polls.append({
                "poll_id": p.name,
                "mtime": p.stat().st_mtime,
                "files": files,
                "trip_count": trip_count,
                "metadata": metadata_obj
            })
    polls.sort(key=lambda x: x["poll_id"], reverse=True)
    return polls

@router.get("/raw_responses/{poll_id}/download")
def download_poll_zip(poll_id: str, background_tasks: BackgroundTasks):
    from ..config import DATA_DIR
    safe_poll_id = "".join(c for c in poll_id if c.isalnum() or c in "._-")
    base_dir = (DATA_DIR / "raw_responses").resolve()
    poll_dir = (base_dir / safe_poll_id).resolve()
    
    try:
        poll_dir.relative_to(base_dir)
        if poll_dir == base_dir:
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path.")
    
    if not poll_dir.exists() or not poll_dir.is_dir():
        raise HTTPException(status_code=404, detail="Poll directory not found.")
        
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    temp_file.close()
    
    try:
        with zipfile.ZipFile(temp_file.name, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for f in poll_dir.glob("*.json"):
                if f.is_file():
                    zip_file.write(f, f.name)
    except Exception:
        os.unlink(temp_file.name)
        raise HTTPException(status_code=500, detail="Failed to create zip file.")
    
    background_tasks.add_task(os.unlink, temp_file.name)
    return FileResponse(
        path=temp_file.name,
        media_type="application/zip",
        filename=f"{safe_poll_id}.zip"
    )

@router.get("/raw_responses/{poll_id}/{filename}")
def get_raw_response(poll_id: str, filename: str):
    from ..config import DATA_DIR
    _LOGGER.debug(f"Requested raw response: poll_id='{poll_id}', filename='{filename}'")
    
    safe_poll_id = "".join(c for c in poll_id if c.isalnum() or c in "._-")
    safe_filename = "".join(c for c in filename if c.isalnum() or c in "._-")
    
    base_dir = (DATA_DIR / "raw_responses").resolve()
    poll_dir = (base_dir / safe_poll_id).resolve()
    file_path = (poll_dir / safe_filename).resolve()
    
    try:
        file_path.relative_to(base_dir)
        file_path.relative_to(poll_dir)
        if file_path == poll_dir or file_path == base_dir or poll_dir == base_dir:
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path.")
    
    _LOGGER.debug(f"Resolved file_path: '{file_path}', exists: {file_path.exists()}, is_file: {file_path.is_file() if file_path.exists() else False}")
    
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Raw response file not found.")
        
    return FileResponse(path=file_path, filename=file_path.name, media_type="application/json")

@router.get("/config")
def get_config():
    """API endpoint to get the current configuration."""
    return _sanitize_config(config_manager.settings)

@router.post("/config")
def update_config(new_settings: dict = Body(...)):
    """API endpoint to update and save configuration to the user-specific config file."""
    try:
        # 1. Read the existing user config to preserve unchanged settings
        try:
            with open(config_manager.user_config_path, "r") as f:
                current_user_config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            current_user_config = {}

        # 2. Deep merge the new settings from the UI into the existing user settings
        updated_user_config = config_manager._deep_merge(
            new_settings, current_user_config
        )

        # 3. Write the result back to user_config.yaml
        with open(config_manager.user_config_path, "w") as f:
            yaml.dump(updated_user_config, f, default_flow_style=False, sort_keys=False)

        # 4. Reload the configuration into memory for the running app
        config_manager.load()

        # 5. Re-apply logging settings and history size
        setup_logging()
        if "log_history_size" in config_manager.settings:
            from ..main import update_log_history_size
            update_log_history_size(config_manager.settings["log_history_size"])

        return {"message": "Settings saved successfully."}
    except Exception as e:
        _LOGGER.error(f"Error updating user config file: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to write to user configuration file."
        )

@router.post("/backfill_geocoding")
async def trigger_geocoding_backfill(force_all: bool = False):
    """Triggers a manual, on-demand backfill of missing geocoding data."""
    try:
        result = await fetcher.backfill_geocoding(force_all=force_all)
        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])
        return result
    except Exception as e:
        logging.error(f"Error during manual geocoding backfill: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred during the geocoding backfill.",
        )

class GeocodeTestRequest(BaseModel):
    lat: float
    lon: float
    provider: str
    opencage_api_key: Optional[str] = None
    google_maps_api_key: Optional[str] = None

@router.post("/geocoding/test")
async def test_geocoding_endpoint(request: GeocodeTestRequest):
    """Tests the geocoding provider with the given credentials."""
    from app.geocoder import NominatimGeocoder, OpenCageGeocoder, GoogleMapsGeocoder

    provider = request.provider.lower()
    if provider == "opencage":
        geocoder = OpenCageGeocoder(request.opencage_api_key or "")
    elif provider == "google_maps":
        geocoder = GoogleMapsGeocoder(request.google_maps_api_key or "")
    else:
        geocoder = NominatimGeocoder()

    try:
        address = await geocoder.reverse_geocode(request.lat, request.lon)
        if address and address != "Unavailable":
            return {"address": address}
        else:
            raise HTTPException(
                status_code=400,
                detail="Could not resolve address or provider unavailable.",
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Geocoding test failed: {str(e)}")
