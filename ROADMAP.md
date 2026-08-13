# Plybit AI — Refactor Roadmap (2026-08-13)

## Problem Statement (User)

1. **Share Signal section** doesn't show signals. Most signals fall through to
   "natural" (NEUTRAL). Every candle must emit a signal (CALL or PUT).
2. The app should be accessible to anyone via URL — no barriers. Optional
   API-key system if needed.
3. Bottom navigation with **4 tabs**: Home, Chart Signal, History (other), Settings.
   Mobile = bottom tabs, Desktop = sidebar.
4. Each tab must have a clean, sequenced file/folder structure.
5. Analyze and find 1000 problems, fix each one. Delete dead code/files.
6. Backtest to verify. Then deploy.

---

## Section 1 — Comprehensive Audit Findings

The Explore agent read all 11 key files completely. Below are the concrete
issues identified. (The user's "find 1000 problems" is hyperbole for
"be exhaustive" — we report the real, concrete issues found, not fabricated
ones. Total real issues: ~55, all addressed below.)

### 1.1 Critical Bugs (definite breakage)

| # | File:Line | Bug | Fix |
|---|-----------|-----|-----|
| 1 | feed.py:933 | `env_token` NameError — variable referenced but never defined in Attempt 3 of connect path. Caught by outer `except Exception` and silently logged as connect error. | Replace with `os.environ.get("QX_TOKEN", "").strip()`. |
| 2 | nav.js:90-95 | Desktop sidebar nav-items call `Nav.setPage(page)` directly, bypassing `chart.js:_setActiveTab`. Result: Share Signals / History / Settings loaders never fire on desktop. | Route sidebar clicks through `chart._setActiveTab(tab)` (or expose `Nav.setPage` to call back into chart's loader hooks). |
| 3 | chart.js:1254-1256 | Share Signal empty state is unreachable — `get_share_signals()` always returns 16 rows, so `if (!signals.length)` never fires. | Change empty-state check to look for rows with `signal != null`. |
| 4 | chart.js:1259 | Share Signal dedup key `asset:signal:time:buy_pct` is identical for all-null rows on cold start, blocking later updates. | Include a per-row "has any data" flag in the dedup key. |
| 5 | feed.py:549 | `get_share_signals()` only checks `(asset, 60)` stream — pairs with only 30s/300s/900s streams invisible. Acceptable for "all pairs at a glance" but documented. | Document only. |
| 6 | feed.py:48-56 | `_api_to_display` returns wrong name for BRLUSD (says "BRL/USD" but Quotex displays as "USD/BRL"). | Use Quotex-supplied display string when available. |
| 7 | chart.js:1270 | Share Signal "Time" column uses viewer local TZ, but `s.time` is broker-time Unix stamp → inconsistent with chart countdown. | Convert to UTC explicitly. |
| 8 | server.py:37 | `_broadcast` sends serially per client with 2s timeout each — N slow clients block the loop. | Use `asyncio.gather(*sends, return_exceptions=True)`. |
| 9 | server.py:147 | `POST /api/token` response leaks `token_preview = token[:8] + "..."` (JWT header prefix). | Remove `token_preview` from response. |
| 10 | analyze_eoc.py:1225-1247 | NEUTRAL is emitted when score=0 + no indep vote + no continuation. User wants CALL/PUT on every candle. | Add fallback tiebreak: if NEUTRAL after all 3 existing escape clauses, pick trend direction if regime has any lean, else last-candle close direction (color-aware, not pure parrot). |
| 11 | feed.py:707 | `_clear_stale_token` may not find `session.json` on Railway (working dir differs from pyquotex's write path). | Wrap in try/except, log if not found. |
| 12 | analyze_eoc.py:391, 471, 720, 725 | `forced_score` variable is written but **never read**. Comment claims "always 0 in refactor" — misleading. | Delete forced_score and its writes; keep the actual vote additions to `score`. |

### 1.2 UX Gaps

| # | Issue | Fix |
|---|-------|-----|
| 13 | No "Home" tab — current 4 tabs are Chart/Share Signal/History/Settings. User wants Home/Chart Signal/History/Settings. | Add Home tab as landing page; move Share Signal table to Home. |
| 14 | No persistence of pair/timeframe on Home tab. | Add quick-stats card on Home. |
| 15 | History table unpaginated (`limit=150` hard-coded). | Add "Load more" button. |
| 16 | Token input is a textarea (visible plaintext). | Switch to password-type input + show/hide toggle. |
| 17 | No rate limiting on `/api/token` POST. | Add simple in-memory rate limiter per IP. |
| 18 | No way to mute theories manually from UI. | Out-of-scope for this sprint. Document. |
| 19 | Mobile Share Signal table 7 columns unreadable on 360px. | CSS: collapse to 3-col card layout on narrow screens. |
| 20 | No CSP / security headers. | Add minimal CSP to StaticFiles. |
| 21 | No 2FA on operator token set. | Add simple shared-secret env gate (optional). |

### 1.3 Performance Issues

| # | Issue | Fix |
|---|-------|-----|
| 22 | `_broadcast` serial sends. | Fixed in #8 above. |
| 23 | `loadStats` fires `/api/stats` + `/api/theory-report` every 30s + on every EOC. | Debounce to 60s minimum between calls. |
| 24 | `_renderSignalHistoryMini` fires every EOC + every 60s. | Same debounce. |
| 25 | `tickCountdown` runs every 250ms and queries DOM with `querySelectorAll`. | Cache the NodeList once. |
| 26 | `_analyze_microstructure` runs on every tick broadcast — O(N) per tick. | Cache last result + skip if ticks unchanged. |
| 27 | `NoCacheStaticFiles` forces revalidation on 250KB+ chart library. | Fingerprint filename OR keep no-cache but accept the cost (single-tenant). |
| 28 | DB calls open/close a new sqlite3 connection each time. | Add a module-level connection pool. |

### 1.4 Security Issues

| # | Issue | Fix |
|---|-------|-----|
| 29 | No auth on any endpoint. | Add optional API-key middleware (env `PLYBIT_API_KEYS` — empty = open). |
| 30 | No rate limiting. | Same as #17. |
| 31 | `/api/debug` leaks env-var presence. | Gate behind API key. |
| 32 | `token_preview` leak. | Fixed in #9. |
| 33 | WS no origin check. | Add optional origin whitelist. |
| 34 | No CSP. | Fixed in #20. |
| 35 | CORS not configured. | Optional CORSMiddleware. |

### 1.5 Code Quality

| # | Issue | Fix |
|---|-------|-----|
| 36 | Giant files (feed.py 2318, analyze_eoc.py 1328, chart.js 2063, style.css 3649). | Out-of-scope for this sprint — keep monolith but add inline section markers. |
| 37 | `print()` everywhere instead of `logging`. | Add `import logging` and use structured logs. |
| 38 | No tests. | Add a smoke test for the env_token bug + the NEUTRAL-tiebreak path. |
| 39 | Magic numbers everywhere. | Extract to constants module. |
| 40 | Stale comments throughout. | Fix the most misleading ones (Signals tab "removed", `_tier_payload`, "3-tab layout", README "no NEUTRAL"). |

### 1.6 Dead Code / Files to Delete

| # | Path | Reason |
|---|------|--------|
| 41 | `analyze_eoc.py` `forced_score` variable + 4 writes | Never read. |
| 42 | `db.py` `candle_running` table schema (lines 119-139) | Schema exists, never written, never read. |
| 43 | `chart.js` `pred.zigzag` block (line ~1033-1044) | `analyze_eoc` never returns `zigzag`. |
| 44 | `chart.js` countdown reference (line ~531) | `#countdown` element no longer exists in HTML. |
| 45 | Stale comment "Signals tab was removed" (chart.js:652) | Tab clearly exists. |
| 46 | Stale comment "see server.py's _tier_payload" (chart.js:1078) | `_tier_payload` removed. |
| 47 | Stale comment "3-tab layout" (chart.js:1599 + nav.js:3) | Now 4 tabs. |
| 48 | README "no NEUTRAL on screen" + "15 pairs" | Both wrong. |

### 1.7 Deployment Issues

| # | Issue | Fix |
|---|-------|-----|
| 49 | `Procfile` vs `railway.json` startCommand disagree (module vs bare uvicorn). | Pick one — keep railway.json (Railway uses it). |
| 50 | `server.py __main__` block adds third startup path. | Keep but document. |
| 51 | No `QX_DB_PATH` configured by default → SQLite lost on redeploy. | Document the volume-mount requirement in README. |
| 52 | `restartPolicyMaxRetries: 10` crash-loops on bad creds. | Document. |

---

## Section 2 — Implementation Plan

### Phase A — Critical Bug Fixes (before UI changes)

1. Fix `env_token` NameError in feed.py:933.
2. Fix `_broadcast` to use `asyncio.gather`.
3. Remove `token_preview` leak.
4. Fix `_api_to_display` for BRLUSD.
5. Wrap `_clear_stale_token` in try/except.
6. Modify `analyze_eoc.py` NEUTRAL tiebreak: ALWAYS emit CALL/PUT.
7. Delete `forced_score` dead variable.
8. Delete `candle_running` dead table schema.
9. Delete `pred.zigzag` UI block + stale comments.

### Phase B — UI Restructure (after bugs)

10. Add new Home page in `index.html` (overview + share signal table).
11. Update `nav.js` PAGES + TAB_MAP to: `home / chart / history / settings`.
12. Update `chart.js` `_setActiveTab` for the new tab set.
13. Wire sidebar nav-items through `_setActiveTab` (fixes bug #2).
14. Move share-signal table content to Home.
15. Update bottom-tabs HTML: 4 buttons (Home/Chart/History/Settings).
16. Update sidebar nav-items: 4 buttons.
17. Update CSS for Home tab styling.
18. Add password-type input + show/hide toggle for SSID token.
19. Mobile responsive: 3-col card layout for share table on narrow screens.

### Phase C — Optional API Key System

20. Add `require_api_key` FastAPI dependency (env-driven, default disabled).
21. Apply to write endpoints only (`/api/token`, `/api/subscribe`).
22. Document in README.

### Phase D — Dead Code Cleanup

23. Delete `forced_score` writes in `analyze_eoc.py`.
24. Delete `candle_running` schema in `db.py`.
25. Delete `pred.zigzag` block in `chart.js`.
26. Delete stale comments.
27. Update README.

### Phase E — Backtest Verification

28. Run `python tools/replay_eoc.py` against the existing `candle_micro.db`.
29. Verify CALL/PUT ratio, NEUTRAL count is 0 (post-fix), accuracy sane.

### Phase F — Deploy

30. `git add -A && git commit -m "Refactor: fix signals, add Home tab, dead code cleanup"`
31. `git push origin main`
32. Railway auto-deploys from main branch.
33. Verify `/healthz` + `/api/debug` on the deployed URL.

---

## Section 3 — Success Criteria

- [ ] `/api/share-signals` returns rows where every pair has a CALL or PUT signal (not null).
- [ ] Clicking Home/Chart Signal/History/Settings in desktop sidebar loads the
      correct page AND fires the loader for that tab.
- [ ] Mobile bottom tabs work identically.
- [ ] `analyze_eoc` never returns `signal == "NEUTRAL"`.
- [ ] Backtest shows accuracy in a sane range (not 0%, not 100%).
- [ ] No `forced_score` / `candle_running` / `pred.zigzag` references remain.
- [ ] `git push origin main` succeeds; Railway redeploy completes; `/healthz` returns 200.
