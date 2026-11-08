"""SpillGuard: temp picker staffing and wave sequencing for one Sao Paulo FC over Black Friday
weekend 2017. Run with `streamlit run app.py`."""
import math
import os
from dataclasses import dataclass

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from spillguard import config as cfg
from spillguard import data, llm, model, sim
from spillguard.scenario import Scenario

st.set_page_config(page_title="SpillGuard", layout="wide")
START = pd.Timestamp(cfg.WINDOW_START)
POLICY_NAMES = {"fifo": "FIFO", "earliest_truck": "Earliest truck first",
                "replan": "Rolling re-plan"}
# Pre-parsed what-ifs: used when no LLM is configured or today's call cap is used up.
EXAMPLES = {sentence: Scenario(**change) for sentence, change in [
    ("The Rio truck is running 45 minutes late",
     {"truck_delay": [{"state": "RJ", "minutes": 45}]}),
    ("Three temps didn't show up and two pack stations are down",
     {"temp_no_shows": 3, "pack_stations_down": 2}),
    ("Minas demand is up 30% and one permanent picker called in sick",
     {"demand_change": [{"state": "MG", "pct": 30}], "perm_absent": 1}),
    ("No more overtime and the agency can only send 20 temps a day",
     {"overtime_hours": 0, "agency_daily": 20}),
]}


@st.cache_resource
def chat_for(config):
    return llm.chat_with(config)


def llm_config():
    """LLM_* settings from .streamlit/secrets.toml (local or Streamlit Cloud) over the
    environment; None runs the app without an LLM."""
    try:
        secrets = dict(st.secrets)
    except FileNotFoundError:
        secrets = {}
    return llm.LLMConfig.from_settings({**os.environ, **secrets})


LLM = llm_config()
CHAT = LLM and chat_for(LLM)


@st.cache_data
def load():
    """(permanent headcount, tier values, planning forecast at buffer 1, realised demand), from
    the Olist CSVs when present, else the committed data.DERIVED arrays."""
    if (data.RAW / "olist_orders_dataset.csv").exists():
        arrays = data.derived(data.load_orders())
    else:
        arrays = dict(np.load(data.DERIVED))
    return (model.base_headcount(arrays["normal"]), arrays["tier_values"], arrays["planning"],
            arrays["actual"])


BASE, TIER_VALUES, FORECAST, ACTUAL = load()


def instance(buffer, penalty, limits):
    # FORECAST x buffer is data.planning_forecast(orders, buffer)
    return model.Instance(FORECAST * buffer, TIER_VALUES, BASE, penalty, limits)


@st.cache_data(show_spinner="Solving the staffing plan...")
def scenario(buffer, penalty, limits):
    """(plan, frontier, marginal ROI) for one slider setting; about 4 s uncached."""
    problem = instance(buffer, penalty, limits)
    plan = model.solve(problem)
    return plan, model.frontier(problem), model.marginal_roi(problem, plan.temps)


@st.cache_data(show_spinner=False)
def compare(buffer, penalty, limits, runs):
    plan = scenario(buffer, penalty, limits)[0]
    return sim.compare(instance(buffer, penalty, limits), ACTUAL, plan.temps, runs=runs,
                       overtime=plan.overtime)


@st.cache_data(show_spinner="Re-planning from now...")
def what_if(buffer, penalty, limits, now, change_json):
    """(before, after) plans from hour `now`; raises ValueError for a change the plan cannot
    absorb."""
    plan = scenario(buffer, penalty, limits)[0]
    return model.what_if(instance(buffer, penalty, limits), plan, now,
                         Scenario.model_validate_json(change_json))


def at(hours):
    return START + pd.to_timedelta(hours, unit="h")


def brl(x):
    return f"R${x:,.0f}"


def delta_brl(x):
    return f"{'-' if x < 0 else '+'}R${abs(x):,.0f}"


def md(text):
    """Escape $ for Markdown elements (captions, help), which read $...$ as LaTeX."""
    return text.replace("$", r"\$")


