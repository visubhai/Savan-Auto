"""
CSV parsers - supports all Indian bus booking platforms.
RedBus, AbhiBus, MakeMyTrip, Paytm, Goibibo, EaseMyTrip, generic.
"""
import csv, io, re


# ── Phone cleanup ────────────────────────────────────────────────────────────
def clean_phone(raw):
    if not raw: return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits: return None
    digits = digits.lstrip("0")
    if len(digits) == 10:   digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"): digits = "91" + digits[1:]
    elif len(digits) == 12 and digits.startswith("91"): pass
    elif len(digits) == 13 and digits.startswith("910"): digits = "91" + digits[3:]
    else:
        if not (10 <= len(digits) <= 15): return None
    if not digits.startswith("91") or len(digits) != 12: return None
    return digits


def clean_name(raw):
    if not raw: return ""
    return re.sub(r"\s+", " ", str(raw).strip()).title()


def clean_route(raw):
    if not raw: return ""
    s = str(raw).strip().strip("()")
    # Don't treat dates as routes
    if re.match(r"^\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}$", s): return ""
    if re.match(r"^\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2}$", s): return ""
    s = re.sub(r"\s*[-–→>|]\s*", " to ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_platform(raw):
    if not raw: return ""
    s = re.sub(r"\s*\(.*?\)\s*", "", str(raw)).strip()
    lower = s.lower().replace(" ", "").replace("-", "").replace("_", "")

    # Match known platforms by substring so suffixes like "Dir", "Direct",
    # "Online", "Travel" are stripped — e.g. "Abhibus Dir" → "AbhiBus".
    # Order matters: check longer/more specific names first.
    known = [
        # Order matters: longer/more-specific names first so we don't
        # accidentally match "ibibo" inside "goibibo" (we want Goibibo).
        ("makemytrip", "MakeMyTrip"), ("easemytrip", "EaseMyTrip"),
        ("railyatri", "RailYatri"), ("redbus", "RedBus"),
        ("abhibus", "AbhiBus"), ("goibibo", "Goibibo"),
        ("paytm", "Paytm"), ("yatra", "Yatra"),
        ("ixigo", "Ixigo"), ("mmt", "MakeMyTrip"),
        # RedPro RnR/NR exports use OrgUnit values like REDBUS_IN /
        # MMT_IN / IBIBO_IN — map ibibo to Goibibo (Ibibo Group's brand).
        ("ibibo", "Goibibo"),
    ]
    for kw, name in known:
        if kw in lower:
            return name

    # Generic booking sources (exact match)
    exact = {
        "direct": "Direct", "offline": "Offline", "counter": "Counter",
        "website": "Website", "app": "App",
    }
    if lower in exact:
        return exact[lower]

    return s.title() if s else ""


def _decode(content):
    if isinstance(content, bytes):
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try: return content.decode(enc)
            except: pass
        return content.decode("latin-1", errors="replace")
    return content


def _is_date(s):
    """Return True if string looks like a date."""
    s = str(s).strip()
    return bool(re.match(r"^\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}$", s) or
                re.match(r"^\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2}$", s))


def _is_status_cancelled(val):
    return str(val).strip().lower() in (
        "cancelled", "canceled", "cancel", "refunded",
        "refund", "failed", "rejected"
    )


# ── Column role detection ─────────────────────────────────────────────────────
def _detect_role(key):
    """
    Given a column header, return its role:
    phone / name / from / to / route / platform / status / booking_id / skip
    """
    # Split camelCase / PascalCase first so "ContactNumber" → "contact number"
    # and "OrgUnit" → "org unit", letting the keyword regexes match them.
    k = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key.strip())
    k = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", k)
    k = k.lower()
    k = re.sub(r"[^a-z0-9 ]", " ", k)
    k = re.sub(r"\s+", " ", k).strip()

    # Skip date/time columns immediately
    if any(w in k for w in ("date", "time", "year", "month", "day", "doj", "departure time")):
        return "skip"

    # Booking ID — standalone 'number' or explicit booking/ticket/order/pnr columns
    if k in ("number", "no", "sl no", "sr no", "serial no", "serial number"):
        return "booking_id"
    if any(k == exact for exact in ("pnr", "gds pnr number", "gds pnr", "booking id",
                                     "booking number", "booking no", "ticket number",
                                     "ticket no", "order id", "txn id", "transaction id",
                                     "reference number", "ref no")):
        return "booking_id"

    # Phone — only when explicitly 'phone', 'mobile', or 'passenger number/phone/mobile'
    name_words = ("name", "passenger", "customer", "traveller", "traveler")
    phone_exact = {"phone", "mobile", "contact", "whatsapp no", "whatsapp number",
                   "passenger phone", "passenger mobile", "passenger contact",
                   "passenger number", "mobile number", "phone number",
                   "contact number", "mobile no", "phone no"}
    if k in phone_exact:
        return "phone"
    # 'mobile' or 'phone' or 'contact' as standalone word, not combined with booking/date/seat
    if re.search(r"\b(mobile|phone|contact)\b", k) and not any(
            w in k for w in ("booking", "order", "ticket", "seat", "date", "id", "gds", "txn")):
        return "phone"

    # Name
    if any(w in k for w in name_words) and "number" not in k and "id" not in k:
        return "name"

    # Combined "From/To" column (e.g. Paytm's "From/To" → normalized to "from to")
    if re.search(r"\bfrom\b.*\bto\b", k):
        return "route"

    # Origin / From
    if re.search(r"\bfrom\b", k) or k in ("origin", "source city", "boarding",
             "from city", "from station", "departure", "boarding point",
             "pickup", "from location", "journey from", "travel from"):
        return "from"
    if re.search(r"\bto\b", k) or k in ("destination", "destination city",
             "to city", "to station", "dropping", "dropping point", "arrival",
             "drop", "to location", "journey to", "travel to"):
        return "to"

    # Platform / booking source
    if k in ("booked by", "booking source", "platform", "channel", "agent",
             "operator", "source", "booking channel", "booked from",
             "booked via", "book via", "booking platform",
             "org unit", "orgunit", "org"):
        if "city" not in k and "station" not in k:
            return "platform"

    # Status
    if k in ("status", "booking status", "travel status", "journey status",
             "trip status", "order status"):
        return "status"

    # Route
    if any(w in k for w in ("route", "travel route", "service name")):
        return "route"

    return "skip"


