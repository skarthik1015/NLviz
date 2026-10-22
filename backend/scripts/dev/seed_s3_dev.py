"""Seed the dev S3-compatible store (SeaweedFS on 127.0.0.1:19000) for the S3 import tests.

Buckets: nlviz-private (owner credentials only) and nlviz-public (anonymous read, set in the
SeaweedFS identity config). Run once after starting the container (see the design doc, section 13).
"""
import boto3

ENDPOINT = "http://127.0.0.1:19000"
OWNER = {"aws_access_key_id": "nlvizminio", "aws_secret_access_key": "nlviz-minio-dev-pw"}

s3 = boto3.client("s3", endpoint_url=ENDPOINT, region_name="us-east-1", **OWNER)
for bucket in ("nlviz-private", "nlviz-public"):
    try:
        s3.create_bucket(Bucket=bucket)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
s3.upload_file("data/parquet/olist/sellers.parquet", "nlviz-private", "exports/sellers.parquet")
s3.put_object(Bucket="nlviz-private", Key="exports/sales.csv", Body=b"Region,Amount\nnorth,10\nsouth,5\nnorth,1\n")
s3.upload_file("data/parquet/olist/category_translation.parquet", "nlviz-public", "category_translation.parquet")
print(sorted(o["Key"] for o in s3.list_objects_v2(Bucket="nlviz-private")["Contents"]))
