---
name: review-backend
description: Independently review backend API, database, authorization, or runtime behavior changes for correctness, privacy, compatibility, and failure handling. Not for frontend UI or documentation-only review.
---

# Review backend changes

Read the assigned diff and only the relevant backend contract, source, and repository docs. Resolve paths from the repository root. Review the requested scope and established conventions; do not assume a tool, framework detail, endpoint, or contract that the repository has not established.

Prioritize actionable defects in:

- API contract compatibility: method/path, auth, request and response shape, errors, nullable fields, versioning, units/timezones, and required idempotency or concurrency behavior.
- Authentication and authorization at every affected boundary, including object ownership, cross-user or cross-trip access, and private source data exposure.
- Input validation, error handling, retry behavior, information leakage, and failure paths.
- Database integrity, transaction boundaries, duplicate requests, races, query behavior, migration compatibility, and rollback effects where relevant.
- Tests or checks that are missing, misleading, brittle, or do not exercise the changed behavior.

Compare public API changes against the FE-authored Notion implementation-request specification supplied by root, following `docs/api-contract.md`. Check method/path, payloads, errors, authorization, and implementation tests. Flag silent BE specification changes, missing/contradictory requirements, insecure or infeasible behavior, and claimed completion without verification. A separate FE/BE contract approval, required API ID/revision/approvers, or mandatory snapshot file is not a review gate. The PR should identify the page, implemented API, key specification details, and actual checks. Root handles external reads/writes and passes relevant changes to workers. OpenAPI alone does not verify authorization, errors, or transaction behavior.

Do not edit implementation files unless the parent explicitly assigns fixes. Return findings by severity with file/line, concrete failure scenario, expected behavior, and a focused way to verify. If no findings are found, state what was reviewed and any verification limits.
