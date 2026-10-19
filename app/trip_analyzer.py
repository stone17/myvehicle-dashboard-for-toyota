# app/trip_analyzer.py
import asyncio
import datetime
import logging
import math
from . import database
from . import time_utils
from .config import config_manager

_LOGGER = logging.getLogger(__name__)


class HighwayEstimator:
    """Kinematic estimator for missing route segment durations."""
    
    @staticmethod
    def _calculate_dynamic_k(v_avg_kmh: float) -> float:
        """
        Dynamically calculates the k-factor (v_hw / v_other).
        High v_avg (>= 90) -> k approaches 1.0.
        Mid  v_avg ( = 30) -> k equals 2.
        Low  v_avg (<= 30) -> k approaches 2.5.
        """
        base_k = 1.0 + max(0.0, (90.0 - v_avg_kmh) / 60.0)
        return min(base_k, 2.5)

    @classmethod
    def estimate_highway_duration(cls, total_distance_km: float, highway_distance_km: float, total_duration_seconds: float) -> float:
        if total_duration_seconds <= 0:
            return 0.0
            
        total_duration_hours = total_duration_seconds / 3600.0
        v_avg = total_distance_km / total_duration_hours
        
        d_hw = min(highway_distance_km, total_distance_km)
        d_other = max(0.0, total_distance_km - d_hw)
        
        k_factor = cls._calculate_dynamic_k(v_avg)
        
        # Calculate baseline v_hw based on dynamic k-factor
        v_hw = (d_hw + k_factor * d_other) / total_duration_hours
        
        # Clamp highway speed to a minimum of 80 km/h
        v_hw = max(v_hw, 80.0)
        
        # Calculate duration based on the clamped speed
        t_hw_hours = (d_hw / v_hw) if v_hw > 0 else 0.0
        
        # Guard against exceeding total time
        t_hw_hours = min(t_hw_hours, total_duration_hours)
        
        return t_hw_hours * 3600.0


class RouteMetricsCalculator:
    """Calculates missing kinematic metrics from raw GPS route nodes."""
    
    @staticmethod
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371.0 
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
        return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    @classmethod
    def calculate(cls, route: list, avg_speed_kmh: float, total_distance_km: float = 0.0, total_duration_seconds: float = 0.0) -> dict:
        metrics = {
            "length_overspeed_km": 0.0,
            "duration_overspeed_seconds": 0.0,
            "length_highway_km": 0.0,
            "duration_highway_seconds": 0.0
        }
        
        if not route or len(route) < 2:
            return metrics

        avg_speed_kms = (avg_speed_kmh / 3600.0) if avg_speed_kmh > 0 else 0.0

        for i in range(1, len(route)):
            p1, p2 = route[i-1], route[i]
            
            os_val = p2.get("overspeed") or p2.get("overSpeed")
            hw_val = p2.get("highway")
            
            is_overspeed = os_val is True or str(os_val).lower() == "true"
            is_highway = hw_val is True or str(hw_val).lower() == "true"

            if is_overspeed or is_highway:
                lat1, lon1 = p1.get("lat"), p1.get("lon")
                lat2, lon2 = p2.get("lat"), p2.get("lon")

                if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
                    continue

                dx_km = cls._haversine(float(lat1), float(lon1), float(lat2), float(lon2))

                if is_overspeed:
                    dt_seconds = (dx_km / avg_speed_kms) if avg_speed_kms > 0 else 0
                    metrics["length_overspeed_km"] += dx_km
                    metrics["duration_overspeed_seconds"] += dt_seconds
                    
                if is_highway:
                    metrics["length_highway_km"] += dx_km

        # Apply kinematic model to resolve highway time
        metrics["duration_highway_seconds"] = HighwayEstimator.estimate_highway_duration(
            total_distance_km, 
            metrics["length_highway_km"], 
            total_duration_seconds
        )

        metrics["length_overspeed_km"] = round(metrics["length_overspeed_km"], 3)
        metrics["length_highway_km"] = round(metrics["length_highway_km"], 3)
        metrics["duration_overspeed_seconds"] = int(metrics["duration_overspeed_seconds"])
        metrics["duration_highway_seconds"] = int(metrics["duration_highway_seconds"])
        
        return metrics