# ── Section splitter (RedBus / AbhiBus multi-section) ────────────────────────
def _split_sections(text):
    """Return {Bookings: [lines], Cancellations: [lines]}"""
    lines = text.splitlines()
    sections = {"Bookings": [], "Cancellations": []}
    cur = None
    for line in lines:
        stripped = line.strip().strip(',"\'').lower()
        if stripped in ("bookings", "booking", "booked"):
            cur = "Bookings"; continue
        if stripped in ("cancellations", "cancellation", "cancelled", "canceled", "refunds", "refund"):
            cur = "Cancellations"; continue
        if stripped in ("summary", "totals", "total"):
            cur = None; continue
        if not line.strip().replace(",", "").strip():
            continue
        if cur:
            sections[cur].append(line)
    return sections


# ── Core row parser ───────────────────────────────────────────────────────────
def _parse_rows(lines, filename_hint="", cancelled_ids=None):
    """
    Parse a list of CSV lines into passengers list.
    cancelled_ids: set of booking IDs to skip (from Cancellations section)
    """
    if not lines:
        return [], set(), 0, 0, 0

    if cancelled_ids is None:
        cancelled_ids = set()

    reader = csv.DictReader(lines)
    passengers = []
    seen_phones = set()
    duplicates = 0
    invalid = 0
    total = 0

    # Build role map for columns
    role_map = {}
    if reader.fieldnames:
        for col in reader.fieldnames:
            if col:
                role_map[col] = _detect_role(col)

    # Auto-detect platform from filename if not in columns
    fname_platform = ""
    if filename_hint:
        fl = filename_hint.lower()
        for kw, name in [("redbus","RedBus"),("abhibus","AbhiBus"),("makemytrip","MakeMyTrip"),
                          ("mmt","MakeMyTrip"),("paytm","Paytm"),("goibibo","Goibibo"),
                          ("easemytrip","EaseMyTrip"),("yatra","Yatra"),("ixigo","Ixigo")]:
            if kw in fl:
                fname_platform = name
                break

    for row in reader:
        total += 1
        phone = name = from_city = to_city = route = platform = status = booking_id = ""

        for col, val in row.items():
            if not col: continue
            role = role_map.get(col, "skip")
            v = (val or "").strip()
            if role == "phone" and not phone:         phone = v
            elif role == "name" and not name:         name = v
            elif role == "from" and not from_city:    from_city = v
            elif role == "to" and not to_city:        to_city = v
            elif role == "route" and not route:       route = v
            elif role == "platform" and not platform: platform = v
            elif role == "status" and not status:     status = v
            elif role == "booking_id" and not booking_id: booking_id = v

        # Skip if booking ID is in cancelled set (from Cancellations section)
        if booking_id and booking_id in cancelled_ids:
            invalid += 1
            continue

        # Skip rows with cancelled status
        if status and _is_status_cancelled(status):
            invalid += 1
            continue

        # Build route from from/to cities
        if from_city and to_city and not _is_date(from_city) and not _is_date(to_city):
            route = f"{clean_name(from_city)} to {clean_name(to_city)}"
        elif from_city and not to_city and not _is_date(from_city):
            # Single combined column value (e.g. "Surat - Ahmedabad") ended up in from_city
            route = clean_route(from_city)
        elif route and not _is_date(route):
            route = clean_route(route)
        else:
            route = ""

        # Platform fallback to filename hint
        if not platform and fname_platform:
            platform = fname_platform

        phone_clean = clean_phone(phone)
        if not phone_clean:
            invalid += 1
            continue
        if phone_clean in seen_phones:
            duplicates += 1
            continue
        seen_phones.add(phone_clean)

        passengers.append({
            "phone": phone_clean,
            "name": clean_name(name) or "Customer",
            "route": route,
            "platform": clean_platform(platform) or "Direct",
        })

    return passengers, seen_phones, total, duplicates, invalid