@dataclass
class WhatIfFacts:
    """A what-if result formatted once: the only numbers its briefing may use, either as
    labelled facts for the LLM or in the template sentence."""
    sentence: str
    now: str
    temps: str
    starts: str
    labour: str
    missed: str
    spill_cost: str

    @classmethod
    def of(cls, sentence, now, before, after):
        added = after.temps - before.temps
        starts = [f"{at(t):%a %H:%M} ({added[t]:+.0f})" for t in np.flatnonzero(added)]
        return cls(sentence, f"{at(now):%a %H:%M}",
                   f"{before.temps.sum():.0f} -> {after.temps.sum():.0f}",
                   ", ".join(starts) or "none",
                   f"{brl(before.labour_cost)} -> {brl(after.labour_cost)}",
                   f"{before.spilled.sum():,.0f} -> {after.spilled.sum():,.0f}",
                   f"{brl(before.spill_cost)} -> {brl(after.spill_cost)}")

    def labelled(self):
        return {"What changed": self.sentence, "Time now": self.now,
                "Temp shifts, before -> after": self.temps,
                "Shift starts that change": self.starts,
                "Labour cost, before -> after": self.labour,
                "Orders missing their truck from now, before -> after": self.missed,
                "Spill cost from now, before -> after": self.spill_cost}

    def template(self):
        return (f'At {self.now}, after "{self.sentence}", temp shifts go {self.temps} and '
                f"labour cost {self.labour}. Orders missing their truck from now go "
                f"{self.missed}, with spill cost {self.spill_cost}. Shift starts that change: "
                f"{self.starts}.")


# --- Sidebar -----------------------------------------------------------------
lim = model.resolve_limits(BASE)
stations = round(lim.pack_cap / cfg.PACK_RATE)
with st.sidebar:
    st.header("Scenario")
    buffer = st.slider("Planning buffer (x forecast volume)", 0.7, 1.3, 1.0, 0.05, key="buffer",
                       help="1.0 plans on the realised day totals (a perfect volume forecast); "
                            "0.8 plans 20% low, 1.2 plans 20% high.")
    penalty = st.slider("Spill penalty (x default)", 0.0, 3.0, 1.0, 0.25, key="penalty",
                        help=md(f"Default: R${cfg.SUPPORT_COST_PER_DAY_LATE:.0f} support cost "
                                f"plus {cfg.CANCEL_PROB_PER_DAY_LATE:.0%} cancellation risk on "
                                "the order's value, per day late."))
    st.header("Limits")
    limits = dict(
        floor_cap=st.slider("Floor cap (pickers on the floor)",
                            math.ceil(BASE / cfg.CROWDING_SHARE), 2 * lim.floor_cap,
                            lim.floor_cap, key="floor_cap"),
        pack_cap=cfg.PACK_RATE * st.slider(f"Pack stations ({cfg.PACK_RATE} orders/h each)",
                                           1, 2 * stations, stations, key="stations"),
        agency_daily=st.slider("Agency temp shifts per day", 0, 2 * int(lim.agency_daily),
                               int(lim.agency_daily), key="agency_daily"),
        agency_night=st.slider("Agency night starts per day (22:00-04:00)", 0,
                               2 * int(lim.agency_night), int(lim.agency_night),
                               key="agency_night"),
        overtime_daily=model.overtime_cap(
            st.slider("Overtime hours per permanent picker per day", 0, cfg.OVERTIME_MAX_H,
                      cfg.OVERTIME_MAX_H, key="overtime"), BASE),
    )
    st.caption(f"{BASE} permanent pickers on every shift, sized to a normal day.")

plan, front, roi = scenario(buffer, penalty, limits)
fc = FORECAST * buffer

# --- Headline ----------------------------------------------------------------
st.title("SpillGuard")
st.caption("Temp pickers and wave sequencing for one Sao Paulo fulfilment centre over Black "
           "Friday weekend 2017 (Olist orders scaled to a 20k-order peak day). Spilled orders "
           "miss their truck and roll into later waves.")
cols = st.columns(5)
cols[0].metric("Temp shifts", f"{plan.temps.sum():.0f}")
cols[1].metric("Labour cost", brl(plan.labour_cost))
cols[2].metric("Spill cost", brl(plan.spill_cost))
cols[3].metric("Planned spill", f"{plan.spill_rate:.1%}")
cols[4].metric("Gap to LP bound", f"{brl(plan.gap_cost)} ({plan.gap:.1%})",
               help="Most this plan could cost above the best possible one.")

# --- Frontier ----------------------------------------------------------------
st.subheader("Labour cost vs spill cost")
curve = pd.DataFrame({"labour": [p.labour_cost for p in front],
                      "spill": [p.spill_cost for p in front],
                      "temps": [p.temps.sum() for p in front]})
