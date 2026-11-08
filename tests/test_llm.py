"""Offline tests use canned replies. The live tests run the same sentences through the configured
LLM and are skipped unless LLM_MODEL is set (with LLM_BASE_URL and LLM_API_KEY as needed)."""
import json
import re
from datetime import date, timedelta

import pytest

from spillguard import config as cfg
from spillguard import llm

CASES = [
    ("The Rio truck is running 45 minutes late", {"truck_delay": [{"state": "RJ", "minutes": 45}]}),
    ("Minas demand is up 30%", {"demand_change": [{"state": "MG", "pct": 30}]}),
    ("Demand is down 20% everywhere", {"demand_change": [{"state": None, "pct": -20}]}),
    ("The Curitiba truck is delayed by an hour and a half",
     {"truck_delay": [{"state": "PR", "minutes": 90}]}),
    ("Bahia orders have doubled and the Salvador truck is 2 hours late",
     {"demand_change": [{"state": "BA", "pct": 100}],
      "truck_delay": [{"state": "BA", "minutes": 120}]}),
    ("Three temps didn't show up for the current shift", {"temp_no_shows": 3}),
    ("Two pack stations are down", {"pack_stations_down": 2}),
    ("One of our permanent pickers called in sick", {"perm_absent": 1}),
    ("The agency can only send 20 temps a day from now on", {"agency_daily": 20}),
    ("No overtime allowed", {"overtime_hours": 0}),
    ("Late orders cost us 50% more than we assumed", {"penalty_scale": 1.5}),
    ("Porto Alegre demand fell by a quarter and 4 temps are missing",
     {"demand_change": [{"state": "RS", "pct": -25}], "temp_no_shows": 4}),
    ("What's the weather like in Sao Paulo?", {}),
]
FACTS = {"Temp shifts": "51", "Labour cost": "R$13,699", "Spill cost": "R$7,131",
         "Planned spill": "3.3%", "Busiest start hour": "Fri 10:00 (8 temps)"}


def reply(text):
    return lambda system, user: text


@pytest.mark.parametrize("sentence, expected", CASES)
def test_canned_replies_are_valid_scenarios(sentence, expected):
    assert llm.parse(sentence, reply(json.dumps(expected))) == llm.Scenario(**expected)


def test_parse_skips_code_fences_and_chatter():
    text = 'Sure:\n```json\n{"perm_absent": 2}\n```\nLet me know!'
    assert llm.parse("x", reply(text)) == llm.Scenario(perm_absent=2)


@pytest.mark.parametrize("text", [
    "no json here",
    '{"perm_absent": 2',                                         # malformed
    '{"perm_absent": 2, "hire_everyone": true}',                 # unknown field
    '{"truck_delay": [{"state": "RJ", "minutes": 45, "x": 1}]}',  # unknown nested field
    '{"truck_delay": [{"state": "AM", "minutes": 45}]}',          # state not served
    '{"temp_no_shows": -1}',
    '{"overtime_hours": 3}',                                     # over the CLT 2-hour cap
    '{"demand_change": [{"state": "SP", "pct": -100}]}',          # demand cannot vanish
])
def test_parse_rejects_invalid_replies(text):
    with pytest.raises(ValueError):
        llm.parse("x", reply(text))


def test_briefing_keeps_given_numbers_and_rejects_invented_ones():
    ok = "Plan 51 temp shifts for R$13,699, busiest at Fri 10:00; spill stays at 3.3%."
    assert llm.briefing(FACTS, reply(ok)) == ok
    with pytest.raises(ValueError, match="52"):
        llm.briefing(FACTS, reply("Plan 52 temp shifts for R$13,699."))


def test_cap_blocks_after_limit_and_resets_next_day(monkeypatch):
    monkeypatch.setattr(cfg, "LLM_DAILY_CALL_CAP", 2)
    monkeypatch.setattr(llm, "_used", {"day": None, "calls": 0})
    llm._spend_call()
    llm._spend_call()
    with pytest.raises(llm.CapReached):
        llm._spend_call()
    llm._used["day"] = date.today() - timedelta(days=1)
    llm._spend_call()


def test_unconfigured_llm_raises_without_spending_a_call(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr(llm, "_used", {"day": None, "calls": 0})
    with pytest.raises(RuntimeError):
        llm.parse("Two pack stations are down")
    assert llm._used["calls"] == 0


live = pytest.mark.skipif(not llm.configured(), reason="LLM_MODEL not set")


@live
@pytest.mark.parametrize("sentence, expected", CASES)
def test_live_parse(sentence, expected):
    assert llm.parse(sentence) == llm.Scenario(**expected)


@live
def test_live_briefing_is_prose_with_only_given_numbers():
    text = llm.briefing(FACTS)  # raises on an invented number
    assert len(re.findall(r"[.!?](?:\s|$)", text)) >= 2, text  # sentences, not a list
