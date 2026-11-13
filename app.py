"""Freight Rewind + the existing Broker Desk Copilot workspace. Owner: UI lead.

Run: streamlit run app.py. This UI branch keeps all writes in dry-run.
"""
import os

import streamlit as st
from ui import crisis_room, email_quote, load_watch, manual_vs_copilot, rewind_style, sidebar, state

# client.py checks this on every write. Keep the legacy views dry-run too.
os.environ['DRY_RUN'] = '1'
st.set_page_config(page_title='Freight Rewind | Crisis Room', page_icon='↶', layout='wide')
rewind_style.apply()
state.init()
sidebar.render_data_source()
view = st.radio('Workspace', ['Crisis Room', 'Load watch', 'Email-to-quote', 'Manual vs copilot'],
                horizontal=True, label_visibility='collapsed', key='workspace_view')
# Render only the selected view: opening Crisis Room never starts legacy API scans.
if view == 'Crisis Room':
    crisis_room.render()
else:
    st.title('Broker Desk Copilot')
    {'Load watch': load_watch.render, 'Email-to-quote': email_quote.render,
     'Manual vs copilot': manual_vs_copilot.render}[view]()
sidebar.render_scoreboard()
