## POC: polls a mailbox over IMAP and saves marker emails as raw .eml objects in S3.
## The mailbox is opened read-only and is never modified (no flags are set).
import os
import imaplib
import email
from email import policy
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

###############################################################################
## POC ONLY: hardcoded mailbox credentials. Replace the placeholders before
## deploying. Never print or log IMAP_PASSWORD. Do not commit real values.
###############################################################################
IMAP_USER = "REPLACE_WITH_IMAP_USER"
IMAP_PASSWORD = "REPLACE_WITH_IMAP_PASSWORD"
PLACEHOLDER_PREFIX = "REPLACE_WITH"

IMAP_HOST = "imap.hostinger.com"
IMAP_PORT = 993
IMAP_MAILBOX = "INBOX"
IMAP_TIMEOUT_SECONDS = 30

SUBJECT_MARKER = "KONARIS-IDP-POC"   # only emails whose Subject contains this are processed
LOOKBACK_DAYS = 2                    # search window, regardless of read state
MAX_MESSAGES_PER_RUN = 10            # max emails saved per invocation
RAW_EMAIL_PREFIX = "emails"

IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

s3_client = boto3.client("s3")


######################### Pure functions (no IMAP / AWS) #########################
def credentials_configured(user=None, password=None):
    user = IMAP_USER if user is None else user
    password = IMAP_PASSWORD if password is None else password
    if not user or not password:
        return False
    return not (user.startswith(PLACEHOLDER_PREFIX) or password.startswith(PLACEHOLDER_PREFIX))

def decode_subject(header_bytes):
    """Returns the decoded (RFC 2047) and unfolded Subject from raw header bytes."""
    if not header_bytes:
        return ""
    try:
        msg = email.message_from_bytes(header_bytes, policy=policy.default)
        subject = msg["Subject"]
        return str(subject) if subject is not None else ""
    except Exception:
        return ""

def subject_matches(subject, marker=SUBJECT_MARKER):
    """True if the decoded subject contains the marker anywhere (case-insensitive)."""
    if not subject:
        return False
    return marker.lower() in str(subject).lower()

def imap_since_date(now, lookback_days=LOOKBACK_DAYS):
    """IMAP date (e.g. 01-Oct-2026) for the SINCE criterion. Built by hand to avoid locale issues."""
    since = now - timedelta(days=lookback_days)
    return f"{since.day:02d}-{IMAP_MONTHS[since.month - 1]}-{since.year}"

def build_raw_email_key(uidvalidity, uid):
    return f"{RAW_EMAIL_PREFIX}/{uidvalidity}/{uid}.eml"

def uid_from_raw_email_key(key):
    """emails/<uidvalidity>/<uid>.eml -> uid as int, or None if the key has another shape."""
    name = key.split("/")[-1]
    if not name.endswith(".eml"):
        return None
    uid = name[:-len(".eml")]
    return int(uid) if uid.isdigit() else None

def first_literal(fetch_data):
    """Returns the literal bytes of the first item in an imaplib FETCH response."""
    for item in fetch_data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[1]
    return None


######################### S3 #########################
def list_existing_uids(bucket, uidvalidity):
    """One paginated LIST of emails/<uidvalidity>/ instead of one HEAD per candidate."""
    existing = set()
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{RAW_EMAIL_PREFIX}/{uidvalidity}/"):
        for obj in page.get("Contents", []):
            uid = uid_from_raw_email_key(obj["Key"])
            if uid is not None:
                existing.add(uid)
    return existing

def save_raw_email(bucket, key, raw_bytes):
    """Writes only if the key does not exist. Returns False if it already exists."""
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=raw_bytes,
            ContentType="message/rfc822",
            IfNoneMatch="*",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "PreconditionFailed":
            return False
        raise


######################### IMAP (read-only) #########################
def connect_read_only():
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT_SECONDS)
    try:
        imap.login(IMAP_USER, IMAP_PASSWORD)
    except Exception:
        # do not chain the original exception, keep credentials out of the logs
        raise RuntimeError("IMAP login failed. Check IMAP_USER / IMAP_PASSWORD.") from None
    # readonly=True sends EXAMINE: the server will not change any flag in this session
    status, _ = imap.select(IMAP_MAILBOX, readonly=True)
    if status != "OK":
        raise RuntimeError(f"Could not open mailbox {IMAP_MAILBOX} read-only")
    return imap

def get_uidvalidity(imap):
    _, data = imap.response("UIDVALIDITY")
    if not data or data[0] is None:
        raise RuntimeError("IMAP server did not return UIDVALIDITY")
    return data[0].decode().strip()

def search_candidate_uids(imap, now):
    """Server-side narrowing only. Every candidate is confirmed in code afterwards."""
    status, data = imap.uid("SEARCH", "SINCE", imap_since_date(now), "SUBJECT", f'"{SUBJECT_MARKER}"')
    if status != "OK":
        raise RuntimeError("IMAP search failed")
    uids = data[0].split() if data and data[0] else []
    return sorted(int(uid) for uid in uids)

def fetch_subject(imap, uid):
    status, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
    if status != "OK":
        raise RuntimeError(f"IMAP header fetch failed for uid {uid}")
    return decode_subject(first_literal(data))

def fetch_raw_email(imap, uid):
    status, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
    raw_bytes = first_literal(data)
    if status != "OK" or not raw_bytes:
        raise RuntimeError(f"IMAP body fetch failed for uid {uid}")
    return raw_bytes


def lambda_handler(event, context):
    summary = {"candidates": 0, "already_saved": 0, "not_matching": 0, "saved": 0, "failed": 0}

    if not credentials_configured():
        print("credentials not configured: IMAP_USER / IMAP_PASSWORD still hold placeholders, exiting without connecting")
        return {"statusCode": 200, "body": "credentials not configured"}

    raw_email_bucket = os.environ["RAW_EMAIL_BUCKET"]
    now = datetime.now(timezone.utc)

    imap = connect_read_only()
    try:
        uidvalidity = get_uidvalidity(imap)
        candidate_uids = search_candidate_uids(imap, now)
        summary["candidates"] = len(candidate_uids)

        existing_uids = list_existing_uids(raw_email_bucket, uidvalidity) if candidate_uids else set()
        new_uids = [uid for uid in candidate_uids if uid not in existing_uids]
        summary["already_saved"] = len(candidate_uids) - len(new_uids)

        for uid in new_uids:
            if summary["saved"] >= MAX_MESSAGES_PER_RUN:
                print(f"Reached the limit of {MAX_MESSAGES_PER_RUN} emails per run, the rest will be picked up next run")
                break
            key = build_raw_email_key(uidvalidity, uid)
            try:
                # headers only: non-matching emails are never downloaded
                if not subject_matches(fetch_subject(imap, uid)):
                    print(f"uid {uid}: subject does not contain {SUBJECT_MARKER}, skipping")
                    summary["not_matching"] += 1
                    continue
                raw_bytes = fetch_raw_email(imap, uid)
                if save_raw_email(raw_email_bucket, key, raw_bytes):
                    print(f"uid {uid}: saved s3://{raw_email_bucket}/{key} ({len(raw_bytes)} bytes)")
                    summary["saved"] += 1
                else:
                    print(f"uid {uid}: s3://{raw_email_bucket}/{key} already exists, skipping")
                    summary["already_saved"] += 1
            except Exception as e:
                # nothing was stored, so the next run retries this email
                print(f"uid {uid}: failed, will retry next run: {type(e).__name__}: {e}")
                summary["failed"] += 1
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    print(f"poll_mailbox summary: {summary}")
    return {"statusCode": 200, "body": summary}
