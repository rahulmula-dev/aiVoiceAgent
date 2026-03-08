import boto3
import os
import logging
from botocore.exceptions import ClientError

logger = logging.getLogger("S3Storage")

class ResidencyViolationError(Exception):
    """Raised when data residency constraints (ca-central-1) are violated."""
    pass

class S3Storage:
    TARGET_REGION = "ca-central-1"

    def __init__(self, bucket_name: str = None):
        self.bucket = bucket_name or os.getenv("S3_AUDIT_BUCKET", "cila-audit-logs")
        
        aws_region = os.getenv("AWS_REGION", self.TARGET_REGION)
        if aws_region != self.TARGET_REGION:
            raise ResidencyViolationError(f"CRITICAL: AWS_REGION is set to {aws_region}. Only {self.TARGET_REGION} is permitted.")

        aws_kwargs = {'region_name': self.TARGET_REGION}
        
        # Security: Bypass IMDS if keys are missing to prevent blocking event loop
        if not os.getenv("AWS_ACCESS_KEY_ID"):
            aws_kwargs['aws_access_key_id'] = 'dummy_key'
            aws_kwargs['aws_secret_access_key'] = 'dummy_secret'
            logger.warning("AWS Credentials missing. Using dummy keys for non-functional initialization.")

        self.client = boto3.client('s3', **aws_kwargs)

    def upload_file(self, local_path: str, s3_key: str, delete_local: bool = True) -> bool:
        """
        Uploads a local file to S3 with 3 retries.
        Deletes local file on success if delete_local is True.
        """
        if not os.path.exists(local_path):
            logger.error(f"Upload failed: Local file {local_path} does not exist.")
            return False

        for attempt in range(1, 4):
            try:
                self.client.upload_file(local_path, self.bucket, s3_key)
                logger.info(f"S3 Upload Success: {s3_key} (Bucket: {self.bucket})")
                
                if delete_local:
                    os.remove(local_path)
                    logger.debug(f"Local file cleaned up: {local_path}")
                return True
            except ClientError as e:
                logger.warning(f"S3 Upload Attempt {attempt}/3 failed for {s3_key}: {e}")
                if attempt == 3:
                    logger.error(f"S3 Upload Hard Failure for {s3_key} after 3 attempts.")
        return False

    def upload_json(self, data: dict, s3_key: str) -> bool:
        """Uploads a dictionary as a JSON object directly to S3."""
        import json
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=s3_key,
                Body=json.dumps(data, indent=2),
                ContentType='application/json'
            )
            logger.info(f"S3 JSON Upload Success: {s3_key}")
            return True
        except ClientError as e:
            logger.error(f"S3 JSON Upload Failed for {s3_key}: {e}")
            return False
