"""Sidebar: scoreboard, data source, dry-run and model. Owner: UI lead.

Two parts: the data source is rendered first (the tabs need it); the scoreboard is rendered
after the tabs, so it shows what this run just did. Positions in the sidebar are fixed by the
containers below, not by call order.
"""

from __future__ import annotations

import streamlit as st

from meridian import client, llm

from . import state


_score_slot = None  # set each run by render_data_source()


def render_data_source() -> None:
    global _score_slot
    with st.sidebar:
        _score_slot, settings_slot = st.container(), st.container()
    with settings_slot:
        st.divider()
        st.radio("Data source", state.DATA_SOURCES, key="data_source",
                 help="Fake data: UI development. Snapshots: demo mode, no network. Live API: the real TMS.")
        state.apply_data_source()
        st.divider()
        st.markdown(f"**Dry-run:** {':green[ON]' if client.dry_run() else ':red[OFF — writes are real]'}")
        st.markdown(f"**Model:** `{llm.MODEL}`")


def render_scoreboard() -> None:
    with _score_slot:
        st.subheader("Today's scoreboard")
        sb = st.session_state.scoreboard
        c1, c2 = st.columns(2)
        c1.metric("Items done", sb["items_done"])
        c2.metric("Time saved", f"~{sb['minutes_saved']:.0f} min")
        c1.metric("Issues caught", sb["issues_caught"])
        c2.metric("Quotes below margin", sb["quotes_below_margin"])
        st.caption("Time saved is estimated from our measured manual baseline.")
        if state.load_baseline().get("_placeholder"):
            st.caption(":orange[baseline.json still has placeholder numbers]")
