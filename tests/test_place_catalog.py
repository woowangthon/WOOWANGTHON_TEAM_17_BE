import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from app.place_catalog import PlaceCatalogError, load_place_catalog


@pytest.fixture
def engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()
    Table("places", metadata, Column("contentid", Integer), Column("title", String),
          Column("mapx", String), Column("mapy", String), Column("geoValidated", String), Column("ignored", String))
    Table("place_details", metadata, Column("contentid", Integer), Column("openingHoursText", String),
          Column("feeText", String), Column("feeCategory", String))
    Table("place_preferences", metadata, Column("contentid", Integer), Column("preferenceTags", String),
          Column("natureScore", Integer))
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_catalog_detaches_rows_preserves_tag_text_and_builds_only_verified_marker(engine):
    with engine.begin() as conn:
        conn.execute(Table("places", MetaData(), autoload_with=conn).insert(), [
            {"contentid": 17, "title": "합성 장소", "mapx": "127.4341", "mapy": "36.6286", "geoValidated": " yes ", "ignored": "omit"},
            {"contentid": 18, "title": "미확인", "mapx": "127.5", "mapy": "36.5", "geoValidated": "no", "ignored": None},
            {"contentid": 19, "title": "요금 확인", "mapx": "127.5", "mapy": "36.5", "geoValidated": "no", "ignored": None},
        ])
        conn.execute(Table("place_details", MetaData(), autoload_with=conn).insert(), [
            {"contentid": 17, "openingHoursText": "  ", "feeText": None, "feeCategory": "UNKNOWN"},
            {"contentid": 17, "openingHoursText": "09:00-18:00", "feeText": "무료입장", "feeCategory": "TEXT"},
            {"contentid": 18, "openingHoursText": None, "feeText": None, "feeCategory": "FREE"},
            {"contentid": 19, "openingHoursText": None, "feeText": "0원", "feeCategory": "UNKNOWN"},
        ])
        conn.execute(Table("place_preferences", MetaData(), autoload_with=conn).insert(), [
            {"contentid": 17, "preferenceTags": "nature,slow", "natureScore": 0},
            {"contentid": 999, "preferenceTags": "orphan", "natureScore": 3},
        ])
        catalog = load_place_catalog(conn, supported_tags=["nature", "culture"])

    assert catalog.preferences[0]["preferenceTags"] == "nature,slow"
    assert catalog.preferences[0]["natureScore"] == 0
    assert "ignored" not in catalog.places[0]
    marker = catalog.presentation_place(17)
    assert marker["coordinate"] == {"latitude": 36.6286, "longitude": 127.4341, "crs": "WGS84"}
    assert "openingHoursText" not in marker
    assert marker["feeLabel"] == "요금 확인 필요"  # duplicate detail rows remain raw; no guessed selection
    assert len(catalog.details) == 4
    assert catalog.presentation_place(18).get("coordinate") is None
    assert catalog.presentation_place(18)["feeLabel"] == "요금 확인 필요"
    assert catalog.presentation_place(19)["feeLabel"] == "0원"
    assert catalog.diagnostics == ("place_details:duplicate_contentids=1", "place_preferences:orphan_rows=1")


def test_catalog_fails_closed_on_duplicate_place_key(engine):
    with engine.begin() as conn:
        conn.execute(Table("places", MetaData(), autoload_with=conn).insert(), [
            {"contentid": 17, "title": "A"}, {"contentid": 17, "title": "B"},
        ])
        with pytest.raises(PlaceCatalogError, match="duplicate places contentid"):
            load_place_catalog(conn)


def test_supported_vocabulary_bounds_and_controls(engine):
    with engine.connect() as conn:
        valid = [f"{index:03d}" + "x" * 77 for index in range(200)]
        assert len(load_place_catalog(conn, supported_tags=valid).supported_tags) == 200
        with pytest.raises(ValueError):
            load_place_catalog(conn, supported_tags=valid + ["extra"])
        with pytest.raises(ValueError):
            load_place_catalog(conn, supported_tags=["x" * 81])
        with pytest.raises(ValueError):
            load_place_catalog(conn, supported_tags=["bad\nvalue"])
