# Ember — Chat frontend (thin slice)

This is the first real slice of Ember's frontend: chat only, connected to
the actual `ember_transport.py` WebSocket server — no mocked backend calls
anywhere in this slice. Systems, Devices, Memory, Projects, History, HUD,
and voice are deliberately **not** built yet; the sidebar shows them as
disabled "soon" entries rather than pretending they work, since none of
those have a backend surface today (only the CLI and the WebSocket
transport exist).

## Run it

```bash
npm install
npm run dev
```

Opens at `http://localhost:5173`. On load it asks for:
- **Server address** — `ws://localhost:8765` by default (matches
  `ember_transport.py`'s own default host/port).
- **Access token** — whatever you've set `EMBER_TRANSPORT_TOKEN` to on the
  backend. Stored only in this browser's `localStorage`, sent only to the
  server address you entered, never anywhere else.

## Backend side — one real gap to close first

`ember_transport.py` defines `run_transport_server(process_turn_fn, host, port)`
but nothing in the project currently *calls* it — `ember_core.py`'s `run()`
is still the CLI-only entry point. You'll need a small entry script to
actually start the WebSocket server against your real `process_turn`. This
frontend doesn't create or modify that file — here's the shape it needs,
for you to wire in wherever you want the actual daemon entry point to live:

```python
import asyncio
import ember_core
from ember_transport import run_transport_server

if __name__ == "__main__":
    ember_core.startup_check()
    runtime = ember_core._build_runtime()
    runtime.start()
    asyncio.run(run_transport_server(ember_core.process_turn, host="localhost", port=8765))
```

`EMBER_TRANSPORT_TOKEN` must be set in that process's environment before
this starts — `run_transport_server` refuses to bind a socket without it
(see that module's own docstring).

## What's actually wired up

- Real auth handshake (`{"type":"auth","token":...}` first, or the
  connection is refused) — matches `ember_transport.py` exactly.
- Real streaming: `chunk` messages render incrementally; a `status` message
  (if your backend sends one — see the note below) shows as a transient
  "Searching…"-style line above the reply.
- Real cancellation: the stop button sends `{"type":"cancel"}` over the
  same connection your turn is running on.
- Real destructive-tool confirmation: a `confirmation_required` message
  pops the approve/deny dialog and answers via `{"type":"confirm",...}`.
- The header's provider dot (Cloud / Fallback / Local) is derived directly
  from the `tag` string `process_turn` actually returns — nothing invented.

**One thing to double check on the backend:** your notes mention
`ember_transport.py`'s `_stream_to_client` was updated to forward a
`kind`/`status` distinction as `{"type":"status",...}`, but the
`ember_transport.py` provided to build this against still only ever sends
`{"type":"chunk",...}`. This frontend handles a `status` message type if
it arrives, but won't break if it never does — text will just stream
without the intermediate "Searching…" line. Worth confirming which version
of that file is actually deployed.

## Not built in this slice (on purpose)

Attach/upload, voice input, Settings page, and all sidebar sections other
than Chat are visible but inert — greyed out or labeled "soon" rather than
faked. Wire them up once their backend counterparts exist.
