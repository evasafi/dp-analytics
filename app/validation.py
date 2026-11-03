"""Input checks for the requester forms. The database enforces the same rules again; these give readable messages."""
import re

from app.districts import DISTRICTS_BY_PROVINCE
from app.provinces import PROVINCE_NAMES
from app.reference import CONTACT_METHODS, EVENTS, MEDICAL_LOCATIONS, PURPOSE_LABELS, limit_key, money

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]+")
_PHONE = re.compile(r"\+?[0-9\s\-().]+")
_WHOLE = re.compile(r"[0-9]{1,9}")


def contact_ok(method: str, contact: str) -> bool:
    if method not in CONTACT_METHODS or not 3 <= len(contact) <= 200:
        return False
    if method == "email":
        return bool(_EMAIL.fullmatch(contact))
    if method in ("phone", "whatsapp"):
        digits = sum(ch in "0123456789" for ch in contact)
        return bool(_PHONE.fullmatch(contact)) and 7 <= digits <= 15
    return True


def contact_problem(method: str, contact: str):
    if contact_ok(method, contact):
        return None
    if method == "email":
        return "That does not look like an email address, for example name@example.com."
    if method in ("phone", "whatsapp"):
        return ("A phone or WhatsApp number must have 7 to 15 digits, and may contain only digits, spaces, "
                "brackets, dots and dashes, with an optional leading +. Use Other for anything else.")
    if method == "other":
        return "Write 3 to 200 characters on how a verifier can reach you."
    return "Choose how a verifier can reach you."


def normalize_iban(iban: str) -> str:
    return re.sub(r"\s+", "", iban).upper()


def iban_problem(iban: str):
    """Shape and check digits (ISO 13616 mod 97), so a mistyped account number is caught before money is sent."""
    iban = normalize_iban(iban)
    if not re.fullmatch(r"TR[0-9]{24}", iban):
        return "Enter a Turkish IBAN: TR followed by 24 digits."
    if int("".join(str(int(ch, 36)) for ch in iban[4:] + iban[:4])) % 97 != 1:
        return "The check digits of this IBAN do not match. Check it for typing mistakes."
    return None


def need_problems(form: dict, limits: dict) -> list:
    """All problems with a posted need, as readable messages. Empty list means the form is fine."""
    errors = []
    purpose, loc = form["purpose"], form["medical_location"]
    if purpose not in PURPOSE_LABELS:
        errors.append("Choose what the money is for.")
    if purpose == "medical" and loc not in MEDICAL_LOCATIONS:
        errors.append("Say whether the treatment is in Turkey or abroad.")
    if purpose == "disaster_recovery" and form["event"] not in dict(EVENTS):
        errors.append("Choose which event.")
    province, district = form["province"], form["district"]
    if province not in PROVINCE_NAMES:
        errors.append("Choose a province from the list.")
    elif district not in DISTRICTS_BY_PROVINCE.get(province, ()):
        errors.append("Choose a district that belongs to the province.")
    hh = form["household_size"]
    if not (_WHOLE.fullmatch(hh) and 1 <= int(hh) <= 12):
        errors.append("Household size must be a whole number from 1 to 12.")
    goal = form["goal"]
    if not _WHOLE.fullmatch(goal) or int(goal) < 100:
        errors.append("The amount must be a whole number of at least 100.")
    elif purpose in PURPOSE_LABELS and not (purpose == "medical" and loc not in MEDICAL_LOCATIONS):
        cur, ceiling = limits[limit_key(purpose, loc)]
        if int(goal) > ceiling:
            errors.append("The amount is above the limit of %s for this purpose." % money(ceiling, cur))
    if not 10 <= len(form["description"]) <= 1000:
        errors.append("The description must be 10 to 1000 characters (it is %d)." % len(form["description"]))
    problem = contact_problem(form["contact_method"], form["contact"])
    if problem:
        errors.append(problem)
    if not 2 <= len(form["payee_name"]) <= 100:
        errors.append("Enter the account holder's name (2 to 100 characters).")
    problem = iban_problem(form["iban"])
    if problem:
        errors.append(problem)
    return errors


_PHONE_LIKE = re.compile(r"(?:\d[\s\-().+]*){7,}")


def _name_words(s: str) -> set:
    return {w for w in re.findall(r"[^\W\d_]+", s.lower()) if len(w) >= 3}


def public_text_problem(public_text: str, contact: str, payee_name: str, username: str):
    """The text the public board shows must not identify the requester or reveal how to reach them."""
    t = public_text.strip()
    if not 10 <= len(t) <= 300:
        return "The public text must be 10 to 300 characters."
    low = t.lower()
    if "@" in t or "://" in t or "www." in low:
        return "The public text must not contain an email address or a link."
    if _PHONE_LIKE.search(t):
        return "The public text looks like it contains a phone number or an account number. Remove digits that could identify someone."
    if len(contact) >= 3 and contact.lower() in low:
        return "The public text contains the requester's contact details."
    if _name_words(payee_name) & _name_words(t):
        return "The public text contains part of the account holder's name. Do not name anyone."
    if len(username) >= 3 and username.lower() in low:
        return "The public text contains the requester's username."
    return None
