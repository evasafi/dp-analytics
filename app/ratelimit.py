from sqlalchemy import text

from app.db import engine

WINDOW_MINUTES = 5
MAX_PER_USER = 5
MAX_PER_IP = 20


def is_locked(username: str, ip: str) -> bool:
    with engine.connect() as c:
        u = c.execute(
            text("SELECT count(*) FROM login_failures WHERE username = :u "
                 "AND ts > now() - make_interval(mins => :m)"),
            {"u": username, "m": WINDOW_MINUTES},
        ).scalar_one()
        i = c.execute(
            text("SELECT count(*) FROM login_failures WHERE ip = :ip "
                 "AND ts > now() - make_interval(mins => :m)"),
            {"ip": ip, "m": WINDOW_MINUTES},
        ).scalar_one()
    return u >= MAX_PER_USER or i >= MAX_PER_IP


def record_failure(username: str, ip: str) -> None:
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO login_failures (username, ip) VALUES (:u, :ip)"),
            {"u": username[:100], "ip": ip},
        )

REGISTER_KEY = "__register__"
MAX_REGISTRATIONS_PER_HOUR = 5


def registration_blocked(ip: str) -> bool:
    with engine.connect() as c:
        n = c.execute(
            text("SELECT count(*) FROM login_failures WHERE username = :k AND ip = :ip "
                 "AND ts > now() - make_interval(hours => 1)"),
            {"k": REGISTER_KEY, "ip": ip},
        ).scalar_one()
    return n >= MAX_REGISTRATIONS_PER_HOUR


def record_registration(ip: str) -> None:
    record_failure(REGISTER_KEY, ip)
