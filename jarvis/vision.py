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


def vision_model(cfg: Section) -> tuple[str, list[str]]:
    """The Ollama model to look with, and what it can do: vision.model if set, else the brain's model if it can
    see, else any installed model that can. Raises ToolFailed (said to the user) when none can."""
    from .llm.ollama_models import OllamaUnavailable, best_model, configured_model, find, has, list_models, ollama_url
    from .tools.builtin import ToolFailed

    wanted, brain = (cfg.get("vision") or {}).get("model"), configured_model(cfg)
    try:
        models = list_models(ollama_url(cfg))
    except OllamaUnavailable:
        return wanted or brain, []           # the request below says what's wrong
    hit = find(models, wanted) if wanted else None
    if hit:
        return hit["name"], hit["capabilities"]
    if wanted:
        log.warning("vision.model %s isn't pulled; looking for another model that can see", wanted)
    chat = best_model(models, brain)         # the brain's model ("auto" resolved the same way)
    if chat and (has(chat, "vision", False) or not any(m["capabilities"] for m in models)):
        return chat["name"], chat["capabilities"]   # (an Ollama too old to say: try the brain's model, as before)
    seeing = best_model([m for m in models if has(m, "vision", False)])
    if seeing:
        return seeing["name"], seeing["capabilities"]
    raise ToolFailed("None of your Ollama models can see images. Pull one that can, for example "
                     "ollama pull qwen3.5:4b or ollama pull gemma3, or set vision provider to claude.")


def describe(cfg: Section, image: bytes, question: str) -> str:
    v = cfg.get("vision") or {}
    provider = v.get("provider", "ollama")
    prompt = PROMPT.format(question=question or "What's on the screen?")
    b64 = base64.b64encode(image).decode()

    if provider == "ollama":
        o = (cfg.llm.get("providers") or {}).get("ollama") or {}
        model, caps = vision_model(cfg)
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt, "images": [b64]}],
            "stream": False, "keep_alive": o.get("keep_alive", "30m"),
            "options": {"temperature": 0.2, "num_ctx": o.get("num_ctx", 8192)},
        }
        if not caps or "thinking" in caps:     # only thinking models take the switch
            body["think"] = False
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
