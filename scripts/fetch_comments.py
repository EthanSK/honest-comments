#!/usr/bin/env python3
"""honest-comments — fetch_comments.py

Stdlib-only YouTube comment fetcher for the "honest-comments" tool. It pulls
top-level comments off a creator's videos using the YouTube Data API v3. The raw
comments land as JSON on the creator's own disk so a downstream agent can
classify/rank them later.

AUTH MODEL — OAUTH FIRST, API KEY AS FALLBACK (changed 2026-06-20):
    The PREFERRED path is now agent-driven OAuth login (run
    `scripts/youtube_login.py` once). That saves an OAuth token bundle to
    ~/.honest-comments/youtube_token.json which this script loads, AUTO-REFRESHES
    when stale, and sends as `Authorization: Bearer <token>` on every API call.
    OAuth unlocks `channels.list?mine=true` — resolving the SIGNED-IN creator's
    own channel with no handle to paste (the --mine flag).

    If there's NO OAuth token but an API KEY is available (--api-key /
    $YOUTUBE_API_KEY / ./.env), we fall back to the old public-data-only key path
    (so the tool still works for anyone who prefers a key and hasn't logged in).
    A plain API key can only read PUBLIC comments and CANNOT use --mine.

    If NEITHER is present, we exit 5 telling the agent to run
    `python3 scripts/youtube_login.py` first.

WHAT IT DOES:
    1. Resolves auth: OAuth token (preferred, auto-refreshed) > API key.
    2. Builds a list of video IDs from EITHER:
         - the signed-in creator's OWN channel via channels.list?mine=true
           (--mine; OAuth only), OR
         - a channel handle / ID / vanity URL (resolves the uploads playlist,
           then pages playlistItems for video IDs), OR
         - an explicit list of video IDs / URLs (--videos), which skips channel
           resolution entirely (cheaper, faster, more targeted).
    3. For each video, pages commentThreads.list (maxResults=100, follows
       nextPageToken) collecting top-level comment text + author + likeCount +
       publishedAt + videoId (and a few more useful fields).
    4. Writes out/comments_<channel-or-batch>_<timestamp>.json plus a
       run_meta.json with per-video counts, errors, and comments-disabled list.

ROBUST ERROR HANDLING (exit-code contract — keep in sync with README
"Troubleshooting"):
    - 0  success (or a --dry-run that printed an estimate without fetching).
    - 1  bad usage (e.g. negative --max-videos / --per-video-cap, or --mine
         without OAuth).
    - 2  403 quotaExceeded -> stop cleanly, write partial data.
    - 3  400 / 403 bad/disabled/restricted credential -> clear message. This
         covers a bad API key AND an OAuth token that won't authorize. It fires
         on BOTH the channel path (credential probed during setup) AND the
         --videos path (no setup probe runs there, so the credential is first
         exercised inside the per-video fetch loop — see the BadCredential guard).
    - 4  channel not found / empty channel (no public uploads) / --videos had
         no usable IDs.
    - 5  NOT AUTHENTICATED — no OAuth token AND no API key. Tells the agent to
         run `python3 scripts/youtube_login.py` first.
    - Per-video, NON-fatal (run continues, recorded in run_meta):
        * 403 commentsDisabled  -> skip that video, note it.
        * 404 videoNotFound     -> invalid/private/deleted explicit ID; skip it,
                                   record in run_meta errors, keep going.

QUOTA NOTE (for context — the script does NOT enforce a hard ledger here, it
just keeps the calls cheap): commentThreads.list and playlistItems.list each
cost 1 unit per call regardless of maxResults, so one call fetches up to 100
comments for 1 unit. Default free quota is 10,000 units/day. search.list is the
EXPENSIVE call (it draws from its own, much smaller search bucket) and is used
ONLY as a last-resort vanity-name fallback — we warn before using it. We avoid
quoting an exact unit figure for search here because Google's published numbers
shift; just treat it as "the one pricey call, used only when nothing else
resolves the channel."

USAGE EXAMPLES:
    # Logged-in creator's OWN channel (the natural default once you've run
    # youtube_login.py) — no handle needed, resolved via mine=true:
    python3 scripts/youtube_login.py        # one-time sign-in
    python3 scripts/fetch_comments.py --mine

    # Whole channel by handle (newest 25 videos by default):
    python3 scripts/fetch_comments.py --channel @SomeCreator

    # Channel by ID, wider scope (uses whatever auth is configured):
    python3 scripts/fetch_comments.py --channel UCxxxxxxxxxxxxxxxxxxxxxx \
        --max-videos 50

    # Specific videos (URLs / short URLs / bare IDs, mixed) — skips channel
    # resolution entirely:
    python3 scripts/fetch_comments.py \
        --videos "https://youtu.be/abc123,https://www.youtube.com/watch?v=def456,ghi789xyz01"

    # API-key fallback (public comments only, no login, no --mine):
    python3 scripts/fetch_comments.py --channel @SomeCreator --api-key AIza...

    # Include reply chains too (off by default — replies are mostly noise):
    python3 scripts/fetch_comments.py --mine --include-replies
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Base URL for all YouTube Data API v3 REST endpoints. We hit the endpoints
# directly with urllib instead of using google-api-python-client on purpose —
# that SDK is a thin REST wrapper we don't need, and it would add pip-install
# friction. Stdlib-only means this runs anywhere Python 3.8+ exists.
API_BASE = "https://www.googleapis.com/youtube/v3"

# YouTube Data API v3 returns at most 100 items per page for list endpoints.
# commentThreads honours maxResults up to 100; playlistItems up to 50.
COMMENTS_PER_PAGE = 100
PLAYLIST_ITEMS_PER_PAGE = 50

# Network politeness / resilience: small retry loop with backoff for transient
# 5xx / network blips. We do NOT retry on 4xx (those are deterministic — bad
# key, quota, comments disabled — and retrying wastes quota and time).
HTTP_MAX_RETRIES = 3
HTTP_RETRY_BACKOFF_SEC = 2

# An 11-character token that isn't a URL is treated as a raw YouTube video ID.
YOUTUBE_ID_LEN = 11

# ---------------------------------------------------------------------------
# OAuth token store + refresh — must agree with scripts/youtube_login.py.
# ---------------------------------------------------------------------------

# Where youtube_login.py saves the OAuth token bundle (under the creator's HOME,
# NOT in the repo). We READ this here; we WRITE it back only after a refresh.
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".honest-comments")
TOKEN_STORE_PATH = os.path.join(CONFIG_DIR, "youtube_token.json")

# Google's token endpoint — same one youtube_login.py uses; we POST here to
# REFRESH an expired access token using the saved refresh_token.
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Refresh proactively when the access token has THIS many seconds (or fewer)
# left, so a long fetch run doesn't have a token expire mid-flight. Google's
# access tokens last ~3600s; refreshing at <120s remaining is a safe margin.
TOKEN_REFRESH_SKEW_SEC = 120


# ---------------------------------------------------------------------------
# Custom exceptions — let us map specific API failure modes to clean exits.
# ---------------------------------------------------------------------------

class QuotaExceeded(Exception):
    """Raised when the API reports the daily quota is exhausted (403)."""


class BadCredential(Exception):
    """Raised when the credential (API key OR OAuth token) is rejected (400/403).

    Renamed from the old `BadApiKey` now that auth can be either an API key or an
    OAuth bearer token — both surface here when Google won't authorize the call.
    """


# Backwards-compatible alias: older code / tests referenced `BadApiKey`. Keep the
# name pointing at the same class so nothing breaks.
BadApiKey = BadCredential


class NotAuthenticated(Exception):
    """Raised when NEITHER an OAuth token NOR an API key is available (-> exit 5).

    The agent should run `python3 scripts/youtube_login.py` first (preferred), or
    provide an API key for the public-data fallback.
    """


class MineRequiresOAuth(Exception):
    """Raised when --mine is used without an OAuth token (-> exit 1).

    `channels.list?mine=true` resolves the SIGNED-IN user's channel, which only
    has meaning under OAuth — an API key has no associated user. So --mine is
    OAuth-only and we fail fast with a clear message.
    """


class ChannelNotFound(Exception):
    """Raised when a channel handle/ID/vanity name can't be resolved."""


