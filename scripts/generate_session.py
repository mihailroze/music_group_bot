from __future__ import annotations

import os

from pyrogram import Client


def main() -> None:
    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id_raw or not api_id_raw.isdigit():
        raise SystemExit("Set TELEGRAM_API_ID to a numeric value before running this script.")
    if not api_hash:
        raise SystemExit("Set TELEGRAM_API_HASH before running this script.")

    api_id = int(api_id_raw)
    with Client(
        name="session_exporter",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
    ) as app:
        session = app.export_session_string()
        print("\nASSISTANT_SESSION_STRING:\n")
        print(session)


if __name__ == "__main__":
    main()
