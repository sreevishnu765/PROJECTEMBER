"""
ember_transport.py
====================
Ember's first non-CLI transport — a minimal, authenticated WebSocket
server. Per the next-phase brief's item #4/#12: Ember has been CLI-only
until now (deliberately — Nexus VII's server.py wasn't ported, since
there was no frontend to plumb into yet). This is that surface, built to
the minimum this session's prerequisite list actually needed: something
a remote client (a future voice pipeline, a future custom frontend) can
connect to, send a message, receive STREAMED tokens back, and send a
cancel signal that genuinely interrupts generation — not a general
API surface with every capability exposed yet.

Protocol (JSON messages over one WebSocket connection):
  Client -> server, first message, required:
    {"type": "auth", "token": "..."}
  Client -> server, after auth:
    {"type": "message", "text": "..."}
    {"type": "cancel"}
    {"type": "confirm", "request_id": "...", "approved": true|false}
    {"type": "query", "id": "...", "name": "...", "params": {...}}
    {"type": "voice", "action": "listen"|"mode"|"voice"|"speaker"|"stop"|"state", ...}
    <binary frame>   mic audio, mono PCM16 @ 16 kHz (only after {"type":"voice","action":"listen","on":true})
  Server -> client:
    {"type": "auth_ok"} / {"type": "auth_failed"}
    {"type": "chunk", "text": "..."}
    {"type": "status", "text": "..."}
    {"type": "done", "tag": "...", "text": "..."}
    {"type": "confirmation_required", "request_id": "...", "tool_name": "...", "args": {...}}
    {"type": "query_result", "id": "...", "ok": true, "data": {...}}
    {"type": "query_result", "id": "...", "ok": false, "error": "..."}
    {"type": "error", "message": "..."}
    voice_state / voice_event / transcript / audio_chunk / audio_stop / voice_duck /
    voice_unavailable — see ember_voice.py's module docstring for their shapes.

Voice (new this session): the VoiceSession is created lazily, on the first
{"type":"voice"} message, so a client that never uses voice pays nothing and
a machine without the voice models/packages still runs chat exactly as
before (the client just gets {"type":"voice_unavailable","reason":...}).
A voice-originated turn goes through the SAME _start_turn()/_run_turn()
path a typed message does — one turn at a time per connection, same
cancel flag, same confirmation gate — the voice layer only adds the
begin_reply/feed_reply/end_reply hooks that turn the reply into speech.

"query"/"query_result" (new this session) is the structured-data
counterpart to "message"/"done" — see ember_query_registry.py's module
docstring for why a UI panel (Systems, Usage, Memory, ...) needs this
instead of typing a sentence at the chat and parsing prose back out.
"id" is caller-chosen and echoed back verbatim so a client that fires
several queries at once (e.g. a panel loading multiple widgets) can match
each response to its request; it is NOT the confirmation "request_id"
concept below and the two are never confused since they arrive as
different message types. A destructive query (memory_forget) reuses this
connection's own confirm_gate — the SAME "confirmation_required" /
"confirm" round trip already used for destructive chat-triggered tools,
not a second confirmation mechanism.

"status" messages (new this session) are transient progress notes sent
during an otherwise-silent, slow step — a Tavily research() round, or an
unavoidable fall-back to a fully blocking, non-streamed generation for
forced native/tool-based grounding when no evidence could be pre-fetched
(see ember_core.py's _emit_stream_status). They are NOT part of the
reply: never append them to whatever "chunk" text has been received so
far, and the final "done" message's `text` field never includes them
either. Render them as a transient indicator (e.g. "Ember is searching
for that...") that disappears once real "chunk"/"done" content arrives.

Token provisioning (new): EMBER_TRANSPORT_TOKEN used to have to be exported by hand in every terminal,
and forgetting it was the most common way to fail to start. ensure_token() now takes it from the
environment, else from the project's .env, else GENERATES a random one (24 bytes, url-safe), saves it to
.env and uses it. That is still not an "unauthenticated mode": every client must present the token; the
change is only who has to remember to create it. voice_client.py reads the same .env automatically.

Baseline auth, not the final security review: an unauthenticated
WebSocket port is a live hole the moment it's reachable, not "premature
security work" to defer — that's a different thing from the planned,
later, whole-system security audit. EMBER_TRANSPORT_TOKEN must be set in
the environment for this server to start at all; there is no
"unauthenticated mode." A client's first message must be exactly
{"type": "auth", "token": "<matching value>"} or the connection is closed
immediately. This is a shared-secret check, not a real per-user auth
system (no accounts, no per-client permissions, no rate limiting, no
TLS termination handled here) — those are legitimate, separate,
larger pieces of the eventual real security-review phase, not
reproduced here. What this DOES prevent: anyone on the network who
doesn't have the token from talking to Ember at all.

One EmberConversation per connection, created on connect and closed on
disconnect — this is the direct payoff of this session's conversation-
model work: a WebSocket client's history/cancel-state/confirmation gate
are fully isolated from the CLI's and from every other connected client,
sharing only the durable backend (memory, reminders, tools), exactly the
split ember_conversation.py's docstring describes.
"""

