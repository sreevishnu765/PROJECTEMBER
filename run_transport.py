import asyncio
import ember_core
from ember_transport import run_transport_server

if __name__ == "__main__":
    ember_core.startup_check()
    runtime = ember_core._build_runtime()
    runtime.start()
    ember_core.warm_up_models()   # load the embedding model + open the Gemini connection now, not inside your first turn
    asyncio.run(run_transport_server(ember_core.process_turn, host="localhost", port=8765))