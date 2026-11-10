"""Read-only adapter for the three imported CSV place catalog tables."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import Integer, MetaData, Table, select


PLACE_COLUMNS = ("contentid", "title", "contenttypeid", "contentTypeName", "planningRole", "recommendationUse",
                 "adminCity", "addr1", "addr2", "mapx", "mapy", "geoValidated", "tel", "homepageUrl",
                 "overviewText", "firstimage")
DETAIL_COLUMNS = ("contentid", "openingHoursText", "closedDaysText", "feeText", "feeCategory", "bookingText",
                  "parkingText", "parkingCategory", "durationText", "estimatedStayMinutes", "stayTimeMethod",
                  "experienceText", "menuText", "checkInTime", "checkOutTime", "petPolicyText",
                  "petPolicyCategory", "eventStartDate", "eventEndDate", "courseText", "planningStatusV2")
PREFERENCE_COLUMNS = ("contentid", "preferenceTags", "preferenceTagConfidence", "vibeTags", "activityLevel",
                      "indoorOutdoor", "natureScore", "relaxationScore", "cultureScore", "activityScore",
                      "foodScore", "shoppingScore")
TABLES = (("places", PLACE_COLUMNS), ("place_details", DETAIL_COLUMNS), ("place_preferences", PREFERENCE_COLUMNS))
INT32_MAX = 2_147_483_647


class PlaceCatalogError(Exception):
    """Safe catalog structure/read failure without SQL or row content."""


class _FrozenMap(tuple):
    """Tuple-backed immutable mapping marker, distinct from stored sequences."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenMap((str(k), _freeze(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(v) for v in value), key=repr))
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, _FrozenMap):
        return {key: _thaw(val) for key, val in value}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return {"$type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$type": "Decimal", "value": str(value)}
    if isinstance(value, bytes):
        return {"$type": "bytes", "value": value.hex()}
    raise TypeError("unsupported catalog value")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _valid_mysql_int(column: Any) -> bool:
    # MySQL SMALLINT/BIGINT are also Integer subclasses in SQLAlchemy; only the
    # signed four-byte INTEGER column is compatible with places.contentid.
    return (isinstance(column.type, Integer) and type(column.type).__name__.upper() == "INTEGER"
            and not bool(getattr(column.type, "unsigned", False)))


def _contentid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and -(2**31) <= value <= INT32_MAX


def _truthy_verified(value: Any) -> bool:
    return (value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1)
            or (isinstance(value, str) and value.strip().lower() == "yes"))


def _coord(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if not _truthy_verified(row.get("geoValidated")):
        return None
    try:
        lon, lat = Decimal(str(row.get("mapx"))), Decimal(str(row.get("mapy")))
        if not lon.is_finite() or not lat.is_finite() or not Decimal("-180") <= lon <= Decimal("180") or not Decimal("-90") <= lat <= Decimal("90"):
            return None
        return {"latitude": float(lat), "longitude": float(lon), "crs": "WGS84"}
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True, repr=False)
class PlaceCatalog:
    _places: tuple = field(repr=False)
    _details: tuple = field(repr=False)
    _preferences: tuple = field(repr=False)
    supported_tags: tuple[str, ...] = field(repr=False)
    digest: str
    diagnostics: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"PlaceCatalog(places={len(self._places)}, details={len(self._details)}, preferences={len(self._preferences)}, digest={self.digest!r})"

    @property
    def places(self) -> tuple[dict[str, Any], ...]:
        return tuple(_thaw(row) for row in self._places)

    @property
    def details(self) -> tuple[dict[str, Any], ...]:
        return tuple(_thaw(row) for row in self._details)

    @property
    def preferences(self) -> tuple[dict[str, Any], ...]:
        return tuple(_thaw(row) for row in self._preferences)

    def presentation_place(self, contentid: int) -> dict[str, Any] | None:
        row = next((r for r in self.places if r["contentid"] == contentid), None)
        if row is None:
            return None
        result = {k: v for k, v in row.items() if k not in {"mapx", "mapy", "geoValidated"}}
        coordinate = _coord(row)
        if coordinate is not None:
            result["coordinate"] = coordinate
        result["feeLabel"] = "요금 확인 필요"
        detail_rows = [d for d in self.details if d["contentid"] == contentid]
        if len(detail_rows) == 1:
            chosen = detail_rows[0]
            opening = chosen.get("openingHoursText")
            if isinstance(opening, str) and opening.strip():
                result["openingHoursText"] = opening
            fee = chosen.get("feeText")
            if isinstance(fee, str) and fee.strip():
                result["feeLabel"] = fee
        return result


