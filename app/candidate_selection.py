"""Deterministic, review-gated candidate selection for itinerary drafting.

This is an internal proposal builder. It does not establish travel feasibility or
persist anything; missing evidence remains unknown and cannot satisfy a filter.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping


class CandidateSelectionError(Exception):
    """Safe internal input failure; exception text never includes caller data."""

    def __init__(self, code: str = "INVALID_CANDIDATE_SELECTION_INPUT"):
        self.code = code
        super().__init__(code)


def _canonical(value: Any) -> str:
    try:
        def validate(node: Any) -> None:
            if node is None or isinstance(node, (bool, int)):
                return
            if isinstance(node, float):
                if not math.isfinite(node):
                    raise ValueError
                return
            if isinstance(node, str):
                node.encode("utf-8", "strict")
                return
            if isinstance(node, list):
                for child in node: validate(child)
                return
            if isinstance(node, dict) and all(isinstance(key, str) for key in node):
                for child in node.values(): validate(child)
                return
            raise ValueError
        validate(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except Exception:
        raise CandidateSelectionError() from None


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8", "strict")).hexdigest()


def normalization_digest(result: Any) -> str:
    """Hash a strict JSON normalization document using canonical UTF-8 JSON."""
    return _digest(result)


def _safe_string(value: Any, max_len: int, *, blank: bool = False, multiline: bool = False) -> bool:
    return (isinstance(value, str) and len(value) <= max_len and (blank or bool(value.strip()))
            and "\ufffd" not in value and all(not (0xD800 <= ord(c) <= 0xDFFF or (ord(c) < 0x20 and not (multiline and c == "\n")) or 0x7f <= ord(c) <= 0x9f) for c in value))


def build_supported_tags(catalog: Any) -> tuple[str, ...]:
    """Return observed exact vocabulary tokens from the imported pipe columns."""
    try:
        tags: set[str] = set()
        for row in (*catalog.preferences,):
            if not isinstance(row, Mapping):
                raise ValueError
            for col in ("preferenceTags", "vibeTags"):
                raw = row.get(col)
                if raw is None or raw == "":
                    continue
                if not isinstance(raw, str):
                    raise ValueError
                for token in raw.split("|"):
                    token = token.strip()
                    if not token:
                        continue
                    if not _safe_string(token, 80):
                        raise ValueError
                    tags.add(token)
                    if len(tags) > 200:
                        raise ValueError
        return tuple(sorted(tags))
    except Exception:
        raise CandidateSelectionError("INVALID_CATALOG_VOCABULARY") from None


@dataclass(frozen=True, repr=False)
class CandidateSelection:
    _document: str = field(repr=False)
    source_fingerprint: str
    catalog_digest: str
    normalization_digest: str
    policy_digest: str

    def __repr__(self) -> str:
        return "CandidateSelection(<private>)"

    @property
    def sourceFingerprint(self) -> str:  # compatibility with internal camelCase contract
        return self.source_fingerprint

    @property
    def catalogDigest(self) -> str:
        return self.catalog_digest

    @property
    def normalizationDigest(self) -> str:
        return self.normalization_digest

    @property
    def policyDigest(self) -> str:
        return self.policy_digest

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self._document)

    def draft_candidates(self) -> list[dict[str, Any]]:
        allowed = {"contentid", "title", "planningRole", "adminCity", "allowedDates", "groupScore", "coordinate", "estimatedStayMinutes"}
        return [{k: v for k, v in row.items() if k in allowed}
                for row in json.loads(self._document)["eligible"]]

    def fixed_visits(self) -> list[dict[str, Any]]:
        return json.loads(self._document)["fixedVisits"]

    @property
    def ready_for_draft(self) -> bool:
        return json.loads(self._document)["readyForDraft"]


KNOWN_CITIES = ("청주시", "충주시", "제천시", "괴산군", "단양군", "옥천군", "진천군", "보은군", "음성군", "영동군", "증평군")
ROLE_USE = {
    ("sightseeing_stop", "MAIN_DESTINATION"), ("activity_stop", "MAIN_DESTINATION"),
    ("culture_stop", "MAIN_DESTINATION"), ("meal_stop", "MEAL"),
    ("shopping_stop", "GENERAL_SHOPPING"), ("shopping_stop", "SPECIAL_SHOPPING"),
    ("overnight_lodging", "STAY"), ("overnight_camping", "STAY"),
    ("date_specific_event", "EVENT"),
}
SCOPE_USE = {"all": None, "meal": "MEAL", "stay": "STAY"}
NORMAL_KEYS = {"terms", "unmapped", "walking", "conflictCandidates", "needsReview"}
TERM_KEYS = {"memberRef", "field", "tag", "evidence", "polarity", "importance", "requiresReview"}


def _iso_day(v: Any) -> date | None:
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return None
    try:
        parsed = date.fromisoformat(v)
        return parsed if parsed.isoformat() == v else None
    except ValueError:
        return None


def _tokens_region(text: Any) -> tuple[set[str], bool]:
    if not isinstance(text, str) or not text.strip():
        return set(), False
    normalized = re.sub(r"[,/]+", " ", text.strip())
    if normalized in {"충북", "충청북도", "충북 전체", "충청북도 전체"}:
        return set(KNOWN_CITIES), True
    # Province prefixes are allowed only at the beginning. `전체` is only
    # meaningful in the exact whole-province forms above.
    normalized = re.sub(r"^(?:충청북도|충북)\s+", "", normalized, count=1)
    parts = normalized.split()
    if not parts or any(p in {"충북", "충청북도", "전체"} for p in parts):
        return set(), False
    cities: set[str] = set()
    for p in parts:
        if p.endswith(("시", "군")):
            candidate = p
        else:
            candidate = p + ("군" if p in {"괴산", "단양", "옥천", "진천", "보은", "음성", "영동", "증평"} else "시")
        if candidate not in KNOWN_CITIES:
            return set(), False
        cities.add(candidate)
    return cities, bool(cities)


def _normalization(value: Any, prefs: list[dict[str, Any]], supported: set[str]) -> tuple[list[dict], list[dict]]:
    if not isinstance(value, dict) or set(value) != NORMAL_KEYS or not isinstance(value["terms"], list) or not isinstance(value["unmapped"], list) or not isinstance(value["walking"], list) or not isinstance(value["conflictCandidates"], list) or not isinstance(value["needsReview"], bool):
        raise CandidateSelectionError("INVALID_NORMALIZATION_RESULT")
    byref = {p["memberRef"]: p for p in prefs}
    terms = []
    identities = set()
    for t in value["terms"]:
        if not isinstance(t, dict) or set(t) != TERM_KEYS:
            raise CandidateSelectionError("INVALID_NORMALIZATION_RESULT")
        ref, fld, tag, evidence = (t[k] for k in ("memberRef", "field", "tag", "evidence"))
        if (ref not in byref or fld not in {"likes", "avoid", "food"} or tag not in supported
                or not _safe_string(tag, 80) or not _safe_string(evidence, 500, multiline=True) or not evidence.strip()
                or not isinstance(byref[ref].get(fld), str) or evidence not in byref[ref][fld]
                or t["polarity"] not in {"prefer", "avoid"} or t["importance"] != ("required" if fld == "avoid" and byref[ref].get("avoidImportance") == "required" or fld == "food" and byref[ref].get("foodImportance") == "required" else "preferred")
                or (fld == "likes" and t["polarity"] != "prefer") or (fld == "avoid" and t["polarity"] != "avoid")
                or (fld == "food" and t["polarity"] == "avoid" and not re.search(r"싫|피하|빼(?:주|고|는|야|줘)|안\s*먹|먹지\s*않|못\s*먹|금지|제외|\b(?:avoid|dislike|hate|no|not|don't|do not|cannot|can't)\b", evidence, re.I))
                or t["requiresReview"] is not True):
            raise CandidateSelectionError("INVALID_NORMALIZATION_RESULT")
        identity = (ref, fld, tag, evidence, t["polarity"])
        if identity in identities:
            raise CandidateSelectionError("INVALID_NORMALIZATION_RESULT")
        identities.add(identity)
        terms.append(dict(t))
    # `unmapped` preserves every non-empty confirmed field, and its reason must
    # reflect whether a mapping was proposed. Never accept a provider omission.
    expected_unmapped = [{"memberRef": p["memberRef"], "field": fld, "text": p[fld],
                          "importance": "preferred" if fld == "likes" else p[fld + "Importance"],
                          "reason": "coverage_unverified" if any(t["memberRef"] == p["memberRef"] and t["field"] == fld for t in terms) else "uncovered"}
                         for p in prefs for fld in ("likes", "avoid", "food") if p.get(fld)]
    if value["unmapped"] != expected_unmapped:
        raise CandidateSelectionError("INVALID_NORMALIZATION_RESULT")
    # Conflict hints are derivable from terms and cannot be forged to alter review.
    by_tag: dict[str, list[int]] = {}
    for index, term in enumerate(terms):
        by_tag.setdefault(term["tag"], []).append(index)
    expected_conflicts = []
    for tag, indexes in by_tag.items():
        for pos, left_index in enumerate(indexes):
            for right_index in indexes[pos + 1:]:
                left, right = terms[left_index], terms[right_index]
                if left["polarity"] == right["polarity"]:
                    continue
                kind = ("required_conflict" if left["importance"] == right["importance"] == "required"
                        else "preference_conflict" if left["importance"] == right["importance"] == "preferred" else "mixed")
                expected_conflicts.append({"tag": tag, "termIndexes": [left_index, right_index], "kind": kind, "requiresReview": True})
    if value["conflictCandidates"] != expected_conflicts:
        raise CandidateSelectionError("INVALID_NORMALIZATION_RESULT")
    # Walking output is not proof of travel feasibility; it must faithfully echo
    # the confirmed member limit and have the adapter's known shape.
    expected = [{"memberRef": p["memberRef"], "scope": "between_places", "maxMinutes": None if p["walking"] == "unlimited" else int(p["walking"]), "unlimited": p["walking"] == "unlimited"} for p in prefs]
    if (len(value["walking"]) != len(expected)
            or any(not isinstance(item, dict) or set(item) != {"memberRef", "scope", "maxMinutes", "unlimited"}
                   or not isinstance(item.get("memberRef"), str) or item.get("scope") != "between_places"
                   or type(item.get("unlimited")) is not bool
                   or (item.get("maxMinutes") is not None and (isinstance(item.get("maxMinutes"), bool) or not isinstance(item.get("maxMinutes"), int)))
                   for item in value["walking"])
            or value["walking"] != expected):
        raise CandidateSelectionError("INVALID_NORMALIZATION_RESULT")
    expected_needs_review = bool(terms or expected_unmapped or expected_conflicts) or any(p["walking"] is None for p in prefs)
    if value["needsReview"] is not expected_needs_review:
        raise CandidateSelectionError("INVALID_NORMALIZATION_RESULT")
    return terms, expected_unmapped


def _date_range(start: Any, end: Any) -> list[date]:
    a, b = _iso_day(start), _iso_day(end)
    if not a or not b or b < a or (b-a).days > 30:
        raise CandidateSelectionError("INVALID_TRIP_DATES")
    return [a + timedelta(days=i) for i in range((b-a).days + 1)]


def _review(review: Any, snapshot: Any, norm_digest: str, catalog_digest: str) -> dict[str, Any] | None:
    if review is None:
        return None
    exact = {"sourceFingerprint", "normalizationDigest", "catalogDigest", "acceptedTermIndexes", "reviewedFields", "verifiedTagAbsences", "reviewedCommonFields"}
    if not isinstance(review, dict) or set(review) != exact or review["sourceFingerprint"] != snapshot.fingerprint or review["normalizationDigest"] != norm_digest or review["catalogDigest"] != catalog_digest:
        raise CandidateSelectionError("STALE_REVIEW")
    return json.loads(_canonical(review))


def _role(r: Mapping[str, Any]) -> bool:
    return (r.get("planningRole"), r.get("recommendationUse")) in ROLE_USE


def build_candidate_selection(snapshot: Any, normalization_result: Any, catalog: Any,
                              review: dict[str, Any] | None = None, policy: dict[str, Any] | None = None,
                              *, confirmed_evidence: dict[str, Any] | None = None) -> CandidateSelection:
    """Build a deterministic selection; only explicit reviewed evidence can pass required filters."""
    try:
        doc = snapshot.as_dict()
        conditions, prefs = doc["conditions"], doc["confirmedPreferences"]
        if not isinstance(conditions, dict) or not isinstance(prefs, list) or not prefs:
            raise CandidateSelectionError("INVALID_SOURCE_SNAPSHOT")
        days = _date_range(conditions.get("startDate"), conditions.get("endDate"))
        day_strings = [d.isoformat() for d in days]
        supported = set(build_supported_tags(catalog))
        if confirmed_evidence is None:
            ndigest = normalization_digest(normalization_result)
            reviewed = _review(review, snapshot, ndigest, catalog.digest)
            terms, _ = _normalization(normalization_result, prefs, supported)
        else:
            ndigest = confirmed_evidence["digest"]
            reviewed = None
            terms = []
        policy = policy or {"version": "candidate-v1", "maxCandidates": 200, "reviewedRegionCities": []}
        if not isinstance(policy, dict) or set(policy) != {"version", "maxCandidates", "reviewedRegionCities"} or policy.get("version") != "candidate-v1" or isinstance(policy.get("maxCandidates"), bool) or not isinstance(policy.get("maxCandidates"), int) or not 1 <= policy["maxCandidates"] <= 200 or not isinstance(policy.get("reviewedRegionCities"), list) or any(c not in KNOWN_CITIES for c in policy["reviewedRegionCities"]) or len(set(policy["reviewedRegionCities"])) != len(policy["reviewedRegionCities"]):
            raise CandidateSelectionError("INVALID_POLICY")
        pdigest = _digest(policy)
        cityset, region_ok = _tokens_region(conditions.get("region"))
        if region_ok:
            cityset |= set(policy["reviewedRegionCities"])
        unresolved = [] if region_ok else ["REGION_REVIEW_REQUIRED"]
        required_fields = []
        for p in prefs:
            for fld, importance in (("avoid", p.get("avoidImportance")), ("food", p.get("foodImportance"))):
                if p.get(fld) and importance == "required":
                    required_fields.append((p["memberRef"], fld, p[fld]))
        common = conditions.get("lodging")
        fixed_schedule = conditions.get("fixedSchedule")
        common_fields = []
        for fld, text in (("lodging", common), ("fixedSchedule", fixed_schedule)):
            if isinstance(text, str) and text.strip():
                common_fields.append((fld, text))
        accepted = [] if reviewed is None else reviewed["acceptedTermIndexes"]
        if any(isinstance(i, bool) or not isinstance(i, int) or i < 0 or i >= len(terms) for i in accepted) or len(set(accepted)) != len(accepted):
            raise CandidateSelectionError("INVALID_REVIEW")
        reviewed_fields = [] if reviewed is None else reviewed["reviewedFields"]
        reviewed_map: dict[tuple[str,str], set[int]] = {}
        reviewed_keys = set()
        for rf in reviewed_fields:
            if not isinstance(rf, dict) or set(rf) != {"memberRef", "field", "text", "termIndexes", "scope"} or rf["field"] not in {"likes", "avoid", "food"} or rf["scope"] not in SCOPE_USE or not _safe_string(rf["text"], 500, blank=True, multiline=True) or not isinstance(rf["termIndexes"], list):
                raise CandidateSelectionError("INVALID_REVIEW")
            p = next((x for x in prefs if x["memberRef"] == rf["memberRef"]), None)
            if not p or rf["text"] != (p.get(rf["field"]) or "") or not rf["text"].strip():
                raise CandidateSelectionError("INVALID_REVIEW")
            review_key = (rf["memberRef"], rf["field"])
            if review_key in reviewed_keys:
                raise CandidateSelectionError("INVALID_REVIEW")
            reviewed_keys.add(review_key)
            if len(set(rf["termIndexes"])) != len(rf["termIndexes"]):
                raise CandidateSelectionError("INVALID_REVIEW")
            inds = set(rf["termIndexes"])
            if any(isinstance(i, bool) or not isinstance(i, int) or i not in accepted or i >= len(terms) or terms[i]["memberRef"] != rf["memberRef"] or terms[i]["field"] != rf["field"] for i in inds):
                raise CandidateSelectionError("INVALID_REVIEW")
            reviewed_map[(rf["memberRef"], rf["field"])] = inds
        for p in prefs if confirmed_evidence is None else []:
            for fld in ("likes", "avoid", "food"):
                text = p.get(fld)
                if text and text.strip() and not reviewed_map.get((p["memberRef"], fld)):
                    if (p["memberRef"], fld, text) in required_fields:
                        unresolved.append("REQUIRED_TERM_REVIEW_REQUIRED")
        predicates = [] if confirmed_evidence is None else [dict(x) for x in confirmed_evidence.get("predicates", [])]
        for i in accepted:
            t = terms[i]
            rf = next((x for x in reviewed_fields if x["memberRef"] == t["memberRef"] and x["field"] == t["field"] and i in x["termIndexes"]), None)
            if not rf:
                raise CandidateSelectionError("INVALID_REVIEW")
            p = next(x for x in prefs if x["memberRef"] == t["memberRef"])
            predicates.append({"memberRef": t["memberRef"], "tag": t["tag"], "polarity": t["polarity"], "scope": rf["scope"], "required": p.get(t["field"] + "Importance") == "required" if t["field"] in {"avoid", "food"} else False})
        # Repeated evidence for the same member/tag/polarity/scope is one
        # predicate, so model duplication cannot amplify one member's weight.
        predicates = list({(p["memberRef"], p["tag"], p["polarity"], p["scope"], p["required"]): p for p in predicates}.values())
        # Every required source field needs a reviewed accepted predicate.
        for ref, fld, _ in required_fields if confirmed_evidence is None else []:
            if not reviewed_map.get((ref, fld)):
                if "REQUIRED_TERM_REVIEW_REQUIRED" not in unresolved: unresolved.append("REQUIRED_TERM_REVIEW_REQUIRED")
        # Validate explicit missing-tag facts and common anchors.
        absences = set()
        for fact in ([] if reviewed is None else reviewed["verifiedTagAbsences"]):
            if not isinstance(fact, dict) or set(fact) != {"contentid", "tag"} or isinstance(fact["contentid"], bool) or not isinstance(fact["contentid"], int) or not -(2**31) <= fact["contentid"] <= 2**31-1 or fact["tag"] not in supported:
                raise CandidateSelectionError("INVALID_REVIEW")
            absence = (fact["contentid"], fact["tag"])
            if absence in absences:
                raise CandidateSelectionError("INVALID_REVIEW")
            absences.add(absence)
        rcommon = (confirmed_evidence.get("reviewedCommonFields", []) if confirmed_evidence is not None
                   else [] if reviewed is None else reviewed["reviewedCommonFields"])
        common_keys = set()
        fixed_visits = []
        for rf in rcommon:
            if not isinstance(rf, dict) or set(rf) != {"field", "text", "fixedVisits"} or rf["field"] not in {"lodging", "fixedSchedule"} or not isinstance(rf["fixedVisits"], list):
                raise CandidateSelectionError("INVALID_REVIEW")
            if rf["field"] in common_keys:
                raise CandidateSelectionError("INVALID_REVIEW")
            common_keys.add(rf["field"])
            original = conditions.get(rf["field"])
            if not isinstance(original, str) or rf["text"] != original:
                raise CandidateSelectionError("INVALID_REVIEW")
            fixed_visits.extend(rf["fixedVisits"])
        for fld, _ in common_fields:
            match = next((x for x in rcommon if x["field"] == fld), None)
            if not match or not match["fixedVisits"]:
                unresolved.append("COMMON_ANCHOR_REVIEW_REQUIRED")
        if confirmed_evidence is not None:
            fixed_visits.extend(confirmed_evidence.get("fixedVisits", []))
        if any(not isinstance(v, dict) or set(v) != {"date", "contentid", "position"} or v["date"] not in day_strings or isinstance(v["contentid"], bool) or not isinstance(v["contentid"], int) or not -(2**31) <= v["contentid"] <= 2**31-1 or isinstance(v["position"], bool) or not isinstance(v["position"], int) or not 0 <= v["position"] < 8 for v in fixed_visits):
            raise CandidateSelectionError("INVALID_REVIEW")
        if len({(v["date"],v["position"]) for v in fixed_visits}) != len(fixed_visits):
            raise CandidateSelectionError("INVALID_REVIEW")
        ids_by_day: dict[int, set[str]] = {}
        for visit in fixed_visits:
            ids_by_day.setdefault(visit["contentid"], set()).add(visit["date"])
        if any(len(ds) != sum(1 for v in fixed_visits if v["contentid"] == cid) for cid, ds in ids_by_day.items()):
            raise CandidateSelectionError("INVALID_REVIEW")
        # Canonicalize catalog rows and use duplicates as unknown evidence.
        places = list(catalog.places); details = list(catalog.details); preferences = list(catalog.preferences)
        def grouped(rows):
            out = {}
            for row in rows:
                if isinstance(row, Mapping): out.setdefault(row.get("contentid"), []).append(row)
            return out
        detail_by, pref_by = grouped(details), grouped(preferences)
        pool, excluded, review_candidates = [], [], []
        pending = ["ROUTE_UNVERIFIED", "WALKING_UNVERIFIED", "BUDGET_UNVERIFIED", "OPENING_HOURS_UNVERIFIED"]
        # MySQL DOUBLE columns may be reflected as Decimal. Catalog rows are
        # already bound by the adapter digest; sort by their signed INT identity
        # without applying the strict JSON input/review validator to DB scalars.
        def place_order(row):
            cid = row.get("contentid") if isinstance(row, Mapping) else None
            return (0, cid) if type(cid) is int and -(2**31) <= cid <= 2**31-1 else (1, 0)
        for place in sorted(places, key=place_order):
            if not isinstance(place, Mapping): continue
            cid = place.get("contentid")
            if isinstance(cid, bool) or not isinstance(cid, int) or not -(2**31) <= cid <= 2**31-1: continue
            reason = []
            if len(detail_by.get(cid, [])) > 1 or len(pref_by.get(cid, [])) > 1:
                review_candidates.append({"contentid": cid, "reason": "DUPLICATE_CATALOG_FACTS"}); continue
            detail = detail_by.get(cid, [None])[0]; pref = pref_by.get(cid, [None])[0]
            title, city = place.get("title"), place.get("adminCity")
            if not _safe_string(title, 200) or not _safe_string(city, 100):
                review_candidates.append({"contentid": cid, "reason": "PLACE_METADATA_REVIEW_REQUIRED"}); continue
            if not _role(place):
                review_candidates.append({"contentid": cid, "reason": "ROLE_REVIEW_REQUIRED"}); continue
            status = detail.get("planningStatusV2") if detail else None
            if status is None:
                pending.append("PLANNING_STATUS_UNKNOWN")
            if status == "manual_review_required":
                review_candidates.append({"contentid": cid, "reason": "MANUAL_REVIEW_REQUIRED"}); continue
            if status == "candidate_needs_live_validation": pending.append("LIVE_PLACE_VALIDATION_REQUIRED")
            if status not in {"candidate_needs_live_validation", "trip_date_check_required", None}:
                review_candidates.append({"contentid": cid, "reason": "PLANNING_STATUS_UNKNOWN"}); continue
            if status == "trip_date_check_required" and place.get("planningRole") != "date_specific_event":
                review_candidates.append({"contentid": cid, "reason": "PLANNING_STATUS_ROLE_MISMATCH"}); continue
            if city not in cityset:
                excluded.append({"contentid": cid, "reason": "OUTSIDE_REVIEWED_REGION"}); continue
            tags = set()
            if pref:
                for col in ("preferenceTags", "vibeTags"):
                    raw = pref.get(col)
                    if isinstance(raw, str): tags.update(x.strip() for x in raw.split("|") if x.strip() in supported)
            if any(absent_cid == cid and tag in tags for absent_cid, tag in absences):
                raise CandidateSelectionError("INVALID_REVIEW")
            # Numeric preference scores are retained by the catalog, but malformed
            # values are unknown data and never become arbitrary ranking weights.
            if pref:
                for key in ("natureScore", "relaxationScore", "cultureScore", "activityScore", "foodScore", "shoppingScore"):
                    raw_score = pref.get(key)
                    if raw_score is not None and (isinstance(raw_score, bool) or not isinstance(raw_score, int) or not 0 <= raw_score <= 5):
                        pref = None
                        break
            coord = None
            if str(place.get("geoValidated", "")).lower() == "yes":
                try:
                    lon, lat = float(place["mapx"]), float(place["mapy"])
                    if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
                        coord = {"latitude": lat, "longitude": lon, "crs": "WGS84"}
                except (KeyError, TypeError, ValueError, OverflowError): pass
            if coord is None:
                review_candidates.append({"contentid": cid, "reason": "COORDINATE_REVIEW_REQUIRED"}); continue
            if status == "trip_date_check_required":
                start, end = (detail or {}).get("eventStartDate"), (detail or {}).get("eventEndDate")
                def eventday(v):
                    if isinstance(v, str) and re.fullmatch(r"\d{8}", v): v = f"{v[:4]}-{v[4:6]}-{v[6:]}"
                    return _iso_day(v)
                a, b = eventday(start), eventday(end)
                if a is None or b is None or b < a:
                    review_candidates.append({"contentid": cid, "reason": "EVENT_DATE_REVIEW_REQUIRED"}); continue
                allowed = [d.isoformat() for d in days if a <= d <= b]
                if not allowed:
                    excluded.append({"contentid": cid, "reason": "EVENT_OUTSIDE_TRIP"}); continue
            else: allowed = day_strings
            member_scores = {}
            unknown_terms = []
            violated = False; unknown_required = False
            for pred in predicates:
                use = place.get("recommendationUse")
                applies = pred["scope"] == "all" or use == SCOPE_USE[pred["scope"]]
                if not applies: continue
                present = pred["tag"] in tags
                absence = (cid, pred["tag"]) in absences
                if pred["required"]:
                    if pred["polarity"] == "avoid": violated |= present; unknown_required |= not present and not absence
                    else: violated |= (not present and absence); unknown_required |= not present and not absence
                    continue
                if not present and not absence:
                    unknown_terms.append({"memberRef": pred["memberRef"], "tag": pred["tag"], "polarity": pred["polarity"], "scope": pred["scope"]})
                score = 1 if pred["polarity"] == "prefer" and present else -1 if pred["polarity"] == "avoid" and present else 0
                member_scores.setdefault(pred["memberRef"], []).append(score)
            for p in prefs:
                vals = member_scores.get(p["memberRef"], [])
                member_scores[p["memberRef"]] = sum(vals) / len(vals) if vals else 0.0
            if violated:
                excluded.append({"contentid": cid, "reason": "REQUIRED_PREDICATE_VIOLATED"}); continue
            if unknown_required:
                review_candidates.append({"contentid": cid, "reason": "REQUIRED_EVIDENCE_UNKNOWN"}); continue
            scores = list(member_scores.values())
            minimum = min(scores) if scores else 0.0
            mean = sum(scores) / len(scores) if scores else 0.0
            item = {"contentid": cid, "title": title, "planningRole": place["planningRole"], "adminCity": city,
                    "coordinate": coord, "allowedDates": allowed, "groupScore": mean,
                    "minimumMemberScore": minimum, "memberScores": member_scores,
                    "matchedTags": sorted(tags), "unknownTerms": unknown_terms}
            stay = (detail or {}).get("estimatedStayMinutes")
            if type(stay) is int and 1 <= stay <= 1440:
                item["estimatedStayMinutes"] = stay
            item["feeLabel"] = "요금 확인 필요"
            for key, source in (("feeLabel", detail), ("openingHoursText", detail)):
                if source and isinstance(source.get("feeText" if key == "feeLabel" else key), str) and source.get("feeText" if key == "feeLabel" else key).strip():
                    item[key] = source.get("feeText" if key == "feeLabel" else key)
            pool.append(item)
        pool.sort(key=lambda x: (-x["minimumMemberScore"], -x["groupScore"], x["contentid"]))
        # Explicit anchors must resolve to eligible rows and remain in the
        # bounded provider pool, even if their score ranks below the cutoff.
        eligible_by_id = {x["contentid"]: x for x in pool}
        if any(v["contentid"] not in eligible_by_id or v["date"] not in eligible_by_id[v["contentid"]]["allowedDates"] for v in fixed_visits):
            unresolved.append("FIXED_VISIT_NOT_ELIGIBLE")
        if confirmed_evidence is not None:
            unresolved.extend(x["code"] for x in confirmed_evidence.get("issues", [])
                              if x.get("code") in {"REQUIRED_PREFERENCE_UNSUPPORTED", "REQUIRED_PREFERENCE_UNSATISFIED"})
        maxc = policy["maxCandidates"]
        anchor_ids = {v["contentid"] for v in fixed_visits}
        if len(anchor_ids) > maxc:
            unresolved.append("FIXED_VISIT_CAPACITY")
        selected = {x["contentid"]: x for x in pool[:maxc]}
        for cid in anchor_ids:
            if cid in eligible_by_id and cid not in selected:
                if len(selected) >= maxc:
                    replaceable = [x for x in selected.values() if x["contentid"] not in anchor_ids]
                    if replaceable:
                        worst = max(replaceable, key=lambda x: (-x["minimumMemberScore"], -x["groupScore"], x["contentid"]))
                        del selected[worst["contentid"]]
                if len(selected) < maxc:
                    selected[cid] = eligible_by_id[cid]
        # Keep meal candidates available to the planner even when their group
        # score ranks behind a large sightseeing pool. The public pool remains
        # bounded and all hard filters above still apply.
        # Lunch, dinner and one alternative in case a proposed restaurant is closed.
        meal_target = min(3 * len(day_strings), max(0, maxc - len(anchor_ids)))
        meal_rows = [x for x in pool if x["planningRole"] == "meal_stop"]
        selected_meals = [x for x in selected.values() if x["planningRole"] == "meal_stop"]
        for meal in meal_rows:
            if len(selected_meals) >= meal_target:
                break
            if meal["contentid"] in selected:
                continue
            replaceable = [x for x in selected.values()
                           if x["contentid"] not in anchor_ids and x["planningRole"] != "meal_stop"]
            if not replaceable:
                break
            worst = min(replaceable, key=lambda x: (x["minimumMemberScore"], x["groupScore"], -x["contentid"]))
            del selected[worst["contentid"]]
            selected[meal["contentid"]] = meal
            selected_meals.append(meal)
        candidate_public = sorted(selected.values(), key=lambda x: (-x["minimumMemberScore"], -x["groupScore"], x["contentid"]))
        result = {"sourceFingerprint": snapshot.fingerprint, "catalogDigest": catalog.digest,
                  "normalizationDigest": ndigest, "policyDigest": pdigest, "eligible": candidate_public,
                  "excluded": sorted(excluded, key=lambda x: (x["contentid"], x["reason"])),
                  "reviewCandidates": sorted(review_candidates, key=lambda x: (x["contentid"], x["reason"])), "omittedCount": max(0, len(pool) - len(selected)),
                  "unresolvedRequired": sorted(set(unresolved)),
                  "pendingValidation": sorted(set(pending)), "readyForDraft": bool(candidate_public) and not unresolved,
                  "fixedVisits": sorted(fixed_visits, key=lambda x: (x["date"], x["position"]))}
        return CandidateSelection(_canonical(result), snapshot.fingerprint, catalog.digest, ndigest, pdigest)
    except CandidateSelectionError:
        raise
    except Exception:
        raise CandidateSelectionError() from None


def build_confirmed_candidate_selection(snapshot: Any, catalog: Any, evidence: dict[str, Any],
                                       common_settings: dict[str, Any] | None = None,
                                       policy: dict[str, Any] | None = None) -> CandidateSelection:
    """Select using deterministic confirmed-v1 predicates without review DTOs."""
    from .confirmed_preferences import group_schedule_anchors
    settings = common_settings or {"reviewedCommonFields": []}
    snapshot_doc = snapshot.as_dict()
    prefs = snapshot_doc["confirmedPreferences"]
    common = []
    conditions = snapshot.as_dict()["conditions"]
    for field in ("lodging", "fixedSchedule"):
        source = conditions.get(field)
        if not source:
            continue
        matched = next((x for x in settings.get("reviewedCommonFields", [])
                       if isinstance(x, dict) and x.get("field") == field and x.get("reviewed") is True), None)
        if matched and isinstance(matched.get("anchors"), list):
            common.append({"field": field, "text": source,
                           "fixedVisits": [{k: a[k] for k in ("date", "contentid", "position")}
                                           for a in matched["anchors"] if isinstance(a, dict)
                                           and all(k in a for k in ("date", "contentid", "position"))]})
    evidence = {**evidence, "issues": list(evidence.get("issues", []))}
    try:
        anchors = group_schedule_anchors(prefs, settings, snapshot_doc.get("_identity"))
    except ValueError:
        anchors = []
        evidence["issues"].append({"field": "scheduleItems", "code": "REQUIRED_PREFERENCE_UNSATISFIED"})
    derived_anchors = [a for a in anchors if a.get("kind") in {"lodging", "appointment"}]
    fixed = [{k: a[k] for k in ("date", "contentid", "position")} for a in derived_anchors
             if all(k in a for k in ("date", "contentid", "position"))]
    invalid_group = [item for pref in prefs for item in pref.get("scheduleItems", [])
                     if item.get("scope") == "group" and (item.get("contentid") is None or item.get("date") is None)]
    if invalid_group or len(fixed) != len(derived_anchors):
        evidence["issues"].append({"field": "scheduleItems", "code": "REQUIRED_PREFERENCE_UNSATISFIED"})
    evidence["digest"] = _digest({k: evidence[k] for k in ("policyVersion", "predicates", "issues")})
    return build_candidate_selection(snapshot, None, catalog, None, policy,
                                     confirmed_evidence={**evidence, "fixedVisits": fixed,
                                                         "reviewedCommonFields": common})
