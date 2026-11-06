"""Validate a trip draft and produce an unconfirmed preference extraction preview."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .gemini import GeminiError, extract_trip_preferences


class TripInputError(ValueError):
    """Raised when fixed trip context is incomplete or invalid."""


def validate_trip_input(data: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(data, dict):
        raise TripInputError("Input must be a JSON object")
    dates = data.get("dates")
    departure = data.get("departure")
    if not isinstance(dates, dict) or not isinstance(departure, dict):
        raise TripInputError("dates and departure objects are required")
    try:
        start_date = date.fromisoformat(dates.get("start", ""))
        end_date = date.fromisoformat(dates.get("end", ""))
    except (TypeError, ValueError) as exc:
        raise TripInputError("dates.start and dates.end must be ISO dates") from exc
    if end_date < start_date:
        raise TripInputError("dates.end must not be before dates.start")

    transport = data.get("transport")
    total_budget_won = data.get("total_budget_won")
    headcount = data.get("headcount")
    departure_time = departure.get("time")
    departure_region = departure.get("region")
    if not isinstance(transport, str) or not transport.strip():
        raise TripInputError("transport is required")
    if isinstance(total_budget_won, bool) or not isinstance(total_budget_won, int) or total_budget_won < 0:
        raise TripInputError("total_budget_won must be a non-negative integer")
    if isinstance(headcount, bool) or not isinstance(headcount, int) or headcount < 1:
        raise TripInputError("headcount must be a positive integer from room context")
    if not isinstance(departure_time, str) or not re.fullmatch(r"\d{2}:\d{2}", departure_time):
        raise TripInputError("departure.time must use HH:MM")
    try:
        datetime.strptime(departure_time, "%H:%M")
    except ValueError as exc:
        raise TripInputError("departure.time must be a valid 24-hour time") from exc
    if not isinstance(departure_region, str) or not departure_region.strip():
        raise TripInputError("departure.region is required")
    free_text = data.get("preferences_text")
    if not isinstance(free_text, str) or not free_text.strip():
        raise TripInputError("preferences_text is required")

    fixed_context = {
        "dates": {"start": dates["start"], "end": dates["end"]},
        "transport": transport,
        "total_budget_won": total_budget_won,
        "departure": {"time": departure_time, "region": departure_region},
        "headcount": headcount,
    }
    return fixed_context, free_text.strip()


def create_trip_preview(data: Any) -> dict[str, Any]:
    fixed_context, free_text = validate_trip_input(data)
    return extract_trip_preferences(fixed_context, free_text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a pending trip preference preview")
    parser.add_argument("input", nargs="?", help="JSON input file; defaults to stdin")
    args = parser.parse_args(argv)
    try:
        if args.input:
            data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        else:
            data = json.load(sys.stdin)
        result = create_trip_preview(data)
    except (OSError, json.JSONDecodeError, TripInputError, GeminiError) as exc:
        print(f"Trip preview failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