def load_place_catalog(conn: Any, supported_tags: tuple[str, ...] | list[str] = ()) -> PlaceCatalog:
    """Reflect only fixed table names/allowlisted fields and issue SELECTs only."""
    if not isinstance(supported_tags, (tuple, list)) or any(not isinstance(tag, str) for tag in supported_tags):
        raise ValueError("supported_tags must be an explicit sequence of strings")
    tags = tuple(supported_tags)
    if len(tags) > 200:
        raise ValueError("supported_tags exceeds the vocabulary limit")
    if len(set(tags)) != len(tags):
        raise ValueError("supported_tags must not contain duplicates")
    if any(not tag or len(tag) > 80 or tag != tag.strip() or "\ufffd" in tag
           or any(0xD800 <= ord(ch) <= 0xDFFF or ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in tag)
           for tag in tags):
        raise ValueError("supported_tags contains an invalid vocabulary entry")
    metadata = MetaData()
    loaded: dict[str, tuple[dict[str, Any], ...]] = {}
    try:
        tables = {name: Table(name, metadata, autoload_with=conn, resolve_fks=False) for name, _ in TABLES}
        for name, allowed in TABLES:
            table = tables[name]
            if "contentid" not in table.c or not _valid_mysql_int(table.c.contentid):
                raise PlaceCatalogError("catalog contentid must be signed INT")
            selected = [table.c[column] for column in allowed if column in table.c]
            if table.c.contentid not in selected:
                selected.insert(0, table.c.contentid)
            rows = conn.execute(select(*selected)).mappings().all()
            detached = [{key: row[key] for key in row.keys()} for row in rows]
            if any(not _contentid(row.get("contentid")) for row in detached):
                raise PlaceCatalogError("catalog contentid value is incompatible")
            detached.sort(key=_canonical)
            loaded[name] = tuple(detached)
    except PlaceCatalogError:
        raise
    except Exception:
        raise PlaceCatalogError("catalog read failed") from None

    place_ids = [row["contentid"] for row in loaded["places"]]
    if len(place_ids) != len(set(place_ids)):
        raise PlaceCatalogError("duplicate places contentid")
    known = set(place_ids)
    diagnostics = []
    for name in ("place_details", "place_preferences"):
        orphan_count = sum(row["contentid"] not in known for row in loaded[name])
        if orphan_count:
            diagnostics.append(f"{name}:orphan_rows={orphan_count}")
        keys = [row["contentid"] for row in loaded[name] if row["contentid"] in known]
        duplicate_keys = len(keys) - len(set(keys))
        if duplicate_keys:
            diagnostics.append(f"{name}:duplicate_contentids={duplicate_keys}")
    invalid_coords = sum(_truthy_verified(row.get("geoValidated")) and _coord(row) is None for row in loaded["places"])
    if invalid_coords:
        diagnostics.append(f"places:unusable_verified_coordinates={invalid_coords}")

    frozen = {name: tuple(_freeze(row) for row in records) for name, records in loaded.items()}
    data = {"tables": {name: loaded[name] for name, _ in TABLES}, "supportedTags": tags,
            "diagnostics": diagnostics}
    digest = hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()
    return PlaceCatalog(frozen["places"], frozen["place_details"], frozen["place_preferences"], tags, digest, tuple(diagnostics))
