"""Which Ollama models are installed, which one to use, and which one the user meant by voice.

Uses Ollama's own API: GET /api/tags lists the models, POST /api/show says what each one can do
("capabilities": completion, tools, vision, thinking, embedding...). Any installed model works as the brain;
the qwen models from hardware.py are only what setup suggests pulling when nothing suitable is installed.
"""
from __future__ import annotations

import difflib
import math
import re

import requests

from ..config import local_url

DEFAULT_URL = "http://127.0.0.1:11434"
TARGET_B = 4.0                 # with no other hint, a ~4B model answers quickly and still uses tools well

_SHOW: dict[tuple[str, str, str], dict] = {}   # (url, name, digest) -> /api/show answer; a digest never changes
_LAST: list[dict] = []         # the latest list, so the fast-path router can match names without a request

NUMBER_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
                "twenty": "20", "thirty": "30", "seventy": "70", "billion": "b", "million": "m"}
# how speech-to-text tends to write model names
SOUNDS_LIKE = {"quen": "qwen", "kwen": "qwen", "queen": "qwen", "lama": "llama", "lamma": "llama",
               "jemma": "gemma", "gamma": "gemma", "fi": "phi", "phy": "phi", "mistrial": "mistral",
               "mixed role": "mixtral", "deep sea": "deepseek"}
FILLER = {"the", "a", "an", "model", "models", "brain", "llm", "ollama", "local", "please", "instead", "version",
          "my", "that", "point", "dot"}


class OllamaUnavailable(RuntimeError):
    """Ollama isn't running (or not at the configured address)."""


def ollama_url(cfg) -> str:
    o = ((cfg.get("llm") or {}).get("providers") or {}).get("ollama") or {}
    return local_url(o.get("base_url") or DEFAULT_URL).rstrip("/")


def configured_model(cfg) -> str:
    """llm.providers.ollama.model: a tag the user picked, or "auto"."""
    o = ((cfg.get("llm") or {}).get("providers") or {}).get("ollama") or {}
    return str(o.get("model") or "auto").strip()


# ------------------------------------------------------------ discovery

def list_models(base_url: str = DEFAULT_URL, timeout: float = 3) -> list[dict]:
    """Installed models, sorted by name: {name, size (bytes), params ("3.2B"), family, capabilities}.
    capabilities is empty when this Ollama is too old to report them. Raises OllamaUnavailable."""
    base = local_url(base_url).rstrip("/")
    try:
        r = requests.get(f"{base}/api/tags", timeout=timeout)
        r.raise_for_status()
        tags = r.json().get("models") or []
    except (requests.RequestException, ValueError) as e:
        raise OllamaUnavailable(f"Ollama isn't running at {base}. Start the Ollama app.") from e
    models = []
    for t in tags:
        name = t.get("name") or t.get("model")
        if not name:
            continue
        show = _show(base, name, t.get("digest") or "", timeout)
        details = {**(t.get("details") or {}), **(show.get("details") or {})}
        models.append({"name": name, "size": int(t.get("size") or 0),
                       "params": str(details.get("parameter_size") or ""),
                       "family": str(details.get("family") or ""),
                       "capabilities": [str(c) for c in show.get("capabilities") or []]})
    models.sort(key=lambda m: m["name"])
    _LAST[:] = models
    return models


