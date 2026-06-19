#!/usr/bin/env python3
"""honest-comments — youtube_login.py

Agent-driven Google OAuth 2.0 LOGIN for honest-comments, STDLIB-ONLY.

WHAT THIS DOES (the one-liner): the creator's agent runs this script, it opens
the creator's browser to Google's sign-in / consent screen, the creator clicks
"Allow" ONCE, and we save an OAuth token bundle to ~/.honest-comments/ that
fetch_comments.py then uses (and auto-refreshes) to read the creator's own
YouTube comments. No manual API-key step.

WHY OAUTH INSTEAD OF AN API KEY:
    The old flow used a plain Google Cloud API key (public-data tier). That
    works but it forces every creator to walk through the Cloud console, enable
    an API, and create+copy a key. OAuth flips it: the PROJECT OWNER (Ethan)
    does that one-time Cloud setup once, ships an OAuth "Desktop app" client,
    and every end-user just signs into their own Google account. With the
    `youtube.readonly` scope we can also resolve the signed-in creator's OWN
    channel via `mine=true` — no handle to paste.

THE OAUTH FLOW WE IMPLEMENT — "installed app" loopback + PKCE:
    This is Google's recommended flow for desktop/CLI apps that can't keep a
    secret (https://developers.google.com/identity/protocols/oauth2/native-app).
    The shape:

      1. Generate a PKCE code_verifier (random) + S256 code_challenge (its
         SHA-256, base64url-encoded). PKCE is what actually secures this flow —
         see the "client_secret isn't secret" note below.
      2. Bind a tiny http.server to 127.0.0.1 on an EPHEMERAL free port. That
         localhost URL is the OAuth redirect_uri Google will send the user back
         to after they approve.
      3. Build the Google authorization URL and open it in the creator's
         browser (and print it, in case the browser doesn't auto-open).
      4. The creator signs in + clicks Allow. Google redirects their browser to
         http://127.0.0.1:<port>/?code=<authorization_code>. Our local server
         captures that `code`, shows a tidy "you're signed in" page, and shuts
         down.
      5. We exchange that `code` (plus the PKCE code_verifier) at Google's token
         endpoint for an access_token + refresh_token.
      6. We save the token bundle to ~/.honest-comments/youtube_token.json with
         0600 perms so only the creator can read it.

    STATE MACHINE (also commented at each transition below):
        START
          -> AWAIT_REDIRECT   (server bound, browser opened, blocking for ?code)
          -> GOT_CODE         (local server captured the authorization code)
          -> EXCHANGED        (code traded for tokens at the token endpoint)
          -> SAVED            (token bundle written to disk, 0600)
        Any leg can fail -> we print a clear message + exit nonzero.

WHY "client_secret" IS NOT ACTUALLY SECRET HERE:
    For an "installed app" (Desktop) OAuth client, the client_secret is baked
    into a binary that ships to users, so Google explicitly treats it as NOT
    confidential. It's effectively a public identifier. The thing that actually
    protects the authorization-code exchange from interception is PKCE (the
    code_verifier never leaves this process; only its hash goes in the auth
    URL). So shipping the client_secret in this repo / a config file is fine and
    expected — do NOT treat it like a password.

STDLIB-ONLY: urllib (HTTP), http.server (loopback redirect catcher), webbrowser
(open the consent screen), json, hashlib + base64 + secrets (PKCE), os, sys,
argparse, time, threading. NOTHING to pip-install.

EXIT CODES (keep in sync with README "Owner / self-host setup" + troubleshooting):
    0   success — tokens saved to ~/.honest-comments/youtube_token.json.
    1   bad usage / unexpected internal error.
    2   no OAuth client configured (owner hasn't dropped in client_id/secret).
    3   the creator denied consent (Google returned error=access_denied).
    4   timed out waiting for the browser redirect (default 300s).
    5   the local loopback server couldn't bind a port.
    6   the token exchange HTTP call failed (network / Google error).
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------------------
# Constants — Google OAuth endpoints + our local config locations
# ---------------------------------------------------------------------------

# Google's OAuth 2.0 authorization endpoint (where we send the creator's
# browser to sign in + consent) and token endpoint (where we exchange the
# returned code, and later refresh).
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# The SENSITIVE scope we request: read-only access to the signed-in creator's
# YouTube data. This is what lets fetch_comments.py read their comments AND
# resolve their own channel via channels.list?mine=true. Because it's a
# "sensitive" scope, Google shows an "unverified app" interstitial until the
# OAuth app passes Google's verification — the README is honest about that.
OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"

# Where we persist everything, under the creator's HOME (NOT in the repo, so a
# `git clone` checkout never carries tokens). Both files live here:
#   client_config.json  — (optional) the owner's downloaded OAuth client JSON
#   youtube_token.json  — the saved access/refresh token bundle (0600)
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".honest-comments")
CLIENT_CONFIG_PATH = os.path.join(CONFIG_DIR, "client_config.json")
TOKEN_STORE_PATH = os.path.join(CONFIG_DIR, "youtube_token.json")

# ---------------------------------------------------------------------------
# OAuth CLIENT credentials — PLACEHOLDER constants.
#
# These are the LAST-RESORT fallback. Resolution priority (see resolve_client_config):
#   1. env vars  HONEST_COMMENTS_OAUTH_CLIENT_ID / _SECRET
#   2. ~/.honest-comments/client_config.json  (Google's downloaded "installed" shape)
#   3. these top-of-file constants
#
# The project OWNER (Ethan) must replace these (or use one of the higher-priority
# paths) with a real Google OAuth "Desktop app" client. See README
# "Owner / self-host setup". Until then the script prints a clear setup message
# and exits 2. Remember: for an installed app the client_secret is NOT truly
# secret (see module docstring) — it's safe to commit a real value here.
# ---------------------------------------------------------------------------
PLACEHOLDER_CLIENT_ID = "REPLACE_WITH_HONEST_COMMENTS_OAUTH_CLIENT_ID"
PLACEHOLDER_CLIENT_SECRET = "REPLACE_WITH_HONEST_COMMENTS_OAUTH_CLIENT_SECRET"


# ---------------------------------------------------------------------------
# Custom exceptions — map each failure mode to a clean exit code in main().
# ---------------------------------------------------------------------------

class NoClientConfig(Exception):
    """No usable OAuth client_id/secret could be resolved (-> exit 2)."""


class ConsentDenied(Exception):
    """The creator clicked "Deny"/cancelled at Google's consent screen (-> 3)."""


