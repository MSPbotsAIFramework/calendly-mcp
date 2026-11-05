# PRD-21349 · Code review

## Round 1 · 2026-11-05 · reviewed dev/PRD-21349 @ 945a05e (base main @ c0978e8)

Status: frozen @ 945a05e

Verdict: approved

### Scope

Single repo, `MSPbotsAIFramework/calendly-mcp`, brand-new service (main was a 2-line README before this ticket). Diff `origin/main...945a05e`: 19 files, all new (scaffold, `src/calendly_mcp/*`, tests, Dockerfile, README, docs). Reviewed every source file (`middleware.py`, `server.py`, `client.py`, `errors.py`, `tools.py`, `settings.py`, `__main__.py`), both test files, `pyproject.toml`, `Dockerfile`, `.gitignore`/`.dockerignore`, `README.md`, and the ticket doc's Self-test/Handoff sections against the tech design and `references/REVIEW.md`.

### Verification performed (commands run this round)

- `uv run pytest -q` → `19 passed in 6.68s` — matches the `[DEV]` stub's and Self-test's claim exactly.
- `grep -rnP '[\x{4e00}-\x{9fff}]' --exclude-dir=.git --exclude-dir=.venv --exclude-dir=dist --exclude-dir=.pytest_cache --exclude-dir=__pycache__ .` → no output (no non-English content), confirming the Self-test's claim.
- `gitleaks detect --source . -v` → one finding, `README.md:126`, the literal placeholder string `your_token_here` in the example `curl` command (`-H 'X-Calendly-Api-Key: your_token_here'`). Confirmed false positive: not a real credential, just a documentation placeholder; no action needed.
- `git status --porcelain` and `git ls-files | grep -E 'dist/|__pycache__|\.pytest_cache'` → clean; build artifacts present in the worktree (`dist/`, `__pycache__/`, `.pytest_cache/`) are untracked and correctly covered by `.gitignore`/`.dockerignore`.
- `git log --format='%H %an <%ae> | %s' origin/main..HEAD` → all 3 commits authored by `Dev01.MBAgent <dev01.mbagent@mspbots.ai>`, English commit messages (Conventional Commits style).
- Diffed `README.md`'s "MSPbots Connector" section (lines 12–82) against `~/.claude/skills/vendor-mcp-development/reference/readme-connector-section.md` — verbatim match, correctly placed before the "Authentication headers" table (SOP §0.5).

### Findings

None (0 must-fix, 0 suggestions).

### Checklist highlights (vendor-mcp SOP §14, spot-checked against code, not re-derived from the ticket doc)

- Credential isolation: `middleware.py` uses a pure ASGI middleware (not `BaseHTTPMiddleware`, which would buffer the Streamable HTTP body) with a `contextvars.ContextVar` reset in `finally`; `client.py` never caches a client or token across requests/tenants (AC12, covered by `test_concurrent_requests_do_not_cross_tokens`).
- `/health` is a pure local route (`server.py:_health`), no upstream call, reachable without the credential header — confirmed by `test_health_endpoint_ok_without_credentials` and by the dev leg's live container test.
- DNS-rebinding protection disabled (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`) and the MCP sub-app's lifespan is passed to the outer `Starlette` app explicitly (`server.py:build_app`) — both required per SOP §14, both present.
- Error envelope is the fixed vocabulary (`errors.py`), every tool wrapped by `_guarded` so no raw exception can escape (SOP §4) — the one extra code `not_configured` is a defensive guard for a path the middleware already makes unreachable in production (missing-header requests never reach a tool), consistent with the README's `code is one of ...` list.
- Outbound timeouts (5s connect / 30s read) and bounded retry/backoff honoring `Retry-After`, capped at ~110s total budget, on 429/5xx only (`client.py`) — matches the tech design and SOP §5.
- Window validation (`_require_window`, 31-day / 7-day caps) runs and raises before any Calendly call — confirmed by `test_available_times_32_day_window_rejected` and `test_busy_times_8_day_window_rejected` asserting `called is False`.
- `Dockerfile`: multi-stage, non-root `app` user in production, `curl` installed for the `HEALTHCHECK`, `EXPOSE 8080`, default `MCP_HTTP_PORT`/`MCP_HTTP_HOST` env — matches the dev leg's self-test (`docker build`, container `HEALTHCHECK: healthy`, `whoami` → `app`).
- No schema/migration (stateless service, Calendly is the system of record) — the REVIEW.md migration BLOCKER category does not apply.
- No `@mspbots/ui` / reuse issue — this is a backend-only MCP repo per `mcp-track.md` §7a, the UI-specific review items do not apply. The one new dependency set (`fastmcp`/`mcp[cli]`, `httpx`, `pydantic-settings`) is exactly what the tech design's "Reuse & cleanup" section named, nothing undeclared.
- 3 test cases (TC-CAL-009, TC-CAL-011, TC-CAL-014) remain `untested · needs a real vendor account` — correctly non-blocking per the vendor-mcp SOP, carried forward unchanged from the design leg's `.ac.json`.

### Conclusion

Implementation matches the tech design's change list and data/API contract exactly (9 tools, 1:1 endpoint wrapping, same shaping/field cuts). No scope creep, no reuse violations, no security gaps, no undeclared dependencies, no migration concerns. Test suite is behavior-focused (verifies rejection-before-call, shape, concurrency isolation, error mapping) rather than implementation-detail-focused. Approved as-is; proceeding to merge into `main`, `POST /api/vendor-register` (new vendor), and the connector wiki check.
