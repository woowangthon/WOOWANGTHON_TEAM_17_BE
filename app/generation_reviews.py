"""Private generation review validation and projection helpers."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def normalization_dto(raw: dict[str, Any] | None, review: dict[str, Any] | None, review_version: int,
                      current: bool = True) -> dict[str, Any]:
    """Return only allowlisted term data from a persisted private normalization."""
    norm = raw.get("normalization") if isinstance(raw, dict) else None
    if not isinstance(norm, dict):
        return {"normalization": None, "reviewVersion": review_version, "review": None, "stale": not current}
    terms = []
    for item in norm.get("terms", []):
        if not isinstance(item, dict) or item.get("field") not in {"likes", "avoid", "food"}:
            continue
        terms.append({"id": f"t{len(terms)}", "field": item["field"], "tag": item.get("tag"),
                      "polarity": item.get("polarity"), "importance": item.get("importance"),
                      "evidence": item.get("evidence")})
    unmapped = [{k: item.get(k) for k in ("field", "text", "importance", "reason")}
                for item in norm.get("unmapped", []) if isinstance(item, dict)]
    walking = norm.get("walking", [])
    walking_doc = walking[0] if isinstance(walking, list) and walking else None
    walk = ({"maxMinutes": walking_doc.get("maxMinutes"), "unlimited": walking_doc.get("unlimited")}
            if isinstance(walking_doc, dict) else None)
    public_review = ({"acceptedTermIds": review.get("acceptedTermIds", []),
                      "reviewedFields": review.get("reviewedFields", [])} if isinstance(review, dict) else None)
    return {"normalization": {"id": raw.get("id"), "terms": terms, "unmapped": unmapped,
                              "walking": walk, "needsReview": norm.get("needsReview", True)},
            "reviewVersion": review_version, "review": public_review, "stale": not current}


def validate_review(body: Any, stored: dict[str, Any], existing_version: int) -> dict[str, Any]:
    exact = {"normalizationId", "expectedReviewVersion", "acceptedTermIds", "reviewedFields"}
    if not isinstance(body, dict) or set(body) != exact:
        raise ValueError("VALIDATION_ERROR")
    if (body["normalizationId"] != stored.get("id") or isinstance(body["expectedReviewVersion"], bool)
            or not isinstance(body["expectedReviewVersion"], int) or body["expectedReviewVersion"] != existing_version):
        raise ValueError("VERSION_CONFLICT")
    norm = stored.get("normalization")
    if not isinstance(norm, dict):
        raise ValueError("VALIDATION_ERROR")
    items = norm.get("terms")
    if not isinstance(items, list):
        raise ValueError("VALIDATION_ERROR")
    ids = {f"t{i}": item for i, item in enumerate(items) if isinstance(item, dict)}
    accepted, fields = body["acceptedTermIds"], body["reviewedFields"]
    if (not isinstance(accepted, list) or any(not isinstance(t, str) for t in accepted)
            or len(accepted) != len(set(accepted)) or any(t not in ids for t in accepted)
            or not isinstance(fields, list)):
        raise ValueError("VALIDATION_ERROR")
    allowed = {"likes", "avoid", "food"}; seen = set(); clean_fields = []
    for field in fields:
        if (not isinstance(field, dict) or set(field) != {"field", "termIds", "scope", "fullyReviewed"}
                or not isinstance(field["field"], str) or field["field"] not in allowed or field["field"] in seen
                or not isinstance(field["scope"], str) or field["scope"] not in {"all", "meal", "stay"} or field["fullyReviewed"] is not True
                or not isinstance(field["termIds"], list)):
            raise ValueError("VALIDATION_ERROR")
        term_ids = field["termIds"]
        if (any(not isinstance(t, str) for t in term_ids) or len(term_ids) != len(set(term_ids))
                or any(t not in ids or ids[t].get("field") != field["field"] or t not in accepted for t in term_ids)):
            raise ValueError("VALIDATION_ERROR")
        seen.add(field["field"])
        clean_fields.append({"field": field["field"], "termIds": sorted(term_ids), "scope": field["scope"], "fullyReviewed": True})
    accepted_fields = {ids[t].get("field") for t in accepted}
    field_term_ids = {t for field in clean_fields for t in field["termIds"]}
    required = {item.get("field") for item in norm.get("unmapped", []) if isinstance(item, dict) and item.get("field") in allowed}
    # Any classified term field also requires explicit full-field review.
    required.update(item.get("field") for item in items if isinstance(item, dict) and item.get("field") in allowed)
    return {"acceptedTermIds": sorted(accepted), "reviewedFields": clean_fields,
            "ready": required <= seen and required <= accepted_fields and set(accepted) <= field_term_ids}


def bind_private_review(normalization: dict[str, Any], review: dict[str, Any], source_fingerprint: str,
                        catalog_digest: str, reviewed_common_fields: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build the strict candidate-selection reviewer object from server-bound state."""
    from .candidate_selection import normalization_digest
    norm = normalization["normalization"]
    indexes = [int(value[1:]) for value in review.get("acceptedTermIds", [])
               if isinstance(value, str) and value.startswith("t") and value[1:].isdigit()]
    # Builder expects term indexes in the normalized global term order.
    return {"sourceFingerprint": source_fingerprint,
            "normalizationDigest": normalization_digest(norm), "catalogDigest": catalog_digest,
            "acceptedTermIndexes": indexes, "reviewedFields": review.get("reviewedFields", []),
            "verifiedTagAbsences": [], "reviewedCommonFields": reviewed_common_fields or []}
