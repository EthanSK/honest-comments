# analyze.md — The Classifier + Delivery Playbook

<!--
  This file is the BRAIN of honest-comments. You (the agent) load it AFTER
  `scripts/fetch_comments.py` has written the fetched comments to `out/*.json`,
  and you execute everything below against that JSON using YOUR OWN model
  (the creator's AI subscription — we host nothing).

  Your job, in one breath:
    classify every comment 3 ways → throw away praise + trolls →
    dedupe + cluster the real criticism into themes →
    rank the themes by how many DISTINCT people raised them (+ severity/actionability) →
    deliver a ranked, harsh-but-fair digest, each theme paired with a concrete fix.

  The creator asked for HONEST. Do not soften the criticism into mush. But never
  editorialize cruelty — quote a troll only if they explicitly ask. Every count
  and every quoted comment MUST be real and verbatim from `out/*.json`. Never
  fabricate a comment, a count, or a theme. If you're unsure, undercount.
-->

## 0. Input you're working from

Load the newest `out/comments_*.json`. Each element has these fields (and only these):

- `comment_id` — unique id for the comment.
- `video_id` — which video it's on.
- `text` — the comment body (plain text).
- `author` — display name (NOT a reliable identity; names collide).
- `author_channel_id` — **the identity key. This is how you count "distinct people." Names lie; channel IDs don't.** It can occasionally be **missing/blank** (the API doesn't always return it). When it's blank, do NOT treat all blank-id comments as one person — that would collapse dozens of distinct anonymous voices into a single voice and badly undercount a theme. Instead use a **synthetic identity** of `"missing:" + comment_id` so each anonymous comment counts as its own distinct voice. Define this once and apply it everywhere you count distinct people (see §3 and the schemas in §2.5).
- `like_count` — how many viewers liked it (a proxy for silent agreement).
- `published_at` — timestamp.
- `reply_count` — number of replies on the thread.

Also load `out/run_meta.json` for context (videos fetched, comments-disabled list, total comment count) — you'll cite these totals in the closing framing line (§6, step 7).

---

## 1. THE 3-WAY CLASSIFICATION

Classify **every** comment into **exactly one** of three buckets. This 3-way split is the entire reason this tool exists — ordinary sentiment tools collapse the middle bucket (the gold) into "negative" alongside the poison (trolls), so creators either ignore all negativity and miss the fix, or marinate in abuse and feel awful. You separate them.

### Bucket A — PRAISE  *(discard from the digest, but COUNT it)*

Approval / enjoyment / support with **no specific, actionable signal** about what to improve. Pure compliments, fan expression, emoji, favourite-moment timestamps, "more please."

Examples:
- "This is exactly what I needed, thank you!!" → PRAISE
- "🔥🔥🔥 underrated channel" → PRAISE
- "the part at 4:32 had me dying" → PRAISE
- "love your energy bro keep going" → PRAISE

**Edge rule — buried ask flips to CONSTRUCTIVE.** Praise carrying an actionable clause is NOT praise:
- "Love it, but please use a pop filter" → the *but* clause is actionable → **CONSTRUCTIVE** (theme: Audio).

### Bucket C — TOXIC / TROLL  *(discard, and NEVER quote back unless explicitly asked)*

Hostile, insulting, bad-faith, off-topic, spam, or self-promo, with **no actionable content**. The test: *does this tell the creator something they could act on, in good faith?* If it's just an insult with no fixable claim, it's a troll.

Examples:
- "this is garbage, kys" → TROLL
- "ratio + you fell off" → TROLL
- "first" / "who's here in 2026" → TROLL (off-topic noise)
- "check out my channel for better content" → TROLL (spam / self-promo)
- "nobody asked" → TROLL

**Edge rule — HARSH ≠ TROLL. Tone is NOT the classifier axis.** This is the single most important distinction in the whole product, and the one every competitor gets wrong. A brutal, stinging comment that contains a real, fixable, good-faith claim is **CONSTRUCTIVE**, not troll:
- "Honestly the audio is unlistenable, I cranked my volume to max and your voice still clips. Fix your gain staging." → harsh tone, concrete fixable claim → **CONSTRUCTIVE** (Audio).
- "your editing is so boring i clicked off at 2 min" → stings, but it's a real retention/pacing signal → **CONSTRUCTIVE** (Pacing/Length).

Do NOT downgrade a comment to TROLL just because it's rude. Downgrade only when there's nothing to act on.

### Bucket B — CONSTRUCTIVE CRITICISM  *(THE PRODUCT — keep, dedupe, cluster, rank)*

A **specific, good-faith, actionable** claim about something the creator could change: audio, video/lighting, pacing, structure, length, factual errors, clarity, on-screen text, thumbnail/title, missing info, captions/accessibility, etc.