chosen = pd.DataFrame({"labour": [plan.labour_cost], "spill": [plan.spill_cost]})
x, y = alt.X("labour:Q", title="Labour cost (R$)"), alt.Y("spill:Q", title="Spill cost (R$)")
st.altair_chart(
    alt.Chart(curve).mark_line(point=True).encode(
        x, y, tooltip=[alt.Tooltip("temps:Q", format=".1f", title="temp shifts"),
                       alt.Tooltip("labour:Q", format=",.0f"), alt.Tooltip("spill:Q", format=",.0f")])
    + alt.Chart(chosen).mark_point(size=150, filled=True, color="red").encode(x, y))
st.caption("Line: the LP relaxation as the cap on temp shifts rises to the optimum. "
           "Red: the whole-number plan.")

# --- Staffing ----------------------------------------------------------------
left, right = st.columns(2)
with left:
    st.subheader("Temps by start hour")
    starts = pd.DataFrame({"start": at(np.arange(len(plan.temps))), "temps": plan.temps})
    st.altair_chart(alt.Chart(starts).mark_bar().encode(
        alt.X("start:T", title=None, axis=alt.Axis(format="%a %H:%M")),
        alt.Y("temps:Q", title="Temps starting an 8-hour shift"),
        tooltip=[alt.Tooltip("start:T", format="%a %H:%M"), "temps:Q"]))
    st.caption(f"Planned overtime: {plan.overtime.sum():.0f} permanent picker-hours.")

with right:
    st.subheader("One more temp, by start hour")
    r = roi.dropna().assign(start=lambda d: at(d.index))
    if r.empty:
        st.info("No start hour has room for another temp (floor or agency limit).")
    else:
        best = r.loc[r.net.idxmax()]
        st.altair_chart(alt.Chart(r).mark_bar().encode(
            alt.X("start:T", title=None, axis=alt.Axis(format="%a %H:%M")),
            alt.Y("net:Q", title="Spill avoided - shift cost (R$)"),
            color=alt.condition("datum.net > 0", alt.value("seagreen"), alt.value("indianred")),
            tooltip=[alt.Tooltip("start:T", format="%a %H:%M"),
                     alt.Tooltip("shift_cost:Q", format=",.0f"),
                     alt.Tooltip("spill_avoided:Q", format=",.0f"),
                     alt.Tooltip("orders_saved:Q", format=",.0f")]))
        lead = "Best" if best.net > 0 else "No start hour pays back. Least bad"
        st.caption(md(f"{lead}: one more temp starting {best.start:%a %H:%M} costs "
                      f"{brl(best.shift_cost)}, saves {best.orders_saved:,.0f} orders and avoids "
                      f"{brl(best.spill_avoided)} of spill. Hours with a full floor or no "
                      "agency supply left are blank."))

# --- Protected states --------------------------------------------------------
left, right = st.columns(2)
with left:
    st.subheader("Planned spill by state and value tier")
    demand = fc.sum(0)
    rate = np.divide(plan.spilled, demand, out=np.zeros_like(demand), where=demand > 0)
    cells = pd.DataFrame([(s, t, rate[i, j], demand[i, j])
                          for i, s in enumerate(cfg.DEST_STATES)
                          for j, t in enumerate(cfg.TIER_NAMES)],
                         columns=["state", "tier", "spill", "orders"])
    st.altair_chart(alt.Chart(cells).mark_rect().encode(
        alt.X("tier:N", sort=cfg.TIER_NAMES, title="Value tier"),
        alt.Y("state:N", sort=cfg.DEST_STATES, title=None),
        alt.Color("spill:Q", title="Spill", scale=alt.Scale(scheme="reds"),
                  legend=alt.Legend(format=".0%")),
        tooltip=["state", "tier", alt.Tooltip("spill:Q", format=".1%"),
                 alt.Tooltip("orders:Q", format=",.0f")]))
    st.caption("Light cells are the orders the plan protects when the floor runs short.")

with right:
    st.subheader("Demand: planned vs realised")
    hourly = pd.DataFrame({"hour": at(np.arange(len(fc))), "Planning forecast": fc.sum((1, 2)),
                           "Realised": ACTUAL.sum((1, 2))})
    st.altair_chart(alt.Chart(hourly).transform_fold(
        ["Planning forecast", "Realised"], as_=["series", "orders"]
    ).mark_line().encode(
        alt.X("hour:T", title=None, axis=alt.Axis(format="%a %H:%M")),
        alt.Y("orders:Q", title="Orders per hour"),
        alt.Color("series:N", title=None, legend=alt.Legend(orient="bottom")),
        alt.StrokeDash("series:N", title=None)))
    st.caption(f"Hourly error (WAPE): {data.wape(fc, ACTUAL):.0%}. The plan sees the forecast; "
               "the simulation below runs on realised demand.")

