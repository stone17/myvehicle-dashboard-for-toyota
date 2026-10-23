# app/routers/trips.py
import datetime
import csv
import io
import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import defer

from .. import database
from .. import time_utils
from ..config import config_manager

_LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["trips"])

@router.get("/trips")
def get_trips(
    vin: str,
    sort_by: str = "start_timestamp",
    sort_direction: str = "desc",
    unit_system: str = "metric",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    countries: Optional[str] = Query(None),
):
    """API endpoint to get all imported trips for a vehicle, with date and country filtering."""
    db = database.SessionLocal()
    try:
        from sqlalchemy import text

        valid_sort_columns = {c.name for c in database.Trip.__table__.columns}
        if sort_by not in valid_sort_columns:
            raise HTTPException(status_code=400, detail=f"Invalid sort_by parameter.")

        sort_column_name = {
            "distance_km": "distance_mi"
            if unit_system.startswith("imperial")
            else "distance_km",
            "fuel_consumption_l_100km": "mpg_uk"
            if unit_system == "imperial_uk"
            else (
                "mpg" if unit_system == "imperial_us" else "fuel_consumption_l_100km"
            ),
            "average_speed_kmh": "average_speed_mph"
            if unit_system.startswith("imperial")
            else "average_speed_kmh",
            "ev_distance_km": "ev_distance_mi"
            if unit_system.startswith("imperial")
            else "ev_distance_km",
        }.get(sort_by, sort_by)

        sort_expression = None
        if sort_by == "fuel_consumption_l_100km":
            if unit_system.startswith("imperial"):
                if sort_direction == "desc":
                    sort_expression = text(
                        f"CASE WHEN {sort_column_name} IS NULL OR {sort_column_name} = 0 THEN 1 ELSE 0 END, {sort_column_name} DESC"
                    )
                else:
                    sort_expression = text(
                        f"CASE WHEN {sort_column_name} IS NULL OR {sort_column_name} = 0 THEN 0 ELSE 1 END, {sort_column_name} ASC"
                    )
            else:
                if sort_direction == "desc":
                    sort_expression = text(
                        f"CASE WHEN {sort_column_name} IS NULL THEN 1 ELSE 0 END, {sort_column_name} ASC"
                    )
                else:
                    sort_expression = text(
                        f"CASE WHEN {sort_column_name} IS NULL THEN 1 ELSE 0 END, {sort_column_name} DESC"
                    )
        else:
            direction_sql = "DESC" if sort_direction == "desc" else "ASC"
            sort_expression = text(f"{sort_column_name} {direction_sql} NULLS LAST")

        # Base query
        query = db.query(database.Trip).options(defer(database.Trip.route))
        if vin != "all":
            query = query.filter(database.Trip.vin == vin)

        # Apply date filters if provided
        if start_date:
            try:
                start_dt = datetime.datetime.fromisoformat(start_date[:10]).replace(
                    hour=0, minute=0, second=0
                )
                start_dt_aware = time_utils.convert_from_local_naive(start_dt, config_manager)
                start_dt_utc = time_utils.convert_to_naive_utc(start_dt_aware)
                query = query.filter(database.Trip.start_timestamp >= start_dt_utc)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid start_date format. Use YYYY-MM-DD."
                )
        if end_date:
            try:
                end_dt = datetime.datetime.fromisoformat(end_date[:10]).replace(
                    hour=23, minute=59, second=59
                )
                end_dt_aware = time_utils.convert_from_local_naive(end_dt, config_manager)
                end_dt_utc = time_utils.convert_to_naive_utc(end_dt_aware)
                query = query.filter(database.Trip.start_timestamp <= end_dt_utc)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid end_date format. Use YYYY-MM-DD."
                )

        # Apply country filter if provided
        if countries:
            country_list = [c.strip() for c in countries.split(",") if c.strip()]
            if country_list:
                country_filters = [
                    func.instr(database.Trip.countries, f'"{country}"') > 0
                    for country in country_list
                ]
                query = query.filter(or_(*country_filters))

        # Apply sorting and fetch all results
        trips = query.order_by(sort_expression).all()
        
        trip_dicts = []
        for trip in trips:
            trip_dict = {c.name: getattr(trip, c.name) for c in trip.__table__.columns if c.name != 'route'}
            trip_dict["start_timestamp"] = time_utils.convert_utc_to_local_aware(trip.start_timestamp, config_manager)
            trip_dict["end_timestamp"] = time_utils.convert_utc_to_local_aware(trip.end_timestamp, config_manager)
            trip_dicts.append(trip_dict)

        # This prevents "N/A" on the frontend if the backfill hasn't run for new trips.
        if unit_system.startswith("imperial"):
            KM_TO_MI = 0.621371
            for trip in trip_dicts:
                if trip.get("distance_km") is not None:
                    trip["distance_mi"] = trip["distance_km"] * KM_TO_MI
                if trip.get("ev_distance_km") is not None:
                    trip["ev_distance_mi"] = trip["ev_distance_km"] * KM_TO_MI
                if trip.get("average_speed_kmh") is not None:
                    trip["average_speed_mph"] = trip["average_speed_kmh"] * KM_TO_MI

                # Check for fuel consumption to avoid division by zero
                if trip.get("fuel_consumption_l_100km") and trip["fuel_consumption_l_100km"] > 0:
                    trip["mpg"] = 235.214 / trip["fuel_consumption_l_100km"]
                    trip["mpg_uk"] = 282.481 / trip["fuel_consumption_l_100km"]
                else:
                    # Assign a default value if no fuel was consumed
                    trip["mpg"] = 0.0
                    trip["mpg_uk"] = 0.0

        return trip_dicts
    finally:
        db.close()

