<!--
  README.md — this file is TWO things at once:
    1. A human-facing intro for anyone landing on the GitHub repo (the first section).
    2. THE OPERATING MANUAL that a creator's agent (Codex CLI / Claude Code) fetches
       via STARTER_PROMPT and executes end-to-end.
  If you are the agent: read this whole file, then follow the "Your procedure"
  section literally, step by step. The deep classifier logic lives in
  prompts/analyze.md — you load that in Step 4.
-->

# honest-comments

**Your harshest fans, ranked.** honest-comments finds the genuinely useful criticism buried in your YouTube comments — no praise, no trolls — and tells you what to actually fix.

It runs entirely on *your* machine and *your* AI. You paste a tiny prompt into Codex CLI or Claude Code, that agent signs you in to your own YouTube account, pulls your comments, and uses your own AI subscription to do the analysis.

**honest-comments has no servers and receives nothing from you** — there's no backend, no account, no honest-comments-hosted anything. To be precise about where your data *does* go (because "nothing leaves your machine" wouldn't be true): you **sign in to your own Google/YouTube account** via Google's standard OAuth login (the sign-in happens on Google's pages, and OAuth tokens are stored locally in `~/.honest-comments/`), your **comment requests go to Google's YouTube API** (that's how the comments get fetched), and the **comment text is processed by your own AI provider** — the Codex/Claude subscription you already use — to classify it, exactly like any other prompt you send them. honest-comments does have a registered Google OAuth identity (that's *its* name on the consent screen) — but that's just an app registration, **not a server and not a data path**: nothing routes through us. There's no third party in the middle that we run.

What you get back, in whichever shape you ask for — a **quick prose summary** of your most useful criticism, or a **ranked top-N** (you pick how many) like _"Audio is too quiet — 11 different people said this (+340 likes). Fix: raise your voice gain, add a compressor, aim for ~-14 LUFS."_ The praise gets counted but skipped. The trolls get filtered out. You're left with the stuff that's harsh but worth acting on.

---

## For the agent: how to run this

You are an agent acting on behalf of a YouTube creator. Your goal is to surface **ranked, actionable criticism** from their comments and pair every problem with a concrete fix. You host nothing — you sign the creator in to their own YouTube account, run a local Python script, and use your own model to do the analysis.

Read the rest of this file, then execute "Your procedure" below.

### Privacy contract — say this to the creator early

State plainly, near the start — and be **honest**, not over-promising. The truthful framing:

- **honest-comments (this project) has no servers and receives nothing.** There's no backend, no account, nothing of ours in the data path.
- **The login goes to Google.** You're signing into *your own* Google/YouTube account on Google's own pages. The OAuth tokens that come back are stored **locally** in `~/.honest-comments/` (readable only by you) and auto-refresh — they never go to honest-comments.
- **honest-comments has a registered Google OAuth identity** — that's the name you'll see on the consent screen, and it's how Google knows which app you're authorizing. That's an *app registration*, **not a server and not a data path**. No comment data, token, or request routes through honest-comments.
- **The consent screen mentions "edit and delete" — but the tool only READS.** Google's API only lets you read comments under the broad `youtube.force-ssl` scope (the read-only scope is *refused* for reading comments — a documented Google limitation), so the consent screen shows scary "edit/delete" wording. honest-comments' actual **behavior is read-only**: it calls only `list` endpoints and never writes, edits, or deletes anything. The creator (or their AI) can confirm by reading `scripts/` — there are no insert/update/delete API calls. See the ["Why the sign-in screen mentions 'edit and delete'" callout](#step-1--log-in-to-youtube) for the full explanation.
- Their **comment-fetch requests go to Google's YouTube API** (authenticated with their own login) — that's how comments are retrieved.
- **Comment text is processed by *their own* AI provider** (the Codex/Claude subscription they already pay for) to classify it — the same as any prompt they send their model. So comment text does leave the machine *to their own model provider*, exactly as it would for any other use of their agent. It never goes to us.
- Everything the script fetches lands in a local `out/` folder they own and can re-use or delete.

Don't say "nothing is ever sent anywhere" — that's not true and creators will (rightly) distrust it. Say: *nothing goes to honest-comments (it has no servers); your login goes to Google and tokens stay on your machine; your comments are classified by your own AI, same as any prompt.*

### Prerequisites

- **An agent runtime:** Codex CLI or Claude Code (whichever the creator pasted the starter prompt into — that's you).
- **Python 3.8+** on the creator's machine. Both scripts are **stdlib-only** — no `pip install`, nothing to set up.
- **A Google/YouTube account** — the creator just needs to be able to sign in to it in a browser. The login (Step 1) handles everything else; there's no manual API-key step.
- *(Owner only, one time)* A Google OAuth "Desktop app" client must be configured so the login works. End users never do this — see [Owner / self-host setup](#owner--self-host-setup). If you cloned this repo and the login says "No honest-comments OAuth client is configured", that section is what to do.

---

## Your procedure

### Step 0 — Get the code (clone the repo FIRST)

**This is your very first action, before anything else.** The fetch script (`scripts/fetch_comments.py`) and the analyzer (`prompts/analyze.md`) live in this repo — they are NOT in the creator's empty workspace yet. If you skip straight to "run scripts/fetch_comments.py" you'll hit *No such file or directory* and the whole flow fails. So clone the repo and work from inside it:

```
git clone https://github.com/EthanSK/honest-comments && cd honest-comments
```

If `git` isn't available, download and unzip the repo instead, then `cd` into it:

```
curl -L -o honest-comments.zip https://github.com/EthanSK/honest-comments/archive/refs/heads/main.zip
unzip honest-comments.zip && cd honest-comments-main
```

From here on, **every command runs from inside this cloned directory** — `python3 scripts/fetch_comments.py ...` and "load `prompts/analyze.md`" both assume that's your working directory. The `out/` folder the script writes to will be created here too.

### Step 1 — Log in to YouTube

The creator signs in to their **own** YouTube account via Google's standard OAuth login. You drive it; they click "Allow" once. There's no manual API-key step any more.

First, check whether they're already signed in:

- If `~/.honest-comments/youtube_token.json` already exists, they're logged in — skip to Step 2. (You can also just run the fetcher; it'll tell you if it needs a login.)

If not, run the login script:

```
python3 scripts/youtube_login.py
```

What happens, in order:

1. The script opens the creator's **browser** to Google's sign-in / consent screen (and also prints the URL, in case the browser doesn't auto-open — they can paste it).
2. They sign in to the Google account that owns their channel.
3. **They will see a "Google hasn't verified this app" screen.** Be honest about this — it's expected, because honest-comments' OAuth app isn't Google-verified yet (verification is a separate Google review process). To continue: click **"Advanced"**, then **"Go to honest-comments (unsafe)"**. It's safe in the sense that matters here: they're signing into *their own* account, honest-comments only ever **reads** their comments, and nothing is sent to honest-comments (it has no servers). If they're not comfortable, point them at the [API-key fallback](#advanced-use-an-api-key-instead) instead.
4. **The consent screen will mention "edit and permanently delete" — don't panic, see the callout below.** They click Allow once.
5. The script saves OAuth tokens to `~/.honest-comments/youtube_token.json` (readable only by them) and prints a success message. From then on the tokens **auto-refresh** — they won't be asked again.

> #### Why the sign-in screen mentions "edit and delete" (it only reads)
>
> When the creator clicks Allow, Google's consent screen says honest-comments wants to **"See, edit, and permanently delete your YouTube videos, comments, and captions."** That wording is alarming, so explain it up front:
>
> - **Google's API only lets you READ comments under the broad `youtube.force-ssl` scope.** The narrower `youtube.readonly` scope *can't* read comments — Google refuses `commentThreads.list` with a `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT` error (a documented, tested limitation). `force-ssl` is the **only** OAuth scope that can read comments via the API, and it happens to carry that scary "edit/delete" label.
> - **honest-comments only ever READS.** It calls only `list` endpoints (`channels.list`, `playlistItems.list`, `commentThreads.list`, `search.list`). There is **no** write/insert/update/delete call anywhere in the code.
> - **Anyone (or their AI) can verify this in seconds.** Read `scripts/youtube_login.py` and `scripts/fetch_comments.py`, or just grep them:
>   ```
>   grep -rn "insert\|update\|delete\|setRating\|rate" scripts/   # → nothing
>   ```
> - In short: the requested **scope** is broad because Google requires it to read, but the tool's **behavior** is strictly read-only. It never changes anything on the creator's channel.

Reassure them on privacy as you go: the **login goes to Google** (their own account), the **tokens stay on their machine**, and **nothing goes to honest-comments**. See the privacy contract above.

If the login prints **"No honest-comments OAuth client is configured yet"**, that's the project-owner setup step — see [Owner / self-host setup](#owner--self-host-setup). End users shouldn't hit this once the owner has shipped a client.

### Step 2 — Pick scope (and confirm before spending anything)

Ask the creator: **whole channel, or specific videos?** A creator usually thinks "my channel," but the lightest, highest-value path is often "I just dropped a video — what should I fix?"

- **Their own channel (the natural default when logged in):** pass `--mine`. Because they're signed in, the script resolves *their own* channel automatically (via `channels.list?mine=true`) — **no handle to paste.** Suggest this first.
- **Specific videos (preferred when they have a target):** they paste any mix of full URLs, short `youtu.be` links, `/shorts/` links, or bare 11-char IDs. This skips channel resolution entirely — cheaper, faster, more targeted.
- **A different channel:** they give a handle (`@SomeCreator`), a channel URL, or a `UC...` channel ID via `--channel`. You'll resolve this to its uploads playlist and page through it. (`--mine` is OAuth-only; `--channel`/`--videos` work with either a login or an API key.)

**Default scope guard:** the newest **25** videos. Do NOT pull a giant back-catalogue on a first run — it's a quota bomb and rarely what they want.

**To get the real numbers, run the fetcher in `--dry-run` mode first.** This resolves the channel, counts its uploads, and prints a video count + estimated API-unit cost — **without fetching a single comment**:

```
python3 scripts/fetch_comments.py --mine --dry-run
```

(or `--channel "@SomeCreator" --dry-run` for a specific channel. `--dry-run` works on the `--videos` path too — it just reports how many explicit videos you'd scan.) Take the count + estimate it prints, show it to the creator in plain English, and **wait for a yes before the real fetch.** Example:

> "Found 312 videos on @YourChannel. The default scope is your newest 25 (~26 of your 10,000 daily API units). Want the newest 25, a different number, or specific videos instead?"

Only after they confirm do you run the same command **without** `--dry-run` (Step 3). Wait for a yes before going wide.

### Step 3 — Run the fetcher

**Confirm scope with `--dry-run` first (Step 2), then run the real fetch** by re-running the same command without `--dry-run`. Run `scripts/fetch_comments.py` with Python 3. Key invocations:

**Their own channel (logged in — newest 25 by default):**
```
python3 scripts/fetch_comments.py --mine
```

**Specific videos:**
```
python3 scripts/fetch_comments.py --videos "https://youtu.be/abc123, https://www.youtube.com/watch?v=def456, ghi789"
```

**A specific channel:**
```
python3 scripts/fetch_comments.py --channel "@SomeCreator"
```

**Useful flags:**

- `--mine` — fetch **the signed-in creator's own channel** (resolved via `mine=true`, no handle needed). OAuth only — needs a login (Step 1), won't work with just an API key. This is the natural default once logged in.
- `--channel <handle|URL|UC-id>` — resolve a specific channel to its uploads and page it.
- `--videos "<url/id, url/id, ...>"` — fetch specific videos directly (URLs, short links, `/shorts/`, or bare IDs, mixed).
- `--max-videos N` — how many newest videos to pull for a channel (default **25**).
- `--per-video-cap N` — max top-level comments per video (default **500**). Comments come back `order=relevance`, so the signal is front-loaded and the deep tail is mostly emoji.
- `--include-replies` — also include the **inline reply preview** that the same `commentThreads` call already returns (off by default — replies are mostly creator-replies and noise). This is **not** full reply chains and costs **no meaningful extra quota** — it just keeps the handful of replies YouTube bundles into each thread.
- `--dry-run` (a.k.a. `--estimate`) — resolve the channel, count its uploads, print a video count + estimated API-unit cost, then exit **without fetching any comments**. Run this first (Step 2) to confirm scope with the creator.
- `--api-key <key>` — use the **public-data API-key fallback** instead of the login (see [Advanced](#advanced-use-an-api-key-instead)). Falls back to `$YOUTUBE_API_KEY`, then `.env`. Public comments only; can't be combined with `--mine`.

**Quota guardrails (you, the agent, enforce the confirmation — the script keeps the calls cheap):**

- The free quota is **10,000 units/day** per Google Cloud project. A `commentThreads` call is **1 unit** for up to 100 comments, so even big channels are cheap (25 videos × ~1,000 comments ≈ ~250 units → you can run the default scope ~39×/day).
- The script keeps quota low by design: the **default scope is the newest 25 videos** (`--max-videos`), each video is capped at **500 top-level comments** (`--per-video-cap`), and the only expensive call (`search.list`, 100 units) is used solely as a last-resort vanity-name fallback and prints a warning before it spends. The script does **not** prompt you — **YOU confirm scope with the creator (Step 2) before running it.**
- If you pass `--max-videos 0` (= ALL videos) and the channel has more than ~200 uploads, the script prints a one-line caution but still proceeds — so don't reach for `0` without an explicit yes from the creator.
- Google resets quota at **midnight US Pacific**; on a `quotaExceeded` (HTTP 403) the script writes whatever it already fetched and exits **2**.
- Stream light progress to the creator from the script's own per-video output, e.g. _"Pulled 4,210 comments across 25 videos. 2 videos had comments disabled."_

**Output:** the script writes `out/comments_<channel-or-batch>_<timestamp>.json` (the stripped comment objects) and `out/run_meta.json` (videos requested, comments per video, total comment counts, comments-disabled list, per-video errors, and a `quota_exceeded` flag). The analyzer (Step 4) writes more files into the same `out/` folder (`classified_*.json`, `claims.json`, `themes.json`). The **entire `out/` directory is gitignored** — it's the creator's local data and never gets committed. Raw JSON on disk means you can re-analyze without re-spending quota.

### Step 4 — Analyze (classify → dedupe → cluster → rank)

**Load `prompts/analyze.md` and follow it.** That file is the classifier brain. It contains the full operational definitions, examples, and the ranking formula. In short, you will, against the `out/*.json` data:

1. **Classify every comment into exactly one of 3 buckets:**
   - **PRAISE** — approval/enjoyment with no actionable signal. Counted, then discarded from the digest.
   - **TROLL / TOXIC** — hostile, bad-faith, spam, or off-topic with nothing to act on. Discarded; never quoted back unless the creator asks.
   - **CONSTRUCTIVE CRITICISM** — a **specific, good-faith, actionable** claim about something the creator could change (audio, pacing, lighting, factual errors, on-screen text, etc.). **This is the product.**

   The single most important rule: **harsh tone is NOT the same as a troll.** A brutal comment with a real, fixable claim ("the audio is unlistenable, fix your gain staging") is CONSTRUCTIVE, not a troll. Tone is not the classifier axis — a fixable good-faith claim is.

2. **Dedupe by canonical claim, not by raw text** — many people say the same thing in different words; count them as one point. Count distinct voices by `author_channel_id`, never by raw comment count (so one obsessive commenter can't inflate a theme).

3. **Cluster** the deduped claims into a small stable set of themes (Audio, Pacing/Length, Video/Lighting, Structure/Clarity, Factual Accuracy, etc.).

4. **Rank** themes most-painful-first, weighting distinct-commenter count most heavily, then likes (silent agreement), then severity and actionability. Collapse each theme into one headline insight with a concrete fix. Then (Step 5) **ask the creator whether they want a short combined prose summary or a ranked top-N (they pick N)** before presenting — don't dump both.

Tell the creator what you're doing in one line: _"Sorting praise from trolls from the stuff you can actually use…"_

### Step 5 — Ask how they want it, then deliver, then go deeper

**Ask first.** Before presenting anything, ask the creator HOW they want the criticism delivered — don't dump a big list unprompted, and don't produce both shapes at once. Offer two modes:

> "Want a quick summary of your most useful criticism, or should I pull the top 10 — or however many you'd like — ranked?"

- **Summary mode** — one or two short paragraphs of flowing prose synthesizing the few **most relevant, most-acted-upon** themes into a readable "here's the gist of what to fix" narrative. Still grounded in real distinct-commenter counts, real themes, and verbatim example phrasing, and still pairs the key problems with concrete fixes — just written as prose a human reads in ~20 seconds, not a card list.
- **Ranked top-N mode** — the ranked insight cards, capped to the number the creator picks (suggest **10** by default). Per insight: theme + distinct-commenter count + total likes + severity, 2–3 **verbatim** example comments, and a concrete suggested fix. For example:

> **#1 · Audio is too quiet / voice gets buried** — 11 people raised this (+340 likes). Severity: high.
> _"Great info but I had to max my volume and your voice still clips."_
> _"bg music way louder than you the whole video"_
> **Fix:** Raise your voice gain and add a compressor; aim for ~-14 LUFS integrated. Duck the music ~12 dB under your voice.

Deliver in whichever shape they chose (the full per-insight rules and the summary-mode definition both live in `prompts/analyze.md` §6). Then add a framing line so it doesn't feel like a pile-on:

> "That's the signal. For context: of 4,210 comments, ~3,100 were praise and ~260 were trolls I filtered out — so this is the ~850 that actually had a point, boiled down to 6 themes."

Then **offer to go deeper** (and to switch format):

> "Want me to pull every comment behind any of these themes? Switch to the full ranked top-10 (or back to a quick summary)? Compare your last 5 videos to see if one of these is getting worse? Or draft a pinned comment addressing the top issue?"

---

## Tone & UX the agent should use

- **Be honest, not nice.** The creator explicitly asked for the harsh-but-useful truth. Don't soften the criticism into mush.
- **But don't editorialize cruelty.** Quote trolls only if explicitly asked. Default to summarizing, not pasting, the toxic bucket.
- **Always pair a problem with a concrete fix.** Never hand someone a complaint without a next step.
- **Conversational, not a form.** Greet, explain, ask scope, confirm, fetch, analyze, deliver, offer more — adapting tone to the creator.
- **Cite real counts and real comments.** Every number comes from the fetched data; every quoted example is a verbatim string from `out/`. Never invent a comment, a count, or a like total.

---

## Owner / self-host setup

*(This is the **one-time setup for the person hosting honest-comments** — i.e. whoever owns the GitHub repo / the deployed copy. **End users never do this**; they just run the login in Step 1. If you're an end user, skip this section.)*

The OAuth login needs a Google OAuth **"Desktop app"** client to exist. Create one once:

1. **Create / pick a Google Cloud project** at https://console.cloud.google.com/.
2. **Enable the YouTube Data API v3:** **APIs & Services → Library** → search **"YouTube Data API v3"** → **Enable**.
3. **Configure the OAuth consent screen:** **APIs & Services → OAuth consent screen**. Set User Type (External is fine), fill in app name (`honest-comments`) + support email, and **add the scope** `https://www.googleapis.com/auth/youtube.force-ssl`. (Use **`force-ssl`, not `readonly`** — the read-only scope is refused for reading comments via `commentThreads.list`; `force-ssl` is the only OAuth scope Google permits for reading comments. The tool still only ever reads — see the ["edit and delete" callout in Step 1](#step-1--log-in-to-youtube).) While the app is unverified you can either keep it in "Testing" (add each creator as a test user) or "In production" (anyone can use it but sees the "unverified app" interstitial). Verification is a separate Google review you can pursue later to remove that screen.
4. **Create the OAuth client:** **APIs & Services → Credentials → Create Credentials → OAuth client ID → Application type: "Desktop app"**. Click **Download JSON** — that file has the `installed` shape `{"installed":{"client_id":"...","client_secret":"...", ...}}`.
5. **Provide the client to the scripts** in any **one** of these ways (checked in this priority order):
   - **Env vars:** `export HONEST_COMMENTS_OAUTH_CLIENT_ID=...` and `export HONEST_COMMENTS_OAUTH_CLIENT_SECRET=...`
   - **Config file:** drop the downloaded JSON, unmodified, at `~/.honest-comments/client_config.json`.
   - **In-source constants:** edit `PLACEHOLDER_CLIENT_ID` / `PLACEHOLDER_CLIENT_SECRET` at the top of `scripts/youtube_login.py`.

**About the "client_secret":** for an installed/Desktop app the client_secret is **not actually secret** — Google treats it as a public identifier that ships with the app. What protects the login is **PKCE** (the script generates a one-time code that never leaves the machine). So it's fine to commit a real client_id/secret into the repo or the constants. *(`client_config.json` and `youtube_token.json` are still gitignored defensively — the token file genuinely IS sensitive.)*

Until the app passes Google verification, every creator sees a **"Google hasn't verified this app"** screen on login — that's expected; they click **Advanced → continue**. Step 1 tells them how.

---

## Advanced: use an API key instead

If a creator would rather **not** log in (e.g. they don't want the unverified-app screen, or they only need public comments), there's a fallback: a plain **YouTube Data API key (public data only — no OAuth, no login)**. Trade-offs: it can only read **public** comments on **public** videos, and **`--mine` won't work** (an API key has no signed-in user, so there's no "my channel" to resolve — use `--channel` instead).

To set it up:

1. Go to https://console.cloud.google.com/ and create/pick a project.
2. **APIs & Services → Library** → enable **"YouTube Data API v3"**.
3. **APIs & Services → Credentials → Create Credentials → API key**, then copy it. *(Optional: restrict the key to "YouTube Data API v3". Do **not** add an HTTP-referrer restriction — this is a server-side call, not a browser call.)*
4. Provide it to the fetcher via `--api-key`, the `YOUTUBE_API_KEY` env var, or a `.env` file (`YOUTUBE_API_KEY=...`) in the working directory (checked in that priority order). The key is never sent to honest-comments and isn't logged.

Then fetch normally, just with a channel/videos target (not `--mine`):
```
python3 scripts/fetch_comments.py --channel "@SomeCreator" --api-key AIza...
```

The fetcher prefers an OAuth login if one exists; the API key is used only when there's no saved login.

---

## Troubleshooting

**`fetch_comments.py` exit codes:** `0` success · `1` bad usage (incl. `--mine` without a login) · `2` quota exceeded (partial data written) · `3` credential rejected (bad key, or expired/revoked login) · `4` channel/videos not found or empty · `5` **not authenticated** (no login and no API key).

**`youtube_login.py` exit codes:** `0` signed in · `1` bad usage / internal error · `2` **no OAuth client configured** (owner setup — see [Owner / self-host setup](#owner--self-host-setup)) · `3` you denied consent · `4` timed out waiting for the browser (default 300s) · `5` couldn't bind the local sign-in port · `6` token exchange failed.

- **Bad usage (negative flag value):** if `--max-videos` or `--per-video-cap` is given a negative number, the fetcher exits **1** with a clear message. Use `0` for "all videos" / "no cap"; any positive integer otherwise.
- **Not signed in / "Not authenticated" (exit 5):** there's no saved login *and* no API key. Run `python3 scripts/youtube_login.py` (Step 1) — that's the normal path. (Or provide an API key for the public-only [fallback](#advanced-use-an-api-key-instead).)
- **`--mine` without a login (exit 1):** `--mine` reads the *signed-in* creator's own channel, so it needs OAuth. If they're using an API key, either log in first, or target the channel explicitly with `--channel`.
- **Login fails / no OAuth client configured (`youtube_login.py` exit 2):** the project owner hasn't shipped an OAuth client yet — see [Owner / self-host setup](#owner--self-host-setup). This is *not* an end-user-fixable error.
- **Consent denied / login timed out (`youtube_login.py` exit 3 / 4):** the creator clicked "Deny", or never finished in the browser. Re-run `python3 scripts/youtube_login.py`. If the browser won't open, pass `--print-url` (or `--no-browser`) and have them paste the URL manually.
- **"Google hasn't verified this app":** expected while the OAuth app is unverified. They click **Advanced → "Go to honest-comments (unsafe)"** to continue (Step 1 explains why it's safe — the tool only reads, it's their own account, nothing is sent to us). If they refuse, offer the [API-key fallback](#advanced-use-an-api-key-instead).
- **Login expired / revoked (fetcher exit 3):** if a saved login can't be refreshed (the creator revoked access, or it lapsed after long inactivity), the fetcher exits **3** and says to re-run the login. `python3 scripts/youtube_login.py` fixes it.
- **Bad API key (fetcher exit 3, key path):** most often the key is correct but the **YouTube Data API v3 wasn't enabled** for the project, or an HTTP-referrer restriction was added by mistake (remove it). On the `--videos` path there's no setup probe, so a bad key first shows up on the very first comment fetch — same exit **3**, same fix.
- **`quotaExceeded` (HTTP 403):** the fetcher exits **2** after writing whatever it already fetched. Tell the creator how much got pulled and that **quota resets at midnight US Pacific** — re-run then, or scope smaller (fewer videos, lower `--per-video-cap`). Their partial data in `out/` is still analyzable now.
- **Comments disabled on a video (HTTP 403 `commentsDisabled`):** not an error — the script skips that video, notes it in `run_meta.json`, and continues. Mention which videos had comments off, then move on.
- **A specific video is invalid / private / deleted (HTTP 404 `videoNotFound`):** not fatal — the script records that one video in `run_meta.json`'s `errors` list and continues to the rest. One bad video link won't abort the whole run. Mention which IDs were unavailable.
- **Channel not found / handle didn't resolve, or `--videos` had no usable links:** the fetcher exits **4** (same code when a channel can't be resolved and when `--videos` contained no recognisable video IDs). Ask the creator to paste their channel URL directly, or their `UC...` channel ID, or just specific video links instead.
- **Empty channel (no public uploads):** if a channel resolves but has zero public videos (brand-new, or everything private/unlisted), the script prints a "nothing to analyze" message and exits **4**. Suggest specific video links, or check the channel actually has public uploads.
- **Private/unlisted videos:** even logged in, this tool reads **public** videos' comments (the `youtube.force-ssl` scope plus the public commentThreads endpoint — note that despite the broad scope name, the tool only ever reads). If a video link doesn't return comments, confirm it's public.
- **Everything came back as praise/trolls, nothing constructive:** that can be real (small or very positive comment set). Say so honestly rather than manufacturing criticism — and offer to widen scope to more videos.

---

## Repo layout

```
honest-comments/
├── README.md                # this file — the agent operating manual
├── STARTER_PROMPT.md        # the tiny paste that kicks it all off
├── scripts/
│   ├── youtube_login.py      # one-time OAuth login (stdlib-only Python)
│   └── fetch_comments.py     # fetches the comments; stdlib-only Python
├── prompts/
│   └── analyze.md            # the classifier + ranking + conversation playbook
├── site/                     # static landing page (GitHub Pages)
├── out/                      # gitignored — your fetched comments land here
└── LICENSE                   # MIT

# Plus, on the creator's machine (NOT in the repo — gitignored defensively):
#   ~/.honest-comments/youtube_token.json   saved OAuth tokens (0600, auto-refreshed)
#   ~/.honest-comments/client_config.json   (optional) owner's OAuth client JSON
```

---

_Open source. BYO-agent, sign in with your own YouTube account, no backend. Read every line before you run it._
