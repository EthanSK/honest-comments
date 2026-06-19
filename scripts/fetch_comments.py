#!/usr/bin/env python3
"""honest-comments — fetch_comments.py

Stdlib-only YouTube comment fetcher for the "honest-comments" tool. It pulls
top-level comments off a creator's videos using the YouTube Data API v3 with a
plain API KEY (public-data tier — NO OAuth). The raw comments land as JSON on
the creator's own disk so a downstream agent can classify/rank them later.

WHY API-KEY-ONLY (no OAuth):
    Public video comments are readable with a simple API key. That means no
    consent screen, no token refresh, no client_secret.json. A creator can be
    running in ~3 minutes. Trade-off we accept: can't read private/unlisted
    videos or comments held for review — that's fine, the constructive
    criticism we care about is on public uploads.

WHAT IT DOES:
    1. Resolves the API key (--api-key > $YOUTUBE_API_KEY > ./.env).
    2. Builds a list of video IDs from EITHER:
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
    - 1  bad usage (e.g. negative --max-videos / --per-video-cap).
    - 2  403 quotaExceeded -> stop cleanly, write partial data.
    - 3  400 / 403 bad/disabled/restricted key -> clear message. This fires on
         BOTH the channel path (key probed during setup) AND the --videos path
         (no setup probe runs there, so the key is first exercised inside the
         per-video fetch loop — see the BadApiKey guard there).
    - 4  channel not found / empty channel (no public uploads) / --videos had
         no usable IDs.
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
    # Whole channel by handle (newest 25 videos by default):
    python3 scripts/fetch_comments.py --channel @SomeCreator --api-key AIza...

    # Channel by ID, wider scope, key from env:
    export YOUTUBE_API_KEY=AIza...
    python3 scripts/fetch_comments.py --channel UCxxxxxxxxxxxxxxxxxxxxxx \
        --max-videos 50

    # Specific videos (URLs / short URLs / bare IDs, mixed) — skips channel
    # resolution entirely:
    python3 scripts/fetch_comments.py \
        --videos "https://youtu.be/abc123,https://www.youtube.com/watch?v=def456,ghi789xyz01"

    # Include reply chains too (off by default — replies are mostly noise):
    python3 scripts/fetch_comments.py --channel @SomeCreator --include-replies
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
# Custom exceptions — let us map specific API failure modes to clean exits.
# ---------------------------------------------------------------------------

class QuotaExceeded(Exception):
    """Raised when the API reports the daily quota is exhausted (403)."""


class BadApiKey(Exception):
    """Raised when the API key is missing/invalid/not-enabled (400/403)."""


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
# API key resolution
# ---------------------------------------------------------------------------

def resolve_api_key(cli_key):
    """Resolve the API key in priority order: --api-key > env > ./.env file.

    We NEVER hardcode and NEVER log the key. The env-var / .env paths are
    preferred so the key doesn't land in shell history or chat logs. The key
    is the creator's own and only ever lives on their machine.
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
            # If the .env can't be read, fall through to the error below.
            pass

    # Nothing found — fail clearly with guidance.
    raise BadApiKey(
        "No API key found. Pass --api-key, set the YOUTUBE_API_KEY env var, "
        "or put YOUTUBE_API_KEY=... in a .env file in this directory."
    )


# ---------------------------------------------------------------------------
# Low-level HTTP — the single choke-point for every API call.
# ---------------------------------------------------------------------------

def api_get(endpoint, params, api_key):
    """GET <API_BASE>/<endpoint>?<params>&key=<api_key> and return parsed JSON.

    This is the ONE place every YouTube API request flows through, so all the
    error-classification logic lives here:

      * 400 / 403 keyInvalid / API-not-enabled  -> BadApiKey
      * 403 quotaExceeded / dailyLimitExceeded   -> QuotaExceeded
      * 403 commentsDisabled                     -> re-raised so the per-video
                                                    loop can skip just that one
      * transient 5xx / network errors           -> retried with backoff

    The api_key is added here and is NEVER included in any log/print output.
    """
    # Copy so we don't mutate the caller's dict, then attach the key last.
    query = dict(params)
    query["key"] = api_key
    url = endpoint if endpoint.startswith("http") else f"{API_BASE}/{endpoint}"
    full_url = f"{url}?{urllib.parse.urlencode(query)}"

    last_err = None
    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(full_url, method="GET")
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
                              "keyInvalid", "ipRefererBlocked"):
                    # Key restricted, API not enabled, or key plain wrong.
                    raise BadApiKey(message or reason)
                # Unknown 403 — treat as a bad-key-ish fatal with context.
                raise BadApiKey(message or f"403 {reason}".strip())

            # --- 400 almost always means a malformed/invalid key here ------
            if e.code == 400:
                if reason in ("keyInvalid", "badRequest"):
                    raise BadApiKey(message or "invalid API key (HTTP 400)")
                raise BadApiKey(message or f"HTTP 400 {reason}".strip())

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

