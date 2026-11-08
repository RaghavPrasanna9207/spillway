from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from spillguard import data

pytestmark = pytest.mark.skipif(not (data.RAW / "olist_orders_dataset.csv").exists(),
                                reason="Olist CSVs not in data/raw")
APP = str(Path(__file__).resolve().parent.parent / "app.py")


def test_buffer_slider_moves_the_plan_and_simulation_runs():
    app = AppTest.from_file(APP, default_timeout=120).run()
    assert not app.exception
    labour = app.metric[1].value
    app.slider(key="buffer").set_value(1.3).run()
    assert not app.exception
    assert app.metric[1].value != labour  # planning for more volume hires more temps
    app.slider(key="penalty").set_value(0.0).run()
    assert app.metric[0].value == "0"  # spill costs nothing, so no temps

    app.number_input(key="runs").set_value(2).run()
    app.button(key="simulate").click().run()
    assert not app.exception
    assert len(app.dataframe) == 1
