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

It runs entirely on *your* machine and *your* AI. You paste a tiny prompt into Codex CLI or Claude Code, that agent fetches this manual, pulls your comments with your own free YouTube API key, and uses your own AI subscription to do the analysis. **We host nothing and we see nothing** — there is no backend, no account, no data sent anywhere.

What you get back: a ranked digest like _"Audio is too quiet — 11 different people said this (+340 likes). Fix: raise your voice gain, add a compressor, aim for ~-14 LUFS."_ The praise gets counted but skipped. The trolls get filtered out. You're left with the stuff that's harsh but worth acting on.

---

## For the agent: how to run this

You are an agent acting on behalf of a YouTube creator. Your goal is to surface **ranked, actionable criticism** from their comments and pair every problem with a concrete fix. You host nothing — you run a local Python script with the creator's own API key and use your own model to do the analysis.

Read the rest of this file, then execute "Your procedure" below.

### Privacy contract — say this to the creator early

State plainly, near the start:

- Their API key lives **only on their machine** and is never sent to honest-comments or anyone else.
- No comment data leaves their computer except the parts you feed to *your own* model (their AI subscription) to classify.
- Everything the script fetches lands in a local `out/` folder they own and can re-use or delete.

### Prerequisites

- **An agent runtime:** Codex CLI or Claude Code (whichever the creator pasted the starter prompt into — that's you).
- **Python 3.8+** on the creator's machine. The fetch script is **stdlib-only** — no `pip install`, nothing to set up.
- **A free YouTube Data API key** (public-data tier). Walk the creator through getting one in Step 1 if they don't already have it.

---

## Your procedure

### Step 1 — Get a free YouTube Data API key

We use a plain **API key (public data only)** — NOT OAuth. Public video comments are readable with just a key: no consent screen, no OAuth client, no token refresh, no "unverified app" warning. A creator can be running in ~3 minutes.

First check whether the key already exists:

- If the environment variable `YOUTUBE_API_KEY` is already set, or there's a `.env` file in the working directory with `YOUTUBE_API_KEY=...`, you're done — skip to Step 2.

If not, walk the creator through this **verbatim**:

1. Go to https://console.cloud.google.com/
2. Create a new project: top bar → project dropdown → **New Project** → name it `honest-comments` → **Create**.
3. **APIs & Services → Library** → search **"YouTube Data API v3"** → **Enable**.
4. **APIs & Services → Credentials → Create Credentials → API key**.
5. Copy the key. _(Optional hardening — offer it, don't force it: click "Edit API key" → API restrictions → restrict to "YouTube Data API v3". Do **not** add an HTTP-referrer restriction — this is a server-side script call, not a browser call, and a referrer restriction would break it.)_
6. **Prefer the env-var path so the key never lands in chat logs.** Tell the creator to run:
   ```
   export YOUTUBE_API_KEY="their-key-here"
   ```
   in the same terminal, OR put it in a `.env` file in the working directory. If they'd rather just paste it to you, that's fine too — but explicitly remind them it's *their* key, lives only on their machine, and is never sent to us.

The script reads the key in this priority order: `--api-key` flag → `$YOUTUBE_API_KEY` → `.env` file in the current directory. It never hardcodes or logs the key.

### Step 2 — Pick scope (and confirm before spending anything)

Ask the creator: **whole channel, or specific videos?** A creator usually thinks "my channel," but the lightest, highest-value path is often "I just dropped a video — what should I fix?"

- **Specific videos (preferred when they have a target):** they paste any mix of full URLs, short `youtu.be` links, `/shorts/` links, or bare 11-char IDs. This skips channel resolution entirely — cheaper, faster, more targeted.
- **Whole channel:** they give a handle (`@SomeCreator`), a channel URL, or a `UC...` channel ID. You'll resolve this to their uploads playlist and page through it.

**Default scope guard:** the newest **25** videos. Do NOT pull a giant back-catalogue on a first run — it's a quota bomb and rarely what they want. Once you know the channel size, **confirm scope and show a quota estimate before fetching a single comment.** Example:

> "Found 312 videos on @YourChannel. I'll start with your newest 25 (~280 of your 10,000 daily API units). Want the newest 25, a different number, or specific videos instead?"

Wait for a yes before going wide.

### Step 3 — Run the fetcher

Run `scripts/fetch_comments.py` with Python 3. Key invocations:

**Specific videos:**
```
python3 scripts/fetch_comments.py --videos "https://youtu.be/abc123, https://www.youtube.com/watch?v=def456, ghi789"
```

**Whole channel (newest 25 by default):**
```
python3 scripts/fetch_comments.py --channel "@SomeCreator"
```

**Useful flags:**

- `--channel <handle|URL|UC-id>` — resolve a channel to its uploads and page it.
- `--videos "<url/id, url/id, ...>"` — fetch specific videos directly (URLs, short links, `/shorts/`, or bare IDs, mixed).
- `--max-videos N` — how many newest videos to pull for a channel (default **25**).
- `--per-video-cap N` — max top-level comments per video (default **500**). Comments come back `order=relevance`, so the signal is front-loaded and the deep tail is mostly emoji.
- `--include-replies` — also fetch reply chains (off by default — replies are mostly creator-replies and noise, and cost extra quota).
- `--api-key <key>` — override the env-var/`.env` key resolution.

**Quota guardrails (you, the agent, enforce the confirmation — the script keeps the calls cheap):**

- The free quota is **10,000 units/day** per Google Cloud project. A `commentThreads` call is **1 unit** for up to 100 comments, so even big channels are cheap (25 videos × ~1,000 comments ≈ ~250 units → you can run the default scope ~39×/day).
- The script keeps quota low by design: the **default scope is the newest 25 videos** (`--max-videos`), each video is capped at **500 top-level comments** (`--per-video-cap`), and the only expensive call (`search.list`, 100 units) is used solely as a last-resort vanity-name fallback and prints a warning before it spends. The script does **not** prompt you — **YOU confirm scope with the creator (Step 2) before running it.**
- If you pass `--max-videos 0` (= ALL videos) and the channel has more than ~200 uploads, the script prints a one-line caution but still proceeds — so don't reach for `0` without an explicit yes from the creator.
- Google resets quota at **midnight US Pacific**; on a `quotaExceeded` (HTTP 403) the script writes whatever it already fetched and exits **2**.
- Stream light progress to the creator from the script's own per-video output, e.g. _"Pulled 4,210 comments across 25 videos. 2 videos had comments disabled."_

**Output:** the script writes `out/comments_<channel-or-batch>_<timestamp>.json` (the stripped comment objects) and `out/run_meta.json` (videos requested, comments per video, total comment counts, comments-disabled list, per-video errors, and a `quota_exceeded` flag). Raw JSON on disk means you can re-analyze without re-spending quota.

### Step 4 — Analyze (classify → dedupe → cluster → rank)

**Load `prompts/analyze.md` and follow it.** That file is the classifier brain. It contains the full operational definitions, examples, and the ranking formula. In short, you will, against the `out/*.json` data:

1. **Classify every comment into exactly one of 3 buckets:**
   - **PRAISE** — approval/enjoyment with no actionable signal. Counted, then discarded from the digest.
   - **TROLL / TOXIC** — hostile, bad-faith, spam, or off-topic with nothing to act on. Discarded; never quoted back unless the creator asks.
   - **CONSTRUCTIVE CRITICISM** — a **specific, good-faith, actionable** claim about something the creator could change (audio, pacing, lighting, factual errors, on-screen text, etc.). **This is the product.**

   The single most important rule: **harsh tone is NOT the same as a troll.** A brutal comment with a real, fixable claim ("the audio is unlistenable, fix your gain staging") is CONSTRUCTIVE, not a troll. Tone is not the classifier axis — a fixable good-faith claim is.

2. **Dedupe by canonical claim, not by raw text** — many people say the same thing in different words; count them as one point. Count distinct voices by `author_channel_id`, never by raw comment count (so one obsessive commenter can't inflate a theme).

3. **Cluster** the deduped claims into a small stable set of themes (Audio, Pacing/Length, Video/Lighting, Structure/Clarity, Factual Accuracy, etc.).

4. **Rank** themes most-painful-first, weighting distinct-commenter count most heavily, then likes (silent agreement), then severity and actionability. Collapse each theme into one headline insight with a concrete fix.

Tell the creator what you're doing in one line: _"Sorting praise from trolls from the stuff you can actually use…"_

### Step 5 — Deliver, then go deeper

Lead with a one-line shape-of-it, then the ranked insights, hardest-but-useful first. Per insight: theme + distinct-commenter count + total likes + severity, 2–3 **verbatim** example comments, and a concrete suggested fix. For example:

> **#1 · Audio is too quiet / voice gets buried** — 11 people raised this (+340 likes). Severity: high.
> _"Great info but I had to max my volume and your voice still clips."_
> _"bg music way louder than you the whole video"_
> **Fix:** Raise your voice gain and add a compressor; aim for ~-14 LUFS integrated. Duck the music ~12 dB under your voice.

After the list, add a framing line so it doesn't feel like a pile-on:

> "That's the signal. For context: of 4,210 comments, ~3,100 were praise and ~260 were trolls I filtered out — so this is the ~850 that actually had a point, boiled down to 6 themes."

Then **offer to go deeper**:

> "Want me to pull every comment behind any of these themes? Compare your last 5 videos to see if one of these is getting worse? Or draft a pinned comment addressing the top issue?"

---

## Tone & UX the agent should use

- **Be honest, not nice.** The creator explicitly asked for the harsh-but-useful truth. Don't soften the criticism into mush.
- **But don't editorialize cruelty.** Quote trolls only if explicitly asked. Default to summarizing, not pasting, the toxic bucket.
- **Always pair a problem with a concrete fix.** Never hand someone a complaint without a next step.
- **Conversational, not a form.** Greet, explain, ask scope, confirm, fetch, analyze, deliver, offer more — adapting tone to the creator.
- **Cite real counts and real comments.** Every number comes from the fetched data; every quoted example is a verbatim string from `out/`. Never invent a comment, a count, or a like total.

---

## Troubleshooting

- **No API key / "API key not valid":** the script exits with code **3**. Re-check Step 1 — most often the key is correct but the **YouTube Data API v3 wasn't enabled** for the project (Step 1.3), or an HTTP-referrer restriction was added by mistake (remove it). Tell the creator the exact thing to fix.
- **`quotaExceeded` (HTTP 403):** the script exits **2** after writing whatever it already fetched. Tell the creator how much got pulled and that **quota resets at midnight US Pacific** — re-run then, or scope smaller (fewer videos, lower `--per-video-cap`). Their partial data in `out/` is still analyzable now.
- **Comments disabled on a video (HTTP 403 `commentsDisabled`):** not an error — the script skips that video, notes it in `run_meta.json`, and continues. Mention which videos had comments off, then move on.
- **Channel not found / handle didn't resolve, or `--videos` had no usable links:** the script exits **4** (it uses the same code when a channel can't be resolved and when `--videos` contained no recognisable video IDs). Ask the creator to paste their channel URL directly, or their `UC...` channel ID, or just specific video links instead (the `--videos` path skips channel resolution entirely).
- **Private/unlisted videos:** an API key can only read **public** videos' comments. If a video link doesn't return comments, confirm it's public.
- **Everything came back as praise/trolls, nothing constructive:** that can be real (small or very positive comment set). Say so honestly rather than manufacturing criticism — and offer to widen scope to more videos.

---

## Repo layout

```
honest-comments/
├── README.md                # this file — the agent operating manual
├── STARTER_PROMPT.md        # the tiny paste that kicks it all off
├── scripts/
│   └── fetch_comments.py     # the only code that runs; stdlib-only Python
├── prompts/
│   └── analyze.md            # the classifier + ranking + conversation playbook
├── site/                     # static landing page (GitHub Pages)
├── out/                      # gitignored — your fetched comments land here
└── LICENSE                   # MIT
```

---

_Open source. BYO-agent, BYO-key, no backend. Read every line before you run it._
