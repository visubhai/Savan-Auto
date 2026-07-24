"""
Multi-step WhatsApp booking conversation.

Flow:
  ask_origin       → list of distinct origins (up to 10)
  ask_destination  → list of destinations reachable from chosen origin
  ask_date         → buttons: Today / Tomorrow / Pick a date
                     (free-text date "DD/MM/YYYY" or "YYYY-MM-DD" also accepted)
  ask_trip         → list of trips on chosen route, with time + bus type + fare
  ask_seats        → free-text number 1–10
  ask_name         → free-text name (skipped if customer's name is already known)
  confirm          → buttons: ✓ Confirm / ✗ Cancel

A session lives in the `booking_sessions` table and auto-expires after 1 hour
of inactivity. Submitting "cancel" / "stop" at any step closes the session.

Public entry points:
  start(api, phone, customer_name)        — begin a fresh flow
  handle(api, phone, msg, content, customer_name)
                                          — feed in the customer's next message,
                                            advance state, send next prompt.
                                            Returns True if handled, False if
                                            the inbound wasn't a flow message
                                            (caller should fall through to
                                             auto-reply / nothing).
"""
from __future__ import annotations
import datetime as _dt
import database as db


# Reverse alphabet so the row ids never collide with auto-reply ids (ar_*)
_BACK_KEYWORDS  = {"back", "previous", "pichla", "પાછા"}
_CANCEL_KEYWORDS = {"cancel", "stop", "exit", "bye", "બંધ", "रद्द"}


def _ist_today():
    """Return today's date in IST."""
    return (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).date()


