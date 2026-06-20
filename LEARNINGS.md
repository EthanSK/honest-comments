# Learnings

Per-repo institutional memory for fixes. Every entry below is a real bug we hit + how we solved it. Check this file BEFORE attempting a same-looking fix.

Maintained by the `learnings` skill — see `~/.claude/skills/learnings/skill.md`.

## Format

Each entry looks like:

```
---
**Date:** YYYY-MM-DDTHH:MM:SSZ
**Trigger:** <voice N / message snippet / null>
**Symptom:** <what was visible>
**Root cause:** <what we actually found>
**Fix:** <file:line + short prose + commit SHA>
**Guard:** <test / lint / watchdog / comment that prevents regression — or 'none'>
---
```

## Entries

(newest first)

---
**Date:** 2026-06-20T01:02:47Z
**Trigger:** Mini end-to-end test for channel reethen, 2026-06-20
**Symptom:** honest-comments OAuth fetch fails: commentThreads.list returns 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT even though login + channels.list?mine=true succeed
**Root cause:** The youtube.readonly OAuth scope is permitted for channels.list?mine=true but Google REFUSES it for commentThreads.list. youtube.force-ssl is the ONLY OAuth scope that can read comments via the API — a documented Google quirk.
**Fix:** Set OAUTH_SCOPE to https://www.googleapis.com/auth/youtube.force-ssl in scripts/youtube_login.py. The tool stays genuinely read-only (only .list calls); the consent screen's scary 'edit/delete' wording is Google's boilerplate for that scope. Transparency note added to README + code comments.
**Commit:** 3bdc61c
**Guard:** README callout + code comment explaining force-ssl-but-read-only with a grep-to-verify; verified zero write/insert/update/delete API calls exist
---

---
**Date:** 2026-06-19T23:40:39Z
**Trigger:** owner decision to switch manual-API-key -> agent-driven OAuth login
**Symptom:** Manual YouTube API-key onboarding too heavy; needed agent-driven login + 'my channel' auto-detect
**Root cause:** API-key-only auth forced every creator through the Google Cloud console; no way to resolve the signed-in creator's own channel
**Fix:** Added scripts/youtube_login.py (OAuth 2.0 installed-app loopback + PKCE, stdlib-only) saving tokens to ~/.honest-comments/youtube_token.json (0600); fetch_comments.py now prefers OAuth bearer (auto-refresh) over API key, added --mine (channels.list?mine=true), exit 5=not-authenticated
**Commit:** uncommitted
**Guard:** py_compile both scripts; offline PKCE/auth-URL/token-path smoke test; 4-copy starter-prompt equality check; exit-code tests (1/5)
---

---
**Date:** 2026-06-19T22:52:41Z
**Trigger:** v5-vibrant landing page build task
**Symptom:** Verifying a self-contained landing-page variant: regex prompt-verbatim check kept reporting False
**Root cause:** An HTML comment near the codeblock literally mentions the marker 'data-copy-source', so a naive regex match started inside the comment instead of the real <code> block; also a 'do NOT claim nothing leaves your machine' guard string lives in a comment, tripping a plain substring check
**Fix:** Strip HTML comments (re.sub('<!--.*?-->','')) BEFORE extracting/checking — mirrors what the browser DOM does anyway. design-variants/v5-vibrant.html paste-prompt is byte-identical to site/starter.txt after html.unescape
**Commit:** uncommitted
**Guard:** Verification snippet strips comments first; honest privacy copy says 'honest-comments sees nothing' / 'nothing goes to us', never 'nothing leaves your machine'
---

---
**Date:** 2026-06-19T22:02:52Z
**Trigger:** Codex review findings list P0-P2 (orchestrator task)
**Symptom:** Broken first run: agent fetched only README then ran scripts/fetch_comments.py + prompts/analyze.md which don't exist in a fresh creator workspace (No such file or directory). Also: bad API key crashed --videos path, bad video IDs aborted the batch, /c/ vanity URLs failed to resolve, empty channels exited 0 silently, no dry-run estimate, privacy copy overstated, analyze.md not executable at scale, ranking likes-term swamped commenter count, out/ partially gitignored, .reveal invisible without JS.
**Root cause:** Starter prompt never cloned the repo. fetch_comments.py: --videos path had no BadApiKey/404 guards (no channel-setup probe runs there); /c/ + bare names never hit search.list fallback; empty playlist wrote empty JSON; no dry-run. analyze.md: no file-based map/reduce; ranking used min(likes,1000)*0.5 (=500 for viral). Privacy: claimed 'nothing leaves machine' (false). CSS: .reveal{opacity:0} unscoped. script.js: execCommand return ignored. .gitignore only had comments_*.json.
**Fix:** STARTER_PROMPT.md+site/starter.txt+index.html<code>: clone repo first (byte-identical, & HTML-escaped in html). README: Step 0 clone, dry-run wiring, honest privacy, exit-code troubleshooting. fetch_comments.py: VideoUnavailable exc + per-video 404 skip; BadApiKey guard in loop -> exit 3; search.list fallback for /c/+bare; empty-channel exit 4; --dry-run/--estimate + get_uploads_total; negative-int validation exit 1; .env inline-comment strip. analyze.md: §2.5 MAP/REDUCE JSON schemas (classified/claims/themes), blank-id synthetic identity, ranking = distinct*10 + ln(1+likes). site: honest privacy, env-var-first, .js-scoped .reveal + inline head script, execCommand return check. .gitignore: /out/.
**Commit:** uncommitted
**Guard:** Verified: py_compile OK, --help shows --dry-run, 3-way prompt identity True (after unescaping html), offline tests for VideoUnavailable/BadApiKey/dry-run/empty-channel/.env all pass. Exit-code contract documented in script docstring AND README troubleshooting (must stay in sync).
---

