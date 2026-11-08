"""Compatibility CLI exports for the separately maintained AI package."""

from chungbuk_trip_ai.trip_preview import (
    TripInputError,
    create_trip_preview,
    main,
    validate_trip_input,
)

__all__ = ["TripInputError", "create_trip_preview", "main", "validate_trip_input"]


if __name__ == "__main__":
    raise SystemExit(main())
