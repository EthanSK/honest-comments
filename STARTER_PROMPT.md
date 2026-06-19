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
2. Help me get a free YouTube Data API key if I don't already have one.
   Prefer setting it as an env var or in a .env file so it stays out of chat
   logs; the script uses it to call Google's YouTube API. It never goes to
   honest-comments.
3. Ask whether I want my whole channel or specific videos, and confirm scope
   (run scripts/fetch_comments.py with --dry-run to show me the quota estimate)
   before fetching anything.
4. Run scripts/fetch_comments.py to pull my comments.
5. Apply prompts/analyze.md to classify, dedupe, cluster, and rank the
   constructive criticism — ignoring praise and trolls.
6. Deliver the ranked harsh-but-useful takes, then offer to go deeper.

Be honest, not nice. Begin now by greeting me.
```