# ── RedBus multi-section ──────────────────────────────────────────────────────
def parse_redbus_csv(text, filename=""):
    sections = _split_sections(text)

    # Collect cancelled booking IDs from Cancellations section
    cancelled_from_section = set()
    if sections["Cancellations"]:
        try:
            r = csv.DictReader(sections["Cancellations"])
            for row in r:
                for k, v in row.items():
                    if k and "number" in k.lower() and "passenger" not in k.lower() and "seat" not in k.lower():
                        if (v or "").strip():
                            cancelled_from_section.add(v.strip())
                            break
        except: pass

    lines_to_parse = sections["Bookings"] if sections["Bookings"] else text.splitlines()
    passengers, _, total, dupes, invalid = _parse_rows(
        lines_to_parse, filename, cancelled_ids=cancelled_from_section
    )

    return {
        "passengers": passengers,
        "cancelled_count": len(cancelled_from_section),
        "duplicates_removed": dupes,
        "invalid_phones": invalid,
        "total_rows_seen": total,
    }


# ── RedBus RnR (Ratings & Reviews) export parser ──────────────────────────────
def parse_review_export(content, filename=""):
    """Parse a RedBus 'RnR' CSV export.

    The file only contains rows for passengers who DID submit a review/rating.
    Each row carries the passenger's phone in the `ContactNumber` column
    (prefixed with a literal apostrophe + '+' — Excel anti-numeric trick).

    Returns {
      reviewers: set of cleaned phones (91XXXXXXXXXX) who reviewed,
      by_phone: dict[phone] -> { name, pnr, route, rating, review, date, platform },
      total_rows, skipped_no_phone, skipped_invalid_phone,
    }
    """
    text = _decode(content) if isinstance(content, (bytes, bytearray)) else str(content)
    reader = csv.DictReader(io.StringIO(text))
    reviewers = set()
    by_phone = {}
    total = 0
    skipped_blank = 0
    skipped_invalid = 0

    for row in reader:
        total += 1
        raw_phone = (row.get("ContactNumber") or "").strip()
        if not raw_phone:
            skipped_blank += 1
            continue
        # Strip Excel-protection apostrophe and any plus / spaces
        cleaned_raw = raw_phone.lstrip("'").lstrip("+").strip()
        phone = clean_phone(cleaned_raw)
        if not phone:
            skipped_invalid += 1
            continue
        reviewers.add(phone)
        org = (row.get("OrgUnit") or "").upper()
        platform = (
            "RedBus"     if "REDBUS" in org else
            "MakeMyTrip" if "MMT"    in org else
            ""
        )
        by_phone[phone] = {
            "name":     clean_name(row.get("Name", "")) or "Customer",
            "pnr":      (row.get("PNR", "") or "").strip().strip('"'),
            "route":    clean_route(row.get("Route", "")) or "",
            "rating":   (row.get("Rating", "") or "").strip(),
            "review":   (row.get("Review", "") or "").strip(),
            "date":     (row.get("DateOfRating", "") or "").strip(),
            "platform": platform,
        }
    return {
        "reviewers": reviewers,
        "by_phone": by_phone,
        "total_rows": total,
        "skipped_no_phone": skipped_blank,
        "skipped_invalid_phone": skipped_invalid,
    }


# ── Bulk Campaign: extract ALL phone numbers from any file ────────────────────
def extract_all_phones(content, filename=""):
    """
    Extract every valid Indian mobile number from any file — CSV, TXT, Excel export,
    WhatsApp export, copy-pasted list, etc. Ignores structure; just hunts for numbers.
    Returns list of unique cleaned phones (e.g. '919876543210').
    """
    text = _decode(content) if isinstance(content, (bytes, bytearray)) else str(content)

    # Find all digit clusters that could be phone numbers (7–15 digits, allow spaces/dashes)
    candidates = re.findall(r'[\+\d][\d\s\(\)\-\.]{6,16}[\d]', text)
    phones = []
    seen = set()
    for c in candidates:
        digits = re.sub(r'\D', '', c)
        # Try various sub-sequences in case multiple numbers are jammed together
        for length in (12, 11, 10):
            if len(digits) >= length:
                chunk = digits[:length]
                cleaned = clean_phone(chunk)
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    phones.append(cleaned)
                    break
    return phones


# ── Auto-detect and parse ─────────────────────────────────────────────────────
def auto_parse(content, filename=""):
    text = _decode(content)
    lower = text[:6000].lower()

    is_multisection = (
        ("bookings" in lower and "cancellations" in lower) or
        "gds pnr" in lower or
        ("passenger number" in lower and "passenger name" in lower)
    )

    if is_multisection:
        return parse_redbus_csv(text, filename)

    # Generic single-section
    lines = text.splitlines()
    passengers, _, total, dupes, invalid = _parse_rows(lines, filename)
    return {
        "passengers": passengers,
        "cancelled_count": 0,
        "duplicates_removed": dupes,
        "invalid_phones": invalid,
        "total_rows_seen": total,
    }