A comment is CONSTRUCTIVE only if **all three** hold:
1. **Specific** — points at an identifiable aspect, not a vibe. ("audio too quiet" ✓ / "didn't like it" ✗).
2. **Actionable** — implies a change the creator could actually make.
3. **Good faith** — aimed at improving the work, not wounding the person.

Examples:
- "Great info but the background music is way louder than your voice." → CONSTRUCTIVE (Audio)
- "Would've loved timestamps in the description for a 40-min video." → CONSTRUCTIVE (Description/Links/Timestamps)
- "At 6:10 you said React 18 but that API is React 19." → CONSTRUCTIVE (Factual Accuracy)
- "The intro is 90 seconds before you get to the point — tighten it." → CONSTRUCTIVE (Pacing/Length)
- "Hard to read the code, font's too small on mobile." → CONSTRUCTIVE (On-screen Text/Legibility)

---

## 2. CLASSIFICATION MECHANICS

### 2.1 Cheap pre-filter (save tokens — do this before any model reasoning)

Route the obvious noise straight to a bucket without deliberation:
- Pure emoji, "first", "who's here in <year>", bare favourite-moment timestamps, <~4 words of pure compliment → **PRAISE (A)** or **TROLL (C)** as appropriate.
- Obvious spam / self-promo links → **TROLL (C)**.
- **Do NOT over-filter.** Anything borderline — especially anything with a noun that could be an aspect of the video (audio, mic, lighting, pacing, intro, font, caption, fact) — goes to full classification. When in doubt, classify properly.

### 2.2 Batched classification

Process the remaining comments in **batches of ~40–60 per model pass** (keeps context tight and output reliable). For each comment emit:

