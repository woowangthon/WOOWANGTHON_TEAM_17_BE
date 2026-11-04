---
name: review-backend
description: Independently review backend API, database, authorization, or runtime behavior changes for correctness, privacy, compatibility, and failure handling. Not for frontend UI or documentation-only review.
---

# Review backend changes

Read the assigned diff and only the relevant backend contract, source, and repository docs. Resolve paths from the repository root. Review the requested scope and established conventions; do not assume a tool, framework detail, endpoint, or contract that the repository has not established.

For API work, root supplies both the applicable Notion common rules and the existing feature child-page specification found through the main index. Check both sources and their stated applicability. Flag index-only assumptions, BE specification-body edits, pages created per commit/PR, or a whole feature marked complete when some APIs or required verification remain. BE status reporting belongs only in that feature's implementation status/BE PR/actual verification fields and corresponding index row; FE owns specification text.

For persistence changes, read `docs/database-schema.md` and the affected table DDL in `docs/database-schema-reference.md`. Check API/model mappings against the supplied existing schema, especially composite trip/plan/version boundaries, generated fields, nullable values, checks, unique request keys and revision/transaction behavior. Distinguish database-enforced constraints from application authorization/JSON validation. Flag invented columns, silent schema rewrites, leaked internal fields, or claims of live DB validation based only on reading the dump.

Prioritize actionable defects in:

- API contract compatibility: method/path, auth, request and response shape, errors, nullable fields, versioning, units/timezones, and required idempotency or concurrency behavior.
- Authentication and authorization at every affected boundary, including object ownership, cross-user or cross-trip access, and private source data exposure.
- Input validation, error handling, retry behavior, information leakage, and failure paths.
- Database integrity, transaction boundaries, duplicate requests, races, query behavior, migration compatibility, and rollback effects where relevant.
- Tests or checks that are missing, misleading, brittle, or do not exercise the changed behavior.

Compare public API changes against the FE-authored Notion implementation-request specification supplied by root, following `docs/api-contract.md`. Check method/path, payloads, errors, authorization, and implementation tests. Flag silent BE specification changes, missing/contradictory requirements, insecure or infeasible behavior, and claimed completion without verification. A separate FE/BE contract approval, required API ID/revision/approvers, or mandatory snapshot file is not a review gate. The PR should identify the page, implemented API, key specification details, and actual checks. Root handles external reads/writes and passes relevant changes to workers. OpenAPI alone does not verify authorization, errors, or transaction behavior.

Do not edit implementation files unless the parent explicitly assigns fixes. Return findings by severity with file/line, concrete failure scenario, expected behavior, and a focused way to verify. If no findings are found, state what was reviewed and any verification limits.
