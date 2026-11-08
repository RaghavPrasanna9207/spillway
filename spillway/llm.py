"""LLM helpers for any OpenAI-compatible endpoint: NVIDIA NIM by default, Ollama offline.

Invariant: the LLM never produces numbers the app reports. parse() turns a sentence into a
Scenario validated against a fixed set of fields; briefing() writes prose from solver numbers
and is rejected if it states a number it was not given.

Both take a chat function, (system, user) -> reply: chat_with(config) for a real endpoint, a
stub in tests. LLMConfig.from_settings() returns None when no model is set, and the app then
runs without an LLM.
"""
import json
import re
import threading
from dataclasses import dataclass
from datetime import date

from openai import OpenAI
from pydantic import ValidationError

from spillway import config as cfg
from spillway.scenario import Scenario

PARSE_PROMPT = f"""You turn a fulfilment-centre manager's sentence into a JSON scenario for a \
staffing model. Reply with one JSON object and nothing else, matching this JSON schema:
{json.dumps(Scenario.model_json_schema())}
Include only fields the sentence mentions; reply {{}} if it mentions none. Never invent changes.
States (code: state, capital): SP: Sao Paulo, Sao Paulo; RJ: Rio de Janeiro, Rio de Janeiro; \
MG: Minas Gerais, Belo Horizonte; RS: Rio Grande do Sul, Porto Alegre; PR: Parana, Curitiba; \
BA: Bahia, Salvador; SC: Santa Catarina, Florianopolis; ES: Espirito Santo, Vitoria. \
"Rio" alone means RJ.
Give durations in minutes and changes in percent (doubled = 100, halved = -50).
Example: "Espirito Santo orders are up 15% and one pack station is broken" -> \
{{"demand_change": [{{"state": "ES", "pct": 15}}], "pack_stations_down": 1}}"""

BRIEF_PROMPT = """You brief a fulfilment-centre shift manager on a staffing plan, using the \
solver results the user sends. Write 3-4 full sentences of plain prose: what the plan does, \
what it costs, and how many orders still miss their truck. Do not list the results back as \
"label: value" pairs, and use no headings or bullets. Use only the numbers given, written \
exactly as given: do not calculate, round or add any number."""


class CapReached(RuntimeError):
    """The daily LLM call cap is used up; the app falls back to examples and templates."""


@dataclass(frozen=True)
class LLMConfig:
    model: str
    api_key: str = "none"
    base_url: str = cfg.LLM_DEFAULT_BASE_URL

    @classmethod
    def from_settings(cls, settings):
        """From LLM_MODEL, LLM_API_KEY and LLM_BASE_URL in a mapping (os.environ, Streamlit
        secrets); None when LLM_MODEL is unset or empty."""
        if not settings.get("LLM_MODEL"):
            return None
        return cls(settings["LLM_MODEL"], settings.get("LLM_API_KEY", "none"),
                   settings.get("LLM_BASE_URL", cfg.LLM_DEFAULT_BASE_URL))


def chat_with(config):
    """Chat function for `config`; every call counts against the daily cap."""
    client = OpenAI(base_url=config.base_url, api_key=config.api_key, timeout=cfg.LLM_TIMEOUT_S)

    def chat(system, user):
        _spend_call()
        r = client.chat.completions.create(
            model=config.model, temperature=0,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return r.choices[0].message.content or ""
    return chat


def parse(sentence, chat):
    """Scenario from a sentence. Raises ValueError if the reply is not a valid scenario."""
    reply = chat(PARSE_PROMPT, sentence)
    start, end = reply.find("{"), reply.rfind("}")  # skips code fences and chatter
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in the LLM reply: {reply[:200]!r}")
    try:
        return Scenario.model_validate_json(reply[start:end + 1])
    except ValidationError as e:
        raise ValueError(f"LLM reply is not a valid scenario: {e}") from e


def briefing(facts, chat):
    """Short briefing from {label: value} solver results. Raises ValueError if it states a
    number not in facts.

    ponytail: checks digits only, so a spelled-out number ("three") gets through; add a
    number-word list if briefings start using them.
    """
    given = "\n".join(f"{k}: {v}" for k, v in facts.items())
    text = chat(BRIEF_PROMPT, given).strip()
    invented = _numbers(text) - _numbers(given)
    if invented:
        raise ValueError(f"briefing states numbers it was not given: {sorted(invented)}")
    return text


def briefing_or(fallback, facts, chat):
    """(text, None) when the LLM writes the briefing; (fallback, reason) when there is no chat,
    the daily cap is used up, or the call or its number check fails."""
    if chat is None:
        return fallback, "no LLM configured"
    try:
        return briefing(facts, chat), None
    except CapReached:
        return fallback, "today's LLM limit is used up"
    except Exception as e:  # network, auth, or a briefing with invented numbers
        return fallback, f"the LLM failed ({e})"


def _numbers(text):
    return {float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


_lock = threading.Lock()
_used = {"day": None, "calls": 0}


def _spend_call():
    """Count one call against the daily cap, shared by every session of this server.

    ponytail: in-process counter, so a restart resets it; move it to a file or database if the
    host restarts often.
    """
    with _lock:
        if _used["day"] != date.today():
            _used.update(day=date.today(), calls=0)
        if _used["calls"] >= cfg.LLM_DAILY_CALL_CAP:
            raise CapReached(f"daily cap of {cfg.LLM_DAILY_CALL_CAP} LLM calls reached")
        _used["calls"] += 1
