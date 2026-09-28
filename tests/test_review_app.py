"""Headless UI test of the Streamlit review app (streamlit.testing.AppTest)."""

from __future__ import annotations

import pytest
import yaml

from conftest import REPO

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = str(REPO / "maintdoc" / "review" / "app.py")
PAGES = ["Dashboard", "Sources", "Statements", "Near duplicates", "Conflicts", "Errors & OCR", "Visual checks",
         "Audit history"]


@pytest.fixture()
def app_env(ws, tmp_path, monkeypatch):
    cfg_file = tmp_path / "review_cfg.yaml"
    cfg_file.write_text(yaml.safe_dump(ws.cfg.to_dict()), encoding="utf-8")
    monkeypatch.setenv("MAINTDOC_CONFIG", str(cfg_file))
    return ws


def _open(page: str, reviewer: str = "UI Tester"):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.sidebar.text_input[0].input(reviewer)
    at.sidebar.radio[0].set_value(page)
    at.run()
    return at


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders(app_env, page):
    at = _open(page)
    assert not at.exception, [e.value for e in at.exception]


def test_ui_decision_is_recorded_with_reviewer(app_env):
    w = app_env
    at = _open("Near duplicates")
    pid = at.selectbox[1].value
    at.text_input[0].input("values differ between revisions")
    next(b for b in at.button if b.label.startswith("Not a duplicate")).click()
    at.run()
    assert not at.exception
    row = w.conn.execute("SELECT status, decided_by FROM near_duplicates WHERE pair_id=?", (pid,)).fetchone()
    assert tuple(row) == ("not_duplicate", "UI Tester")
