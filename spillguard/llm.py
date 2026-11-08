"""LLM helpers for any OpenAI-compatible endpoint: NVIDIA NIM by default, Ollama offline.

Invariant: the LLM never produces numbers the app reports. parse() turns a sentence into a
Scenario validated against a fixed set of fields; briefing() writes prose from solver numbers
and is rejected if it states a number it was not given. With LLM_MODEL unset, configured() is
False and the app runs without either.

Settings come from the environment: LLM_BASE_URL (default NIM), LLM_MODEL, LLM_API_KEY.
"""
import json
import os
import re
import threading
from datetime import date
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spillguard import config as cfg

State = Literal[tuple(cfg.DEST_STATES)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemandChange(_Strict):
    state: State | None = Field(None, description="state code, or null for all states")
    pct: float = Field(gt=-100, description="percent change in orders, e.g. 30 or -20")


class TruckDelay(_Strict):
    state: State
    minutes: float = Field(gt=0, description="minutes late the state's next truck leaves")


class Scenario(_Strict):
    """A what-if change; fields left at their defaults mean no change. Limits that depend on the
    plan (no-shows up to temps on shift, absences up to permanent headcount, stations down up
    to stations) are checked where the scenario is applied."""
    demand_change: list[DemandChange] = []
    truck_delay: list[TruckDelay] = []
    temp_no_shows: int = Field(0, ge=0, description="temps on the current shift who did not turn up")
    pack_stations_down: int = Field(0, ge=0, description="pack stations out of service")
    perm_absent: int = Field(0, ge=0, description="permanent pickers absent")
    agency_daily: int | None = Field(None, ge=0,
                                     description="most temp shifts the agency can fill per day")
    overtime_hours: float | None = Field(
        None, ge=0, le=cfg.OVERTIME_MAX_H,
        description="most overtime hours per permanent picker per day; 0 means no overtime")
    penalty_scale: float | None = Field(
        None, ge=0, description="multiplier on the cost of a late order: 1 is the default, "
                                "1.5 means 50% more")


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
    """The daily LLM call cap is used up; the app shows pre-generated examples instead."""


def configured():
    return bool(os.environ.get("LLM_MODEL"))


def parse(sentence, chat=None):
    """Scenario from a sentence. Raises ValueError if the reply is not a valid scenario."""
    reply = (chat or _chat)(PARSE_PROMPT, sentence)
    start, end = reply.find("{"), reply.rfind("}")  # skips code fences and chatter
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in the LLM reply: {reply[:200]!r}")
    try:
        return Scenario.model_validate_json(reply[start:end + 1])
    except ValidationError as e:
        raise ValueError(f"LLM reply is not a valid scenario: {e}") from e


def briefing(facts, chat=None):
    """Short briefing from {label: value} solver results. Raises ValueError if it states a
    number not in facts.

    ponytail: checks digits only, so a spelled-out number ("three") gets through; add a
    number-word list if briefings start using them.
    """
    given = "\n".join(f"{k}: {v}" for k, v in facts.items())
    text = (chat or _chat)(BRIEF_PROMPT, given).strip()
    invented = _numbers(text) - _numbers(given)
    if invented:
        raise ValueError(f"briefing states numbers it was not given: {sorted(invented)}")
    return text


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


def _chat(system, user):
    if not configured():
        raise RuntimeError("no LLM configured: set LLM_MODEL (and LLM_BASE_URL, LLM_API_KEY)")
    _spend_call()
    client = OpenAI(base_url=os.environ.get("LLM_BASE_URL", cfg.LLM_DEFAULT_BASE_URL),
                    api_key=os.environ.get("LLM_API_KEY", "none"), timeout=cfg.LLM_TIMEOUT_S)
    r = client.chat.completions.create(
        model=os.environ["LLM_MODEL"], temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return r.choices[0].message.content or ""