# --- Policy comparison -------------------------------------------------------
st.subheader("Picking policies on realised demand, same headcount")
runs = st.number_input("Simulation runs", 2, 30, 10, key="runs")
key = (buffer, penalty, tuple(limits.items()), runs)
if st.button("Run simulation", key="simulate"):
    st.session_state.sim = key
if st.session_state.get("sim") == key:
    with st.spinner(f"Simulating {runs} runs per policy (about {runs * 7} s)..."):
        res = compare(buffer, penalty, limits, runs)
    fmt = {"spill_rate": "{:.1%} ± {:.1%}"}
    st.dataframe(pd.DataFrame(
        {POLICY_NAMES[p]: {k.replace("_", " ").capitalize():
                           fmt.get(k, "R${:,.0f} ± {:,.0f}").format(*res[p][k]) for k in res[p]}
         for p in res}).T)
    st.caption("Mean ± 95% CI over runs with Poisson demand noise, absenteeism and pick-speed "
               "noise. FIFO and earliest-truck keep the planned overtime; the re-plan re-solves "
               "every 2 hours and re-chooses overtime from the real backlog.")
else:
    st.caption("FIFO, earliest-truck-first and the rolling re-plan, simulated with the temps "
               "above. Takes about 7 s per run.")

# --- What-if -----------------------------------------------------------------
st.subheader("What if...")
now = st.select_slider("Now", options=list(range(1, len(fc))), value=14, key="now",
                       format_func=lambda h: f"{at(h):%a %H:%M}")
st.caption("Shifts starting at or before now stay as planned; later shifts, overtime and wave "
           "order are re-planned. Missed trucks and spill cost count from now.")
if CHAT:
    typed = st.text_input("What changed?", key="sentence",
                          placeholder="e.g. Rio truck 45 min late and two pickers off sick")
    if st.button("Read it", key="parse", disabled=not typed):
        try:
            st.session_state.change = (typed, llm.parse(typed, CHAT))
        except llm.CapReached:
            st.warning("Today's LLM limit is used up; pick an example instead.")
        except Exception as e:  # network, auth, or a reply that is not a valid scenario
            st.error(f"Could not read that: {e}")
else:
    st.caption("No LLM configured, so pick an example. To type your own, set LLM_MODEL and "
               "LLM_API_KEY in .streamlit/secrets.toml.")
pick = st.selectbox("Example", list(EXAMPLES), key="example")
if st.button("Use example", key="use_example"):
    st.session_state.change = (pick, EXAMPLES[pick])

if "change" in st.session_state:
    sentence, change = st.session_state.change
    lines = change.describe()
    st.markdown(md(f"**{sentence}**\n\n" + ("\n".join(f"- {x}" for x in lines)
                                            or "Nothing in that sentence changes the plan.")))
    wkey = (buffer, penalty, tuple(limits.items()), now, change.model_dump_json())
    if st.button("Apply", key="apply", disabled=not lines):
        try:
            before, after = what_if(buffer, penalty, limits, now, change.model_dump_json())
        except ValueError as e:
            st.error(str(e))
        else:
            facts = WhatIfFacts.of(sentence, now, before, after)
            text, why = llm.briefing_or(facts.template(), facts.labelled(), CHAT)
            source = (f"Written by {LLM.model} from the numbers above." if why is None
                      else f"Template briefing: {why}.")
            st.session_state.result = (wkey, before, after, text, source)
    if st.session_state.get("result", (None,))[0] == wkey:
        _, before, after, text, source = st.session_state.result
        cols = st.columns(4)
        cols[0].metric("Temp shifts", f"{after.temps.sum():.0f}",
                       f"{after.temps.sum() - before.temps.sum():+.0f}", delta_color="off")
        cols[1].metric("Labour cost", brl(after.labour_cost),
                       delta_brl(after.labour_cost - before.labour_cost), delta_color="inverse")
        cols[2].metric("Orders missing their truck from now", f"{after.spilled.sum():,.0f}",
                       f"{after.spilled.sum() - before.spilled.sum():+,.0f}",
                       delta_color="inverse")
        cols[3].metric("Spill cost from now", brl(after.spill_cost),
                       delta_brl(after.spill_cost - before.spill_cost), delta_color="inverse")
        st.info(md(text))
        st.caption(md(source))
