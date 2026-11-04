---
name: deliver-pr
description: Deliver authorized backend implementation work through an issue-linked branch, repository checks, commits, push, and a self-contained PR report. Use for implementation, not explanation-only requests or explicitly local-only work.
---

# Deliver a backend implementation PR

Read root `AGENTS.md` and `docs/github-workflow.md`. Resolve paths from the backend repository root. This skill coordinates authorized repository delivery; it does not authorize external messages, deployments, merges, or unrelated changes.

The root explicitly selects and confirms the backend checkout path, repository, remote, and base branch before any GitHub delivery. Never infer the backend target from the project's primary folder, primary Git/PR action, or a review pane that happens to show multiple repositories. Confirm current branch, user changes, relevant existing issue/PR, and authenticated GitHub access without printing credentials. Reuse the issue and branch for the same work. Otherwise create or identify the task issue and use the workflow's `codex/` branch before implementation. Respect an explicit local-only instruction.

Keep repository histories separate: backend issue, branch, commits, and PR belong to the backend checkout. Frontend work uses separate frontend issue/branch/commits/PR. Link coordinated PRs and identify the Notion specification, implemented method/path, key specification details or changes, and actual checks. FE writes the specification, BE implements it, and FE verifies integration when the server is ready. Do not invent absent schema files or commits, mix `.git` directories or files across repositories in one commit, or treat a primary-project PR action as backend delivery.

Let the root own Git operations. Preserve unrelated edits and stage only task files. Do not reset user work, force-push, merge, or include temporary files/secrets. Do not assume `.agent-work/` is ignored. Do not commit or push unless explicitly assigned that responsibility by the root.

Read the repository's actual README and configuration before running checks. Use only commands and tools that exist. Fix attributable local failures and review findings, rerun focused verification, and accurately record commands that passed, failed, were not run, or were not applicable. Never claim a test or server check ran when it did not.

For API changes, follow `docs/api-contract.md`: implement FE-authored implementation-request specifications, report missing/contradictory/infeasible or insecure details to FE, and preserve FE ownership of specification text. Compare the completed behavior with the specification and reverify affected changes if it changed during work. No separate dual approval gate, mandatory API ID/revision/approver fields, or snapshot file is required. Include the page, method/path, key specification details or changes, and actual checks in the PR. For database changes, describe relevant migration, compatibility, and rollback effects.

For persistence work, use the supplied baseline in `docs/database-schema.md` and the affected table DDL in `docs/database-schema-reference.md`. Report affected tables and any API/schema conflict or proposed schema change. Separate static schema/document checks from actual integration checks in a dedicated MySQL environment. Do not include the original dump, data rows, connection credentials, or treat reading DDL as a completed database setup.

Authorized BE feature delivery includes implementation-status and BE PR-link updates in Notion through root, while FE owns API specification edits. Read-only/local-only or task-specific write restrictions take precedence. Reread relevant content before targeted edits, preserve unrelated content, and check uncertain write results before retrying. Report failed reads/writes accurately. No watcher or automatic cross-repository update is installed.

Use an existing `.github/pull_request_template.md` if present; it is optional. If absent, write a self-contained PR body with purpose, issue, changes, commands/results/not-run/not-applicable, API contract baseline and diff, database migration/rollback effects when relevant, reviewer, final head SHA, and remaining work. Put any temporary body in the OS temp directory or a confirmed excluded location. Keep existing CI and branch protections intact; do not add CI during setup or use an unapproved CI status as a new delivery requirement.

Create or update one Draft PR. Before retrying an uncertain remote write, check whether it already succeeded. Validate after the last code change and record the matching final head SHA. Mark Ready for human review only when applicable Codex checks ran, important review findings are resolved, and required behavior verification is complete. Otherwise leave a truthful Draft and explain the blocker. If authentication or network prevents delivery, report the local result and the required next action.

Return issue and PR URLs, final head commit, actual verification results, and remaining work. Distinguish ready-for-human-review, draft-with-blockers, and delivery-not-created.