class TripCheckItem(BaseModel):
    id: Optional[str] = None
    start_timestamp: Optional[str] = None

class CheckTripsRequest(BaseModel):
    items: List[TripCheckItem]

def parse_to_naive_utc(ts_str: str) -> Optional[datetime.datetime]:
    if not ts_str:
        return None
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(ts_str)
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None

@router.post("/raw_responses/check_trips")
def check_trips_exist(request: CheckTripsRequest):
    _LOGGER.debug(f"check_trips_exist called with {len(request.items)} items")
    db = database.SessionLocal()
    try:
        ids_to_check = [item.id for item in request.items if item.id]
        timestamps_to_check = []
        
        for item in request.items:
            if item.start_timestamp:
                dt = parse_to_naive_utc(item.start_timestamp)
                if dt:
                    timestamps_to_check.append(dt)
                    
        existing_ids = set()
        if ids_to_check:
            results = db.query(database.Trip.api_id).filter(database.Trip.api_id.in_(ids_to_check)).all()
            existing_ids = {r[0] for r in results if r[0]}
            
        existing_timestamps = set()
        if timestamps_to_check:
            results = db.query(database.Trip.start_timestamp).filter(database.Trip.start_timestamp.in_(timestamps_to_check)).all()
            existing_timestamps = {r[0] for r in results if r[0]}
            
        response_map = {}
        for item in request.items:
            exists = False
            if item.id and item.id in existing_ids:
                exists = True
            elif item.start_timestamp:
                dt = parse_to_naive_utc(item.start_timestamp)
                if dt and dt in existing_timestamps:
                    exists = True
                    
            key = item.id if item.id else item.start_timestamp
            if key:
                response_map[key] = exists
                
        return response_map
    finally:
        db.close()

@router.get("/trips/{trip_id}/route")
def get_trip_route(trip_id: int):
    """Fetches the route data for a single trip."""
    db = database.SessionLocal()
    try:
        # Query only the 'route' column for efficiency
        trip = db.query(database.Trip.route).filter(database.Trip.id == trip_id).first()
        if not trip:
            raise HTTPException(status_code=404, detail="Trip not found")
        return {"route": trip.route}
    finally:
        db.close()

