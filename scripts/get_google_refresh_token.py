"""One-time helper: obtain a Google OAuth refresh token for the Sheets API.

Prerequisites:
  - In Google Cloud Console -> APIs & Services -> Credentials, create an
    "OAuth client ID" of type "Desktop app".
  - Put its client id + secret in your .env as GOOGLE_OAUTH_CLIENT_ID /
    GOOGLE_OAUTH_CLIENT_SECRET (or export them in your shell).
  - Make sure the OAuth consent screen has your Google account added as a
    test user (or is published), and includes the
    ".../auth/spreadsheets" scope is allowed (any scope is fine for Desktop
    apps in testing mode).

Run:
    python scripts/get_google_refresh_token.py

A browser window opens; sign in as the Google account that has EDIT access to
the target Sheet and approve. The refresh token is printed at the end -- paste
it into .env as GOOGLE_OAUTH_REFRESH_TOKEN.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def main() -> None:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set.\n"
            "Add them to .env first (from your Desktop-app OAuth client)."
        )
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install deps first:  pip install -r requirements.txt")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=SCOPES,
    )

    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="Opening browser for Google consent...",
        success_message="Done. You can close this tab and return to the terminal.",
    )

    if not creds.refresh_token:
        print(
            "\nNo refresh token was returned. Revoke the app's access at "
            "https://myaccount.google.com/permissions and run this again."
        )
        sys.exit(1)

    print("\n" + "=" * 70)
    print("SUCCESS. Add this line to your .env:\n")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 70)


if __name__ == "__main__":
    main()
