"""Item needs: the lists and checks behind the item posting form."""
import re

from sqlalchemy import text

from app.districts import DISTRICTS_BY_PROVINCE
from app.provinces import PROVINCE_NAMES
from app.validation import contact_problem

# Item posting switch. True shows the item option on the post form and accepts item needs; False closes
# the form again. Item needs that already exist keep working either way.
ITEM_NEEDS_OPEN = True

MAX_QUANTITY = 50
MAX_SPECIFICATION = 300

HANDOVER_METHODS = [
    ("public_place", "Meet in a public place, in daylight (recommended)"),
    ("courier_post", "Courier or post"),
    ("pickup_from_donor", "I pick it up from the donor"),
    ("other", "Other (explain to the verifier)"),
]

CATEGORY_LABELS = {
    "mobility": "Mobility and assistive devices",
    "medical_equipment": "Medical equipment and supplies",
    "heating": "Heating and fuel",
    "clothing": "Clothing and footwear",
    "bedding": "Bedding and sleeping gear",
    "kitchen": "Kitchen and home basics",
    "hygiene_baby": "Hygiene and baby items",
    "food": "Food parcel",
    "power_comms": "Power and communication",
    "other": "Other",
}

_WHOLE = re.compile(r"[0-9]{1,9}")


def load_item_types(conn):
    """Enabled item types grouped by category, and the set of their ids."""
    rows = conn.execute(text("SELECT id, category, name FROM item_types WHERE enabled ORDER BY id")).all()
    groups, ids, index = [], set(), {}
    for r in rows:
        if r.category not in index:
            index[r.category] = {"label": CATEGORY_LABELS.get(r.category, r.category), "types": []}
            groups.append(index[r.category])
        index[r.category]["types"].append((r.id, r.name))
        ids.add(r.id)
    return groups, ids


def item_problems(form: dict, type_ids: set) -> list:
    """All problems with a posted item need, as readable messages. Empty list means the form is fine."""
    errors = []
    t = form["item_type_id"]
    if not (_WHOLE.fullmatch(t) and int(t) in type_ids):
        errors.append("Choose what you need from the list.")
    q = form["quantity_needed"]
    if not (_WHOLE.fullmatch(q) and 1 <= int(q) <= MAX_QUANTITY):
        errors.append("The quantity must be a whole number from 1 to %d." % MAX_QUANTITY)
    if len(form["specification"]) > MAX_SPECIFICATION:
        errors.append("The size or model note can be at most %d characters (it is %d)."
                      % (MAX_SPECIFICATION, len(form["specification"])))
    if form["used_ok"] not in ("used", "new"):
        errors.append("Say whether a used item is fine or only a new one.")
    if form["handover_method"] not in dict(HANDOVER_METHODS):
        errors.append("Choose how you would prefer to receive it.")
    province, district = form["province"], form["district"]
    if province not in PROVINCE_NAMES:
        errors.append("Choose a province from the list.")
    elif district not in DISTRICTS_BY_PROVINCE.get(province, ()):
        errors.append("Choose a district that belongs to the province.")
    hh = form["household_size"]
    if not (_WHOLE.fullmatch(hh) and 1 <= int(hh) <= 12):
        errors.append("Household size must be a whole number from 1 to 12.")
    if not 10 <= len(form["description"]) <= 1000:
        errors.append("The description must be 10 to 1000 characters (it is %d)." % len(form["description"]))
    problem = contact_problem(form["contact_method"], form["contact"])
    if problem:
        errors.append(problem)
    return errors
