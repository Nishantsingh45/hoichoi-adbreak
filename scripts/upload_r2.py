"""Upload the sample episodes in data/videos to a Cloudflare R2 bucket (S3-compatible API).

    pip install boto3
    python scripts/upload_r2.py            # needs R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET in .env
"""
import json
import os
import sys
from pathlib import Path

import boto3
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    region_name="auto",
)
bucket = os.environ["R2_BUCKET"]
samples = json.loads((ROOT / "samples.json").read_text(encoding="utf-8"))["videos"]

existing = {o["Key"]: o["Size"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", [])}
for vid in samples:
    f = ROOT / "data" / "videos" / f"{vid}.mp4"
    key = f"{vid}.mp4"
    if not f.exists():
        print(f"skip {key}: not in data/videos (run scripts/fetch_samples.py first)", file=sys.stderr)
        continue
    if existing.get(key) == f.stat().st_size:
        print(f"skip {key}: already uploaded")
        continue
    print(f"uploading {key} ({f.stat().st_size / 1e6:.0f} MB)...", flush=True)
    s3.upload_file(str(f), bucket, key, ExtraArgs={"ContentType": "video/mp4"})
print("done")