def _parse_date(text):
    """Accept 'DD/MM/YYYY', 'DD-MM-YYYY', 'YYYY-MM-DD', plus 'today'/'tomorrow'.
    Returns 'YYYY-MM-DD' or None if unparseable / in the past."""
    if not text:
        return None
    t = text.strip().lower()
    today = _ist_today()
    if t in ("today", "aaj", "આજ"):
        return today.strftime("%Y-%m-%d")
    if t in ("tomorrow", "kal", "કાલે"):
        return (today + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d %b %Y"):
        try:
            dt = _dt.datetime.strptime(text.strip(), fmt).date()
            if dt < today:
                return None  # no booking in the past
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _send_and_log(api, phone, body, rows=None, button_label="Choose",
                   buttons=None, footer=None):
    """Send the right kind of interactive message and log the outbound.
    `rows` (list) → list message. `buttons` (list) → button message.
    `body` only → plain text."""
    if rows:
        ok, result = api.send_interactive_list(
            phone, body, rows, button_label=button_label, footer_text=footer
        )
        preview_tail = "\n[List: " + " · ".join(r["title"] for r in rows) + "]"
        kind = "interactive"
    elif buttons:
        ok, result = api.send_interactive_buttons(
            phone, body, buttons, footer_text=footer
        )
        preview_tail = "\n[" + " | ".join(b["title"] for b in buttons) + "]"
        kind = "interactive"
    else:
        ok, result = api.send_text(phone, body)
        preview_tail = ""
        kind = "text"
    if ok:
        db.save_chat_message(phone, None, "out", body + preview_tail,
                              wa_message_id=result, message_type=kind)
    return ok


def _extract_choice(msg, content):
    """Return (kind, value).
      kind = 'button' or 'list' if a tap, 'text' if plain text, None otherwise.
      value = the title/id of the row, or the text typed."""
    if msg.get("type") == "interactive":
        inter = msg.get("interactive", {}) or {}
        if inter.get("type") == "button_reply":
            return "button", (inter.get("button_reply", {}).get("title", "").strip(),
                              inter.get("button_reply", {}).get("id", ""))
        if inter.get("type") == "list_reply":
            return "list", (inter.get("list_reply", {}).get("title", "").strip(),
                            inter.get("list_reply", {}).get("id", ""))
    if msg.get("type") == "text":
        return "text", ((content or "").strip(), "")
    return None, ("", "")


# ─── Step renderers ────────────────────────────────────────────────────────

def _ask_origin(api, phone):
    origins = db.list_distinct_origins()
    if not origins:
        _send_and_log(api, phone,
            "Sorry, no routes are configured yet. "
            "Please call us: +91-7567529300")
        db.close_booking_session(phone)
        return
    rows = [{"id": f"bkO_{i}", "title": o[:24]} for i, o in enumerate(origins[:10])]
    _send_and_log(api, phone,
        "🚌 *Book a Seat — Step 1 of 5*\nWhere are you travelling *from*?",
        rows=rows, button_label="Pick origin")


def _ask_destination(api, phone, origin):
    dests = db.list_destinations_from(origin)
    if not dests:
        _send_and_log(api, phone,
            f"Sorry, no destinations from {origin} right now.\n"
            "Type 'back' to pick a different origin.")
        return
    rows = [{"id": f"bkD_{r['id']}", "title": r["destination"][:24]} for r in dests[:10]]
    _send_and_log(api, phone,
        f"🚌 *Step 2 of 5*\nFrom *{origin}* — where to?",
        rows=rows, button_label="Pick destination")


def _ask_date(api, phone):
    today = _ist_today()
    tomorrow = today + _dt.timedelta(days=1)
    btns = [
        {"id": "bkdToday",    "title": f"Today ({today.strftime('%d %b')})"[:20]},
        {"id": "bkdTomorrow", "title": f"Tomorrow ({tomorrow.strftime('%d %b')})"[:20]},
        {"id": "bkdOther",    "title": "Another date"},
    ]
    _send_and_log(api, phone,
        "🚌 *Step 3 of 5*\nWhen do you want to travel?",
        buttons=btns)


def _ask_trip(api, phone, route_id, travel_date):
    trips = db.list_trips_for_route(route_id, active_only=True)
    if not trips:
        _send_and_log(api, phone,
            "No buses on this route yet. Please call us: +91-7567529300")
        db.close_booking_session(phone)
        return
    rows = []
    for t in trips[:10]:
        title = f"{t['departure_time']} {t['bus_type']}"[:24]
        desc  = f"₹{t['fare']} per seat"
        rows.append({"id": f"bkT_{t['id']}", "title": title, "description": desc})
    _send_and_log(api, phone,
        f"🚌 *Step 4 of 5*\nBuses on *{travel_date}*. Pick one:",
        rows=rows, button_label="See buses")


def _ask_seats(api, phone):
    _send_and_log(api, phone,
        "🚌 *Step 5 of 5*\nHow many seats? (reply with a number, e.g. *2*)")


def _ask_name(api, phone):
    _send_and_log(api, phone,
        "Almost done! What name should the booking be under?")


def _ask_confirm(api, phone, session):
    trip  = db.get_trip(session["trip_id"])
    seats = int(session["seats"] or 1)
    total = (trip["fare"] if trip else 0) * seats
    summary = (
        "📋 *Please confirm your booking:*\n\n"
        f"Name:  {session.get('customer_name') or '—'}\n"
        f"Route: {session['origin']} → {session['destination']}\n"
        f"Date:  {session['travel_date']}\n"
        f"Bus:   {trip['departure_time']} {trip['bus_type']}\n"
        f"Seats: {seats}\n"
        f"Fare:  ₹{trip['fare']} × {seats}  =  *₹{total}*\n\n"
        "Pay-on-board / pay at office. We'll confirm seats within 30 min."
    )
    btns = [
        {"id": "bkConfirm", "title": "✓ Confirm"},
        {"id": "bkRedo",    "title": "↻ Start over"},
        {"id": "bkCancel",  "title": "✗ Cancel"},
    ]
    _send_and_log(api, phone, summary, buttons=btns)


def _complete_booking(api, phone, session, customer_name_fallback):
    trip = db.get_trip(session["trip_id"])
    name = session.get("customer_name") or customer_name_fallback or "Customer"
    pnr = db.create_booking(
        phone=phone, customer_name=name,
        route_id=session["route_id"],
        origin=session["origin"], destination=session["destination"],
        trip_id=session["trip_id"],
        departure_time=trip["departure_time"], bus_type=trip["bus_type"],
        travel_date=session["travel_date"],
        seats=session["seats"], fare_per_seat=trip["fare"],
    )
    seats = int(session["seats"] or 1)
    total = trip["fare"] * seats
    body = (
        "🎉 *Booking Request Received!*\n\n"
        f"PNR: *{pnr}*\n"
        f"{session['origin']} → {session['destination']}\n"
        f"Date: {session['travel_date']}, {trip['departure_time']}\n"
        f"Bus: {trip['bus_type']}\n"
        f"Seats: {seats}  •  Total: ₹{total}\n\n"
        "✅ Our team will confirm your seats within 30 min and "
        "send the pickup-point address.\n\n"
        "Need to talk to us? ☎ +91-7567529300"
    )
    _send_and_log(api, phone, body)
    db.close_booking_session(phone)


# ─── Public entry points ──────────────────────────────────────────────────

def start(api, phone, customer_name=None):
    """Begin a fresh booking flow."""
    sid = db.create_booking_session(phone, state="ask_origin")
    if customer_name:
        db.update_booking_session(sid, customer_name=customer_name)
    _ask_origin(api, phone)


def handle(api, phone, msg, content, customer_name=None):
    """Advance the flow based on the inbound. Returns True if we handled it."""
    session = db.get_active_booking_session(phone)
    if not session:
        return False

    kind, (value, item_id) = _extract_choice(msg, content)
    if kind is None:
        # Ignore non-actionable inbound (image, voice, reaction) — keep session.
        return True

    text = value.strip()
    low  = text.lower()

    # Global escape hatches
    if low in _CANCEL_KEYWORDS or item_id == "bkCancel":
        db.close_booking_session(phone)
        _send_and_log(api, phone,
            "Booking cancelled. Send 'hi' anytime to start again.")
        return True
    if low in _BACK_KEYWORDS:
        # Step back one state
        order = ["ask_origin","ask_destination","ask_date","ask_trip","ask_seats","ask_name","confirm"]
        cur = session["state"]
        if cur in order and order.index(cur) > 0:
            prev = order[order.index(cur)-1]
            db.update_booking_session(session["id"], state=prev)
            session = db.get_active_booking_session(phone)
        return _resume(api, phone, session)
    if item_id == "bkRedo":
        db.close_booking_session(phone)
        start(api, phone, customer_name=customer_name)
        return True

    state = session["state"]

    if state == "ask_origin":
        # Either button/list "bkO_<idx>" → look up by index, or typed text
        chosen = text
        if item_id.startswith("bkO_"):
            chosen = text  # the title IS the origin name
        origins = db.list_distinct_origins()
        match = next((o for o in origins if o.lower() == chosen.lower()), None)
        if not match:
            _send_and_log(api, phone,
                "Couldn't find that origin. Please tap one of the buttons below.")
            _ask_origin(api, phone)
            return True
        db.update_booking_session(session["id"], origin=match, state="ask_destination")
        _ask_destination(api, phone, match)
        return True

    if state == "ask_destination":
        # item_id is "bkD_<route_id>" if tapped
        if item_id.startswith("bkD_"):
            route_id = int(item_id.split("_",1)[1])
            route = db.get_route(route_id)
            if not route:
                _send_and_log(api, phone, "Route not found. Tap again.")
                _ask_destination(api, phone, session["origin"])
                return True
            db.update_booking_session(session["id"],
                destination=route["destination"], route_id=route_id, state="ask_date")
            _ask_date(api, phone)
            return True
        # Text fallback: match destination name
        dests = db.list_destinations_from(session["origin"])
        match = next((r for r in dests if r["destination"].lower() == text.lower()), None)
        if not match:
            _send_and_log(api, phone, "Couldn't match that destination. Tap one below.")
            _ask_destination(api, phone, session["origin"])
            return True
        db.update_booking_session(session["id"],
            destination=match["destination"], route_id=match["id"], state="ask_date")
        _ask_date(api, phone)
        return True

    if state == "ask_date":
        chosen_date = None
        if item_id == "bkdToday":
            chosen_date = _ist_today().strftime("%Y-%m-%d")
        elif item_id == "bkdTomorrow":
            chosen_date = (_ist_today() + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        elif item_id == "bkdOther":
            _send_and_log(api, phone,
                "Type the date in *DD/MM/YYYY* format (e.g. 30/06/2026).")
            return True
        else:
            chosen_date = _parse_date(text)
        if not chosen_date:
            _send_and_log(api, phone,
                "Sorry, that date is invalid or in the past. "
                "Use DD/MM/YYYY (e.g. 30/06/2026) or tap Today/Tomorrow.")
            return True
        db.update_booking_session(session["id"], travel_date=chosen_date, state="ask_trip")
        _ask_trip(api, phone, session["route_id"], chosen_date)
        return True

    if state == "ask_trip":
        if item_id.startswith("bkT_"):
            trip_id = int(item_id.split("_",1)[1])
            trip = db.get_trip(trip_id)
            if not trip or trip["route_id"] != session["route_id"]:
                _send_and_log(api, phone, "Trip not found. Tap again.")
                _ask_trip(api, phone, session["route_id"], session["travel_date"])
                return True
            db.update_booking_session(session["id"], trip_id=trip_id, state="ask_seats")
            _ask_seats(api, phone)
            return True
        _send_and_log(api, phone, "Please tap one of the buses below.")
        _ask_trip(api, phone, session["route_id"], session["travel_date"])
        return True

    if state == "ask_seats":
        try:
            n = int(text)
        except ValueError:
            _send_and_log(api, phone, "Please reply with just a number, e.g. *2*.")
            return True
        if n < 1 or n > 10:
            _send_and_log(api, phone,
                "Seats must be between 1 and 10. For larger groups, "
                "use the 👥 Group Booking option from the main menu.")
            return True
        next_state = "confirm" if session.get("customer_name") else "ask_name"
        db.update_booking_session(session["id"], seats=n, state=next_state)
        if next_state == "ask_name":
            _ask_name(api, phone)
        else:
            session = db.get_active_booking_session(phone)
            _ask_confirm(api, phone, session)
        return True

    if state == "ask_name":
        if len(text) < 2 or len(text) > 50:
            _send_and_log(api, phone, "Please type your full name (2–50 letters).")
            return True
        db.update_booking_session(session["id"], customer_name=text, state="confirm")
        session = db.get_active_booking_session(phone)
        _ask_confirm(api, phone, session)
        return True

    if state == "confirm":
        if item_id == "bkConfirm" or low in ("yes","y","confirm","ok","haan"):
            _complete_booking(api, phone, session, customer_name)
            return True
        _send_and_log(api, phone, "Tap *✓ Confirm* to book, or *✗ Cancel* to discard.")
        _ask_confirm(api, phone, session)
        return True

    # Unknown state — defensive reset.
    db.close_booking_session(phone)
    return False


def _resume(api, phone, session):
    """Re-render the prompt for the session's current state (used by 'back')."""
    if not session:
        return False
    state = session["state"]
    if state == "ask_origin":      _ask_origin(api, phone)
    elif state == "ask_destination": _ask_destination(api, phone, session["origin"])
    elif state == "ask_date":        _ask_date(api, phone)
    elif state == "ask_trip":        _ask_trip(api, phone, session["route_id"], session["travel_date"])
    elif state == "ask_seats":       _ask_seats(api, phone)
    elif state == "ask_name":        _ask_name(api, phone)
    elif state == "confirm":         _ask_confirm(api, phone, session)
    return True
