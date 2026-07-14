# realtime-voice — hybrid voice assistant (gpt-realtime mouth/ears, Claude brain)

Live spoken conversation from a phone browser, ChatGPT-AVM style, where OpenAI's
`gpt-realtime` handles speech and small talk while **Claude stays the brain**: any
substantive question goes through an `ask_claude` function to a headless Claude
session on your box (via the [telegram-gateway](../telegram-gateway) bridge), and
the realtime model relays the answer in its own voice.

Works with **no public IP and no VPS** — every connection is outbound: the browser
talks to OpenAI directly over WebRTC (audio never touches your server), the server
only mints ephemeral tokens and answers tool calls, and the page itself is exposed
through an outbound tunnel (Tailscale Funnel or `cloudflared`).

## Features

- **~300 ms voice conversation** in any language the model speaks; voice picker
  (male/female) — the voice locks per session, an OpenAI limitation.
- **`ask_claude` tool** → full-tool Claude turn with persistent memory; answers are
  cached semantically (see `qa_cache.py` in telegram-gateway) so repeat questions
  return in ~0.1 s.
- **`show_media` tool** → "show me X" renders product photos/videos inline from a
  local CLIP index (sub-second, no LLM), served via one-shot tokens with HTTP Range
  support (iOS video).
- **`open_camera` tool** → in-page camera (getUserMedia — works from a voice
  command, unlike file pickers), tap-to-snap; the photo shows in chat, saves
  server-side, forwards to a Telegram group, and Claude can *see* it for 10 min
  ("what part is this?"). After a snap the bot stays silent 5 s so the user can
  give instructions; if they don't, it asks what to do with the photo.
- **`clear_chat` tool** → "clear screen" wipes the transcript silently; "clear
  context / new chat" also resets the Claude session and reconnects fresh.
- **Speakerphone-proof:** semantic VAD, echo cancellation, half-duplex mic gating
  (mic muted while the bot talks — toggleable for headphones), shutter/tap noise
  suppression, cancel-stray-responses window after photos.
- **Full transcripts** (both sides) archived into the chat-archive SQLite log;
  audio never touches the box.
- **Auth:** random secret path + HTTP Basic (any username casing) over the
  tunnel's HTTPS; the OpenAI key never reaches the browser (10-min ephemeral
  tokens minted server-side).

## Install

1. Copy `src/` somewhere (e.g. `~/DST/voice/realtime/`), alongside a working
   [telegram-gateway](../telegram-gateway) checkout (the server imports its
   `bridge`, `qa_cache`, and `tg_api` modules — adjust the `sys.path` line).
2. Secrets in `~/.config/dst/secrets.env`: `OPENAI_API_KEY=...` and
   `VOICE_APP_PASSWORD=...`. Env: `VOICE_APP_USER` (basic-auth user),
   `TG_OWNER_NAME`, `TG_VOICE_CHAT` (Telegram chat id for photo forwards, 0 = off).
3. Expose 127.0.0.1:8478 with `tailscale funnel --bg 8478` (stable URL) or
   `./cloudflared tunnel --url http://127.0.0.1:8478` (ephemeral).
4. `./start.sh` — prints the full secret URL (also written to `url.txt`);
   `--rotate` mints a new secret path, `--quick` uses the cloudflared fallback.
   Add an `@reboot` cron for persistence.

Media search expects a warm CLIP server on 127.0.0.1:8477 (see
[clip-media-search](../clip-media-search)); without it, `show_media` just returns
no results and everything else works.
