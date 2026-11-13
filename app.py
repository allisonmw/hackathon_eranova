"""Broker Desk Copilot: Streamlit entry point. Owner: UI lead.

Run:  streamlit run app.py
"""

import streamlit as st

from ui import email_quote, load_watch, manual_vs_copilot, sidebar, state

st.set_page_config(page_title="Broker Desk Copilot", page_icon="🚚", layout="wide")
state.init()
sidebar.render_data_source()

st.title("Broker Desk Copilot")

tab_watch, tab_quote, tab_numbers = st.tabs(["Load watch", "Email-to-quote", "Manual vs copilot"])
with tab_watch:
    load_watch.render()
with tab_quote:
    email_quote.render()
with tab_numbers:
    manual_vs_copilot.render()

# Last, so the scoreboard reflects what the tabs just did in this run.
sidebar.render_scoreboard()