@router.get("/export/trips.csv")
def export_trips_to_csv(
    vin: str,
    unit_system: str = "metric",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    countries: Optional[str] = Query(None),
):
    """
    Exports the currently filtered list of trips to a CSV file.
    This endpoint re-uses the same filtering logic as the get_trips endpoint.
    """
    # Use a temporary database session
    db = database.SessionLocal()
    try:
        # --- 1. Fetch filtered trip data (logic copied from get_trips) ---
        query = db.query(database.Trip).options(defer(database.Trip.route))
        if vin != "all":
            query = query.filter(database.Trip.vin == vin)

        if start_date:
            start_dt = datetime.datetime.fromisoformat(start_date[:10]).replace(
                hour=0, minute=0, second=0
            )
            start_dt_aware = time_utils.convert_from_local_naive(start_dt, config_manager)
            start_dt_utc = time_utils.convert_to_naive_utc(start_dt_aware)
            query = query.filter(database.Trip.start_timestamp >= start_dt_utc)
        if end_date:
            end_dt = datetime.datetime.fromisoformat(end_date[:10]).replace(
                hour=23, minute=59, second=59
            )
            end_dt_aware = time_utils.convert_from_local_naive(end_dt, config_manager)
            end_dt_utc = time_utils.convert_to_naive_utc(end_dt_aware)
            query = query.filter(database.Trip.start_timestamp <= end_dt_utc)

        if countries:
            country_list = [c.strip() for c in countries.split(",") if c.strip()]
            if country_list:
                country_filters = [
                    func.instr(database.Trip.countries, f'"{country}"') > 0
                    for country in country_list
                ]
                query = query.filter(or_(*country_filters))

        trips = query.order_by(database.Trip.start_timestamp.desc()).all()

        # --- 2. Prepare CSV data in memory ---
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")

        is_imperial = unit_system.startswith("imperial")
        is_uk = unit_system == "imperial_uk"
        dist_unit = "mi" if is_imperial else "km"
        speed_unit = "mph" if is_imperial else "kmh"
        consumption_unit = (
            f"mpg_{'uk' if is_uk else 'us'}" if is_imperial else "l_100km"
        )

        # --- 3. Write CSV Header ---
        headers = [
            "start_timestamp_utc",
            "end_timestamp_utc",
            "start_address",
            "end_address",
            f"distance_{dist_unit}",
            f"consumption_{consumption_unit}",
            "duration_seconds",
            f"average_speed_{speed_unit}",
            f"max_speed_{speed_unit}",
            f"ev_distance_{dist_unit}",
            "ev_duration_seconds",
            "score_global",
            "score_acceleration",
            "score_braking",
            "score_constant_speed",
            "night_trip",
            "countries",
            f"overspeed_distance_{dist_unit}",
            "overspeed_duration_seconds",
            f"highway_distance_{dist_unit}",
            "highway_duration_seconds",
            f"hdc_eco_distance_{dist_unit}",
            "hdc_eco_duration_seconds",
            f"hdc_power_distance_{dist_unit}",
            "hdc_power_duration_seconds",
            f"hdc_charge_distance_{dist_unit}",
            "hdc_charge_duration_seconds",
        ]
        writer.writerow(headers)

        # --- 4. Write Data Rows ---
        for trip in trips:
            # Perform unit conversions on the fly for the export
            distance_mi = None
            average_speed_mph = None
            max_speed_mph = None
            ev_distance_mi = None
            length_overspeed_mi = None
            length_highway_mi = None
            hdc_eco_distance_mi = None
            hdc_power_distance_mi = None
            hdc_charge_distance_mi = None
            mpg_us = 0.0
            mpg_uk = 0.0

            if is_imperial:
                distance_mi = (
                    trip.distance_km * 0.621371
                    if trip.distance_km is not None
                    else None
                )
                average_speed_mph = (
                    trip.average_speed_kmh * 0.621371
                    if trip.average_speed_kmh is not None
                    else None
                )
                max_speed_mph = (
                    trip.max_speed_kmh * 0.621371
                    if trip.max_speed_kmh is not None
                    else None
                )
                ev_distance_mi = (
                    trip.ev_distance_km * 0.621371
                    if trip.ev_distance_km is not None
                    else None
                )
                length_overspeed_mi = (
                    trip.length_overspeed_km * 0.621371
                    if trip.length_overspeed_km is not None
                    else None
                )
                length_highway_mi = (
                    trip.length_highway_km * 0.621371
                    if trip.length_highway_km is not None
                    else None
                )
                hdc_eco_distance_mi = (
                    trip.hdc_eco_distance_km * 0.621371
                    if trip.hdc_eco_distance_km is not None
                    else None
                )
                hdc_power_distance_mi = (
                    trip.hdc_power_distance_km * 0.621371
                    if trip.hdc_power_distance_km is not None
                    else None
                )
                hdc_charge_distance_mi = (
                    trip.hdc_charge_distance_km * 0.621371
                    if trip.hdc_charge_distance_km is not None
                    else None
                )

                if trip.fuel_consumption_l_100km and trip.fuel_consumption_l_100km > 0:
                    mpg_us = 235.214 / trip.fuel_consumption_l_100km
                    mpg_uk = 282.481 / trip.fuel_consumption_l_100km

            row = [
                trip.start_timestamp,
                trip.end_timestamp,
                trip.start_address,
                trip.end_address,
                distance_mi if is_imperial else trip.distance_km,
                mpg_uk
                if is_uk
                else (mpg_us if is_imperial else trip.fuel_consumption_l_100km),
                trip.duration_seconds,
                average_speed_mph if is_imperial else trip.average_speed_kmh,
                max_speed_mph if is_imperial else trip.max_speed_kmh,
                ev_distance_mi if is_imperial else trip.ev_distance_km,
                trip.ev_duration_seconds,
                trip.score_global,
                trip.score_acceleration,
                trip.score_braking,
                trip.score_constant_speed,
                trip.night_trip,
                ",".join(trip.countries) if trip.countries else "",
                length_overspeed_mi if is_imperial else trip.length_overspeed_km,
                trip.duration_overspeed_seconds,
                length_highway_mi if is_imperial else trip.length_highway_km,
                trip.duration_highway_seconds,
                hdc_eco_distance_mi if is_imperial else trip.hdc_eco_distance_km,
                trip.hdc_eco_duration_seconds,
                hdc_power_distance_mi
                if is_imperial
                else trip.hdc_power_distance_km,
                trip.hdc_power_duration_seconds,
                hdc_charge_distance_mi
                if is_imperial
                else trip.hdc_charge_distance_km,
                trip.hdc_charge_duration_seconds,
            ]
            writer.writerow(row)

        output.seek(0)
        # --- 5. Return the CSV file as a streaming response ---
        return StreamingResponse(
            output,
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=trips_export_{vin}_{datetime.date.today()}.csv"
            },
        )
    finally:
        db.close()