def _show(base: str, name: str, digest: str, timeout: float) -> dict:
    key = (base, name, digest)
    if key not in _SHOW:
        try:
            r = requests.post(f"{base}/api/show", json={"model": name}, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return {}            # not cached: asked again next time
        _SHOW[key] = {"capabilities": data.get("capabilities") or [], "details": data.get("details") or {}}
    return _SHOW[key]


def known_models() -> list[dict]:
    """The models found by the last successful list_models() (no request)."""
    return list(_LAST)


def has(m: dict, capability: str, unknown: bool = True) -> bool:
    """Whether a model can do something; `unknown` when Ollama didn't say (older versions)."""
    caps = m.get("capabilities") or []
    return capability in caps if caps else unknown


def is_chat(m: dict) -> bool:
    """Generates text (embedding-only models like nomic-embed-text can't be the brain)."""
    if m.get("capabilities"):
        return "completion" in m["capabilities"]
    return "embed" not in m["name"].lower() and "bert" not in m.get("family", "").lower()


def billions(text: str) -> float | None:
    """'3.2B' -> 3.2, '137M' -> 0.137, 'llama3.1:8b' -> 8.0, 'mixtral:8x7b' -> 56.0."""
    m = re.search(r"(?:^|[:\-_])(?:(\d+)x)?(\d+(?:\.\d+)?)([bmt])\b", (text or "").lower())
    if not m:
        return None
    n = float(m.group(2)) * int(m.group(1) or 1)
    return n * {"b": 1, "m": 0.001, "t": 1000}[m.group(3)]


def _size_b(m: dict) -> float | None:
    found = billions(m.get("params", "")) or billions(m["name"])
    return found or (m.get("size", 0) / 0.6e9 if m.get("size") else None)   # ~0.6 GB per 1B at 4-bit


def find(models: list[dict], name: str) -> dict | None:
    """Exact tag; 'llama3.2' also finds 'llama3.2:latest'."""
    name = (name or "").strip().lower()
    for m in models:
        tag = m["name"].lower()
        if name in (tag, tag.removesuffix(":latest")) or tag == f"{name}:latest":
            return m
    return None


# ------------------------------------------------------------ choosing

def ranked(models: list[dict], preferred: str | None = None) -> list[dict]:
    """Chat models, best first: `preferred` if it's installed, then the ones that can use tools (Jarvis needs
    them to act on the PC), nearest in size to `preferred`'s size (or ~4B)."""
    chat = [m for m in models if is_chat(m)]
    exact = find(chat, preferred) if preferred and preferred != "auto" else None
    target = billions(preferred or "") or TARGET_B

    def key(m):
        size = _size_b(m) or target
        return m is not exact, not has(m, "tools"), abs(math.log(size / target)), m["name"]
    return sorted(chat, key=key)


def best_model(models: list[dict], preferred: str | None = None) -> dict | None:
    """`preferred` when it's installed, else the best installed chat model (None if there's none)."""
    best = ranked(models, preferred)
    return best[0] if best else None


def suggestion() -> str:
    """The model setup suggests pulling on this PC (qwen; any installed model with tools works as well)."""
    try:
        from ..hardware import detect, recommend

        return recommend(detect()).ollama_model
    except Exception:  # noqa: BLE001  (no psutil, odd nvidia-smi output...)
        return "qwen3.5:4b"


def resolve(models: list[dict], wanted: str = "auto") -> tuple[dict | None, str]:
    """The model to run for a configured name ("auto" or a tag) and a note on anything worth saying
    (a fallback, no tools). Nothing usable installed: (None, what to pull)."""
    wanted = (wanted or "auto").strip()
    pick = best_model(models, wanted)
    if pick is None:
        pull = suggestion() if wanted == "auto" else wanted
        return None, f"No Ollama chat model is installed. Run: ollama pull {pull}"
    notes = []
    if wanted != "auto" and pick is not find(models, wanted):
        why = "can't chat" if find(models, wanted) else "isn't pulled"
        notes.append(f"{wanted} {why}, using {pick['name']}; pick another in the tray or with --model.")
    if not has(pick, "tools"):
        notes.append(f"{pick['name']} can't use tools, so only the quick commands will control the PC.")
    return pick, " ".join(notes)


def model_to_pull(base_url: str = DEFAULT_URL) -> str:
    """setup.ps1: the suggested model when no installed model can use tools, else '' (keep what you have)."""
    try:
        models = list_models(base_url)
    except OllamaUnavailable:
        models = []
    return "" if any(has(m, "tools") for m in models if is_chat(m)) else suggestion()


# ------------------------------------------------------------ names, as spoken

def _words(text: str) -> list[str]:
    """'llama3.2:3b' and 'llama three point two 3 billion' -> ['llama', '3', '2', '3', 'b']."""
    t = text.lower().split("/")[-1].replace(":latest", "")
    for heard, meant in SOUNDS_LIKE.items():
        t = re.sub(rf"\b{heard}\b", meant, t)
    out: list[str] = []
    for w in re.findall(r"[a-z]+|\d+", t):
        if w == "one" and not (out and out[-1].isdigit()):
            continue           # "the llama one" (but "three point one" is 3.1)
        w = NUMBER_WORDS.get(w, w)
        if w not in FILLER:
            out.append(w)
    return out


def _forms(name: str) -> set[str]:
    """What a tag can be called: its family ('llama'), its name ('llama32') and the full tag ('llama323b')."""
    base = _words(name.split(":")[0])
    return {f for f in (base[0] if base and base[0].isalpha() else "", "".join(base), "".join(_words(name))) if f}


def _family(name: str) -> str:
    w = _words(name.split(":")[0])
    return w[0] if w else name


def fuzzy_pick(models: list[dict], spoken: str, strict: bool = False) -> str | None:
    """The installed chat model a spoken (maybe misheard) name means: "llama", "gemma three", "deep seek",
    "quen 3". None when nothing is close or two different models are equally close.
    strict: whole names only (for the fast path, where "use ..." could mean something else)."""
    want = "".join(_words(spoken))
    chat = [m for m in models if is_chat(m)]
    if len(want) < 2 or not chat:
        return None

    def one_of(hits):
        if len({_family(m["name"]) for m in hits}) > 1:
            return None        # e.g. "coder" with qwen2.5-coder and deepseek-coder installed
        plain = [m for m in hits if all(w.isdigit() for w in _words(m["name"].split(":")[0])[1:])]
        return best_model(plain or hits)["name"]     # "qwen": qwen3 rather than qwen2.5-coder

    hits = [m for m in chat if want in _forms(m["name"])]
    if hits or strict:
        return one_of(hits) if hits else None
    hits = [m for m in chat if len(want) >= 3 and any(want in f for f in _forms(m["name"]))]
    if hits:
        return one_of(hits)

    def score(m):
        best = 0.0
        for f in _forms(m["name"]):
            best = max(best, difflib.SequenceMatcher(None, want, f).ratio())
            for n in (len(want) - 1, len(want), len(want) + 1):     # "deepsea" ~ the start of "deepseekr1"
                if len(f) > n > 2:
                    best = max(best, difflib.SequenceMatcher(None, want, f[:n]).ratio() - 0.05)
        return best
    scored = [(score(m), m) for m in chat]
    top = max(s for s, _ in scored)
    if top < 0.75:
        return None
    return one_of([m for s, m in scored if s >= top - 0.03])


def say_name(name: str) -> str:
    """A tag the voice can read: 'llama3.2:3b' -> 'llama 3.2 3B', 'qwen3.5:latest' -> 'qwen 3.5'."""
    base, _, tag = name.split("/")[-1].partition(":")
    words = re.sub(r"(?<=[A-Za-z]{2})(?=\d)", " ", re.sub(r"[-_]", " ", base))
    if tag and tag != "latest":
        words += " " + re.sub(r"^(\d+(?:\.\d+)?)([bm])$", lambda m: m.group(1) + m.group(2).upper(), tag)
    return words


def features(m: dict) -> str:
    """'tools, vision' / 'vision, no tools' / 'embeddings only' ('' when Ollama doesn't say)."""
    if not m.get("capabilities"):
        return ""
    if not is_chat(m):
        return "embeddings only"
    found = [c for c in ("tools", "vision", "thinking") if c in m["capabilities"]]
    return ", ".join(found + ([] if "tools" in found else ["no tools"]))


# ------------------------------------------------------------ jarvis.bat --models / --model NAME

def print_models(cfg) -> int:
    """jarvis.bat --models: what's installed, what each model can do, and which one Jarvis uses."""
    url, wanted = ollama_url(cfg), configured_model(cfg)
    try:
        models = list_models(url)
    except OllamaUnavailable as e:
        print(e)
        return 1
    if not models:
        print(f"No Ollama models installed yet. Get one with: ollama pull {suggestion()}")
        return 1
    active, note = resolve(models, wanted)
    print(f"Ollama models at {url}:\n")
    for m in models:
        mark = "*" if active is not None and m["name"] == active["name"] else " "
        print(f" {mark} {m['name']:<34} {m['size'] / 1e9:5.1f} GB  {m['params']:>7}  {features(m)}")
    print(f"\n* = the one Jarvis uses (llm.providers.ollama.model: {wanted})")
    if note:
        print(note)
    if (cfg.get("llm") or {}).get("provider", "ollama") != "ollama":
        print(f"(The brain is set to {cfg['llm']['provider']} right now; --model switches it back to Ollama.)")
    print("Choose another: jarvis.bat --model NAME   (part of the name is enough, e.g. --model llama)")
    return 0


def choose_model(cfg, spoken: str) -> int:
    """jarvis.bat --model NAME: save an installed model (matched loosely) as the brain in config.local.yaml."""
    from ..config import save_local

    tag = "auto" if spoken.strip().lower() == "auto" else None
    if tag is None:
        try:
            models = list_models(ollama_url(cfg))
        except OllamaUnavailable as e:
            print(e)
            return 1
        hit = find(models, spoken)
        tag = hit["name"] if hit and is_chat(hit) else (None if ":" in spoken else fuzzy_pick(models, spoken))
        if not tag:
            names = ", ".join(m["name"] for m in models if is_chat(m)) or "none"
            print(f"No installed model matches {spoken!r}. Installed: {names}\n(To download it: ollama pull {spoken})")
            return 1
    path = save_local({"llm": {"provider": "ollama", "providers": {"ollama": {"model": tag}}}})
    print(f"Saved to {path.name}: the brain is now Ollama {tag}. Restart Jarvis (or pick it in the tray) to apply.")
    return 0
