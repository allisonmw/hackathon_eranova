"""Tab 2: Email-to-quote. Owner: UI lead."""

from __future__ import annotations

import time
from datetime import date

import streamlit as st

from meridian import quote_playbook
from meridian.models import FreightLine, Location, QuoteProposal, QuoteRequest

from . import state
from .fake_data import fake_proposal

EMAILS_DIR = state.ROOT / "evals" / "emails"
PASTE = "(paste your own)"


def _fmt_minutes(m: float) -> str:
    return f"{int(m)} min {round((m % 1) * 60)} s"


def _build(email: str, request: QuoteRequest | None = None) -> None:
    t0 = time.perf_counter()
    with st.status("Building quote…", expanded=True) as status:
        if state.use_fake():
            for msg in ["Reading email", "Finding customer", "Checking requirements", "Pricing", "Drafting reply"]:
                status.write(msg)
                time.sleep(0.2)
            has_po = bool(request.references.get("PO")) if request else "PO" in email.upper()
            proposal = fake_proposal(has_po=has_po)
        else:
            proposal = quote_playbook.build_proposal(email, request=request, on_step=status.write)
        secs = time.perf_counter() - t0
        status.update(label=f"Built in {secs:.1f} s", state="complete", expanded=False)
    st.session_state.proposal, st.session_state.quote_seconds, st.session_state.created_quote = proposal, secs, None


def _inbox() -> None:
    files = sorted(EMAILS_DIR.glob("*.txt"))
    choice = st.selectbox("Sample email", [PASTE] + [f.name for f in files])
    text = "" if choice == PASTE else (EMAILS_DIR / choice).read_text(encoding="utf-8")
    email = st.text_area("Customer email", text, height=320, key=f"email-{choice}")
    if st.button("Build quote", type="primary", disabled=not email.strip()):
        st.session_state.email_text = email
        _build(email)


def _edit_fields(p: QuoteProposal) -> QuoteRequest | None:
    """Editable extracted fields. Returns the edited request when the user clicks Re-check."""
    r = p.request
    with st.form("fields"):
        c1, c2, c3 = st.columns(3)
        customer = c1.text_input("Customer", r.customer_name or "")
        mode = c2.selectbox("Mode", ["TL", "LTL", "EXPEDITE"], index=["TL", "LTL", "EXPEDITE"].index(r.mode) if r.mode else 0)
        equipment = c3.text_input("Equipment", r.equipment_type or "")
        o1, o2, d1, d2 = st.columns([3, 1, 3, 1])
        o_city, o_state = o1.text_input("Origin city", r.origin.city or ""), o2.text_input("ST", r.origin.state or "", key="ost")
        d_city, d_state = d1.text_input("Destination city", r.destination.city or ""), d2.text_input("ST", r.destination.state or "", key="dst")
        p1, p2, p3 = st.columns(3)
        pickup = p1.date_input("Pickup date", r.pickup_date or None)
        declared = p2.number_input("Declared value ($)", value=r.declared_value or 0.0, min_value=0.0)
        refs = p3.text_input("References (PO=123, SO=456)", ", ".join(f"{k}={v}" for k, v in r.references.items()))
        lines = st.data_editor([fl.model_dump() for fl in r.freight_lines] or [FreightLine().model_dump()],
                               num_rows="dynamic", use_container_width=True, key="freight")
        if not st.form_submit_button("Re-check"):
            return None
    references = dict(part.split("=", 1) for part in (s.strip() for s in refs.split(",")) if "=" in part)
    return r.model_copy(update={
        "customer_name": customer or None, "mode": mode, "equipment_type": equipment or None,
        "origin": Location(city=o_city or None, state=o_state.upper() or None),
        "destination": Location(city=d_city or None, state=d_state.upper() or None),
        "pickup_date": pickup if isinstance(pickup, date) else None,
        "declared_value": declared or None,
        "references": {k.strip().upper(): v.strip() for k, v in references.items()},
        "freight_lines": [FreightLine(**{k: v for k, v in row.items() if v is not None}) for row in lines],
    })


def _proposal(p: QuoteProposal) -> None:
    if p.missing_fields:
        st.warning("Missing: " + ", ".join(p.missing_fields))
    edited = _edit_fields(p)
    if edited is not None:
        _build(st.session_state.get("email_text", ""), request=edited)
        st.rerun()

    st.markdown("**Checks**")
    for c in p.checks:
        st.markdown(f"{'✅' if c.passed else '❌'} **{c.name}**: {c.detail}")

    if p.sell_total is not None:
        m1, m2, m3 = st.columns(3)
        m1.metric("Buy", f"${p.buy_total:,.2f}")
        m2.metric("Sell", f"${p.sell_total:,.2f}")
        m3.metric("GP %", f"{p.gp_pct}%", help=f"Priced at {p.margin_pct_used}% margin")
        if p.request.mode == "TL" and p.gp_pct is not None and p.margin_pct_used and p.gp_pct < p.margin_pct_used:
            st.caption("TL fuel passes through at cost, so blended GP lands slightly under the linehaul margin. Not a bug.")
        with st.expander("Buy and sell lines"):
            st.markdown("Buy (rate engine)")
            st.dataframe(p.buy_lines, use_container_width=True)
            st.markdown("Sell (price preview)")
            st.dataframe(p.sell_lines, use_container_width=True)

    reply = st.text_area("Draft reply", p.draft_reply, height=160)

    b1, b2, b3 = st.columns(3)
    created = st.session_state.created_quote
    if b1.button("✅ Create quote", type="primary", disabled=not p.ready_to_create or bool(created)):
        res = {"ok": True, "dry_run": True} if state.use_fake() else quote_playbook.create_quote(p)
        st.session_state.created_quote = res
        if res["ok"]:
            state.record_done("quote", st.session_state.quote_seconds or 0)
        st.rerun()
    quote_id = (created or {}).get("quote_id")
    if b2.button("📤 Mark as sent", disabled=not quote_id):
        res = quote_playbook.mark_sent(quote_id)
        st.toast("Marked as sent" if res["ok"] else f"Failed: {res.get('detail')}")
    with b3.popover("📋 Copy reply"):
        st.code(reply, language=None)

    if created:
        if not created["ok"]:
            st.error(created.get("detail") or created.get("error"))
        elif created.get("dry_run"):
            st.success("Dry-run: the quote would be created and priced. Nothing was written.")
        else:
            st.success(f"Quote {created['quote_id']} · {created['status']} · GP {created['gp_pct']}% · "
                       + ("verified ✓" if created["verified"] else ":red[NOT verified]"))


def render() -> None:
    left, right = st.columns([2, 3], gap="large")
    with left:
        st.subheader("Inbox")
        _inbox()
    with right:
        st.subheader("Proposal")
        p = st.session_state.proposal
        if p is None:
            st.caption("Pick or paste an email, then **Build quote**.")
            return
        baseline = state.load_baseline().get("quote_from_email", {}).get("minutes")
        if st.session_state.quote_seconds is not None and baseline:
            st.caption(f"Built in {st.session_state.quote_seconds:.1f} s · Manual baseline: {_fmt_minutes(baseline)}")
        _proposal(p)
