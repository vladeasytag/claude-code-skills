"""Hybrid realtime voice server (2026-07-13).

Live spoken conversation like ChatGPT's Advanced Voice Mode, with Claude as the
brain: the phone's browser opens this page and connects DIRECTLY to OpenAI's
gpt-realtime over WebRTC (speech in/out, ~300ms). The realtime model is only the
mouth and ears — for anything that needs real knowledge (DST business, products,
customers, email, files) it calls the ask_claude tool, which the page forwards
here and we run a normal Claude turn via the Telegram bridge (persistent
"realtime-voice" session).

No public IP / VPS required: every connection is OUTBOUND (browser->OpenAI
WebRTC, box->OpenAI for token minting, cloudflared->Cloudflare for the tunnel
that exposes this page over HTTPS). Nothing listens on a public address.

All endpoints live under a random secret path prefix (./.secret, printed by
start.sh): GET /<secret>/ (the app), POST /<secret>/session (mint a 10-min
ephemeral Realtime token — the real API key never reaches the browser),
POST /<secret>/ask (the Claude bridge). Restart start.sh to rotate the URL.
"""
import datetime, json, mimetypes, os, re, sys, threading, time, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/DST/telegram"))
sys.path.insert(0, os.path.expanduser("~/DST/chatlog"))
import bridge
import qa_cache
import tg_api as TG
try:
    import chatdb           # searchable chat archive; best-effort like the gateway
except Exception:
    chatdb = None

PORT = 8478
MODEL = "gpt-realtime"
# The voice is locked once a session starts speaking (OpenAI limitation) — it can
# only be chosen up front, so the page offers a picker and sends it to /session.
VOICES = {"cedar", "echo", "verse", "marin", "coral"}
VOICE = "cedar"                         # default voice; see VOICES
BRIDGE_CHAT = "realtime-voice"          # persistent Claude session key
SECRET = open(os.path.join(DIR, ".secret")).read().strip()