class TripCoordinates:
    """Robustly extracts trip coordinates from varying Pytoyoda model structures."""
    def __init__(self, trip_obj):
        self.start_lat = getattr(trip_obj, "start_lat", None)
        self.start_lon = getattr(trip_obj, "start_lon", None)
        self.end_lat = getattr(trip_obj, "end_lat", None)
        self.end_lon = getattr(trip_obj, "end_lon", None)

        if self.start_lat is None and hasattr(trip_obj, "locations") and trip_obj.locations:
            if hasattr(trip_obj.locations, "start") and trip_obj.locations.start:
                self.start_lat = getattr(trip_obj.locations.start, "lat", None)
                self.start_lon = getattr(trip_obj.locations.start, "lon", None)
            if hasattr(trip_obj.locations, "end") and trip_obj.locations.end:
                self.end_lat = getattr(trip_obj.locations.end, "lat", None)
                self.end_lon = getattr(trip_obj.locations.end, "lon", None)
        
        if self.start_lat is None and hasattr(trip_obj, "route") and trip_obj.route:
            start_node = trip_obj.route[0]
            end_node = trip_obj.route[-1]
            
            self.start_lat = start_node.get("lat") if isinstance(start_node, dict) else getattr(start_node, "lat", None)
            self.start_lon = start_node.get("lon") if isinstance(start_node, dict) else getattr(start_node, "lon", None)
            self.end_lat = end_node.get("lat") if isinstance(end_node, dict) else getattr(end_node, "lat", None)
            self.end_lon = end_node.get("lon") if isinstance(end_node, dict) else getattr(end_node, "lon", None)
    
    @property
    def is_valid(self):
        return self.start_lat is not None and self.start_lon is not None


