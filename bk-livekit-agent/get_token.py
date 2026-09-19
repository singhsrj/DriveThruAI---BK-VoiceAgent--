"""
Prints a LiveKit access token + server URL to paste into frontend.html.

The frontend is a static HTML file with no backend of its own, so it
can't safely hold your LIVEKIT_API_SECRET (that must never reach the
browser). This script does the one thing that needs the secret --
mint a short-lived token -- and just prints the result for you to
copy in.

Usage:
    uv run get_token.py                  # random room name, identity "customer"
    uv run get_token.py --room my-room
    uv run get_token.py --identity alice --ttl 30

Requires LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET in your .env
(the same ones bot.py uses).
"""

import argparse
import os
import uuid
from datetime import timedelta

from dotenv import load_dotenv
from livekit import api

load_dotenv(override=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a LiveKit access token")
    parser.add_argument(
        "--room", default=None,
        help="Room name to join (default: a random bk-order-<id> room)",
    )
    parser.add_argument(
        "--identity", default="customer",
        help="Participant identity (default: customer)",
    )
    parser.add_argument(
        "--ttl", type=int, default=60,
        help="Token lifetime in minutes (default: 60)",
    )
    args = parser.parse_args()

    room_name = args.room or f"bk-order-{uuid.uuid4().hex[:8]}"

    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    livekit_url = os.getenv("LIVEKIT_URL")

    if not api_key or not api_secret:
        print("Missing LIVEKIT_API_KEY / LIVEKIT_API_SECRET in .env")
        return
    if not livekit_url:
        print("Missing LIVEKIT_URL in .env")
        return

    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(args.identity)
        .with_name(args.identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_ttl(timedelta(minutes=args.ttl))
        .to_jwt()
    )

    print("\nPaste these into frontend.html:\n")
    print(f"  Server URL : {livekit_url}")
    print(f"  Room       : {room_name}")
    print(f"  Token      : {token}\n")
    print(
        "With bot.py running in `dev` mode, it auto-dispatches into any "
        "room a participant joins -- just open frontend.html, paste the "
        "URL and token above, and connect.\n"
    )


if __name__ == "__main__":
    main()