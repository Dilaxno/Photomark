import os
import json
from typing import Optional
from app.core.config import s3, R2_BUCKET, R2_PUBLIC_BASE_URL, STATIC_DIR, logger
from botocore.exceptions import ClientError


def write_json_key(key: str, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    if s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
        bucket.put_object(Key=key, Body=data.encode('utf-8'), ContentType='application/json', ACL='private')
    else:
        path = os.path.join(STATIC_DIR, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(data)


def read_json_key(key: str) -> Optional[dict]:
    try:
        if s3 and R2_BUCKET:
            obj = s3.Object(R2_BUCKET, key)
            try:
                body = obj.get()["Body"].read().decode("utf-8")
            except ClientError as ce:
                # Treat missing object as None without warning noise
                if ce.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                    return None
                raise
            return json.loads(body)
        else:
            path = os.path.join(STATIC_DIR, key)
            if not os.path.isfile(path):
                return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as ex:
        logger.warning(f"read_json_key failed for {key}: {ex}")
        return None


def upload_bytes(key: str, data: bytes, content_type: str = "image/jpeg") -> str:
    if not s3 or not R2_BUCKET:
        local_path = os.path.join(STATIC_DIR, key)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        logger.info(f"Saved locally: {local_path}")
        return f"/static/{key}"

    bucket = s3.Bucket(R2_BUCKET)
    bucket.put_object(Key=key, Body=data, ContentType=content_type, ACL="public-read")

    if R2_PUBLIC_BASE_URL:
        return f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"

    client = s3.meta.client
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": key},
        ExpiresIn=60 * 60 * 24 * 7,
    )