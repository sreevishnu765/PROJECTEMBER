"""
ember_tools/vision_ocr.py
============================
Adapted from jarvisforember/modules/ocr_module.py.

The original hardcodes a single NEMOTRON_API_KEY env var with no fallback —
if that one key is missing or rate-limited, OCR simply fails with a string
error, which is exactly the single-point-of-failure pattern your tiered
provider chain in llm_client.py exists to avoid.

This version takes an injected vision_call_fn: Callable[[bytes, str, str], str]
(image_bytes, mime_type, query) -> response text, so it can run through
whatever vision-capable tier your llm_client.py already prefers (Gemini
vision is already used for image description in the reviewed repo's
memory/add endpoint, so you likely already have a vision call site to
reuse). The NVIDIA Nemotron path is kept below as an OPTIONAL extra tier,
not the only option — pass it in as vision_call_fn if you want it, or write
a thin wrapper around your Gemini vision call and pass that instead.
"""

import os
import base64
import time
from typing import Callable, Optional

MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

DEFAULT_QUERY = "Please extract all text and analyze the document."


def nvidia_nemotron_vision_call(image_bytes: bytes, mime_type: str, query: str, api_key: Optional[str] = None) -> str:
    """
    Optional extra tier, ported from the original ocr_module.py's request
    logic. Only used if you explicitly pass this as vision_call_fn — not
    wired in as a hidden default, since it introduces a new dependency
    (NVIDIA API key) your other providers don't need.
    """
    import requests

    key = api_key or os.getenv("NVIDIA_API_KEY") or os.getenv("NEMOTRON_API_KEY")
    if not key:
        raise RuntimeError("No NVIDIA/Nemotron API key configured.")

    b64_data = base64.b64encode(image_bytes).decode("utf-8")
    image_url = f"data:{mime_type};base64,{b64_data}"

    response = requests.post(
        "https://integrate.api.nvidia.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": query},
                    ],
                }
            ],
            "model": "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
            "temperature": 0.1,
            "top_p": 1.0,
            "max_tokens": 2048,
            "stream": False,
        },
        timeout=30,
    )
    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"]
    raise RuntimeError(f"Nemotron API failed: {response.status_code} - {response.text[:200]}")


def analyze_document(
    image_path: str,
    vision_call_fn: Callable[[bytes, str, str], str],
    query: str = DEFAULT_QUERY,
) -> str:
    """
    vision_call_fn should be a thin wrapper around whichever vision-capable
    tier of your existing provider chain you want to use as primary — e.g.:

        def gemini_vision_call(image_bytes, mime_type, query):
            # reuse your existing genai.Client(...).models.generate_content
            # pattern here, same as the memory/add vision path
            ...

        result = analyze_document(path, vision_call_fn=gemini_vision_call)
    """
    if not os.path.exists(image_path):
        return f"File not found: {image_path}"

    ext = os.path.splitext(image_path)[1].lower()
    mime_type = MIME_MAP.get(ext, "image/png")

    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        return vision_call_fn(image_bytes, mime_type, query)
    except Exception as e:
        return f"Failed to analyze document: {e}"


def capture_screenshot(output_path: "str | None" = None) -> "str | None":
    """Captures the current screen to a PNG file and returns its path, or
    None on failure — never raises. Picked up as the missing half of
    brief item #8: analyze_document() above could already look at an
    EXISTING image file, but Ember had no way to actually see the live
    screen ("Ember, look at this screenshot and tell me what's wrong"
    implies Ember takes the screenshot itself, not that the user has
    already saved one to disk somewhere and knows the path).

    Uses Pillow's ImageGrab, which is Windows/macOS only (no X11 support)
    — consistent with this project's actual Windows deployment target, so
    no new platform-support burden beyond what already exists elsewhere
    (app_launcher.py, spotify.py are Windows-only for the same reason).
    Deliberately NOT wired to any confirmation gate: capturing the
    CURRENT user's own screen on their own request, to analyze locally
    via their own configured vision tier, isn't a destructive or
    third-party-facing action — consistent with how launch_app/read_file
    aren't gated either, only delete/execute-class actions are."""
    try:
        from PIL import ImageGrab
    except ImportError:
        print("[vision_ocr] Pillow isn't installed — screenshot capture needs `pip install pillow`.")
        return None

    if output_path is None:
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "screenshots")
        os.makedirs(data_dir, exist_ok=True)
        output_path = os.path.join(data_dir, f"screenshot_{int(time.time())}.png")

    try:
        image = ImageGrab.grab()
        image.save(output_path, "PNG")
        return output_path
    except Exception as e:
        print(f"[vision_ocr] Screenshot capture failed: {e}")
        return None


def analyze_screenshot(
    vision_call_fn: Callable[[bytes, str, str], str],
    query: str = "Describe what's on screen and point out anything that looks wrong or worth attention.",
) -> str:
    """Captures the current screen and analyzes it in one step — this is
    the actual capability behind "Ember, look at this screenshot and tell
    me what's wrong," rather than requiring the user to have already
    saved a screenshot file and know its path (which is what
    analyze_document() alone would require)."""
    path = capture_screenshot()
    if path is None:
        return "Couldn't capture the screen, sir — see the console for why."
    return analyze_document(path, vision_call_fn, query=query)