def _secret(name):
    for line in open(os.path.expanduser("~/.config/dst/secrets.env")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{name} not in secrets.env")

OPENAI_KEY = _secret("OPENAI_API_KEY")
# HTTP Basic auth on every route (on top of the secret path). Safe over the
# funnel's HTTPS; the browser caches it and attaches it to the page's fetches.
import base64
# Phones auto-capitalize the username field — accept any casing of it.
_USER = os.environ.get("VOICE_APP_USER", "user")
AUTH = {"Basic " + base64.b64encode((u + ":" + _secret("VOICE_APP_PASSWORD")).encode()).decode()
        for u in {_USER, _USER.lower(), _USER.upper(), _USER.capitalize()}}

INSTRUCTIONS = (
    "You are the live voice interface of Claude, the AI assistant of DST (Digital "
    "Sign Technologies — printhead maintenance equipment). You are speaking with "
    "the owner. Match whatever language they speak — English, "
    "Russian, but Spanish, German, French or any other language works the same — "
    "and switch when he does. Keep replies short and conversational: this is speech, "
    "not text.\n\n"
    "You yourself are mostly the mouth and ears, with one exception: the QUICK "
    "REFERENCE at the end of these instructions. Questions it answers (prices, "
    "product basics, company facts) you answer DIRECTLY, instantly, without any "
    "tool — that's the fast path. For everything else involving DST's business, "
    "customers, emails, documents, the state of the server, or anything requiring "
    "knowledge beyond the reference or an action, you MUST call the ask_claude tool "
    "and then relay its answer faithfully in your own voice (translate to the spoken "
    "language if needed, condense long answers to their substance). Never guess or "
    "extrapolate beyond the reference — a price or spec not listed there goes to "
    "ask_claude. "
    "Claude can take ten seconds or more — say a brief 'one moment' phrase BEFORE "
    "calling the tool. Only pure small talk (greetings, 'how are you', chit-chat) may "
    "be answered without the tool. Never invent facts about DST; when in doubt, ask "
    "Claude.\n\n"
    "When the user asks to SEE something — a photo, picture, image or video of a "
    "product or part — call show_media at once (instant, no 'one moment'). After "
    "media appears, say two-three words at most ('Here you go') — never describe or "
    "enumerate what's on screen unless the user explicitly asks.\n\n"
    "If asked to change your voice: explain that the voice is fixed for the current "
    "call — pick a different one in the dropdown next to Start, then reconnect.\n\n"
    "Turn discipline: the user is often on a speakerphone. If an input sounds like an "
    "echo of your own last words, or is unintelligible noise, do NOT reply — stay "
    "silent and wait. Never switch language on your own: switch only when the user "
    "clearly speaks a full sentence in the other language. System state-update notes "
    "arrive in English — they are NOT user speech, never respond to them aloud and "
    "never let them change the conversation language: always use the language of the "
    "user's most recent spoken sentence."
)

TOOLS = [{
    "type": "function",
    "name": "open_camera",
    "description": "Open the phone's camera. Call IMMEDIATELY and UNCONDITIONALLY "
                   "every time the user says 'open camera' / 'take a photo' / 'take "
                   "a picture' / 'start camera' (RU: 'открой камеру', 'сделай "
                   "фото/снимок') — even if you believe the camera is already open "
                   "(it closes itself after every shot; re-calling is always safe "
                   "and just reopens it). Never answer 'it is already open'. Once "
                   "the user snaps, the photo appears in the chat, is saved on the "
                   "DST server, and forwarded to Telegram. Say a few words like "
                   "'camera is opening'. If they then ask what's ON the photo, use "
                   "ask_claude — Claude can see it.",
    "parameters": {"type": "object", "properties": {}},
}, {
    "type": "function",
    "name": "clear_chat",
    "description": "Clear the conversation. Call IMMEDIATELY when the user asks — "
                   "scope 'screen' for 'clear screen' / 'clear chat' / 'очисти экран' "
                   "/ 'очисти чат' (wipes the on-screen transcript only); scope "
                   "'context' for 'clear context' / 'clear history' / 'start a new "
                   "chat' / 'очисти контекст' / 'новый чат' (also erases Claude's "
                   "conversation memory and reconnects fresh). After scope 'screen', "
                   "say NOTHING AT ALL — total silence, no 'done', no confirmation. "
                   "Only scope 'context' gets a two-word confirmation.",
    "parameters": {
        "type": "object",
        "properties": {"scope": {"type": "string", "enum": ["screen", "context"]}},
        "required": ["scope"],
    },
}, {
    "type": "function",
    "name": "show_media",
    "description": "Display photos/videos from DST's product media library on the "
                   "user's screen. Call IMMEDIATELY whenever the user asks to see or "
                   "show a photo, picture, image or video of something — it is instant, "
                   "do NOT say 'one moment' and do NOT use ask_claude for this. After "
                   "the result, say ONLY two-three words ('Here you go' / 'Вот, "
                   "пожалуйста' — or 'nothing found'). NEVER describe the images, read "
                   "captions, or list what appeared — the user can see the screen; "
                   "describe only if they explicitly ask.",
    "parameters": {
        "type": "object",
        "properties": {"query": {
            "type": "string",
            "description": "What to show, as short ENGLISH keywords (translate if the "
                           "user spoke Russian), e.g. 'ricoh gen5 head cable'. Spell "
                           "DST product names EXACTLY — the main families are PHD "
                           "(Print Head Doctor), PHT (Print Head Tester), PHD Connect "
                           "(boards), PHD-LE, PHT-M, PG-17, Fluid S1/S2. If the user "
                           "says something like 'PDT' or 'BHD' they almost certainly "
                           "mean PHD.",
        }},
        "required": ["query"],
    },
}, {
    "type": "function",
    "name": "ask_claude",
    "description": "Ask Claude (the real DST assistant with full workspace, email, KB "
                    "and tool access) anything. Use for every substantive question or "
                    "action. Claude keeps conversation memory across calls.",
    "parameters": {
        "type": "object",
        "properties": {"question": {
            "type": "string",
            "description": "The user's request, restated self-contained (include "
                           "context the user implied). Keep the user's language.",
        }},
        "required": ["question"],
    },
}]

# Quick-reference digest injected into the session instructions: the full price
# list + company blurb + the most-asked cached Q&As, rebuilt at most every 5 min —
# so easy questions are answered by the realtime model itself, zero round-trip.
KB = os.path.expanduser("~/DST/knowledge-base")
_digest = {"text": "", "ts": 0}


def kb_digest():
    if _digest["text"] and time.time() - _digest["ts"] < 300:
        return _digest["text"]
    parts = []
    try:
        parts.append("### DST price list (current)\n"
                     + open(os.path.join(KB, "products", "price-list.md")).read())
    except Exception:
        pass
    try:
        parts.append("### Company basics\n"
                     + open(os.path.join(KB, "company", "company-profile.md")).read()[:1500])
    except Exception:
        pass
    try:
        rows = qa_cache._cx().execute(
            "SELECT question, answer FROM qa WHERE hits > 0 "
            "ORDER BY hits DESC LIMIT 25").fetchall()
        if rows:
            parts.append("### Frequently asked\n" + "\n".join(
                f"Q: {q}\nA: {a[:400]}" for q, a in rows))
    except Exception:
        pass
    _digest.update(text="\n\n".join(parts), ts=time.time())
    return _digest["text"]


def session_body(voice):
    return {
        "expires_after": {"anchor": "created_at", "seconds": 600},
        "session": {
            "type": "realtime",
            "model": MODEL,
            "instructions": INSTRUCTIONS
                + "\n\n==== QUICK REFERENCE — answer these directly and instantly, "
                  "no tool call ====\n\n" + kb_digest(),
            "tools": TOOLS,
            "audio": {
                # semantic_vad ignores partial noise/echo fragments far better than
                # the default server_vad — key for speakerphone use.
                "input": {"transcription": {"model": "gpt-4o-mini-transcribe"},
                          "turn_detection": {"type": "semantic_vad", "eagerness": "medium"}},
                "output": {"voice": voice if voice in VOICES else VOICE},
            },
        },
    }


def mint_client_secret(voice=VOICE):
    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/client_secrets",
        data=json.dumps(session_body(voice)).encode(),
        headers={"Authorization": f"Bearer {OPENAI_KEY}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# Media search: warm CLIP server (~10ms) over the KB media library. Files are
# exposed to the page via one-shot random tokens — no filesystem paths in URLs.
CLIP_URL = "http://127.0.0.1:8477/find"
VID_EXTS = (".mp4", ".mov", ".webm", ".m4v")
MEDIA_TOKENS = {}


def find_media(query, k=4, min_score=0.25, ratio=0.8):
    url = CLIP_URL + "?" + urllib.parse.urlencode({"q": query, "k": k})
    with urllib.request.urlopen(url, timeout=10) as r:
        hits = [h for h in json.load(r)["results"] if h["score"] >= min_score]
    if hits:                       # keep only hits close to the best one
        hits = [h for h in hits if h["score"] >= hits[0]["score"] * ratio]
    # Keyword hits are authoritative (exact product-code/tag match) — when any
    # exist, drop the fuzzier embedding-only hits so near-misses don't tag along.
    if any(h.get("kw") for h in hits):
        hits = [h for h in hits if h.get("kw")]
    items = []
    for h in hits:
        tok = uuid.uuid4().hex
        MEDIA_TOKENS[tok] = h["path"]
        items.append({"token": tok,
                      "kind": "video" if h["path"].lower().endswith(VID_EXTS) else "image",
                      "caption": h.get("annotation") or h.get("tags") or
                                 os.path.basename(h["path"])})
    return items


def archive(who, text):
    """Log one spoken line (both sides) to the chat archive under its own
    pseudo-chat, so voice sessions are searchable like any Telegram thread."""
    if chatdb and text and text.strip():
        chatdb.record(text, "in" if who == "you" else "out",
                      sender="owner" if who == "you" else "voice-model",
                      chat_id=BRIDGE_CHAT, chat_title="Realtime Voice", kind="voice")


# Camera photos land here (also forwarded to the Telegram group) and the freshest
# one is offered to Claude for 10 min, so "what is this part?" works by voice.
PHOTO_DIR = os.path.join(DIR, "camera")
VOICE_TG_CHAT = int(os.environ.get("TG_VOICE_CHAT", "0"))  # Telegram group for photo forwards
LAST_PHOTO = {"path": None, "ts": 0}


def save_camera_photo(data, ctype):
    os.makedirs(PHOTO_DIR, exist_ok=True)
    ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) or ".jpg"
    path = os.path.join(PHOTO_DIR,
                        datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ext)
    with open(path, "wb") as f:
        f.write(data)
    LAST_PHOTO.update(path=path, ts=time.time())
    archive("you", f"[took a camera photo — saved at {path}, also forwarded to the "
                   f"Voice Claude Telegram group]")
    try:
        with open(path, "rb") as fh:
            TG._call("sendPhoto", _files={"photo": fh}, _timeout=60,
                     chat_id=VOICE_TG_CHAT, caption="📷 from the voice app")
    except Exception as e:
        print(f"[server] telegram forward failed: {e}", flush=True)
    return path


def ask_claude(question):
    # While a fresh camera photo exists, questions may refer to it — point Claude at
    # the file and keep such turns OUT of the Q&A cache (both lookup and store).
    fresh_photo = LAST_PHOTO["path"] and time.time() - LAST_PHOTO["ts"] < 600
    if not fresh_photo:
        # Semantic Q&A cache: a repeat question (even reworded) returns in ~0.1s
        # instead of a full Claude turn. The realtime model restates questions
        # self-contained (tool description), so bridge questions cache safely.
        cached = qa_cache.lookup(question)
        if cached:
            print(f"[server] qa-cache HIT: {question[:80]!r}", flush=True)
            return cached
    prompt = ("[Live voice conversation (realtime web app): the user SPOKE this and your "
              "answer will be READ ALOUD by a voice model relaying you. Be brief and "
              "conversational — plain prose, no markdown, no lists, no code. Reply in "
              f"the user's language.]\n\n{question}")
    if fresh_photo:
        prompt += (f"\n\n[The user just took a photo with their phone camera, saved at "
                   f"{LAST_PHOTO['path']}. If the question refers to what they "
                   f"photographed ('this part', 'what is this'), open that image and "
                   f"look at it.]")
    answer = bridge.ask(BRIDGE_CHAT, prompt, sender=os.environ.get("TG_OWNER_NAME", "Owner") + " (realtime voice)")
    if not fresh_photo:
        qa_cache.store(question, answer, source="voice-app")
    return answer


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self):
        if self.headers.get("Authorization") not in AUTH:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="DST Voice"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return "unauthorized"
        if not self.path.startswith(f"/{SECRET}"):
            return None
        return self.path[len(SECRET) + 1:].split("?")[0] or "/"

    def _serve_media(self, path):
        """Stream a media file with Range support (iOS <video> requires 206s)."""
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        with open(path, "rb") as f:
            if rng and (rng.group(1) or rng.group(2)):
                start = int(rng.group(1) or 0)
                end = min(int(rng.group(2) or size - 1), size - 1)
                f.seek(start)
                data = f.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                data = f.read()
                self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def do_GET(self):
        r = self._route()
        if r == "unauthorized":
            return
        if r in ("/", "/index.html"):
            self._send(200, open(os.path.join(DIR, "index.html"), "rb").read(),
                       "text/html; charset=utf-8")
        elif r and r.startswith("/file/") and MEDIA_TOKENS.get(r[6:]):
            self._serve_media(MEDIA_TOKENS[r[6:]])
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        r = self._route()
        if r == "unauthorized":
            return
        try:
            if r == "/session":
                n = int(self.headers.get("Content-Length", 0))
                voice = (json.loads(self.rfile.read(n)) if n else {}).get("voice", VOICE)
                self._send(200, mint_client_secret(voice))
            elif r == "/media":
                n = int(self.headers.get("Content-Length", 0))
                q = json.loads(self.rfile.read(n))["query"]
                items = find_media(q)
                self.log_message("show_media: %r -> %d hit(s)", q, len(items))
                archive("you", f"[asked to see: {q}]")
                self._send(200, {"items": items})
            elif r == "/photo":
                n = int(self.headers.get("Content-Length", 0))
                path = save_camera_photo(self.rfile.read(n),
                                         self.headers.get("Content-Type", "image/jpeg"))
                self.log_message("camera photo saved: %s", os.path.basename(path))
                self._send(200, {"ok": True, "name": os.path.basename(path)})
            elif r == "/reset":
                bridge.reset(BRIDGE_CHAT)
                archive("you", "[cleared context — new conversation]")
                self.log_message("bridge context reset")
                self._send(200, {"ok": True})
            elif r == "/log":
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n))
                if d.get("who") == "diag":     # page-side diagnostics -> server.log only
                    self.log_message("DIAG %s", str(d.get("text", ""))[:300])
                else:
                    archive(d.get("who", ""), d.get("text", ""))
                self._send(200, {"ok": True})
            elif r == "/ask":
                n = int(self.headers.get("Content-Length", 0))
                q = json.loads(self.rfile.read(n))["question"]
                self.log_message("ask_claude: %s", q[:120])
                self._send(200, {"answer": ask_claude(q)})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self.log_message("ERROR %s: %s", r, e)
            self._send(500, {"error": str(e)[:300]})

    def log_message(self, fmt, *args):
        print("[server]", fmt % args, flush=True)


if __name__ == "__main__":
    print(f"[server] listening on 127.0.0.1:{PORT} path=/{SECRET}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