class RedirectTimeout(Exception):
    """No browser redirect arrived within the timeout window (-> 4)."""


class ServerBindError(Exception):
    """Couldn't bind the loopback HTTP server to a free port (-> 5)."""


class TokenExchangeError(Exception):
    """The code->token (or refresh) HTTP exchange failed (-> 6)."""


# ---------------------------------------------------------------------------
# OAuth client config resolution
# ---------------------------------------------------------------------------

def resolve_client_config():
    """Return (client_id, client_secret) for the OAuth "Desktop app" client.

    Priority order (first that yields a REAL, non-placeholder value wins):
      1. Env vars HONEST_COMMENTS_OAUTH_CLIENT_ID / HONEST_COMMENTS_OAUTH_CLIENT_SECRET.
         (Easiest for CI / quick self-host — nothing to write to disk.)
      2. ~/.honest-comments/client_config.json in Google's downloaded "installed"
         client shape: {"installed": {"client_id": "...", "client_secret": "...", ...}}.
         (This is literally the file Google's console hands you when you create a
         Desktop client and click "Download JSON" — drop it in unmodified.)
      3. The top-of-file PLACEHOLDER_* constants (only useful once the owner edits
         them to real values).

    Raises NoClientConfig if nothing resolves to a real value, so main() can
    print the owner-setup guidance and exit 2.
    """
    # 1. Environment variables.
    env_id = os.environ.get("HONEST_COMMENTS_OAUTH_CLIENT_ID", "").strip()
    env_secret = os.environ.get("HONEST_COMMENTS_OAUTH_CLIENT_SECRET", "").strip()
    if env_id and not _is_placeholder(env_id):
        # client_secret CAN legitimately be empty for some installed-app configs,
        # so we don't hard-require it here — Google still accepts the exchange
        # with PKCE. We pass whatever we have.
        return env_id, env_secret

    # 2. The downloaded "installed" client JSON.
    if os.path.isfile(CLIENT_CONFIG_PATH):
        try:
            with open(CLIENT_CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Google nests under "installed" (Desktop) — fall back to "web" or a
            # flat shape just in case the owner pasted a trimmed file.
            node = data.get("installed") or data.get("web") or data
            cid = str(node.get("client_id", "")).strip()
            csecret = str(node.get("client_secret", "")).strip()
            if cid and not _is_placeholder(cid):
                return cid, csecret
        except (OSError, ValueError, json.JSONDecodeError):
            # Malformed file — fall through to the constants / error below rather
            # than crash. (A clear "no client configured" message is friendlier
            # than a JSON traceback.)
            pass

    # 3. Top-of-file constants (only real if the owner edited them).
    if PLACEHOLDER_CLIENT_ID and not _is_placeholder(PLACEHOLDER_CLIENT_ID):
        return PLACEHOLDER_CLIENT_ID, PLACEHOLDER_CLIENT_SECRET

    # Nothing usable.
    raise NoClientConfig(
        "No honest-comments OAuth client is configured yet.\n\n"
        "This is the PROJECT OWNER'S one-time setup (end users never do this):\n"
        "  1. Create a Google Cloud project + enable the YouTube Data API v3.\n"
        "  2. Configure the OAuth consent screen with the\n"
        "     https://www.googleapis.com/auth/youtube.readonly scope.\n"
        "  3. Create an OAuth client of type \"Desktop app\".\n"
        "  4. Provide it to this script in ANY one of these ways:\n"
        "       - export HONEST_COMMENTS_OAUTH_CLIENT_ID=... and\n"
        "         export HONEST_COMMENTS_OAUTH_CLIENT_SECRET=...\n"
        f"       - drop the downloaded client JSON at {CLIENT_CONFIG_PATH}\n"
        "       - or edit the PLACEHOLDER_* constants at the top of\n"
        "         scripts/youtube_login.py\n\n"
        "See the README \"Owner / self-host setup\" section for the full walkthrough."
    )


def _is_placeholder(value):
    """True if `value` is still one of our REPLACE_WITH_* placeholder tokens."""
    return value.startswith("REPLACE_WITH_")


# ---------------------------------------------------------------------------
# PKCE — Proof Key for Code Exchange (RFC 7636)
# ---------------------------------------------------------------------------

def generate_pkce_pair():
    """Return (code_verifier, code_challenge) for the S256 PKCE method.

    - code_verifier: a high-entropy URL-safe random string (43-128 chars). It
      NEVER leaves this process until the token exchange; it's the secret half.
    - code_challenge: base64url(SHA256(code_verifier)), with '=' padding
      stripped (Google rejects padded challenges). This is the half that goes in
      the public authorization URL. Because an attacker who intercepts the auth
      URL only sees the HASH, they can't reconstruct the verifier needed to
      redeem the code — that's the whole point of PKCE, and why an installed
      app's "secret" not being secret is acceptable.
    """
    # 32 random bytes -> ~43 url-safe chars (no padding). Comfortably inside the
    # RFC's 43-128 char window.
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# ---------------------------------------------------------------------------
# Authorization-URL construction
# ---------------------------------------------------------------------------

def build_auth_url(client_id, redirect_uri, code_challenge, state):
    """Build the Google OAuth 2.0 authorization URL the browser is sent to.

    Parameters we set and WHY:
      - response_type=code          we want an authorization code (then we
                                    exchange it server-side for tokens).
      - client_id                   identifies the honest-comments OAuth app.
      - redirect_uri                the loopback URL Google bounces the browser
                                    back to (http://127.0.0.1:<ephemeral-port>/).
      - scope                       youtube.readonly (see OAUTH_SCOPE).
      - code_challenge / _method    the PKCE public half (S256).
      - access_type=offline         REQUIRED to receive a refresh_token (so we
                                    can auto-refresh later without re-prompting).
      - prompt=consent              force the consent screen every time so Google
                                    reliably re-issues a refresh_token. (Google
                                    only returns a refresh_token on the FIRST
                                    consent unless you force the prompt — and
                                    re-logins would otherwise silently lack one.)
      - state                       random anti-CSRF token; we verify the
                                    redirect echoes it back unchanged.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return GOOGLE_AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# Loopback redirect catcher — a one-shot localhost HTTP server.
# ---------------------------------------------------------------------------

class _RedirectCatcher(BaseHTTPRequestHandler):
    """Handle the single GET that Google's redirect makes to our loopback URL.

    Google sends the creator's browser to:
        http://127.0.0.1:<port>/?code=<auth_code>&state=<state>
      OR, on denial:
        http://127.0.0.1:<port>/?error=access_denied&state=<state>

    We stash the result on the server object (`server.auth_result`) so the main
    thread can read it after the server stops, then we respond with a small HTML
    page telling the creator to return to their agent. We deliberately handle
    only the ONE expected request and ignore favicon/etc.
    """

    # Silence the default per-request stderr logging (keeps the agent's console
    # clean — the script prints its own status lines).
    def log_message(self, *_args):
        return

    def do_GET(self):  # noqa: N802 (http.server requires this exact name)
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        # Browsers often probe /favicon.ico — answer 204 and don't treat it as
        # the OAuth redirect (it carries no code/error/state).
        if parsed.path == "/favicon.ico" and "code" not in query and "error" not in query:
            self.send_response(204)
            self.end_headers()
            return

        # Record what we got for the main thread. STATE transition: the blocking
        # main thread is in AWAIT_REDIRECT; setting auth_result moves us to
        # GOT_CODE (or to a denial/error that main() will translate).
        self.server.auth_result = {
            "code": query.get("code", [None])[0],
            "error": query.get("error", [None])[0],
            "state": query.get("state", [None])[0],
        }

        # Respond with a tidy page so the creator sees a friendly confirmation in
        # their browser (success OR a denial message), then knows to go back.
        if self.server.auth_result["error"]:
            title, body = (
                "Sign-in cancelled",
                "You declined access, so honest-comments wasn’t signed in. "
                "You can close this tab and re-run the login if that was a mistake.",
            )
        elif self.server.auth_result["code"]:
            title, body = (
                "You’re signed in ✓",
                "honest-comments now has read-only access to your YouTube data. "
                "You can close this tab and return to your agent — it’ll "
                "continue from here.",
            )
        else:
            title, body = (
                "Hmm, no authorization code",
                "Something went wrong with the redirect. Close this tab and try "
                "running the login again.",
            )

        html = _result_page_html(title, body)
        encoded = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _result_page_html(title, body):
    """Render the minimal self-contained HTML page shown in the creator's browser.

    Inline CSS only (no external assets) so it renders even though our loopback
    server serves exactly one resource and then dies.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} — honest-comments</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: flex; align-items: center;
           justify-content: center; background: #FCFCFB; color: #16181C;
           font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui,
           Roboto, Helvetica, Arial, sans-serif; }}
    .card {{ max-width: 440px; padding: 40px 36px; text-align: center; }}
    h1 {{ font-size: 24px; letter-spacing: -0.02em; margin: 0 0 12px; }}
    p {{ font-size: 16px; line-height: 1.5; color: #555A62; margin: 0; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{body}</p>
  </div>
</body>
</html>"""


def run_loopback_capture(port_hint, timeout_seconds, open_browser, print_url,
                         client_id):
    """Bind a loopback server, open the consent screen, and BLOCK for the redirect.

    Returns (auth_result_dict, redirect_uri, code_verifier).

    STATE MACHINE realised here:
        START
          -> (bind loopback server on an ephemeral/given port)  [ServerBindError if fail]
          -> AWAIT_REDIRECT (browser opened to the auth URL, we block on handle_request
             in a worker thread, polling until a redirect lands or timeout fires)
          -> GOT_CODE (handler set server.auth_result; we return it to main())
    The actual code->token EXCHANGED and SAVED transitions happen back in main().
    """
    # PKCE secret pair — generated fresh per login attempt.
    code_verifier, code_challenge = generate_pkce_pair()

    # Anti-CSRF state token echoed back by Google in the redirect; we verify it.
    state = secrets.token_urlsafe(24)

    # Bind the loopback server. port_hint=0 asks the OS for any free ephemeral
    # port (the default + recommended path — avoids "address already in use").
    try:
        server = HTTPServer(("127.0.0.1", port_hint), _RedirectCatcher)
    except OSError as e:
        raise ServerBindError(
            f"Couldn't start the local sign-in server on 127.0.0.1:{port_hint} "
            f"({e}). If you passed --port, that port may be in use; omit --port "
            f"to let the OS pick a free one."
        )

    server.auth_result = None  # populated by the handler when the redirect lands
    bound_port = server.server_address[1]
    # NOTE: the redirect_uri MUST match EXACTLY what we send to Google (trailing
    # slash included) — Google compares it byte-for-byte at the token exchange.
    redirect_uri = f"http://127.0.0.1:{bound_port}/"

    auth_url = build_auth_url(client_id, redirect_uri, code_challenge, state)

    # Always PRINT the URL so a headless / no-default-browser environment can
    # still complete the flow by pasting it manually. Optionally also try to
    # auto-open it.
    print("\nOpening your browser to sign in to YouTube (Google)...")
    if print_url or not open_browser:
        print("\nIf your browser didn't open, paste this URL into it manually:\n")
        print(f"  {auth_url}\n")
    if open_browser:
        try:
            import webbrowser
            # webbrowser.open returns False if it couldn't find a browser; we
            # already printed the URL above as the fallback in that case.
            opened = webbrowser.open(auth_url)
            if not opened and not (print_url or not open_browser):
                print("\nCouldn't auto-open a browser. Paste this URL manually:\n")
                print(f"  {auth_url}\n")
        except Exception:
            # Never let a webbrowser hiccup abort the login — the printed URL is
            # the reliable fallback.
            if not (print_url or not open_browser):
                print("\nCouldn't auto-open a browser. Paste this URL manually:\n")
                print(f"  {auth_url}\n")

    print(f"Waiting for you to approve access (up to {timeout_seconds}s)...")

    # --- AWAIT_REDIRECT -----------------------------------------------------
    # handle_request() serves exactly ONE request then returns. We run it in a
    # daemon thread so the MAIN thread can enforce the timeout: if the creator
    # never approves, the worker would block forever on handle_request, so we
    # bound the wait here and tear the server down on timeout.
    def _serve_one():
        try:
            server.handle_request()
        except Exception:
            # If the socket is closed out from under us on timeout teardown,
            # handle_request can raise — that's expected; swallow it.
            pass

    worker = threading.Thread(target=_serve_one, daemon=True)
    worker.start()

    deadline = time.monotonic() + timeout_seconds
    while worker.is_alive() and time.monotonic() < deadline:
        worker.join(timeout=0.5)

    if worker.is_alive():
        # Timed out — no redirect arrived. Closing the socket unblocks the
        # worker's handle_request so the thread can exit, then we raise.
        try:
            server.server_close()
        except Exception:
            pass
        raise RedirectTimeout(
            f"No sign-in response after {timeout_seconds}s. Did the browser open? "
            f"Re-run the login, and approve the access in the page that opens. "
            f"(Tip: pass --print-url and open the URL manually if the browser "
            f"won't launch.)"
        )

    # Worker finished -> the handler ran -> we have a result. Close the socket.
    try:
        server.server_close()
    except Exception:
        pass

    result = server.auth_result or {}

    # --- Anti-CSRF check ----------------------------------------------------
    # Verify Google echoed our exact `state`. A mismatch means the redirect
    # didn't originate from the request we made — abort rather than trust it.
    if result.get("state") != state:
        raise TokenExchangeError(
            "OAuth state mismatch — the sign-in response didn't match the request "
            "we started (possible cross-site request). Aborting for safety; "
            "please re-run the login."
        )

    return result, redirect_uri, code_verifier


# ---------------------------------------------------------------------------
# Token exchange + persistence
# ---------------------------------------------------------------------------

def exchange_code_for_tokens(code, code_verifier, redirect_uri,
                             client_id, client_secret):
    """Trade the authorization `code` for an access_token + refresh_token.

    POSTs to Google's token endpoint (GOOGLE_TOKEN_ENDPOINT) with:
      - grant_type=authorization_code
      - code                 the value the loopback redirect captured
      - code_verifier        the PKCE secret half (proves we started this flow)
      - redirect_uri         must byte-match what we sent in the auth URL
      - client_id / secret   identify the app

    Returns Google's parsed JSON token response, which includes:
      access_token, expires_in (seconds), refresh_token, scope, token_type.

    Raises TokenExchangeError on any HTTP / network failure (caller -> exit 6).
    """
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    # Some installed-app clients have a secret, some don't. Only send it if set.
    if client_secret:
        form["client_secret"] = client_secret

    return _post_token_endpoint(form)


def _post_token_endpoint(form):
    """Low-level POST to Google's token endpoint; return parsed JSON or raise.

    Shared by both the initial code exchange AND the refresh path (in
    fetch_comments.py we re-implement an equivalent; here it serves the login
    exchange). Body is application/x-www-form-urlencoded per the OAuth spec.
    """
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        GOOGLE_TOKEN_ENDPOINT,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Google returns a JSON error body (e.g. {"error":"invalid_grant",...}).
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        raise TokenExchangeError(
            f"Token exchange failed (HTTP {e.code}): {detail[:300] or e.reason}. "
            f"If this says 'invalid_client' or 'redirect_uri_mismatch', the OAuth "
            f"client is misconfigured (owner setup); 'invalid_grant' usually means "
            f"the code expired — just re-run the login."
        )
    except urllib.error.URLError as e:
        raise TokenExchangeError(
            f"Network error reaching Google's token endpoint: {e.reason}. "
            f"Check your connection and re-run."
        )


def save_token_bundle(token_response, client_id, client_secret):
    """Persist the token bundle to ~/.honest-comments/youtube_token.json (0600).

    We store enough to let fetch_comments.py both USE and REFRESH the token
    without ever re-prompting the creator:
      - access_token     the bearer token sent on API calls
      - refresh_token    used to mint fresh access tokens when they expire
      - expires_in       lifetime in seconds (Google typically returns 3599)
      - obtained_at      epoch seconds when WE received it — so refresh logic can
                         compute staleness as (obtained_at + expires_in) vs now
      - scope/token_type echoed back from Google
      - client_id/secret cached here so the refresh call in fetch_comments.py
                         doesn't have to re-resolve the client config (the
                         refresh grant also requires client_id/secret)

    SECURITY: we create the directory + file, then chmod 0600 (owner read/write
    only) so other local users can't read the creator's tokens.
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # Tighten the directory too (best-effort; ignore if the FS doesn't support it).
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass

    bundle = {
        "access_token": token_response.get("access_token", ""),
        # A refresh_token is only present when access_type=offline + prompt=consent
        # did their job. If it's somehow absent, we still save (the access token
        # works until it expires) but fetch_comments.py will tell the creator to
        # re-login once it can't refresh.
        "refresh_token": token_response.get("refresh_token", ""),
        "expires_in": token_response.get("expires_in", 0),
        "obtained_at": int(time.time()),
        "scope": token_response.get("scope", OAUTH_SCOPE),
        "token_type": token_response.get("token_type", "Bearer"),
        # Cached client creds for the refresh grant (see docstring).
        "client_id": client_id,
        "client_secret": client_secret,
    }

    # Write atomically-ish: write then chmod. We open with a restrictive mode up
    # front so there's no brief window where the file is world-readable.
    fd = os.open(TOKEN_STORE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=2)
    finally:
        # Belt-and-suspenders: enforce 0600 even if the umask altered the open mode.
        try:
            os.chmod(TOKEN_STORE_PATH, 0o600)
        except OSError:
            pass

    return TOKEN_STORE_PATH


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser():
    """Define the argparse CLI. --help explains the whole flow in plain English."""
    p = argparse.ArgumentParser(
        prog="youtube_login.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Sign in to YouTube for honest-comments via Google OAuth 2.0.\n\n"
            "What happens when you run this:\n"
            "  1. Your browser opens to Google's sign-in / consent screen.\n"
            "  2. You approve READ-ONLY access to your YouTube data (one click).\n"
            "  3. Tokens are saved locally to ~/.honest-comments/youtube_token.json\n"
            "     and auto-refresh from then on — you won't be asked again.\n\n"
            "After this, run scripts/fetch_comments.py (use --mine to grab your\n"
            "own channel). No manual API key needed.\n\n"
            "NOTE: until honest-comments' OAuth app is Google-verified, you'll see\n"
            "a \"Google hasn't verified this app\" screen — click Advanced →\n"
            "\"Go to honest-comments (unsafe)\" to continue. It's signing into YOUR\n"
            "own Google account; nothing is sent to honest-comments (it has no servers)."
        ),
        epilog=(
            "Owner one-time setup (end users skip this): create a Google OAuth\n"
            "\"Desktop app\" client with the youtube.readonly scope and provide it via\n"
            "HONEST_COMMENTS_OAUTH_CLIENT_ID / _SECRET env vars, a\n"
            "~/.honest-comments/client_config.json file, or the PLACEHOLDER_*\n"
            "constants at the top of this script. See the README."
        ),
    )
    p.add_argument(
        "--port", type=int, default=0,
        help="Local loopback port for the OAuth redirect. Default 0 = let the OS "
             "pick a free ephemeral port (recommended). Only set this if you must "
             "pin the redirect to a specific port.",
    )
    p.add_argument(
        "--timeout", type=int, default=300,
        help="How many seconds to wait for you to approve in the browser before "
             "giving up (default 300).",
    )
    p.add_argument(
        "--print-url", action="store_true",
        help="Always print the auth URL (for headless machines / when no browser "
             "auto-opens). The URL is also printed automatically if --no-browser "
             "is set.",
    )
    p.add_argument(
        "--no-browser", action="store_true",
        help="Don't try to auto-open a browser; just print the URL to paste "
             "manually. Useful on headless / remote machines.",
    )
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    # Basic usage validation.
    if args.port < 0 or args.port > 65535:
        print("ERROR: --port must be between 0 and 65535 (0 = auto).",
              file=sys.stderr)
        return 1
    if args.timeout <= 0:
        print("ERROR: --timeout must be a positive number of seconds.",
              file=sys.stderr)
        return 1

    # --- Resolve the OAuth client (owner setup gate) -----------------------
    try:
        client_id, client_secret = resolve_client_config()
    except NoClientConfig as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # --- AWAIT_REDIRECT: open consent screen + capture the code ------------
    try:
        result, redirect_uri, code_verifier = run_loopback_capture(
            port_hint=args.port,
            timeout_seconds=args.timeout,
            open_browser=not args.no_browser,
            print_url=args.print_url,
            client_id=client_id,
        )
    except ServerBindError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 5
    except RedirectTimeout as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 4
    except TokenExchangeError as e:
        # state-mismatch surfaces here before any exchange happens.
        print(f"ERROR: {e}", file=sys.stderr)
        return 6

    # Did the creator deny consent? Google returns ?error=access_denied.
    if result.get("error"):
        print(
            f"\nSign-in was cancelled (Google said: {result['error']}).\n"
            f"honest-comments was NOT granted access. Re-run "
            f"`python3 scripts/youtube_login.py` to try again.",
            file=sys.stderr,
        )
        return 3

    code = result.get("code")
    if not code:
        # No code and no explicit error — unusual; treat as a generic failure.
        print("ERROR: the sign-in redirect carried no authorization code. "
              "Re-run the login.", file=sys.stderr)
        return 6

    # --- GOT_CODE -> EXCHANGED ---------------------------------------------
    print("Got your approval. Exchanging it for an access token...")
    try:
        token_response = exchange_code_for_tokens(
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            client_id=client_id,
            client_secret=client_secret,
        )
    except TokenExchangeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 6

    # --- EXCHANGED -> SAVED ------------------------------------------------
    if not token_response.get("access_token"):
        print("ERROR: Google's token response had no access_token. Re-run the "
              "login.", file=sys.stderr)
        return 6

    path = save_token_bundle(token_response, client_id, client_secret)

    # Warn (but don't fail) if no refresh_token came back — the access token
    # still works until it expires (~1h), but auto-refresh won't be possible.
    refresh_warning = ""
    if not token_response.get("refresh_token"):
        refresh_warning = (
            "\n  ! Heads up: Google didn't return a refresh token this time, so "
            "this access will expire in ~1 hour and you'll need to log in again. "
            "(Usually re-running the login fixes it.)"
        )

    print(
        "\n" + "=" * 60 + "\n"
        "Signed in to YouTube. ✓\n"
        f"  Tokens saved to: {path} (readable only by you)\n"
        "  These auto-refresh — you won't be asked again.\n"
        f"{refresh_warning}\n"
        "\nNext: fetch your comments, e.g.\n"
        "  python3 scripts/fetch_comments.py --mine\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
