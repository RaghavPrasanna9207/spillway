from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from spillguard import data

pytestmark = pytest.mark.skipif(not (data.RAW / "olist_orders_dataset.csv").exists(),
                                reason="Olist CSVs not in data/raw")
APP = str(Path(__file__).resolve().parent.parent / "app.py")


def test_buffer_slider_moves_the_plan_and_simulation_runs(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "")  # the app keeps it over .streamlit/secrets.toml
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


def test_what_if_example_applies_without_an_llm(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "")
    app = AppTest.from_file(APP, default_timeout=120).run()
    app.selectbox(key="example").select("The Rio truck is running 45 minutes late").run()
    app.button(key="use_example").click().run()
    app.button(key="apply").click().run()
    assert not app.exception
    assert len(app.metric) == 5 + 4  # headline row + before/after row
    assert app.info[0].value.startswith("At Fri 14:00")  # template briefing


def test_app_runs_from_committed_arrays_without_the_csvs(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "")
    with_csvs = AppTest.from_file(APP, default_timeout=120).run()
    monkeypatch.setattr(data, "RAW", tmp_path)  # no CSVs here
    st.cache_data.clear()  # or load() returns the CSV result
    without = AppTest.from_file(APP, default_timeout=120).run()
    assert not without.exception
    assert [m.value for m in without.metric] == [m.value for m in with_csvs.metric]