import asyncio
import json
import os
import secrets
import threading
import uuid
from pathlib import Path

import ember_confirmation
from ember_bus import get_bus
from ember_conversation import get_conversation_registry
from ember_query_registry import get_query_registry
from ember_attachments import save_attachments, default_upload_dir

AUTH_TOKEN_ENV = "EMBER_TRANSPORT_TOKEN"
AUTH_TIMEOUT_SECONDS = 10
# websockets' own default is 1 MiB, which silently closes the connection on any
# real chat attachment (files travel base64-encoded in one JSON message, ~1.33x
# their size). ember_attachments.MAX_TOTAL_BYTES is 15 MB, so ~20 MB on the wire.
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


class TransportAuthError(Exception):
    pass


def _get_required_token() -> str:
    token = os.environ.get(AUTH_TOKEN_ENV)
    if not token:
        raise TransportAuthError(
            f"{AUTH_TOKEN_ENV} is not set — refusing to start the transport server "
            f"unauthenticated. Set it to a real shared secret before enabling remote access."
        )
    return token


def _dotenv_path() -> Path:
    return Path(__file__).resolve().parent / ".env"


def read_dotenv_value(path, key: str) -> "str | None":
    """Value of KEY in a simple KEY=VALUE .env file (comments and quotes tolerated), else None."""
    try:
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip().strip("'\"") or None
    except OSError:
        pass
    return None


def ensure_token(env_path=None) -> str:
    """Guarantees EMBER_TRANSPORT_TOKEN exists for this process and returns it: environment first, then
    the .env file, else a freshly generated random token that is saved to .env (and set in the
    environment). If .env can't be written the token still works for this run; a warning says so."""
    token = os.environ.get(AUTH_TOKEN_ENV)
    if token:
        return token
    path = Path(env_path) if env_path else _dotenv_path()
    token = read_dotenv_value(path, AUTH_TOKEN_ENV)
    if not token:
        token = secrets.token_urlsafe(24)
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            with open(path, "a", encoding="utf-8") as f:
                f.write(("" if not existing or existing.endswith("\n") else "\n") + f"{AUTH_TOKEN_ENV}={token}\n")
            print(f"[ember_transport] {AUTH_TOKEN_ENV} wasn't set, so one was generated and saved to {path}. "
                  f"voice_client.py reads it from there automatically.")
        except OSError as e:
            print(f"[ember_transport] {AUTH_TOKEN_ENV} wasn't set and {path} isn't writable ({e}); using a temporary "
                  f"token for this run only — clients must be given it explicitly.")
    os.environ[AUTH_TOKEN_ENV] = token
    return token


