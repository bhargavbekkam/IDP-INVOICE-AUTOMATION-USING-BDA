## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import boto3
from botocore.config import Config
from io import BytesIO
import pypdfium2


#increase the standard time out limits in boto3, because Bedrock may take a while to respond to large requests.
my_config = Config(
    connect_timeout=60*5,
    read_timeout=60*5,
)

s3_client=boto3.client("s3")
s3_resource = boto3.resource('s3')
ssm = boto3.client('ssm')
bda_client = boto3.client('bedrock-data-automation')
bda_runtime_client = boto3.client('bedrock-data-automation-runtime')

def save_image_to_s3(image, bucket, key):
    with BytesIO() as image_buffer:
        image.save(image_buffer, 'PNG')
        image_buffer.seek(0)
        s3_client.upload_fileobj(image_buffer, bucket, key)
    return bucket, key

def convert_pdf_to_png(input_bucket, input_key, output_bucket, output_key):
    pdf_obj = s3_resource.Object(input_bucket, input_key).get()['Body'].read()
    pdf = pypdfium2.PdfDocument(pdf_obj)
    n_pages = len(pdf)  # get the number of pages in the document
    print("Number of pages:", n_pages)
    page = pdf[0] # we will take first page only
    bitmap = page.render(scale=2, rotation=0)
    pil_image = bitmap.to_pil()
    save_image_to_s3(pil_image, output_bucket, output_key)
    return output_bucket, output_key

def trim_pdf_to_max_pages(pdf_bytes, max_pages):
    """Returns (trimmed_pdf_bytes, total_pages, pages_processed) with only the first max_pages pages.
    Raises pypdfium2.PdfiumError / ValueError if the file cannot be opened as a PDF (corrupt or password-protected)."""
    max_pages = max(1, int(max_pages))
    pdf = pypdfium2.PdfDocument(pdf_bytes)
    total_pages = len(pdf)
    if total_pages < 1:
        raise ValueError("PDF has no pages")
    pages_processed = min(total_pages, max_pages)
    trimmed_pdf = pypdfium2.PdfDocument.new()
    trimmed_pdf.import_pages(pdf, pages=list(range(pages_processed)))
    with BytesIO() as pdf_buffer:
        trimmed_pdf.save(pdf_buffer)
        trimmed_pdf_bytes = pdf_buffer.getvalue()
    # cost guard: never hand over more than max_pages pages
    if len(pypdfium2.PdfDocument(trimmed_pdf_bytes)) > max_pages:
        raise RuntimeError("Trimmed PDF has more pages than max_pages")
    return trimmed_pdf_bytes, total_pages, pages_processed

def stage_first_pages_of_pdf(input_bucket, input_key, output_bucket, output_key, max_pages):
    """Saves a PDF with only the first max_pages pages in the staging bucket.
    Returns the page info, or None if the file cannot be opened as a PDF."""
    pdf_obj = s3_resource.Object(input_bucket, input_key).get()['Body'].read()
    try:
        trimmed_pdf_bytes, total_pages, pages_processed = trim_pdf_to_max_pages(pdf_obj, max_pages)
    except (pypdfium2.PdfiumError, ValueError) as e:
        print(f"Could not open PDF {input_key}: {e}")
        return None
    print("Number of pages:", total_pages, "pages processed:", pages_processed)
    truncated = total_pages > pages_processed
    s3_client.put_object(
        Bucket=output_bucket,
        Key=output_key,
        Body=trimmed_pdf_bytes,
        ContentType="application/pdf",
        Metadata={
            "total-pages": str(total_pages),
            "pages-processed": str(pages_processed),
            "truncated": "true" if truncated else "false",
        },
    )
    return {"total_pages": total_pages, "pages_processed": pages_processed, "truncated": truncated}

def invoke_data_automation(input_s3_uri, output_s3_uri, blueprint_arn, data_automation_profile_arn):
    response = bda_runtime_client.invoke_data_automation_async(
                                dataAutomationProfileArn=data_automation_profile_arn,
                                inputConfiguration={
                                    's3Uri': input_s3_uri
                                },
                                outputConfiguration={
                                    's3Uri': output_s3_uri
                                },
                                notificationConfiguration={
                                    'eventBridgeConfiguration': {
                                        'eventBridgeEnabled': True
                                    }
                                },
                                blueprints=[{'blueprintArn': blueprint_arn},]
                            )
    return response

def get_parameter_from_ssm(param_name):
    response = ssm.get_parameter(
            Name=param_name,
            WithDecryption=True
        )
    param_value = response['Parameter']['Value']
    return param_value