class TripAnalyzer:
    """Handles fetching, parsing, and storing vehicle trip data."""
    
    PROTECTED_FIELDS = {"start_address", "end_address", "countries"}
    KM_TO_MI = 0.621371

    def __init__(self, vehicle, db_session, geocode_callback=None):
        self.vehicle = vehicle
        self.db_session = db_session
        self.geocode_callback = geocode_callback

    async def fetch_and_process(self, from_date, to_date):
        _LOGGER.info(f"Fetching trip summaries for VIN {self.vehicle.vin} from {from_date} to {to_date}...")

        fetch_full_route = config_manager.settings.get("fetch_full_trip_route", False)
        all_trips = await self.vehicle.get_trips(
            from_date=from_date, to_date=to_date, full_route=fetch_full_route
        )

        if not isinstance(all_trips, list):
            _LOGGER.error(f"Expected a list of trips, but got {type(all_trips)}. Aborting trip fetch.")
            return {"new": 0, "updated": 0, "skipped": 0, "error": "Invalid response from API library"}

        _LOGGER.info(f"API returned a total of {len(all_trips)} trips for the period.")
        
        counts = {"new": 0, "updated": 0, "skipped": 0}

        for trip in all_trips:
            try:
                coords = TripCoordinates(trip)
                if not coords.is_valid:
                    _LOGGER.warning("Skipping a trip object because it's missing coordinate data.")
                    counts["skipped"] += 1
                    continue

                new_data = self._extract_trip_data(trip, coords, fetch_full_route)
                api_id = new_data.get("api_id")
                start_ts = new_data.get("start_timestamp")
                db_id = self._upsert_trip(new_data, counts)

                try:
                    from .fetcher import current_poll_updates
                    updates = current_poll_updates.get()
                    if updates is not None:
                        updates["trips"].append({
                            "id": api_id,
                            "start_timestamp": start_ts.isoformat() if start_ts else None,
                            "database_id": db_id
                        })
                except Exception:
                    pass

            except Exception as e:
                _LOGGER.warning(f"Could not process a trip summary due to an error: {e}. Skipping.", exc_info=True)
                self.db_session.rollback()

        _LOGGER.info(f"Trip summary fetch complete. New: {counts['new']}, Updated: {counts['updated']}, Skipped: {counts['skipped']}.")
        return counts

    def _extract_trip_data(self, trip, coords, fetch_full_route):
        """Extracts and normalizes data points from the API trip object."""
        summary = trip._trip.summary if hasattr(trip, "_trip") and hasattr(trip._trip, "summary") else None
        scores = trip._trip.scores if hasattr(trip, "_trip") and hasattr(trip._trip, "scores") else None
        hdc = trip._trip.hdc if hasattr(trip, "_trip") and hasattr(trip._trip, "hdc") else None

        api_id = getattr(trip, "id", None) or (trip.get("id") if isinstance(trip, dict) else None)

        # 1. Safely extract timestamps 
        start_ts_raw = getattr(trip, "start_time", None) or (summary.start_ts if summary else None)
        end_ts_raw = getattr(trip, "end_time", None) or (summary.end_ts if summary else None)
        start_timestamp = time_utils.convert_to_naive_utc(start_ts_raw)
        end_timestamp = time_utils.convert_to_naive_utc(end_ts_raw)

        # 2. Safely extract core metrics avoiding NoneType math errors
        distance_km = getattr(trip, "distance", 0.0) or 0.0
        fuel_consumption_l_100km = getattr(trip, "average_fuel_consumed", 0.0) or 0.0
        
        raw_duration = getattr(trip, "duration", 0) or 0
        duration_seconds = raw_duration.total_seconds() if hasattr(raw_duration, "total_seconds") else float(raw_duration)
        
        average_speed_kmh = (distance_km / (duration_seconds / 3600)) if duration_seconds > 0 and distance_km > 0 else 0.0

        hdc_ev_distance = getattr(hdc, "ev_distance", None)
        ev_distance_km = (hdc_ev_distance / 1000) if hdc_ev_distance is not None else getattr(trip, "ev_distance", 0.0)
        
        raw_ev_duration = getattr(trip, "ev_duration", 0) or 0
        fallback_ev_seconds = raw_ev_duration.total_seconds() if hasattr(raw_ev_duration, "total_seconds") else float(raw_ev_duration)
        hdc_ev_time = getattr(hdc, "ev_time", None)
        hdc_charge_dist = getattr(hdc, "charge_dist", None)
        hdc_eco_dist = getattr(hdc, "eco_dist", None)
        hdc_power_dist = getattr(hdc, "power_dist", None)
        
        # 3. Route Calculation
        route_data = None
        route_metrics = {"length_overspeed_km": 0.0, "duration_overspeed_seconds": 0, "length_highway_km": 0.0, "duration_highway_seconds": 0}

        # Bypass the Pytoyoda wrapper to access the raw Pydantic payload
        raw_route = trip._trip.route if hasattr(trip, "_trip") and hasattr(trip._trip, "route") else getattr(trip, "route", None)

        if fetch_full_route and raw_route:
            route_data = []
            for point in raw_route:
                # Extract directly from the unadulterated raw model
                if hasattr(point, "model_dump"):
                    point_dict = point.model_dump(mode="json", by_alias=True)
                elif isinstance(point, dict):
                    point_dict = point
                else:
                    point_dict = vars(point)
                
                # Standardize keys for the calculator
                route_data.append({
                    "lat": point_dict.get("lat"),
                    "lon": point_dict.get("lon"),
                    "overspeed": point_dict.get("overspeed") or point_dict.get("overSpeed") or False,
                    "highway": point_dict.get("highway", False)
                })
            
            route_metrics = RouteMetricsCalculator.calculate(
                route_data, average_speed_kmh, distance_km, duration_seconds
            )
            
        return {
            "api_id": api_id,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "start_lat": coords.start_lat,
            "start_lon": coords.start_lon,
            "end_lat": coords.end_lat,
            "end_lon": coords.end_lon,
            "distance_km": distance_km,
            "fuel_consumption_l_100km": fuel_consumption_l_100km,
            "duration_seconds": int(duration_seconds),
            "average_speed_kmh": average_speed_kmh,
            "max_speed_kmh": summary.max_speed if summary else None,
            "countries": summary.countries if summary else None,
            "length_overspeed_km": (summary.length_overspeed / 1000) if summary and summary.length_overspeed else route_metrics["length_overspeed_km"],
            "duration_overspeed_seconds": summary.duration_overspeed if summary and summary.duration_overspeed else route_metrics["duration_overspeed_seconds"],
            "length_highway_km": (summary.length_highway / 1000) if summary and summary.length_highway else route_metrics["length_highway_km"],
            "duration_highway_seconds": summary.duration_highway if summary and summary.duration_highway else route_metrics["duration_highway_seconds"],
            "night_trip": summary.night_trip if summary else None,
            "score_global": scores.global_ if scores else getattr(trip, "score", None),
            "score_acceleration": scores.acceleration if scores else None,
            "score_braking": scores.braking if scores else None,
            "score_advice": scores.advice if scores else None,
            "score_constant_speed": scores.constant_speed if scores else None,
            "ev_distance_km": ev_distance_km,
            "ev_duration_seconds": hdc_ev_time if hdc_ev_time is not None else int(fallback_ev_seconds),
            "hdc_charge_duration_seconds": getattr(hdc, "charge_time", None),
            "hdc_charge_distance_km": (hdc_charge_dist / 1000) if hdc_charge_dist is not None else None,
            "hdc_eco_duration_seconds": getattr(hdc, "eco_time", None),
            "hdc_eco_distance_km": (hdc_eco_dist / 1000) if hdc_eco_dist is not None else None,
            "hdc_power_duration_seconds": getattr(hdc, "power_time", None),
            "hdc_power_distance_km": (hdc_power_dist / 1000) if hdc_power_dist is not None else None,
            "distance_mi": distance_km * self.KM_TO_MI,
            "mpg": (235.214 / fuel_consumption_l_100km) if fuel_consumption_l_100km > 0 else 0.0,
            "mpg_uk": (282.481 / fuel_consumption_l_100km) if fuel_consumption_l_100km > 0 else 0.0,
            "average_speed_mph": average_speed_kmh * self.KM_TO_MI,
            "ev_distance_mi": (ev_distance_km or 0.0) * self.KM_TO_MI,
            "route": route_data,
        }

    def _upsert_trip(self, new_data, counts):
        """Inserts or updates the trip in the database and triggers geocoding."""
        existing_trip = self.db_session.query(database.Trip).filter_by(
            vin=self.vehicle.vin, start_timestamp=new_data["start_timestamp"]
        ).first()

        start_ts_utc = new_data.pop("start_timestamp")

        if existing_trip:
            has_changes = False
            for key, value in new_data.items():
                # 1. Skip fields that should never be overwritten by the API
                if key not in self.PROTECTED_FIELDS:
                    existing_val = getattr(existing_trip, key)
                    if existing_val != value:
                        # 2. Prevent overwriting valid data with empty/zero values from historical fetches
                        existing_is_empty = existing_val in (None, "", "N/A", 0, 0.0)
                        new_is_empty = value in (None, "", "N/A", 0, 0.0)
                        
                        if existing_is_empty or not new_is_empty:
                            setattr(existing_trip, key, value)
                            has_changes = True
            
            if has_changes:
                self.db_session.commit()
                counts["updated"] += 1
            else:
                counts["skipped"] += 1

            if not existing_trip.countries and self.geocode_callback:
                asyncio.create_task(self.geocode_callback(existing_trip.id))
            return existing_trip.id
        else:
            new_trip = database.Trip(
                vin=self.vehicle.vin,
                start_timestamp=start_ts_utc,
                start_address="Geocoding...",
                end_address="Geocoding...",
                **new_data,
            )
            self.db_session.add(new_trip)
            self.db_session.commit()
            self.db_session.refresh(new_trip)
            counts["new"] += 1
            if self.geocode_callback:
                asyncio.create_task(self.geocode_callback(new_trip.id))
            return new_trip.id