async def _handle_connection(websocket, process_turn_fn, voice_engines=None, enable_voice=True):
    """One coroutine per connected client. process_turn_fn is injected
    (ember_core.process_turn) rather than imported directly, so this
    module has no import-time dependency on ember_core.py's full startup
    sequence (memory DB, reminder store, proactive engine, ...) — a
    transport test can pass a fake turn function instead."""
    required_token = _get_required_token()

    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=AUTH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await websocket.close(code=4001, reason="auth timeout")
        return

    try:
        first_msg = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.close(code=4002, reason="invalid first message")
        return

    if first_msg.get("type") != "auth" or first_msg.get("token") != required_token:
        # Deliberately vague on the wire (no "wrong token" vs "wrong
        # format" distinction) — no reason to help an unauthenticated
        # caller narrow down what's wrong.
        await websocket.send(json.dumps({"type": "auth_failed"}))
        await websocket.close(code=4003, reason="auth failed")
        return

    await websocket.send(json.dumps({"type": "auth_ok"}))

    session_id = f"ws-{uuid.uuid4().hex[:12]}"
    bus = get_bus()
    confirm_gate = ember_confirmation.SessionConfirmationGate(bus=bus)
    registry = get_conversation_registry()
    conversation = registry.get_or_create(session_id)
    conversation.confirm_gate = confirm_gate
    loop = asyncio.get_running_loop()

    # Deliver this connection's own confirmation prompts without leaking
    # them to every other connected client — subscribe once per
    # connection, filter by session_id, unsubscribe on disconnect.
    def _on_confirmation_requested(event_type, payload):
        if payload.get("session_id") != session_id:
            return
        asyncio.run_coroutine_threadsafe(
            websocket.send(json.dumps({"type": "confirmation_required", **payload})),
            loop,
        )

    bus.subscribe("confirmation.requested", _on_confirmation_requested)

    turn_task: "asyncio.Task | None" = None
    query_registry = get_query_registry()

    # ---- voice plumbing --------------------------------------------------
    voice = None
    voice_failed_reason: "str | None" = None

    def _send_threadsafe(msg: dict) -> None:
        # Called from VoiceSession's worker threads — same pattern as
        # _stream_to_client below.
        asyncio.run_coroutine_threadsafe(websocket.send(json.dumps(msg)), loop)

    def _turn_active() -> bool:
        t = turn_task
        return t is not None and not t.done()

    def _request_cancel(source: str) -> None:
        """Cancel only means something while a turn is running. A cancel that
        arrives with nothing in flight (a stop-button click after the reply
        finished, a stray "stop") used to leave the flag set, and any cancel
        that lands a moment into the NEXT turn made that turn answer
        "Cancelled, sir." for no reason. The log line says who asked, so a
        spurious cancel can be traced."""
        active = _turn_active()
        print(f"[ember_transport] cancel requested by {source} (turn {'active' if active else 'idle - ignored'})")
        if active:
            conversation.request_cancel()

    def _ensure_voice():
        """Creates this connection's VoiceSession on first use. Returns None
        (and remembers why) if voice can't run on this machine — chat is
        unaffected either way."""
        nonlocal voice, voice_failed_reason
        if voice is not None or voice_failed_reason is not None:
            return voice
        if not enable_voice:
            voice_failed_reason = "voice is disabled on this server"
            return None
        try:
            from ember_voice import VoiceEngines, VoiceSession
            engines = voice_engines or VoiceEngines.shared()
            session = VoiceSession(
                send=_send_threadsafe,
                submit_turn=_submit_turn_from_voice,
                cancel_turn=lambda: _request_cancel("voice"),
                turn_active=_turn_active,
                engines=engines,
            )
            session.start()
            voice = session
        except Exception as e:
            voice_failed_reason = f"{type(e).__name__}: {e}"
            print(f"[ember_transport] voice unavailable for {session_id}: {voice_failed_reason}")
        return voice

    async def _start_turn(text: str, spoken: "bool | None" = None) -> bool:
        """The single place a turn gets started, typed or spoken. False if
        one is already in flight on this connection.

        `spoken` says whether this turn's reply will be read aloud; it's
        published as conversation.spoken_reply so process_turn can ask the
        model for a speech-friendly answer (short, no markdown). Typed turns
        are spoken only if the client turned the speaker on."""
        nonlocal turn_task
        if turn_task is not None and not turn_task.done():
            return False
        if spoken is None:
            spoken = voice is not None and voice.speaker_enabled
        conversation.spoken_reply = bool(spoken)
        turn_task = asyncio.create_task(_run_turn(text))
        return True

    def _submit_turn_from_voice(text: str) -> bool:
        # Runs on a VoiceSession worker thread (never the event loop thread —
        # .result() would deadlock it), so hop onto the loop to start the task.
        try:
            return asyncio.run_coroutine_threadsafe(_start_turn(text, spoken=True), loop).result(timeout=5)
        except Exception as e:
            print(f"[ember_transport] couldn't start voice turn: {e}")
            return False

    async def _run_turn(text: str):
        v = voice
        if v is not None:
            v.begin_reply()

        def _stream_to_client(delta, kind="text", _loop=loop, _ws=websocket):
            # kind="status" -> a transient progress note (see the module
            # docstring's "status" entry above), sent as its own message
            # type so the client never confuses it with real reply
            # content. kind="text" (the default, and the only kind that
            # existed before this session) is an actual incremental
            # delta of the reply, unchanged from before.
            message_type = "status" if kind == "status" else "chunk"
            asyncio.run_coroutine_threadsafe(
                _ws.send(json.dumps({"type": message_type, "text": delta})), _loop
            )
            if v is not None:
                v.feed_reply(delta, kind)
        try:
            tag, reply_text = await asyncio.to_thread(
                process_turn_fn, text, conversation, _stream_to_client
            )
            if v is not None:
                v.end_reply(reply_text)
            await websocket.send(json.dumps({"type": "done", "tag": tag, "text": reply_text}))
        except Exception as e:
            if v is not None:
                v.end_reply("")
            await websocket.send(json.dumps({"type": "error", "message": str(e)}))

    async def _run_query(query_id: str, name: str, params: dict):
        # Run off the event loop thread, same reason _run_turn does:
        # a destructive query blocks on confirm_gate.request_confirmation()
        # (a threading.Event wait) until this connection's own "confirm"
        # message arrives — if that ran ON the event loop, this coroutine
        # would deadlock itself, since the loop needs to keep running to
        # ever RECEIVE that "confirm" message in the first place.
        #
        # Fired as its own task, not serialized against turn_task — a
        # panel query (read-only, almost always) has no reason to wait
        # behind an in-flight chat reply, and EmberMemory/quota state are
        # already thread-safe for concurrent access from either path.
        result = await asyncio.to_thread(
            query_registry.dispatch, name, params, confirm_gate, conversation
        )
        await websocket.send(json.dumps({"type": "query_result", "id": query_id, **result}))

    try:
        async for raw_msg in websocket:
            if isinstance(raw_msg, (bytes, bytearray)):
                # Mic audio. feed_audio() is non-blocking (bounded queue,
                # drops oldest), so it's safe to call on the event loop.
                if voice is not None:
                    voice.feed_audio(bytes(raw_msg))
                continue
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                await websocket.send(json.dumps({"type": "error", "message": "invalid JSON"}))
                continue

            msg_type = msg.get("type")

            if msg_type == "cancel":
                _request_cancel("client")
                if voice is not None:
                    voice.interrupt()   # also silences any audio already queued/playing
                continue

            if msg_type == "voice":
                v = _ensure_voice()
                if v is None:
                    await websocket.send(json.dumps({"type": "voice_unavailable", "reason": voice_failed_reason}))
                else:
                    v.handle_control(msg)
                continue

            if msg_type == "confirm":
                confirm_gate.resolve(msg.get("request_id", ""), bool(msg.get("approved")))
                continue

            if msg_type == "query":
                query_id = msg.get("id", "")
                name = msg.get("name", "")
                params = msg.get("params") or {}
                if not name:
                    await websocket.send(json.dumps({"type": "query_result", "id": query_id, "ok": False, "error": "missing query name"}))
                    continue
                # Not awaited directly — see _run_query's own comment on
                # why a destructive query needs the event loop free to
                # keep receiving this connection's "confirm" reply.
                asyncio.create_task(_run_query(query_id, name, params))
                continue

            if msg_type != "message":
                await websocket.send(json.dumps({"type": "error", "message": f"unknown message type: {msg_type}"}))
                continue

            text = msg.get("text", "")
            raw_atts = msg.get("attachments") or []
            if not text.strip() and not raw_atts:
                continue

            if raw_atts:
                # Checked BEFORE saving anything, so a rejected "already in
                # progress" message never leaves orphaned files in data/uploads.
                if _turn_active():
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "a turn is already in progress on this connection — send 'cancel' first if you want to redirect it.",
                    }))
                    continue
                saved, att_error = await asyncio.to_thread(save_attachments, raw_atts, default_upload_dir())
                if att_error:
                    await websocket.send(json.dumps({"type": "error", "message": att_error}))
                    continue
                # Read (and cleared) by ember_core.process_turn once per turn.
                conversation.attachments = saved
                if not text.strip():
                    text = "Please take a look at the attached file(s)."

            if not await _start_turn(text):
                conversation.attachments = None
                # One turn at a time per connection — a second "message"
                # arriving mid-turn is rejected rather than silently
                # racing two process_turn_fn calls against the same
                # conversation's history. A real client sends "cancel"
                # first if it wants to interrupt and redirect, exactly
                # the workflow this whole layer exists to support.
                await websocket.send(json.dumps({
                    "type": "error",
                    "message": "a turn is already in progress on this connection — send 'cancel' first if you want to redirect it.",
                }))
                continue

    finally:
        if voice is not None:
            voice.stop()
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
        bus.unsubscribe("confirmation.requested", _on_confirmation_requested)
        registry.close(session_id)


