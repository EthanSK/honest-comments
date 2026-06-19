<!--
  STARTER_PROMPT.md — the tiny paste a creator drops into Codex CLI or Claude Code.
  Intentionally minimal: one fetch + one instruction. ALL the real logic lives in
  README.md so we can ship updates without anyone re-pasting this.
-->

# Get started

Copy the prompt below and paste it into your **Codex CLI** or **Claude Code** session. That's the whole setup — the agent fetches the operating manual and walks you through the rest conversationally.

```
You are running "honest-comments". Your VERY FIRST action: clone the repo and
work from inside it (the script and prompts live there, not in this folder):

  git clone https://github.com/EthanSK/honest-comments && cd honest-comments

If git is unavailable, download and unzip the repo instead, then cd into it:

  curl -L -o honest-comments.zip https://github.com/EthanSK/honest-comments/archive/refs/heads/main.zip
  unzip honest-comments.zip && cd honest-comments-main

Then read README.md from this local checkout in full and follow it as your
instructions. Run every later command from inside this cloned directory.

I'm a YouTube creator. Your job: dig through my comments and surface the
criticism that's actually worth acting on — ranked, with concrete fixes. Skip
the praise, filter out the trolls, and don't soften the useful-but-harsh stuff.

Once you've read the manual, run the whole flow conversationally:
1. Greet me and explain what you're about to do.
2. Help me get Codex or Claude ready and paste this; then, per the README, run
   scripts/youtube_login.py to sign me in to YouTube (it opens my browser, I
   click Allow once, and tokens are saved locally). The login goes to Google —
   nothing goes to honest-comments.
3. Confirm scope (run scripts/fetch_comments.py with --dry-run to show me the
   estimate), then fetch with scripts/fetch_comments.py — use --mine to grab my
   own channel now that I'm logged in.
4. Apply prompts/analyze.md to classify, dedupe, cluster, and rank the
   constructive criticism — ignoring praise and trolls.
5. Ask how I want it first — a short summary, or the top N (you choose how
   many) ranked — then deliver the harsh-but-useful takes that way, and offer
   to go deeper.

Be honest, not nice. Begin now by greeting me.
```
