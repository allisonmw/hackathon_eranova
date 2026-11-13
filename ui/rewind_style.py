"""Presentation styles for the Streamlit workspace."""
import streamlit as st


def apply():
    st.html('''<style>
    :root {color-scheme: dark;}
    .stApp {background:#0a1220; color:#e2e8f0;}
    [data-testid="stHeader"] {background:#0a1220;}
    [data-testid="stSidebar"] {background:#101c2d; border-right:1px solid #223247;}
    .block-container {max-width:1480px; padding-top:2rem; padding-bottom:4rem;}
    h1,h2,h3 {letter-spacing:-.035em; color:#f1f5f9;}
    h2 {font-size:1.45rem !important; margin-top:1.5rem !important;}
    [data-testid="stMetric"] {background:#111e30; border:1px solid #26374e; border-radius:12px; padding:16px;}
    [data-testid="stMetricValue"] {font-size:1.65rem;}
    [data-testid="stVerticalBlockBorderWrapper"]>div {border-color:#26374e !important; border-radius:14px;}
    .fr-hero {background:linear-gradient(115deg,#14283d,#102134 65%,#113b42); padding:38px 40px; border:1px solid #24465b; border-radius:20px; margin:14px 0 28px;}
    .fr-hero h1 {font-size:clamp(2.1rem,4vw,3.5rem); line-height:1.07; margin:14px 0; font-weight:650;}
    .fr-hero p {font-size:1.05rem; color:#b7cbd9;}
    .fr-hero span {display:inline-block; color:#78dcca; font-size:11px; letter-spacing:.12em; margin-top:12px;}
    .fr-eyebrow {color:#64ddcc; font-size:11px; letter-spacing:.15em; font-weight:650;}
    .fr-card {min-height:150px; padding:22px; background:#111e30; border:1px solid #26374e; border-radius:14px; margin-bottom:16px;}
    .fr-card h3 {font-size:1.1rem; letter-spacing:-.02em; margin:12px 0;}
    .fr-card p {color:#9eb2c7; font-size:13px; margin:0;}
    button[kind="primary"] {background:#22c9b4; color:#06191b; border-color:#22c9b4; font-weight:650;}
    [data-testid="stCaptionContainer"] {color:#9fb2c7;}
    </style>''')