class CommentsDisabled(Exception):
    """Raised (and caught per-video) when a video has comments turned off."""


class VideoUnavailable(Exception):
    """Raised (and caught per-video) when an explicit video ID can't be read.

    Covers the 404 `videoNotFound` case (private / deleted / unlisted / just
    plain wrong ID) on the `--videos` path. WHY this is its own type: a single
    bad explicit video ID must NOT abort the whole run — we record it in the
    per-video errors list and move on to the next video. `api_get()` raises the
    generic `ChannelNotFound` on a 404; the per-video loop translates that into
    this exception so the bad-ID case stays local instead of bubbling out as a
    fatal channel-resolution failure (see fetch loop in main()).
    """


# ---------------------------------------------------------------------------
# AUTH — the single object every API call authenticates with.
#
# An Auth carries EXACTLY ONE of two credentials:
#   - bearer:  an OAuth access token (preferred). Sent as an Authorization
#              header; NO `key=` query param. Supports --mine.
#   - api_key: a plain public-data API key (fallback). Sent as a `key=` query
#              param; NO Authorization header. Cannot use --mine.
# api_get() reads this to decide how to authenticate each request, and (for the
# OAuth case) calls auth.ensure_fresh() before each call so a long run can't be
# killed by a mid-flight token expiry.
# ---------------------------------------------------------------------------

class Auth:
    """Holds the resolved credential and knows how to keep an OAuth token fresh.

    is_oauth == True  -> bearer-token mode (loaded from youtube_token.json)
    is_oauth == False -> api-key mode (from --api-key / env / .env)
    """

    def __init__(self, bearer=None, api_key=None, token_bundle=None):
        # Exactly one of bearer / api_key is set. token_bundle is the full saved
        # dict (only in OAuth mode) — we mutate + re-save it on refresh.
        self.bearer = bearer
        self.api_key = api_key
        self._bundle = token_bundle or {}

    @property
    def is_oauth(self):
        return self.bearer is not None

    def ensure_fresh(self):
        """If we're in OAuth mode and the access token is expired/near-expiry,
        refresh it via the refresh_token and re-save the bundle.

        TOKEN LIFECYCLE (the bit the README promises "auto-refreshes"):
            load bundle (in resolve_auth)
              -> on each api_get(): ensure_fresh()
                   -> if (obtained_at + expires_in - skew) <= now AND we have a
                      refresh_token: POST refresh grant -> new access_token +
                      expires_in -> update self.bearer + bundle -> save 0600.
              -> attach `Authorization: Bearer <self.bearer>` to the request.
        API-key mode is a no-op here (keys don't expire).
        """
        if not self.is_oauth:
            return  # API keys never expire — nothing to refresh.

        obtained_at = self._bundle.get("obtained_at", 0)
        expires_in = self._bundle.get("expires_in", 0)
        # Seconds of remaining validity. If we don't know (missing fields), assume
        # it's stale and try to refresh.
        expiry_epoch = obtained_at + expires_in
        seconds_left = expiry_epoch - int(time.time())

        if seconds_left > TOKEN_REFRESH_SKEW_SEC:
            return  # Still comfortably valid — keep using the current bearer.

        refresh_token = self._bundle.get("refresh_token", "")
        if not refresh_token:
            # We can't refresh without a refresh_token. The current access token
            # may already be dead; let the call proceed and surface a clean
            # BadCredential (which the caller turns into "re-run youtube_login").
            return

        # --- Refresh grant: trade refresh_token for a fresh access_token -----
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._bundle.get("client_id", ""),
        }
        # client_secret is optional for installed apps; only send if present.
        if self._bundle.get("client_secret"):
            form["client_secret"] = self._bundle["client_secret"]

        body = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            GOOGLE_TOKEN_ENDPOINT, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                refreshed = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # A failed refresh (e.g. the user revoked access, or the refresh
            # token expired after long inactivity) is an auth failure -> the
            # creator must log in again. Map to BadCredential so main()'s exit-3
            # path fires with a "re-run youtube_login.py" hint.
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            raise BadCredential(
                f"Couldn't refresh your YouTube login (HTTP {e.code}: "
                f"{detail[:200] or e.reason}). Your saved session may have been "
                f"revoked or expired. Re-run `python3 scripts/youtube_login.py`."
            )
        except urllib.error.URLError as e:
            # Network blip during refresh — surface as a generic runtime error
            # (the api_get retry loop won't help here since this is a separate
            # endpoint, so we fail the run cleanly).
            raise RuntimeError(f"Network error refreshing OAuth token: {e.reason}")

        new_access = refreshed.get("access_token")
        if not new_access:
            raise BadCredential(
                "Token refresh returned no access_token. Re-run "
                "`python3 scripts/youtube_login.py`."
            )

        # --- Update in-memory + on-disk bundle -------------------------------
        self.bearer = new_access
        self._bundle["access_token"] = new_access
        self._bundle["expires_in"] = refreshed.get("expires_in", 0)
        self._bundle["obtained_at"] = int(time.time())
        # A refresh response usually does NOT include a new refresh_token; keep
        # the existing one. If it does include one, honour it.
        if refreshed.get("refresh_token"):
            self._bundle["refresh_token"] = refreshed["refresh_token"]
        _save_token_bundle(self._bundle)
        print("  (refreshed your YouTube access token)")