- `bucket` — `A` | `B` | `C`.
- For **bucket B only**, additionally:
  - `theme` — free-text label (you'll normalize onto the taxonomy in §4).
  - `claim` — a one-line **canonical restatement** of what they're asking for (e.g. "audio too quiet", "intro too long", "code font too small on mobile"). This canonical claim — NOT the raw text — is your dedupe key.
  - `severity` — 1–5 (how much it hurts the viewer experience / how broken it is).
  - `actionability` — 1–5 (how concretely and cheaply the creator could fix it).

Keep `like_count` and `author_channel_id` attached to every bucket-B comment — both feed ranking.

### 2.5 MAP / REDUCE — work through files, not all in context at once

**This is operationally critical.** Real channels return thousands of comments — far more than you can hold in context at once. If you try to load every comment and reason over them in one pass you WILL stall, truncate, or hallucinate counts. So you process incrementally and **write intermediate JSON files to `out/`**, in this exact pipeline:

```
out/comments_*.json   (input: raw fetched comments)
        │  MAP: classify in batches
        ▼
out/classified_*.json (one record per comment, bucket + canonical_claim)
        │  REDUCE 1: collapse claims
        ▼
out/claims.json       (one record per distinct canonical claim)
        │  REDUCE 2: cluster claims into themes
        ▼
out/themes.json       (one record per theme — ready to rank + deliver)
```

Each stage reads the file the previous stage wrote and appends/writes the next file. You never need more than one batch of raw comments in context at a time, and you can resume mid-run if interrupted.

#### MAP — `out/classified_<label>_<batch>.json`

Classify the raw comments in **batches of ~40–60** (per §2.2). Append each batch's results to `out/classified_*.json` as you go (one file per source label, or one per batch — either is fine; the reduce step globs them all). **One record per comment**, schema:

```json
{
  "comment_id": "UgxAbc123",
  "video_id": "dQw4w9WgXcQ",
  "author_channel_id": "UCxxxx",          // raw value; may be "" if blank
  "identity": "UCxxxx",                    // = author_channel_id, OR "missing:<comment_id>" when blank (§3 rule)
  "text": "the audio is way too quiet",   // verbatim, for later example-quoting
  "likes": 12,                             // = like_count
  "bucket": "constructive",                // one of: "praise" | "troll" | "constructive"
  "canonical_claim": "audio too quiet"     // REQUIRED for "constructive"; null/omit for praise|troll
}
```

Only `bucket: "constructive"` records carry a `canonical_claim`. Praise/troll records still get written (so the closing-framing totals in §6 step 7 are exact), just with no claim.

#### REDUCE 1 — `out/claims.json`

In ONE reasoning pass over all `constructive` records, merge semantically-identical `canonical_claim`s into **distinct claims** (per §3). Write `out/claims.json` — **one record per distinct claim**:

```json
{
  "canonical_claim": "audio too quiet",
  "comment_ids": ["UgxAbc123", "UgxDef456"],          // every comment supporting this claim
  "distinct_author_channel_ids": ["UCxxxx", "missing:UgxDef456"],  // dedup of `identity` (NOT raw channel id — applies the blank-id rule)
  "distinct_commenters": 2,                            // = len(distinct_author_channel_ids)
  "sum_likes": 47,                                     // summed `likes` across comment_ids
  "avg_severity": 4.0,                                 // averaged over supporting comments
  "avg_actionability": 4.5
}
```

`distinct_commenters` is the length of the deduped `identity` set — this is where the "1 obsessive commenter ≠ 5 voices" and "each blank-id comment is its own voice" rules actually bite.

#### REDUCE 2 — `out/themes.json`

Cluster the distinct claims onto the §4 taxonomy. Write `out/themes.json` — **one record per theme**:

```json
{
  "theme": "Audio",
  "claims": ["audio too quiet", "background music too loud"],   // canonical_claims rolled up
  "distinct_commenters": 11,            // distinct identities ACROSS all the theme's claims (re-dedup; don't just sum claim counts — the same person may span two claims)
  "total_likes": 340,                   // summed likes across the theme's comments
  "avg_severity": 4.2,
  "avg_actionability": 4.0,
  "severity": "high",                   // low|medium|high bucket of avg_severity, for display
  "example_comment_ids": ["UgxAbc123", "UgxGhi789"],   // 2–3 clearest/highest-liked, for verbatim quoting in §6
  "suggested_fix": "Raise voice gain, add a compressor, target ~-14 LUFS; duck music ~12 dB."
}
```

**Important:** `theme.distinct_commenters` must be a fresh dedup of identities across ALL comments in the theme — one person who complained about two different audio things counts **once**, not twice.

You then rank `out/themes.json` (§5) and deliver from it (§6), pulling the verbatim example `text` for each `example_comment_id` back out of `out/classified_*.json`.

---

## 3. DEDUPE — collapse near-identical critiques into distinct POINTS

Many people say the same thing in different words. You want **distinct points**, not distinct strings.

- **Dedupe on the canonical `claim`, never on raw text.** Two comments with the same `claim` ("audio too quiet") are the SAME critique even if worded completely differently.
- Do this as a single "merge these claims into distinct points" reasoning pass over all bucket-B `claim`s. You ARE the LLM — semantic grouping in one pass is simpler and better than embeddings/cosine math, and adds no dependencies.
- **Count distinctness by `author_channel_id`, NEVER by comment count.** If one person leaves the same complaint three times, that's **1** distinct voice — not 3. This stops a single obsessive (or angry) commenter from inflating a theme.
- **Blank `author_channel_id` → synthetic identity.** When `author_channel_id` is missing/empty, use `"missing:" + comment_id` as the identity. Each anonymous comment is then its OWN distinct voice (they're different people), instead of all blanks collapsing into one undercounted "person". Apply this exact rule wherever you compute `distinct_author_channel_ids` in the schemas below.

---

## 4. CLUSTER — group deduped claims into themes

Map the distinct points onto this seed taxonomy (you may add a new theme if something genuinely doesn't fit, but prefer the existing ones):

`Audio` · `Video/Lighting` · `Pacing/Length` · `Structure/Clarity` · `Factual Accuracy` · `On-screen Text/Legibility` · `Thumbnail/Title` · `Captions/Accessibility` · `Description/Links/Timestamps` · `Content Depth` · `Other`

For each theme, aggregate (this is exactly the `out/themes.json` record from §2.5 REDUCE 2):
- `distinct_commenters` — count of distinct identities (per §3: `author_channel_id`, or `"missing:"+comment_id` when blank) across all comments in the theme. **Re-dedup across the theme** — one person spanning two of the theme's claims counts once.
- `total_likes` — total `like_count` across the theme's comments.
- `avg_severity` and `avg_actionability` — averaged over the theme's deduped claims.
- `example_comment_ids` — 2–3 **representative** comments (clearest-stated and/or highest-liked); you quote their verbatim `text` (looked up from `out/classified_*.json`) in §6. Never paraphrase into quotes.
- a concrete **suggested fix** (see §6).

---

## 5. RANK — surface most-painful-first

Score each theme with this blended formula and sort **descending**:

```
theme_score =  (distinct_commenters  × 10)        # consensus DOMINATES — this is the headline signal
             + ln(1 + total_likes)                # silent agreement, LOG-DAMPED so one viral comment can't dominate
             + (avg_severity          × 2)        # how badly it hurts the experience
             + (avg_actionability     × 2)        # how fixable it is right now
```

**Why these weights (apply them consistently):**

- **Distinct commenters are weighted ×10** — they MUST dominate. The whole product thesis is "many voices → one ranked line": 11 different people raising the audio is the headline, and it must outrank one lonely severe complaint. ×10 makes each additional distinct person worth ~10 points.
- **Likes use `ln(1 + total_likes)` (natural-log / `log1p`), NOT a linear term.** This is the fix for the old bug where likes swamped commenter count: a single +1000-like comment used to add ~500 points and bury dozens of distinct voices. Log-damped, +1000 likes adds only `ln(1001) ≈ 6.9` points — roughly the weight of **less than one** extra distinct commenter. So likes nudge ties; they never override consensus. (10 likes ≈ 2.4 pts, 100 ≈ 4.6, 1000 ≈ 6.9, 10000 ≈ 9.2 — it grows brutally slowly on purpose.)
- **Severity + actionability (×2 each)** tilt toward "worth fixing AND fixable now" without ever outweighing how many people actually said it.

Sanity check the intent: a theme with **5 distinct commenters and 20 likes** (`50 + ~3.0 = ~53` before sev/action) should beat a theme with **1 commenter and 5000 likes** (`10 + ~8.5 = ~18.5`). Distinct-commenter count wins — exactly as the priority below states.

Ranking priorities, in order of weight:
1. **Distinct-commenter count dominates** (the ×10 term). "11 different people said your audio is too quiet" is the headline insight and must outrank one severe-but-lonely complaint. This is the core collapse of the whole tool: **many voices → one ranked line.**
2. **Likes** are a soft, log-damped nudge (silent agreement) — they break ties, never decide the ranking.
3. **Severity + actionability** tilt toward "worth fixing AND fixable now."

The collapse in one line, by example:
> 11 voices → "**Audio is too quiet** (11 people, +340 likes). Fix: raise voice gain / add a compressor; target ~-14 LUFS."

Keep the top **3–7 themes**. Don't pad — if only 4 real themes exist, deliver 4.

---

## 6. OUTPUT FORMAT — the ranked digest for the creator

Deliver conversationally, hardest-but-useful first. Lead with a one-line shape-of-it, then the ranked insights, then a framing line, then the drill-down menu.

**Per insight, use exactly this shape:**

> **#1 · Audio is too quiet / voice gets buried** — 11 people raised this (+340 likes). Severity: high.
> _"Great info but I had to max my volume and your voice still clips."_
> _"bg music way louder than you the whole video"_
> **Fix:** Raise your voice gain and add a compressor; aim for ~-14 LUFS integrated. Duck the music ~12 dB under your voice.
>
> **#2 · Intro is too long** — 7 people. Severity: medium.
> _"90 seconds of intro before the actual content, please get to the point"_
> **Fix:** Cut the cold-open to <15s; put the payoff promise in the first 10 seconds.

Each insight MUST carry:
- **rank + theme** (a tight, plain-English headline, not a taxonomy label dump).
- **how many DISTINCT people raised it** + total likes (the consensus number is the punch).
- **severity** (low / medium / high).
- **2–3 verbatim example comments** — real, unedited, lifted straight from `out/*.json`. Never paraphrase an example into quotes. Never invent one.
- **a concrete suggested fix** — specific and doable, not "improve your audio." Name the actual lever (gain/compressor/LUFS target, cut intro to <15s, add timestamps to the description, bump the on-screen font / increase contrast, correct the fact at <timestamp>, add captions, etc.). Every problem MUST be paired with a fix.

**Step 7 — closing framing line** (so it doesn't read as a pile-on). Use the real totals from `run_meta.json`:
> "That's the signal. For context: of 4,210 comments, ~3,100 were praise and ~260 were trolls I filtered out — so this is the ~850 that actually had a point, boiled down to 6 themes."

**Step 8 — offer to go deeper:**
> "Want me to pull every comment behind any of these themes? Compare your last 5 videos to see if one of these is getting worse? Or draft a pinned comment addressing the top issue?"

Drill-downs you can offer: full verbatim dump for one theme, per-video trend, flip to "what people are LOVING" (bucket A) on request, or draft a response / community post / pinned comment.

---

## 7. HARD RULES (non-negotiable)

- **Never fabricate.** Every count, every theme, every quoted example must be real and verbatim from `out/*.json`. If you can't back a number with the data, don't state it. When uncertain, **undercount** — credibility beats drama.
- **Distinct people = distinct `author_channel_id`.** Never report comment counts as if they were people.
- **Harsh-with-a-fixable-claim is CONSTRUCTIVE, not troll.** Tone is never the classifier. Re-read §1 Bucket C edge rule before downgrading anything to troll.
- **Never quote a troll back to the creator** unless they explicitly ask to see them.
- **Always pair every problem with a concrete fix.** No naked criticism.
- **Be honest and direct — the creator WANTS the harsh truth — but stay fair.** Don't soften real criticism into vague mush, and don't manufacture cruelty. State what people actually said, how many said it, and what to do about it.
