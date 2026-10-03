"""Mark companies you EMAILED in the Queue's `email_track` column, from your Gmail sent mail.

    python scripts/sync_email_sends.py --since 2026-09-01            # dry run: report only, writes nothing
    python scripts/sync_email_sends.py --since 2026-09-01 --apply    # write the send dates
    python scripts/sync_email_sends.py --since 14d --apply           # weekly top-up (last 14 days)

What it does
  * Reads ONLY the headers of messages in your Sent folder (To / Cc / Bcc / Date). Never the body, never
    attachments, never subjects. Nothing is stored except the report below.
  * Matches each recipient's domain to a Queue row's website domain (exact, or a subdomain of it).
    Mailbox providers (gmail.com ...) and your own address never match.
  * Writes the latest send date (YYYY-MM-DD) into email_track. The scheduler then leaves that company
    alone for CFG.sched_email_hold_days (90) from that date, for calls AND as your "next available send".
  * Companies from the Clay-table imports are NOT written unless you pass --include-clay (that earlier
    campaign was a different, non-personalized one).
  * A date already in email_track is only ever replaced by a NEWER one; a note you typed ("sent",
    "2026-Q3") is never overwritten.

First run opens a browser: sign in as the Gmail account your outreach is SENT FROM (it can be a different
Google account from the one that owns the Sheet). The sign-in is saved in .gmail_token.json (local only,
gitignored, read-only scope). One-time Google Cloud setup, using the same project as the Sheets client:
  1. APIs & Services -> Library -> "Gmail API" -> Enable.
  2. OAuth consent screen -> Audience/Test users -> add the sending Gmail address.
  3. (If it asks) Data access / Scopes -> add .../auth/gmail.readonly.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import email_sync  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN = Path(".gmail_token.json")
REPORT = Path("email_sync_report.csv")
UNDO = Path("email_sync_undo.json")


def parse_since(s: str) -> str:
    s = s.strip().lower()
    if s.endswith("d") and s[:-1].isdigit():
        return (datetime.now(timezone.utc) - timedelta(days=int(s[:-1]))).date().isoformat()
    return datetime.fromisoformat(s).date().isoformat()


def gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        # Google expires sign-ins of apps in "Testing" mode after 7 days: fall back to a fresh sign-in.
        try:
            creds.refresh(Request())
        except Exception as e:  # noqa: BLE001
            print(f"Saved Gmail sign-in no longer works ({type(e).__name__}); signing in again.")
            creds = None
    if not creds or not creds.valid:
        cid, secret = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip(), os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
        if not cid or not secret:
            sys.exit("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set in .env")
        flow = InstalledAppFlow.from_client_config(
            {"installed": {"client_id": cid, "client_secret": secret, "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                           "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}, scopes=SCOPES)
        print("Opening the browser. Sign in as the Gmail account your outreach is SENT FROM.")
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                      success_message="Done. You can close this tab and return to the terminal.")
        TOKEN.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def fetch_sends(svc, since: str):
    me = svc.users().getProfile(userId="me").execute()["emailAddress"].lower()
    ids, page = [], None
    while True:
        r = svc.users().messages().list(userId="me", q=f"in:sent after:{since.replace('-', '/')}", maxResults=500, pageToken=page).execute()
        ids += [m["id"] for m in r.get("messages", [])]
        page = r.get("nextPageToken")
        if not page:
            break
    sends = []

    def cb(_rid, resp, exc):
        if exc is not None or not resp:
            return
        hdr = {}
        for h in resp.get("payload", {}).get("headers", []):
            hdr.setdefault(h["name"].lower(), []).append(h["value"])
        when = datetime.fromtimestamp(int(resp["internalDate"]) / 1000, tz=timezone.utc).date().isoformat()
        doms = email_sync.recipient_domains(hdr.get("to", []) + hdr.get("cc", []) + hdr.get("bcc", []), {me})
        sends.append({"date": when, "domains": doms})

    for i in range(0, len(ids), 50):
        batch = svc.new_batch_http_request(callback=cb)
        for mid in ids[i:i + 50]:
            batch.add(svc.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=["To", "Cc", "Bcc"]))
        batch.execute()
        time.sleep(0.3)
    return me, len(ids), sends


def read_queue():
    from app import google_sheets as gs
    from app.config import CFG
    svc = gs._service()
    q = svc.spreadsheets().values().get(spreadsheetId=CFG.sheet_id, range=f"{CFG.sched_queue_tab}!A1:AZ100000",
                                        valueRenderOption="UNFORMATTED_VALUE").execute()["values"]
    h = q[0]
    rows = []
    for i, x in enumerate(q[1:], start=2):
        x = x + [""] * (len(h) - len(x))
        d = dict(zip(h, x))
        rows.append({"row": i, "domain": d["domain"], "source": d["source"], "email_track": d["email_track"],
                     "company": d["company_name"]})
    return svc, CFG, h, rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-01", help="YYYY-MM-DD or Nd (e.g. 14d)")
    ap.add_argument("--apply", action="store_true", help="write email_track (default: dry run)")
    ap.add_argument("--include-clay", action="store_true", help="also mark companies from the Clay-table imports")
    a = ap.parse_args()
    since = parse_since(a.since)

    gm = gmail_service()
    me, n_msgs, sends = fetch_sends(gm, since)
    svc, CFG, header, rows = read_queue()
    names = {r["row"]: r["company"] for r in rows}
    p = email_sync.plan(sends, rows, include_clay=a.include_clay)

    print(f"\nGmail account: {me}   since {since}")
    print(f"Sent messages found: {n_msgs} | with a company recipient: {p.sends_used}")
    print(f"Company websites matched: {p.matched_domains} | Queue rows to write: {len(p.writes)} | already up to date: {p.already_ok}")
    print(f"Left alone (you typed a note in email_track): {len(p.kept_text)}")
    print(f"HELD BACK, Clay-table companies (use --include-clay to mark them): {len(p.clay_held_back)}")
    print(f"Recipient domains with NO Queue match: {len(p.unmatched)}")
    if p.multi_row_domains:
        print(f"Websites shared by several Queue rows (all marked): {len(p.multi_row_domains)}")
    with REPORT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bucket", "sheet_row", "company", "domain_or_note", "send_date"])
        for rn, when in p.writes: w.writerow(["WRITE", rn, names[rn], "", when])
        for rn, when in p.clay_held_back: w.writerow(["CLAY_HELD_BACK", rn, names[rn], "", when])
        for rn in p.kept_text: w.writerow(["KEPT_NOTE", rn, names[rn], "", ""])
        for d, (n, when) in sorted(p.unmatched.items(), key=lambda kv: (-kv[1][0], kv[0])): w.writerow(["NO_MATCH", "", "", d, when])
    print(f"Report: {REPORT}  (company names + dates only)")
    if not a.apply:
        print("\nDry run -- nothing written. Add --apply to write the send dates.")
        return
    if not p.writes:
        print("Nothing to write.")
        return
    from googleapiclient.errors import HttpError
    from app.queue_model import col_letter
    col = col_letter(header.index("email_track"))
    cur = {r["row"]: r["email_track"] for r in rows}
    UNDO.write_text(json.dumps([{"row": rn, "old": cur[rn], "new": when} for rn, when in p.writes]))
    data = [{"range": f"{CFG.sched_queue_tab}!{col}{rn}", "values": [[when]]} for rn, when in p.writes]
    for s in range(0, len(data), 400):
        for _try in range(6):
            try:
                svc.spreadsheets().values().batchUpdate(spreadsheetId=CFG.sheet_id, body={"valueInputOption": "RAW", "data": data[s:s + 400]}).execute()
                break
            except HttpError as e:
                if e.resp.status != 429:
                    raise
                time.sleep(30)
        else:
            sys.exit("rate limited -- stopped early; rerun (it only writes what is still missing)")
    print(f"Wrote {len(data)} send dates to email_track. Undo data: {UNDO}")


if __name__ == "__main__":
    main()