def resolve_channel_uploads_playlist(channel_arg, api_key):
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

    data = api_get("channels", params, api_key)
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
        channel_id = _search_channel_id(search_name, api_key)
        data = api_get("channels",
                       {"part": "contentDetails,snippet", "id": channel_id},
                       api_key)
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


def _search_channel_id(query, api_key):
    """Last-resort channel lookup via search.list. THE EXPENSIVE CALL.

    Only used when forUsername/forHandle return nothing for a legacy vanity
    name or /c/ custom URL. search.list draws from its own (much smaller)
    search quota bucket — treat it as the one pricey call and avoid it unless
    nothing else resolves the channel. We warn the caller before spending it.
    """
    data = api_get("search",
                   {"part": "snippet", "type": "channel", "q": query,
                    "maxResults": 1},
                   api_key)
    items = data.get("items", [])
    if not items:
        raise ChannelNotFound(f"Search found no channel for {query!r}.")
    return items[0]["snippet"]["channelId"]


def get_uploads_total(uploads_playlist_id, api_key):
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
                   api_key)  # 1 quota unit
    return data.get("pageInfo", {}).get("totalResults", 0)


def list_uploads_video_ids(uploads_playlist_id, api_key, max_videos):
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

        data = api_get("playlistItems", params, api_key)  # 1 quota unit/page

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

def fetch_video_comments(video_id, api_key, per_video_cap, include_replies):
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
            data = api_get("commentThreads", params, api_key)  # 1 quota unit/page
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
        description="Fetch top-level YouTube comments (API-key only, stdlib only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 fetch_comments.py --channel @SomeCreator --api-key AIza...\n"
            "  YOUTUBE_API_KEY=AIza... python3 fetch_comments.py "
            "--channel UCxxxx --max-videos 50\n"
            "  python3 fetch_comments.py --videos "
            "'https://youtu.be/abc,watch?v=def,ghi123'\n"
        ),
    )
    # Source: exactly one of --channel / --videos is required.
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--channel",
                     help="Channel handle (@name), channel ID (UC...), or "
                          "channel URL. Resolves the uploads playlist and "
                          "pages its videos.")
    src.add_argument("--videos",
                     help="Comma/space-separated list of video URLs / short "
                          "URLs / bare IDs. Skips channel resolution entirely.")

    p.add_argument("--api-key",
                   help="YouTube Data API v3 key. Falls back to "
                        "$YOUTUBE_API_KEY, then ./.env.")
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

    # --- Resolve the API key (clear failure if missing) --------------------
    try:
        api_key = resolve_api_key(args.api_key)
    except BadApiKey as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

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
            print(f"Resolving channel {args.channel!r} ...")
            uploads, ch_title, ch_id = resolve_channel_uploads_playlist(
                args.channel, api_key)
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
                total = get_uploads_total(uploads, api_key)
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
            vids = list_uploads_video_ids(uploads, api_key, max_v)
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
    except BadApiKey as e:
        print(f"ERROR: API key problem: {e}", file=sys.stderr)
        return 3
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
                vid, api_key,
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

        except BadApiKey as e:
            # P0-2: the --videos path does NOT run the channel-setup key probe
            # (that only happens in resolve_channel_uploads_playlist), so a
            # bad/disabled/referrer-restricted key first surfaces HERE, on the
            # very first commentThreads call. A bad key won't fix itself by
            # skipping to the next video — every call will fail the same way —
            # so this is FATAL: stop cleanly and exit 3 pointing at README Step
            # 1. We return directly (no partial output worth writing on a key
            # failure — nothing was fetched).
            print(f"ERROR: API key problem: {e}\n"
                  f"  The key looks invalid / disabled / restricted. See README "
                  f"Step 1: enable the YouTube Data API v3 for the key's project, "
                  f"check the key is correct, and remove any HTTP-referrer "
                  f"restriction (this is a server-side call, not a browser call).",
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
        "source": "channel" if args.channel else "videos",
        "source_arg": args.channel or args.videos,
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
