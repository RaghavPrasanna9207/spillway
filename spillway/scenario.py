"""A what-if change to the plan: the one schema the LLM fills, the model applies and the app
shows."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from spillway import config as cfg

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
    to stations, a delay shorter than the gap to the next truck) are checked by model.what_if."""
    demand_change: list[DemandChange] = []
    truck_delay: list[TruckDelay] = []
    temp_no_shows: int = Field(0, ge=0,
                               description="temps on the current shift who did not turn up")
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

    def describe(self):
        """One plain line per change; empty when nothing changes."""
        lines = [f"Demand {'in all states' if c.state is None else 'to ' + c.state}: "
                 f"{c.pct:+g}% from now" for c in self.demand_change]
        lines += [f"Next {c.state} truck leaves {c.minutes:g} min late" for c in self.truck_delay]
        for count, label in [(self.temp_no_shows, "Temps on shift who did not turn up"),
                             (self.pack_stations_down, "Pack stations out of service"),
                             (self.perm_absent, "Permanent pickers absent until midnight")]:
            if count:
                lines.append(f"{label}: {count}")
        if self.agency_daily is not None:
            lines.append(f"Agency fills at most {self.agency_daily} temp shifts a day")
        if self.overtime_hours is not None:
            lines.append(f"Overtime limited to {self.overtime_hours:g} h per permanent picker "
                         "a day")
        if self.penalty_scale is not None:
            lines.append(f"Late orders cost {self.penalty_scale:g}x the current penalty")
        return lines
