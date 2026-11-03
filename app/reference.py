"""Reference lists shared by the route modules. The database enforces the same rules; these drive the forms."""
from sqlalchemy import text

PURPOSES = [("rent_housing", "Rent and housing"), ("utilities", "Utilities and bills"), ("food", "Food"),
            ("medical", "Medical"), ("education", "Education costs"), ("disaster_recovery", "Disaster recovery"),
            ("debt", "Debt"), ("other", "Other")]
PURPOSE_LABELS = dict(PURPOSES)
EVENTS = [("earthquake", "Earthquake"), ("flood", "Flood"), ("fire", "Fire"), ("other_disaster", "Other disaster")]
MEDICAL_LOCATIONS = ("turkey", "abroad")
CONTACT_METHODS = ("phone", "email", "whatsapp", "other")
STATUS_TEXT = {"pending": "Waiting for review", "published": "On the board", "rejected": "Rejected",
               "fulfilled": "Fully funded", "withdrawn": "Withdrawn"}
MAX_PENDING = 3


def limit_key(purpose: str, medical_location: str) -> str:
    return "medical_" + medical_location if purpose == "medical" else purpose


def load_limits(conn) -> dict:
    """{limit key: (currency, ceiling)} read from the purpose_limits table."""
    return {k: (cur, ceil) for k, cur, ceil in conn.execute(text("SELECT key, currency, ceiling FROM purpose_limits")).all()}


def need_label(purpose: str, medical_location) -> str:
    if purpose == "medical":
        return "Medical " + ("abroad" if medical_location == "abroad" else "in Turkey")
    return PURPOSE_LABELS.get(purpose, purpose)


def money(amount, currency: str) -> str:
    return f"{amount:,.0f} {currency}"