async def run_transport_server(process_turn_fn, host: str = "localhost", port: int = 8765,
                               enable_voice: bool = True, voice_engines=None):
    """Starts the WebSocket server. A token is always in force (see ensure_token and the module
    docstring): there is no unauthenticated mode.

    host defaults to "localhost", not "0.0.0.0" — binding to all
    interfaces (actually reachable from other devices on the LAN, which
    real multi-device connectivity needs) is an explicit choice the
    caller has to make, not this function's default, since it directly
    changes who can even attempt to authenticate at all."""
    ensure_token()  # from the environment / .env, or generated and saved — never "no token"
    import websockets

    if enable_voice and voice_engines is None:
        # Startup banner + background model warm-up, so the first wake word
        # doesn't pay a multi-second model load (Whisper downloads ~150 MB the
        # very first time). Chat starts immediately either way.
        try:
            from ember_voice import VoiceEngines, voice_status
            ready, summary = voice_status()
            print(f"[ember_transport] Voice: {summary}")
            if ready:
                threading.Thread(target=VoiceEngines.shared().warm_up, name="ember-voice-warmup", daemon=True).start()
        except Exception as e:
            print(f"[ember_transport] Voice: unavailable ({e})")

    async def handler(websocket):
        await _handle_connection(websocket, process_turn_fn, voice_engines=voice_engines, enable_voice=enable_voice)

    async with websockets.serve(handler, host, port, max_size=MAX_MESSAGE_BYTES):
        print(f"[ember_transport] WebSocket server listening on ws://{host}:{port}")
        await asyncio.Future()  # run forever