def _save_token_bundle(bundle):
    """Re-write ~/.honest-comments/youtube_token.json with 0600 perms.

    Used after a refresh so the next run starts from the fresh token. Mirrors the
    save logic in youtube_login.py (kept simple/independent to avoid importing it
    — fetch_comments.py must run standalone even if youtube_login.py is absent).
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        fd = os.open(TOKEN_STORE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=2)
        os.chmod(TOKEN_STORE_PATH, 0o600)
    except OSError:
        # If we can't persist the refreshed token, the run still continues with
        # the in-memory bearer; we just won't have cached it for next time.
        pass


def load_oauth_token():
    """Load the saved OAuth token bundle, or return None if there isn't one.

    Returns the parsed dict from ~/.honest-comments/youtube_token.json (must
    contain at least an access_token). Returns None if the file is missing,
    unreadable, malformed, or has no access_token — so the caller can cleanly
    fall back to the API-key path.
    """
    if not os.path.isfile(TOKEN_STORE_PATH):
        return None
    try:
        with open(TOKEN_STORE_PATH, "r", encoding="utf-8") as fh:
            bundle = json.load(fh)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not bundle.get("access_token"):
        return None
    return bundle


def resolve_auth(cli_key):
    """Resolve the credential to use: OAuth token (preferred) > API key.

    Returns an Auth object. Resolution order:
      1. OAuth bundle at ~/.honest-comments/youtube_token.json (from
         youtube_login.py). Preferred — supports --mine and reads the creator's
         own data.
      2. API key: --api-key > $YOUTUBE_API_KEY > ./.env  (public-data fallback).
      3. Neither -> NotAuthenticated (exit 5; tells the agent to log in first).

    We NEVER log the token or key.
    """
    # 1. OAuth first.
    bundle = load_oauth_token()
    if bundle:
        return Auth(bearer=bundle["access_token"], token_bundle=bundle)

    # 2. API-key fallback.
    api_key = _resolve_api_key(cli_key)
    if api_key:
        return Auth(api_key=api_key)

    # 3. Nothing — not authenticated.
    raise NotAuthenticated(
        "Not signed in and no API key found.\n"
        "  Preferred: run `python3 scripts/youtube_login.py` to sign in to "
        "YouTube (opens your browser, one click).\n"
        "  Or (public comments only): pass --api-key, set $YOUTUBE_API_KEY, or "
        "put YOUTUBE_API_KEY=... in a .env file."
    )


def _resolve_api_key(cli_key):
    """Resolve the API key in priority order: --api-key > env > ./.env file.

    Returns the key string, or None if none is configured (so resolve_auth can
    decide whether that's fatal). We NEVER hardcode and NEVER log the key. The
    env-var / .env paths are preferred so the key doesn't land in shell history
    or chat logs. The key is the creator's own and only ever lives on their
    machine.
    """
    # 1. Explicit CLI flag wins.
    if cli_key:
        return cli_key.strip()

    # 2. Environment variable (the recommended path — keeps it out of logs).
    env_key = os.environ.get("YOUTUBE_API_KEY")
    if env_key:
        return env_key.strip()

    # 3. A simple .env file in the current working directory. We parse it by
    #    hand (no python-dotenv dependency) — just look for the one line we
    #    care about. Tolerates `export KEY=...`, quotes, and inline comments.
    env_path = os.path.join(os.getcwd(), ".env")
    if os.path.isfile(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Allow an optional leading `export `.
                    if line.startswith("export "):
                        line = line[len("export "):]
                    if line.startswith("YOUTUBE_API_KEY="):
                        val = line.split("=", 1)[1].strip()
                        # Parse the value, tolerating optional surrounding quotes
                        # AND a trailing ` #...` inline comment. The order matters
                        # because a QUOTED value may legally contain a `#` that is
                        # NOT a comment, e.g.  KEY="ab#cd" # real comment.
                        if val and val[0] in "\"'":
                            # Quoted: take everything up to the matching closing
                            # quote; anything after it (incl. a trailing comment)
                            # is discarded. This keeps a `#` INSIDE the quotes.
                            quote = val[0]
                            end = val.find(quote, 1)
                            if end != -1:
                                val = val[1:end]
                            else:
                                # No closing quote — treat the rest as the value
                                # minus the opening quote (best-effort).
                                val = val[1:]
                        elif " #" in val:
                            # Unquoted: strip a trailing ` #...` inline comment.
                            # We require a SPACE before the `#` so a `#` that's
                            # actually part of the key isn't chopped mid-value
                            # (`KEY=abc#def` stays intact; `KEY=abc # note` -> abc).
                            val = val.split(" #", 1)[0].strip()
                        if val:
                            return val
        except OSError:
            # If the .env can't be read, fall through.
            pass

    # Nothing found.
    return None


# ---------------------------------------------------------------------------
# Low-level HTTP — the single choke-point for every API call.
# ---------------------------------------------------------------------------

def api_get(endpoint, params, auth):
    """GET <API_BASE>/<endpoint>?<params> authenticated via `auth`, return JSON.

    This is the ONE place every YouTube API request flows through, so all the
    auth attachment AND error-classification logic lives here:

      AUTH (depends on auth.is_oauth):
        * OAuth bearer mode: call auth.ensure_fresh() (refresh if near-expiry),
          then send `Authorization: Bearer <token>` — NO `key=` param.
        * API-key mode: append `&key=<api_key>` to the query — NO auth header.

      ERROR CLASSIFICATION:
        * 400 / 403 keyInvalid / API-not-enabled / auth errors -> BadCredential
        * 403 quotaExceeded / dailyLimitExceeded                -> QuotaExceeded
        * 401 (expired/invalid OAuth token)                     -> BadCredential
        * 403 commentsDisabled                                  -> re-raised so
                                                                  the per-video
                                                                  loop can skip
        * transient 5xx / network errors                        -> retried

    The token/key is NEVER included in any log/print output.
    """
    # OAuth tokens can expire mid-run; refresh BEFORE building the request so the
    # bearer we attach below is guaranteed fresh. (No-op for API-key mode.)
    auth.ensure_fresh()

    # Copy so we don't mutate the caller's dict. In API-key mode we attach the
    # key as a query param; in OAuth mode we attach NOTHING here (the bearer goes
    # in a header below).
    query = dict(params)
    if not auth.is_oauth:
        query["key"] = auth.api_key
    url = endpoint if endpoint.startswith("http") else f"{API_BASE}/{endpoint}"
    full_url = f"{url}?{urllib.parse.urlencode(query)}"

    # In OAuth mode, the access token rides in the Authorization header.
    headers = {}
    if auth.is_oauth:
        headers["Authorization"] = f"Bearer {auth.bearer}"

    last_err = None
    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(full_url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)

        except urllib.error.HTTPError as e:
            # The API returns a JSON error body even on non-2xx. Parse it to
            # find the specific "reason" so we can react precisely.
            raw = ""
            try:
                raw = e.read().decode("utf-8")
            except Exception:
                pass
            reason, message = _extract_api_error(raw)

            # --- 401: an expired / invalid OAuth bearer token ---------------
            # API-key calls don't return 401 (a bad key is 400/403), so a 401
            # here means the OAuth access token was rejected. ensure_fresh()
            # already tried to keep it valid, so reaching here means refresh
            # failed or wasn't possible -> tell the creator to log in again.
            if e.code == 401:
                raise BadCredential(
                    (message or "your YouTube login was rejected (401).")
                    + " Re-run `python3 scripts/youtube_login.py` to sign in again."
                )

            # --- 403s carry the most meaningful reasons ---------------------
            if e.code == 403:
                if reason in ("quotaExceeded", "dailyLimitExceeded",
                              "rateLimitExceeded", "userRateLimitExceeded"):
                    # Quota / rate ceiling. Don't retry — it won't recover today.
                    raise QuotaExceeded(message or reason)
                if reason == "commentsDisabled":
                    # Bubble up so the per-video loop skips just this video.
                    raise CommentsDisabled(message or "comments are disabled")
                if reason in ("forbidden", "accessNotConfigured",
                              "keyInvalid", "ipRefererBlocked",
                              "authError", "insufficientPermissions"):
                    # Key restricted, API not enabled, key plain wrong, OR an
                    # OAuth token without the right scope.
                    raise BadCredential(message or reason)
                # Unknown 403 — treat as a bad-credential-ish fatal with context.
                raise BadCredential(message or f"403 {reason}".strip())

            # --- 400 almost always means a malformed/invalid credential ----
            if e.code == 400:
                if reason in ("keyInvalid", "badRequest"):
                    raise BadCredential(message or "invalid credential (HTTP 400)")
                raise BadCredential(message or f"HTTP 400 {reason}".strip())

            # --- 404: the requested resource doesn't exist -----------------
            if e.code == 404:
                raise ChannelNotFound(message or "resource not found (404)")

            # --- 5xx: transient server error — retry with backoff ----------
            if 500 <= e.code < 600 and attempt < HTTP_MAX_RETRIES:
                last_err = e
                time.sleep(HTTP_RETRY_BACKOFF_SEC * attempt)
                continue

            # Anything else: surface a generic error.
            raise RuntimeError(
                f"YouTube API HTTP {e.code} on {endpoint}: {message or raw[:200]}"
            )

        except urllib.error.URLError as e:
            # DNS / connection / timeout — transient, retry with backoff.
            last_err = e
            if attempt < HTTP_MAX_RETRIES:
                time.sleep(HTTP_RETRY_BACKOFF_SEC * attempt)
                continue
            raise RuntimeError(f"Network error calling {endpoint}: {e.reason}")

    # Exhausted retries on a transient error.
    raise RuntimeError(f"Failed to call {endpoint} after {HTTP_MAX_RETRIES} "
                       f"attempts: {last_err}")


def _extract_api_error(raw_body):
    """Pull (reason, human_message) out of a YouTube API JSON error body.

    The shape is: {"error": {"code": 403, "message": "...",
                             "errors": [{"reason": "quotaExceeded", ...}]}}
    Returns ("", "") if it can't be parsed so callers can fall back gracefully.
    """
    try:
        data = json.loads(raw_body)
        err = data.get("error", {})
        message = err.get("message", "")
        errors = err.get("errors", [])
        reason = errors[0].get("reason", "") if errors else ""
        return reason, message
    except Exception:
        return "", ""


# ---------------------------------------------------------------------------
# Channel resolution -> uploads playlist -> video IDs
# ---------------------------------------------------------------------------

def resolve_channel_uploads_playlist(channel_arg, auth):
    """Resolve a channel handle/ID/vanity URL to its uploads playlist ID.

    Returns (uploads_playlist_id, channel_title, channel_id).

    The uploads playlist is where EVERY public upload for a channel lives. We
    always read it from the API's contentDetails.relatedPlaylists.uploads
    rather than string-munging the channel ID (UC.. -> UU..) — string-munging
    works most of the time but the API value is authoritative.
    """
    # Normalise: the creator might paste a full URL, a handle, or a raw ID.
    handle, channel_id, username = _parse_channel_arg(channel_arg)

    # Build the channels.list query depending on what we extracted.
    # part=contentDetails gives us the uploads playlist; snippet gives a title.
    params = {"part": "contentDetails,snippet"}
    if channel_id:
        # Direct UC... id — cheapest, most reliable (1 unit).
        params["id"] = channel_id
    elif handle:
        # @handle — forHandle is the modern resolver (1 unit).
        params["forHandle"] = handle
    elif username:
        # Legacy /user/Name vanity — forUsername (1 unit). Often returns empty
        # for modern channels; we fall back to search below if so.
        params["forUsername"] = username
    else:
        raise ChannelNotFound(f"Couldn't understand channel: {channel_arg!r}")

    data = api_get("channels", params, auth)
    items = data.get("items", [])

    # Fallback when the direct resolver found nothing. TWO cases land here:
    #   1. Legacy /user/Name vanity — forUsername frequently misses for modern
    #      channels.
    #   2. /c/Vanity legacy custom URLs AND bare names — these arrive as
    #      `handle` (see _parse_channel_arg) and are tried via forHandle, but a
    #      legacy custom-URL vanity name often DIFFERS from the channel's actual
    #      @handle, so forHandle returns nothing. The README promises "channel
    #      URL" works, so before giving up we resolve the name via search.list.
    # Both fall through to the same search.list lookup (costs 100 quota units;
    # used only as a last resort because of that cost).
    if not items and (username or handle):
        search_name = username or handle.lstrip("@")
        print(f"  ! Direct lookup found nothing for {search_name!r}; "
              f"falling back to search.list (the expensive call — drawing from "
              f"the search quota bucket, used only as a last resort).")
        channel_id = _search_channel_id(search_name, auth)
        data = api_get("channels",
                       {"part": "contentDetails,snippet", "id": channel_id},
                       auth)
        items = data.get("items", [])

    if not items:
        raise ChannelNotFound(
            f"No channel matched {channel_arg!r}. Double-check the handle/ID/URL."
        )

    item = items[0]
    uploads = (item.get("contentDetails", {})
                   .get("relatedPlaylists", {})
                   .get("uploads"))
    if not uploads:
        raise ChannelNotFound(
            f"Channel {channel_arg!r} has no uploads playlist (no public videos?)."
        )

    title = item.get("snippet", {}).get("title", "channel")
    cid = item.get("id", channel_id or "")
    return uploads, title, cid


def resolve_my_channel_uploads_playlist(auth):
    """Resolve the SIGNED-IN creator's OWN uploads playlist via mine=true.

    Returns (uploads_playlist_id, channel_title, channel_id) — same shape as
    resolve_channel_uploads_playlist, so the rest of main() doesn't care which
    path produced it.

    OAUTH-ONLY: `channels.list?mine=true` means "the channel of the user whose
    OAuth token this is". An API key has no associated user, so this is
    meaningless (and would 400) with a key — main() guards --mine to OAuth before
    we ever get here, but we assert it again for safety.

    This is what makes the logged-in experience handle-free: the creator never
    pastes their channel; we just ask "give me MY channel's uploads playlist".
    """
    if not auth.is_oauth:
        # Defensive: main() should have already raised MineRequiresOAuth.
        raise MineRequiresOAuth(
            "--mine needs an OAuth login (it reads YOUR signed-in channel). Run "
            "`python3 scripts/youtube_login.py` first."
        )

    # part=contentDetails -> uploads playlist; snippet -> channel title.
    # mine=true scopes the lookup to the token's own channel (1 quota unit).
    data = api_get("channels",
                   {"part": "contentDetails,snippet", "mine": "true"},
                   auth)
    items = data.get("items", [])
    if not items:
        raise ChannelNotFound(
            "Your Google account doesn't appear to have a YouTube channel. "
            "Make sure you signed in with the Google account that owns your "
            "channel (re-run youtube_login.py if needed)."
        )

    item = items[0]
    uploads = (item.get("contentDetails", {})
                   .get("relatedPlaylists", {})
                   .get("uploads"))
    if not uploads:
        raise ChannelNotFound(
            "Your channel resolved but has no uploads playlist (no public videos?)."
        )

    title = item.get("snippet", {}).get("title", "my-channel")
    cid = item.get("id", "")
    return uploads, title, cid


def _parse_channel_arg(channel_arg):
    """Classify a channel argument into (handle, channel_id, username).

    Exactly one of the three is non-empty. Handles:
      - @SomeCreator                              -> handle
      - https://youtube.com/@SomeCreator          -> handle
      - UCxxxxxxxxxxxxxxxxxxxxxx (24 chars, UC..) -> channel_id
      - https://youtube.com/channel/UC...         -> channel_id
      - https://youtube.com/c/Name  (vanity)      -> treated as handle-ish/username
      - https://youtube.com/user/Name (legacy)    -> username
    """
    arg = channel_arg.strip()

    # Strip a URL down to its meaningful path/handle portion.
    if arg.startswith("http://") or arg.startswith("https://"):
        parsed = urllib.parse.urlparse(arg)
        path = parsed.path.strip("/")
        parts = [p for p in path.split("/") if p]   # drop empty segments
        # BUG GUARD: a copied channel URL almost always carries a trailing tab
        # segment — youtube.com/@SomeCreator/videos, /@Name/streams, /UC.../about.
        # We must classify on the FIRST path segment only; if we returned the raw
        # path we'd hand YouTube the handle "@SomeCreator/videos" (with the slash),
        # which forHandle rejects -> the channel "doesn't resolve". So everything
        # below keys off parts[0] (the identity) and ignores parts[1:] (the tab).
        if not parts:
            raise ChannelNotFound(f"Couldn't parse channel URL: {arg!r}")
        first = parts[0]
        # /@handle  (the modern URL shape)
        if first.startswith("@"):
            return first, "", ""
        # /channel/UC... , /user/LegacyName , /c/Vanity  (two-segment shapes)
        if len(parts) >= 2:
            kind, value = parts[0], parts[1]
            if kind == "channel":
                return "", value, ""         # /channel/UC...
            if kind == "user":
                return "", "", value         # /user/LegacyName
            if kind in ("c", "@"):
                return value, "", ""         # /c/Vanity -> try as handle
        # Single bare segment (e.g. youtube.com/SomeName) -> treat as a handle.
        return first, "", ""

    # Not a URL.
    if arg.startswith("@"):
        return arg, "", ""                   # @handle
    if arg.startswith("UC") and len(arg) == 24:
        return "", arg, ""                   # raw channel ID
    # Bare name with no @ — assume it's a handle (forHandle tolerates no @).
    return arg, "", ""


def _search_channel_id(query, auth):
    """Last-resort channel lookup via search.list. THE EXPENSIVE CALL.

    Only used when forUsername/forHandle return nothing for a legacy vanity
    name or /c/ custom URL. search.list draws from its own (much smaller)
    search quota bucket — treat it as the one pricey call and avoid it unless
    nothing else resolves the channel. We warn the caller before spending it.
    """
    data = api_get("search",
                   {"part": "snippet", "type": "channel", "q": query,
                    "maxResults": 1},
                   auth)
    items = data.get("items", [])
    if not items:
        raise ChannelNotFound(f"Search found no channel for {query!r}.")
    return items[0]["snippet"]["channelId"]


def get_uploads_total(uploads_playlist_id, auth):
    """Return the TOTAL number of videos in a channel's uploads playlist.

    Used by --dry-run to estimate scope without paging every video ID. We do a
    single playlistItems.list call (1 quota unit) with maxResults=1 and read
    `pageInfo.totalResults` — that field reports the full playlist size even
    though we only asked for one item. Returns 0 if the field is missing.
    """
    data = api_get("playlistItems",
                   {"part": "contentDetails",
                    "playlistId": uploads_playlist_id,
                    "maxResults": 1},
                   auth)  # 1 quota unit
    return data.get("pageInfo", {}).get("totalResults", 0)


def list_uploads_video_ids(uploads_playlist_id, auth, max_videos):
    """Page playlistItems.list to collect the channel's video IDs.

    Returns a list of dicts: [{"video_id", "title", "published_at"}, ...]
    newest-first (the uploads playlist is already in reverse-chronological
    order). Stops once we've collected `max_videos` (default scope guard so we
    don't accidentally pull a 1,000-video back-catalogue — a quota bomb).
    """
    videos = []
    page_token = None

    while True:
        params = {
            "part": "contentDetails,snippet",
            "playlistId": uploads_playlist_id,
            "maxResults": PLAYLIST_ITEMS_PER_PAGE,
        }
        if page_token:
            params["pageToken"] = page_token

        data = api_get("playlistItems", params, auth)  # 1 quota unit/page

        for item in data.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            if not vid:
                continue
            snip = item.get("snippet", {})
            videos.append({
                "video_id": vid,
                "title": snip.get("title", ""),
                # contentDetails.videoPublishedAt is the real upload time;
                # snippet.publishedAt is when it was added to the playlist.
                "published_at": (item.get("contentDetails", {})
                                     .get("videoPublishedAt")
                                 or snip.get("publishedAt", "")),
            })
            if max_videos and len(videos) >= max_videos:
                return videos[:max_videos]

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return videos


# ---------------------------------------------------------------------------
# Explicit video list (--videos) parsing
# ---------------------------------------------------------------------------

def parse_video_ids(videos_arg):
    """Parse the --videos argument into a list of bare 11-char video IDs.

    Accepts a comma- (or whitespace-) separated mix of:
      - full watch URLs:   https://www.youtube.com/watch?v=abc123XYZ01
      - short URLs:        https://youtu.be/abc123XYZ01
      - shorts URLs:       https://www.youtube.com/shorts/abc123XYZ01
      - embed URLs:        https://www.youtube.com/embed/abc123XYZ01
      - bare IDs:          abc123XYZ01
    Dedupes while preserving order. Unparseable tokens are skipped with a warn.
    """
    raw_tokens = []
    for chunk in videos_arg.replace("\n", ",").split(","):
        raw_tokens.extend(chunk.split())  # also split on whitespace

    ids = []
    seen = set()
    for tok in raw_tokens:
        tok = tok.strip()
        if not tok:
            continue
        vid = _extract_video_id(tok)
        if not vid:
            print(f"  ! Skipping unrecognised video token: {tok!r}")
            continue
        if vid not in seen:
            seen.add(vid)
            ids.append(vid)
    return ids


def _extract_video_id(token):
    """Extract an 11-char video ID from a URL or bare token, else return None."""
    # Bare ID: exactly 11 URL-safe chars and not a URL.
    if "/" not in token and "?" not in token and len(token) == YOUTUBE_ID_LEN:
        return token

    if token.startswith("http://") or token.startswith("https://"):
        parsed = urllib.parse.urlparse(token)
        host = parsed.netloc.lower()
        path = parsed.path

        # youtu.be/<id>
        if "youtu.be" in host:
            cand = path.strip("/").split("/")[0]
            # Only accept a well-formed 11-char ID; anything else is unparseable.
            return cand if len(cand) == YOUTUBE_ID_LEN else None

        # youtube.com/watch?v=<id>   (parse the query string once)
        query = urllib.parse.parse_qs(parsed.query)
        if "v" in query and query["v"]:
            cand = query["v"][0]
            return cand if len(cand) == YOUTUBE_ID_LEN else None

        # youtube.com/shorts/<id>  or  /embed/<id>  or  /live/<id>  or  /v/<id>
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
            cand = parts[1]
            return cand if len(cand) == YOUTUBE_ID_LEN else None

    return None


# ---------------------------------------------------------------------------
# Comment fetching — the core loop
# ---------------------------------------------------------------------------

def fetch_video_comments(video_id, auth, per_video_cap, include_replies):
    """Page commentThreads.list for one video, return a list of kept-field dicts.

    YouTube API call shape (per page, costs 1 quota unit):
        GET /youtube/v3/commentThreads
            ?part=snippet[,replies]
            &videoId=<video_id>
            &maxResults=100
            &order=relevance         # YouTube's own "top comments" ordering —
                                     # front-loads the signal so the first pages
                                     # already hold the comments worth analysing
            &textFormat=plainText    # strip HTML so the text is clean for an LLM

    We follow nextPageToken until exhausted OR until we hit `per_video_cap`
    top-level comments (default cap avoids paying to fetch the long emoji /
    "first" tail — order=relevance means the signal is already up front).

    Raises CommentsDisabled if the video has comments turned off (caller skips).
    Top-level only by default; --include-replies also captures reply chains.
    """
    collected = []
    page_token = None
    # part determines what the API returns. Adding "replies" makes the API
    # include up to ~5 inline replies per thread for free (no extra quota),
    # but replies are mostly creator-replies / reply-chain noise, so off by
    # default.
    part = "snippet,replies" if include_replies else "snippet"

    while True:
        params = {
            "part": part,
            "videoId": video_id,
            "maxResults": COMMENTS_PER_PAGE,
            "order": "relevance",
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token

        # This is the call that can raise CommentsDisabled / QuotaExceeded /
        # BadApiKey — all intentionally allowed to propagate to the right
        # handler in the per-video loop. ONE exception we translate here: a 404
        # videoNotFound surfaces from api_get() as ChannelNotFound (api_get is
        # channel-agnostic — any 404 maps to that type). For an explicit/private/
        # deleted video ID that's really "this video is unavailable", so we
        # re-raise it as VideoUnavailable. WHY: the per-video loop must record a
        # single bad video and continue; an uncaught ChannelNotFound here would
        # traceback and abort ALL output for one bad ID (P0-3).
        try:
            data = api_get("commentThreads", params, auth)  # 1 quota unit/page
        except ChannelNotFound as e:
            raise VideoUnavailable(str(e) or "video not found / not accessible")

        for thread in data.get("items", []):
            # The top-level comment lives at snippet.topLevelComment.snippet.
            top = (thread.get("snippet", {})
                        .get("topLevelComment", {})
                        .get("snippet", {}))
            collected.append(_extract_comment_fields(
                comment_id=thread.get("snippet", {})
                                 .get("topLevelComment", {}).get("id", ""),
                snip=top,
                video_id=video_id,
                total_replies=thread.get("snippet", {}).get("totalReplyCount", 0),
                is_reply=False,
            ))

            # Optionally flatten the inline reply objects too.
            if include_replies:
                for reply in thread.get("replies", {}).get("comments", []):
                    r_snip = reply.get("snippet", {})
                    collected.append(_extract_comment_fields(
                        comment_id=reply.get("id", ""),
                        snip=r_snip,
                        video_id=video_id,
                        total_replies=0,
                        is_reply=True,
                    ))

            # Cap is measured on TOP-LEVEL comments only (replies are bonus).
            top_level_count = sum(1 for c in collected if not c["is_reply"])
            if per_video_cap and top_level_count >= per_video_cap:
                return collected

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return collected


def _extract_comment_fields(comment_id, snip, video_id, total_replies, is_reply):
    """Strip a comment snippet down to ONLY the fields we keep.

    We discard everything else to keep the JSON small for the downstream LLM.
    author_channel_id is the dedupe / distinct-person key (display names
    collide; channel IDs don't), which is why we keep it even though it's not
    shown to the creator.
    """
    return {
        "comment_id": comment_id,
        "video_id": snip.get("videoId", video_id),
        # textOriginal is the raw author text (we requested plainText format).
        "text": snip.get("textOriginal", snip.get("textDisplay", "")),
        "author": snip.get("authorDisplayName", ""),
        # authorChannelId is a nested {"value": "UC..."} object — guard for None.
        "author_channel_id": (snip.get("authorChannelId", {}) or {}).get("value", ""),
        "like_count": snip.get("likeCount", 0),
        "published_at": snip.get("publishedAt", ""),
        "reply_count": total_replies,
        "is_reply": is_reply,
    }


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------

def slugify(text):
    """Make a filesystem-safe slug for the output filename."""
    keep = []
    for ch in text.lower():
        if ch.isalnum():
            keep.append(ch)
        elif ch in (" ", "-", "_", "@"):
            keep.append("-")
        # drop everything else
    slug = "".join(keep).strip("-")
    # collapse runs of dashes
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "channel"


def write_outputs(out_dir, label, comments, run_meta):
    """Write comments_<label>_<timestamp>.json and run_meta.json into out_dir.

    Writing raw JSON to disk means the downstream agent can re-analyse without
    re-spending quota, and the creator owns their data locally.
    """
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    comments_path = os.path.join(out_dir, f"comments_{slugify(label)}_{stamp}.json")
    meta_path = os.path.join(out_dir, "run_meta.json")

    with open(comments_path, "w", encoding="utf-8") as fh:
        json.dump(comments, fh, ensure_ascii=False, indent=2)

    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(run_meta, fh, ensure_ascii=False, indent=2)

    return comments_path, meta_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser():
    """Define the argparse CLI. See module docstring for usage examples."""
    p = argparse.ArgumentParser(
        prog="fetch_comments.py",
        description="Fetch top-level YouTube comments (OAuth-first, API-key "
                    "fallback; stdlib only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 youtube_login.py            # one-time sign-in\n"
            "  python3 fetch_comments.py --mine    # YOUR channel (logged in)\n"
            "  python3 fetch_comments.py --channel @SomeCreator\n"
            "  python3 fetch_comments.py --videos "
            "'https://youtu.be/abc,watch?v=def,ghi123'\n"
            "  python3 fetch_comments.py --channel @X --api-key AIza...  # key fallback\n"
        ),
    )
    # Source: exactly one of --mine / --channel / --videos is required.
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--mine", action="store_true",
                     help="Use YOUR OWN channel (the one you signed into with "
                          "youtube_login.py). Resolves it via mine=true — no "
                          "handle needed. OAuth only (won't work with just an "
                          "API key). This is the natural default once logged in.")
    src.add_argument("--channel",
                     help="Channel handle (@name), channel ID (UC...), or "
                          "channel URL. Resolves the uploads playlist and "
                          "pages its videos.")
    src.add_argument("--videos",
                     help="Comma/space-separated list of video URLs / short "
                          "URLs / bare IDs. Skips channel resolution entirely.")

    p.add_argument("--api-key",
                   help="YouTube Data API v3 key for the PUBLIC-DATA FALLBACK "
                        "(used only if you haven't run youtube_login.py). Falls "
                        "back to $YOUTUBE_API_KEY, then ./.env. Can't use --mine.")
    p.add_argument("--max-videos", type=int, default=25,
                   help="When using --channel, cap how many of the NEWEST "
                        "videos to scan (default 25; 0 = all — quota bomb, "
                        "be careful).")
    p.add_argument("--per-video-cap", type=int, default=500,
                   help="Max top-level comments to fetch per video "
                        "(default 500; 0 = no cap).")
    p.add_argument("--include-replies", action="store_true",
                   help="Also capture the inline reply preview returned by the "
                        "same commentThreads call (off by default — replies are "
                        "mostly noise; this is NOT full reply chains).")
    p.add_argument("--out-dir", default="out",
                   help="Directory to write JSON output into (default ./out).")
    # --dry-run / --estimate: resolve the channel + count its uploads and print
    # an estimated quota cost, then EXIT WITHOUT fetching any comments. This is
    # what the agent runs first (README Step 2/3) to show the creator the scope
    # + cost and get a yes before spending quota on the real fetch.
    p.add_argument("--dry-run", "--estimate", action="store_true",
                   dest="dry_run",
                   help="Resolve the channel, count its uploads, print a video "
                        "count + estimated API-unit cost, then exit WITHOUT "
                        "fetching comments. Use this to confirm scope first.")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    # --- Validate numeric flags (must be integers >= 0) --------------------
    # argparse already enforces `type=int`, so the only bad values that reach
    # here are NEGATIVE. A negative cap is meaningless (and `--max-videos -1`
    # would silently behave like "no slicing" deep in list_uploads_video_ids),
    # so we reject it loudly with exit code 1 (generic usage error) rather than
    # let it produce surprising results.
    if args.max_videos < 0:
        print("ERROR: --max-videos must be 0 or a positive integer "
              "(0 = all videos).", file=sys.stderr)
        return 1
    if args.per_video_cap < 0:
        print("ERROR: --per-video-cap must be 0 or a positive integer "
              "(0 = no cap).", file=sys.stderr)
        return 1

    # --- Resolve auth: OAuth token (preferred) > API key -------------------
    # NotAuthenticated (no token AND no key) is exit 5 — the agent must run
    # youtube_login.py first (or supply an API key).
    try:
        auth = resolve_auth(args.api_key)
    except NotAuthenticated as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 5

    # --- --mine requires OAuth (it reads the signed-in user's channel) -----
    # An API key has no associated user, so mine=true is meaningless with one.
    # Fail fast with exit 1 (usage error) and a pointer to log in.
    if args.mine and not auth.is_oauth:
        print("ERROR: --mine needs an OAuth login (it reads YOUR signed-in "
              "channel), but you're using an API key. Run "
              "`python3 scripts/youtube_login.py` first, or target a channel "
              "explicitly with --channel.", file=sys.stderr)
        return 1

    # Tell the creator which credential we're using (no secrets printed).
    print("Auth: signed-in YouTube account (OAuth)." if auth.is_oauth
          else "Auth: public API key (no login — public comments only).")

    # --- Build the list of videos to scan ----------------------------------
    # video_meta maps video_id -> {"title", "published_at"} for the run report.
    video_meta = {}
    try:
        if args.videos:
            # Explicit-video path: cheapest, most targeted. No channel calls.
            label = "videos"
            video_ids = parse_video_ids(args.videos)
            if not video_ids:
                print("ERROR: --videos contained no recognisable video IDs.",
                      file=sys.stderr)
                return 4

            # --- Dry-run on the --videos path -------------------------------
            # No channel/playlist to read totals from here, so the "estimate" is
            # just the count of explicit IDs. Each video costs ~1+ commentThreads
            # units (1 per 100-comment page). Print + exit WITHOUT fetching.
            if args.dry_run:
                n = len(video_ids)
                print(f"\nDRY RUN — {n} explicitly-listed video(s) to scan.")
                print(f"  Estimated cost: ~{n}+ API units (>=1 commentThreads "
                      f"unit per video; more for videos with >100 comments).")
                print("  No comments fetched. Re-run without --dry-run to fetch.")
                return 0

            for vid in video_ids:
                video_meta[vid] = {"title": "", "published_at": ""}
            print(f"Scanning {len(video_ids)} explicitly-listed video(s).")
        else:
            # Channel path: resolve uploads playlist, then page video IDs.
            # Two sub-cases produce the SAME (uploads, title, id) shape:
            #   --mine     -> the signed-in creator's own channel (mine=true)
            #   --channel  -> an explicit handle / ID / URL
            if args.mine:
                print("Resolving YOUR signed-in channel (mine=true) ...")
                uploads, ch_title, ch_id = resolve_my_channel_uploads_playlist(auth)
            else:
                print(f"Resolving channel {args.channel!r} ...")
                uploads, ch_title, ch_id = resolve_channel_uploads_playlist(
                    args.channel, auth)
            label = ch_title
            max_v = args.max_videos if args.max_videos else 0
            print(f"  Channel: {ch_title} ({ch_id})")
            print(f"  Uploads playlist: {uploads}")

            # --- Dry-run on the --channel path ------------------------------
            # Read the uploads playlist's TOTAL video count (1 quota unit) and
            # print a scope + cost estimate, then EXIT before fetching any
            # comments. This is what the agent runs first (README Step 2/3) to
            # show the creator the size + cost and get a yes.
            if args.dry_run:
                total = get_uploads_total(uploads, auth)
                scanned = total if max_v == 0 else min(total, max_v)
                scope_desc = (f"newest {max_v}" if max_v else "ALL")
                print(f"\nDRY RUN — {ch_title} has {total} public upload(s).")
                print(f"  Scope: {scope_desc} -> would scan {scanned} video(s).")
                # Rough estimate: ~1 playlistItems unit per 50 videos paged +
                # >=1 commentThreads unit per video. We keep this deliberately
                # approximate (don't over-claim exact unit math).
                est = (scanned // PLAYLIST_ITEMS_PER_PAGE + 1) + scanned
                print(f"  Estimated cost: ~{est}+ of your 10,000 daily API units "
                      f"(>=1 commentThreads unit per video; more for videos with "
                      f">100 comments).")
                print("  No comments fetched. Re-run without --dry-run to fetch.")
                return 0

            scope_desc = (f"newest {max_v}" if max_v else "ALL")
            print(f"  Fetching {scope_desc} video IDs ...")
            vids = list_uploads_video_ids(uploads, auth, max_v)
            video_ids = [v["video_id"] for v in vids]
            for v in vids:
                video_meta[v["video_id"]] = {
                    "title": v["title"], "published_at": v["published_at"]}
            print(f"  Got {len(video_ids)} video(s).")

            # --- Empty-channel guard (P1-2) ---------------------------------
            # A brand-new / private / no-public-upload channel resolves fine but
            # yields zero video IDs. Without this guard we'd write empty JSON and
            # exit 0 with no explanation. Tell the creator there's nothing to
            # analyze and exit 4 (same family as "channel not found / no usable
            # source") so the README troubleshooting can name it.
            if not video_ids:
                print(f"ERROR: {ch_title!r} resolved, but it has no public "
                      f"uploads to analyze (empty channel, or all videos are "
                      f"private/unlisted). Nothing to fetch.", file=sys.stderr)
                return 4

            # Pre-flight quota sense-check (rough: ~1-5 calls/video typical).
            if max_v == 0 and len(video_ids) > 200:
                print(f"  ! NOTE: {len(video_ids)} videos is a lot — this can "
                      f"consume significant quota. Consider --max-videos.")
    except QuotaExceeded as e:
        print(f"ERROR: YouTube quota exceeded during setup: {e}\n"
              f"Quota resets at midnight US Pacific. Re-run then or scope smaller.",
              file=sys.stderr)
        return 2
    except BadCredential as e:
        # Bad API key OR an OAuth token that won't authorize / refresh.
        print(f"ERROR: authentication problem: {e}", file=sys.stderr)
        if auth.is_oauth:
            print("  Your sign-in may have expired or been revoked. Re-run "
                  "`python3 scripts/youtube_login.py`.", file=sys.stderr)
        else:
            print("  Check the API key, ensure YouTube Data API v3 is enabled "
                  "for its project, and remove any HTTP-referrer restriction.",
                  file=sys.stderr)
        return 3
    except MineRequiresOAuth as e:
        # Defensive — main() already guards --mine, but resolve_my_* re-checks.
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except ChannelNotFound as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 4

    # --- Fetch comments per video, tolerating per-video failures -----------
    all_comments = []
    comments_disabled = []   # videos we skipped because comments were off
    per_video_counts = {}    # video_id -> top-level comments fetched
    errors = []              # (video_id, message) for non-fatal errors
    quota_hit = False

    for idx, vid in enumerate(video_ids, start=1):
        title = video_meta.get(vid, {}).get("title", "")
        label_str = f"{title[:50]} " if title else ""
        print(f"[{idx}/{len(video_ids)}] {label_str}({vid}) ...", flush=True)
        try:
            comments = fetch_video_comments(
                vid, auth,
                per_video_cap=args.per_video_cap,
                include_replies=args.include_replies)
            top_level = sum(1 for c in comments if not c["is_reply"])
            per_video_counts[vid] = top_level
            all_comments.extend(comments)
            print(f"    -> {top_level} top-level comments"
                  + (f" (+{len(comments) - top_level} replies)"
                     if args.include_replies else ""))

        except CommentsDisabled:
            # One video having comments off must NOT kill the whole run.
            comments_disabled.append(vid)
            print("    -> comments disabled; skipping.")

        except VideoUnavailable as e:
            # P0-3: an invalid / private / deleted explicit video ID returns
            # 404 videoNotFound. That's a per-video problem, NOT a fatal one —
            # record it in run_meta's errors list and CONTINUE to the next
            # video. Without this, one bad ID on the --videos path would
            # traceback and abort all output for the whole batch.
            errors.append((vid, f"video unavailable: {e}"))
            print(f"    -> video unavailable (skipping): {e}", file=sys.stderr)

        except BadCredential as e:
            # P0-2: the --videos path does NOT run the channel-setup credential
            # probe (that only happens in resolve_*_uploads_playlist), so a
            # bad/disabled key OR an expired/unrefreshable OAuth token first
            # surfaces HERE, on the very first commentThreads call. It won't fix
            # itself by skipping to the next video — every call fails the same
            # way — so this is FATAL: stop cleanly and exit 3. We return directly
            # (nothing was fetched, so no partial output worth writing).
            if auth.is_oauth:
                print(f"ERROR: your YouTube sign-in was rejected: {e}\n"
                      f"  Re-run `python3 scripts/youtube_login.py` to sign in "
                      f"again, then retry.", file=sys.stderr)
            else:
                print(f"ERROR: API key problem: {e}\n"
                      f"  The key looks invalid / disabled / restricted. See "
                      f"README: enable the YouTube Data API v3 for the key's "
                      f"project, check the key is correct, and remove any "
                      f"HTTP-referrer restriction (this is a server-side call).",
                      file=sys.stderr)
            return 3

        except QuotaExceeded as e:
            # Stop cleanly and write whatever we already have (partial data).
            quota_hit = True
            print(f"    -> QUOTA EXCEEDED: {e}", file=sys.stderr)
            print("Stopping early; writing partial results.", file=sys.stderr)
            break

        except RuntimeError as e:
            # Transient/unknown per-video error — note it and keep going.
            errors.append((vid, str(e)))
            print(f"    -> error (skipping this video): {e}", file=sys.stderr)

    # --- Assemble the run-meta report --------------------------------------
    total_top_level = sum(1 for c in all_comments if not c["is_reply"])
    run_meta = {
        "generated_at": datetime.now().isoformat(),
        # Source kind: "videos" for explicit IDs, "mine" for the signed-in
        # creator's own channel, "channel" for an explicit handle/ID/URL.
        "source": ("videos" if args.videos
                   else "mine" if args.mine
                   else "channel"),
        # The literal arg the creator gave; for --mine there's no arg, so record
        # the resolved channel label instead so run_meta is self-describing.
        "source_arg": (args.videos or args.channel or f"mine:{label}"),
        "label": label,
        "videos_requested": len(video_ids),
        "videos_with_comments_fetched": len(per_video_counts),
        "total_comments_fetched": len(all_comments),
        "total_top_level_comments": total_top_level,
        "include_replies": args.include_replies,
        "per_video_counts": per_video_counts,
        "comments_disabled_video_ids": comments_disabled,
        "errors": [{"video_id": v, "message": m} for v, m in errors],
        "quota_exceeded": quota_hit,
        "video_meta": video_meta,
    }

    # --- Write outputs ------------------------------------------------------
    comments_path, meta_path = write_outputs(
        args.out_dir, label, all_comments, run_meta)

    # --- Final summary ------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"Done. {len(all_comments)} comments "
          f"({total_top_level} top-level) across "
          f"{len(per_video_counts)} video(s).")
    if comments_disabled:
        print(f"  {len(comments_disabled)} video(s) had comments disabled.")
    if errors:
        print(f"  {len(errors)} video(s) hit non-fatal errors (see run_meta).")
    print(f"  Comments -> {comments_path}")
    print(f"  Run meta -> {meta_path}")

    if quota_hit:
        print("\nWARNING: stopped early on quota. Data above is PARTIAL. "
              "Quota resets at midnight US Pacific.", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
