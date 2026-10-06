## POC: parses a raw .eml object and uploads each PDF attachment to the input bucket
## under invoices/, where the existing pipeline picks it up.
import os
import re
import email
from email import policy
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

INVOICES_PREFIX = "invoices"
MAX_FILENAME_STEM_LENGTH = 80
DEFAULT_FILENAME_STEM = "attachment"
RAW_EMAIL_KEY_PATTERN = re.compile(r"^emails/(\d+)/(\d+)\.eml$")

s3_client = boto3.client("s3")


######################### Pure functions (no AWS) #########################
def parse_raw_email_key(raw_email_key):
    """emails/<uidvalidity>/<uid>.eml -> (uidvalidity, uid), or None if the key has another shape."""
    match = RAW_EMAIL_KEY_PATTERN.match(raw_email_key)
    if not match:
        return None
    return match.group(1), match.group(2)

def sanitize_filename_stem(filename):
    """Original attachment filename -> safe key fragment, without the extension."""
    stem = (filename or "").strip()
    if stem.lower().endswith(".pdf"):
        stem = stem[:-len(".pdf")]
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    stem = re.sub(r"_+", "_", stem)
    stem = stem[:MAX_FILENAME_STEM_LENGTH].strip("._-")
    return stem or DEFAULT_FILENAME_STEM

def build_pdf_key(uidvalidity, uid, index, filename):
    """Unique per email + attachment, always under invoices/ and always lowercase .pdf."""
    return f"{INVOICES_PREFIX}/{uidvalidity}-{uid}-{index:02d}-{sanitize_filename_stem(filename)}.pdf"

def to_ascii_metadata(value, max_length=200):
    """S3 user metadata must be ASCII."""
    value = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    return value.encode("ascii", "replace").decode("ascii")[:max_length]

def is_pdf_part(content_type, filename, payload):
    declared_pdf = content_type == "application/pdf" or (filename or "").lower().endswith(".pdf")
    if not declared_pdf or not payload:
        return False
    return payload[:1024].lstrip().startswith(b"%PDF")

def extract_pdf_attachments(raw_bytes):
    """Returns (sender, subject, message_id, [{"index", "filename", "content"}]) for a raw email."""
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    sender = str(msg["From"] or "")
    subject = str(msg["Subject"] or "")
    message_id = str(msg["Message-ID"] or "")

    attachments = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if is_pdf_part(part.get_content_type(), filename, payload):
            attachments.append({
                "index": len(attachments) + 1,
                "filename": filename or "",
                "content": payload,
            })
    return sender, subject, message_id, attachments


######################### S3 #########################
def save_pdf(bucket, key, content, metadata):
    """Writes only if the key does not exist. Returns False if it already exists."""
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content,
            ContentType="application/pdf",
            Metadata=metadata,
            IfNoneMatch="*",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "PreconditionFailed":
            return False
        raise


def lambda_handler(event, context):
    input_bucket = os.environ["INPUT_BUCKET"]
    uploaded = []

    for record in event["Records"]:
        raw_email_bucket = record["s3"]["bucket"]["name"]
        raw_email_key = unquote_plus(record["s3"]["object"]["key"])

        parsed_key = parse_raw_email_key(raw_email_key)
        if parsed_key is None:
            print(f"Unexpected raw email key, skipping: {raw_email_key}")
            continue
        uidvalidity, uid = parsed_key

        raw_bytes = s3_client.get_object(Bucket=raw_email_bucket, Key=raw_email_key)["Body"].read()
        sender, subject, message_id, attachments = extract_pdf_attachments(raw_bytes)

        if not attachments:
            print(f"sender: {sender} | subject: {subject} | raw email key: {raw_email_key} | no PDF attachment, skipping")
            continue

        for attachment in attachments:
            pdf_key = build_pdf_key(uidvalidity, uid, attachment["index"], attachment["filename"])
            metadata = {
                "source-email-bucket": raw_email_bucket,
                "source-email-key": raw_email_key,
                "attachment-index": str(attachment["index"]),
                "original-filename": to_ascii_metadata(attachment["filename"]),
                "message-id": to_ascii_metadata(message_id),
            }
            if save_pdf(input_bucket, pdf_key, attachment["content"], metadata):
                print(f"Uploaded s3://{input_bucket}/{pdf_key} from raw email key: {raw_email_key}")
                uploaded.append(pdf_key)
            else:
                print(f"s3://{input_bucket}/{pdf_key} already exists, skipping (raw email key: {raw_email_key})")

    return {"statusCode": 200, "body": {"uploaded": uploaded}}
