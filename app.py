"""Broker Desk Copilot: Streamlit entry point. Owner: UI lead.

Run:  streamlit run app.py
"""

import streamlit as st

from ui import load_watch, manual_vs_copilot, sidebar, state, tracking

st.set_page_config(page_title="Broker Desk Copilot", page_icon="🚚", layout="wide")
state.init()
sidebar.render_data_source()

st.title("Broker Desk Copilot")

# Email-to-quote (ui/email_quote.py) is out of scope for the dispatch-support demo; the code stays in the repo.
tab_watch, tab_track, tab_numbers = st.tabs(["Load watch", "Live tracking", "Manual vs copilot"])
with tab_watch:
    load_watch.render()
with tab_track:
    tracking.render()
with tab_numbers:
    manual_vs_copilot.render()

# Last, so the scoreboard reflects what the tabs just did in this run.
sidebar.render_scoreboard()
