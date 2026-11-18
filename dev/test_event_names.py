"""Readable event names in the verifier's pledge history.

Run from the project folder:  python -m dev.test_event_names
Reads source and a mapping only; it needs no database and writes nothing. It cannot render the page, so also look
at one pledge's history in the browser (see the note printed at the end).
"""
import pathlib
import sys

from app.routes.verify import EVENT_NAMES

fails = []
count = 0


def check(name, ok, extra=""):
    global count
    count += 1
    if not ok:
        fails.append(name)
        print("FAIL", name, extra)


SHOWN = {"pledged", "pledge_sent", "pledge_cancelled", "receipt_confirmed", "receipt_disputed",
         "dispute_arrived", "dispute_not_arrived"}
SOURCE = (pathlib.Path(__file__).resolve().parent.parent / "app" / "routes" / "verify.py").read_text(encoding="utf-8")

check("a readable name for every event the history query can return", set(EVENT_NAMES) == SHOWN,
      sorted(set(EVENT_NAMES) ^ SHOWN))
check("no name shows an underscore", all("_" not in v for v in EVENT_NAMES.values()))
check("every name is different", len(set(EVENT_NAMES.values())) == len(EVENT_NAMES))
check("the two settled outcomes say which way it went",
      "did not arrive" in EVENT_NAMES["dispute_not_arrived"] and "did not arrive" not in EVENT_NAMES["dispute_arrived"])
check("the history list uses the names, with the stored key as fallback",
      '"event": EVENT_NAMES.get(e.event, e.event)' in SOURCE)
check("the history query still filters on the stored keys", "'dispute_arrived', 'dispute_not_arrived') ORDER BY e.id" in SOURCE)

if fails:
    print(f"{len(fails)} of {count} checks FAILED")
    sys.exit(1)
print(f"event names: all {count} checks passed")
print("Not covered: the rendered page. Open one pledge's history as a verifier and read the Event column.")
