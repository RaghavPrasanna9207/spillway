"""SpillGuard: temp picker staffing and wave sequencing for one Sao Paulo FC over Black Friday
weekend 2017. Run with `streamlit run app.py`."""
import math

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from spillguard import config as cfg
from spillguard import data, model, sim

st.set_page_config(page_title="SpillGuard", layout="wide")
START = pd.Timestamp(cfg.WINDOW_START)
POLICY_NAMES = {"fifo": "FIFO", "earliest_truck": "Earliest truck first",
                "replan": "Rolling re-plan"}

if not (data.RAW / "olist_orders_dataset.csv").exists():
    st.error(f"Olist CSVs not found in {data.RAW}. Download them from "
             "kaggle.com/datasets/olistbr/brazilian-ecommerce.")
    st.stop()


@st.cache_data
def load():
    """(permanent headcount, tier values, planning forecast at buffer 1, realised demand)."""
    orders = data.load_orders()
    return (model.base_headcount(data.forecast_demand(orders)), data.tier_values(orders),
            data.planning_forecast(orders), data.actual_demand(orders))


BASE, TIER_VALUES, FORECAST, ACTUAL = load()


@st.cache_data(show_spinner="Solving the staffing plan...")
def scenario(buffer, penalty, limits):
    """(plan, frontier, marginal ROI) for one slider setting; about 4 s uncached."""
    fc = FORECAST * buffer  # same as data.planning_forecast(orders, buffer)
    plan = model.solve(fc, TIER_VALUES, BASE, penalty_scale=penalty, **limits)
    front = model.frontier(fc, TIER_VALUES, BASE, penalty_scale=penalty, **limits)
    roi = model.marginal_roi(fc, TIER_VALUES, BASE, plan.temps, penalty_scale=penalty, **limits)
    return plan, front, roi


@st.cache_data(show_spinner=False)
def compare(buffer, penalty, limits, runs):
    plan = scenario(buffer, penalty, limits)[0]
    return sim.compare(FORECAST * buffer, ACTUAL, TIER_VALUES, BASE, plan.temps, runs=runs,
                       penalty_scale=penalty, overtime=plan.overtime, **limits)


def at(hours):
    return START + pd.to_timedelta(hours, unit="h")


def brl(x):
    return f"R${x:,.0f}"


def md(text):
    """Escape $ for Markdown elements (captions, help), which read $...$ as LaTeX."""
    return text.replace("$", r"\$")


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
        overtime_daily=st.slider("Overtime hours per permanent picker per day", 0,
                                 cfg.OVERTIME_MAX_H, cfg.OVERTIME_MAX_H, key="overtime")
        * BASE * 24 / cfg.SHIFT_HOURS,
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
