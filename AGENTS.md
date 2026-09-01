# Lead Machine Repository Agent Rules

These rules apply to this repository in addition to the global agent operating rules.

## 1. Integration branch safety

- `Night-Mode-Hardening-Integration` is an integration branch.
- Do not perform non-trivial implementation work directly on it unless explicitly instructed.
- Prefer a dedicated task branch and neighbouring Git worktree for implementation work.
- When a task provides an expected base SHA, use that exact SHA as the task base and verify it before editing.

## 2. Runtime and local-state files

Treat runtime-progress and local-state files as non-source artifacts unless a task explicitly says otherwise.

In particular:

- `data/runtime_progress/current_run_progress.json` is runtime state.
- Do not stage, commit, reset, overwrite, or otherwise modify runtime-state files merely because they are dirty.
- Preserve existing runtime-state changes unless the task explicitly includes them.

Do not commit generated, cache, environment, credential, temporary, or machine-local files unless explicitly required by the task.

## 3. Discovery and enrichment changes

For changes affecting discovery, enrichment, identity resolution, profile validation, lead admission, dedupe, provenance, or export:

- Preserve existing source provenance unless the ticket explicitly changes it.
- Do not weaken deterministic identity or admission checks merely to make a test pass.
- Prefer focused regression coverage for the specific failure mode being fixed.
- Preserve unrelated directory/platform behaviour.
- Do not broaden a platform-specific fix into a cross-platform refactor unless required by the ticket.

## 4. Network and live-service behaviour

- Do not send outreach emails, messages, uploads, campaigns, or other external communications as part of normal code verification.
- Do not perform production mutations or live third-party actions merely to prove a code change.
- Prefer deterministic tests, mocks, fixtures, recorded inputs, or explicitly authorised read-only checks.
- If live credentials or third-party access are required to verify a task and are unavailable, stop and report the limitation.

## 5. Test discipline

For implementation work:

- Run the focused tests for the changed subsystem.
- Run adjacent regression tests where the change could affect nearby discovery/enrichment behaviour.
- Run any broader application gate explicitly required by the task.
- Do not claim completion if a required test, lint, typecheck, build, SQL gate, or integration check fails.

Where a test depends on unrelated live network behaviour, isolate or stub only that unrelated dependency rather than weakening the production logic.

## 6. Scope and diff discipline

Before completion:

- Inspect all changed paths.
- Confirm no unrelated runtime-state file was included.
- Confirm no secrets or credentials were introduced.
- Confirm no unrelated directory/platform behaviour was modified.
- If the task supplied a file allowlist, the final changed path set must match that allowlist unless the task explicitly permits expansion.

## 7. Commit and integration policy

- Do not merge a feature/fix/task branch into `Night-Mode-Hardening-Integration` unless explicitly instructed.
- Do not push unless explicitly instructed.
- Do not delete task worktrees or branches unless explicitly instructed.
- When a commit is requested, stage only the accepted task paths and inspect the staged diff before committing.

## 8. Completion evidence

In addition to the global completion report, include when relevant:

- focused test command(s) and result
- broader regression gate(s) and result
- exact changed path list
- whether runtime-state files were preserved untouched
- whether any live service or external mutation occurred
- whether the branch was pushed or merged

Never claim a Lead Machine task is complete solely because implementation code was written; completion requires the specified verification evidence.
