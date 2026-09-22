from pathlib import Path

import psycopg
from Crypto.Hash import keccak

from config import CONTRACTS, EVENTS, PG_DSN


def connect():
    return psycopg.connect(PG_DSN)


def keccak256(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


def hb(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str[2:])


def init(conn):
    conn.execute(
        Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    )
    with conn.cursor() as cur:
        for addr, label in CONTRACTS.items():
            cur.execute(
                "INSERT INTO contracts VALUES (%s, %s) ON CONFLICT (address) DO UPDATE SET label = excluded.label",
                (hb(addr), label),
            )
        for sig in EVENTS:
            cur.execute(
                "INSERT INTO event_signatures VALUES (%s, %s, %s) ON CONFLICT (topic0) DO NOTHING",
                (keccak256(sig.encode()), sig.split("(")[0], sig),
            )
    conn.commit()


def get_state(conn, key):
    row = conn.execute("SELECT value FROM sync_state WHERE key = %s", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn, key, value):
    conn.execute(
        "INSERT INTO sync_state VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
