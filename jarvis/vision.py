"""Ask a vision model about an image (the screen, by default). Local Ollama model or Claude."""
from __future__ import annotations

import base64
import io
import logging
import os

import requests

from .config import Section, local_url

log = logging.getLogger(__name__)

PROMPT = ("You are looking at the user's computer screen for a voice assistant. Answer the question in two or "
          "three short spoken sentences, with no markdown. Read out exact text (errors, numbers, names) when it "
          "matters. Question: {question}")


def grab_screen(max_side: int = 1600, all_screens: bool = False) -> bytes:
    """Screenshot as JPEG bytes, downscaled so the model gets a reasonable number of image tokens."""
    from PIL import ImageGrab

    img = ImageGrab.grab(all_screens=all_screens).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def describe(cfg: Section, image: bytes, question: str) -> str:
    v = cfg.get("vision") or {}
    provider = v.get("provider", "ollama")
    prompt = PROMPT.format(question=question or "What's on the screen?")
    b64 = base64.b64encode(image).decode()

    if provider == "ollama":
        o = (cfg.llm.get("providers") or {}).get("ollama") or {}
        body = {
            "model": v.get("model") or o.get("model", "qwen3.5:4b"),
            "messages": [{"role": "user", "content": prompt, "images": [b64]}],
            "stream": False, "think": False, "keep_alive": o.get("keep_alive", "30m"),
            "options": {"temperature": 0.2, "num_ctx": o.get("num_ctx", 8192)},
        }
        r = requests.post(f"{local_url(o.get('base_url', 'http://127.0.0.1:11434')).rstrip('/')}/api/chat", json=body, timeout=180)
        r.raise_for_status()
        return r.json()["message"]["content"].strip()

    if provider == "claude":
        import anthropic

        c = (cfg.llm.get("providers") or {}).get("claude") or {}
        key = os.environ.get(c.get("api_key_env") or "ANTHROPIC_API_KEY")
        client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        model = v.get("model") or c.get("model", "claude-opus-5-5")
        kwargs = dict(
            model=model, max_tokens=4096, output_config={"effort": c.get("effort", "low")},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": prompt}]}])
        if model.startswith("claude-haiku"):
            response = client.messages.create(**kwargs)
        else:
            response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                   fallbacks="default", **kwargs)
        return " ".join(b.text for b in response.content if b.type == "text").strip()

    raise ValueError(f"unknown vision provider {provider!r}")
