"""Meridian TMS MCP tool server: READ-ONLY tools for the Investigate agent. Owner: tools & rules lead.

Run standalone for testing:   mcp dev server.py
agent.py launches this file itself over stdio.
Every tool wraps a meridian.client function, so results are {"ok": ..., "data" | "detail": ...}.
Never print to stdout here: stdout is the MCP channel.
"""

from mcp.server.fastmcp import FastMCP

from meridian import client

mcp = FastMCP("meridian")


@mcp.tool()
def get_shipment(shipment_id: str) -> dict:
    """Get one shipment by its file number (e.g. 7300172): status, stops (appointments, MABD), carrier, money lines, milestones and summary."""
    return client.get_shipment(shipment_id)


@mcp.tool()
def get_shipment_events(shipment_id: str) -> dict:
    """The shipment's activity log (notes, check calls, status changes), newest first."""
    return client.get_shipment_events(shipment_id)


@mcp.tool()
def get_carrier(vendor_id: str) -> dict:
    """One carrier (vendor id, e.g. ACMECARR): status, compliance light, insurance policies and expiry dates, contacts."""
    return client.get_vendor(vendor_id)


@mcp.tool()
def get_facility(facility_id: str) -> dict:
    """One facility (e.g. BBR-DC135): hours, appointment rules, must-arrive-by (MABD) program and chargeback note."""
    return client.get_facility(facility_id)


@mcp.tool()
def get_customer(customer_id: str) -> dict:
    """One customer (e.g. NORTHSTAR): margin floor, required references, contacts."""
    return client.get_customer(customer_id)


@mcp.tool()
def get_quote(quote_id: str) -> dict:
    """One quote by file number: status (EXPIRED is computed from valid_until), lane, buy/sell lines, GP."""
    return client.get_quote(quote_id)


@mcp.tool()
def search(q: str) -> dict:
    """Find records by any reference number (file, PO, PRO, BOL, MC...) or by part of a name."""
    return client.search(q)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
