from spillway.scenario import Scenario


def test_describe_lists_each_change_and_nothing_for_no_change():
    assert Scenario().describe() == []
    change = Scenario(truck_delay=[{"state": "RJ", "minutes": 45}], perm_absent=1,
                      overtime_hours=0)
    assert change.describe() == ["Next RJ truck leaves 45 min late",
                                 "Permanent pickers absent until midnight: 1",
                                 "Overtime limited to 0 h per permanent picker a day"]
