import os
import time
import json
import logging
import datetime
import boto3
import asyncio
from crm.client import CRMClient

# Configure Logger for the background process
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("CRM-Reconciler")

class CRMReconciler:
    """
    Background job that reads failed CRM tickets from the AWS S3 (ca-central-1)
    failover queue, enforces exponential backoff retries, expires tickets > 24 hours,
    and alerts Admin/IT if >50 tickets are backed up, as per PRD Section 5.
    """
    def __init__(self):
        # Using exact region required by PRD
        # AWS Bypass: S3 failover requires credentials. If missing, use dummy keys to prevent hanging.
        region = os.getenv("AWS_REGION", "ca-central-1")
        aws_kwargs = {'region_name': region}
        if not os.getenv("AWS_ACCESS_KEY_ID"):
            aws_kwargs['aws_access_key_id'] = 'dummy_key'
            aws_kwargs['aws_secret_access_key'] = 'dummy_secret'
            logger.warning("AWS Credentials missing. Background worker operating in LOCAL-ONLY mode.")

        self.s3 = boto3.client('s3', **aws_kwargs)
        self.is_local_only = not os.getenv("AWS_ACCESS_KEY_ID")
        self.bucket = os.getenv("S3_AUDIT_BUCKET", "crm-failover-queue")
        self.crm_client = CRMClient()
        self.max_age_hours = 24
        self.dlq_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "crm_dlq")

    async def sync_local_to_s3(self):
        """Sweeps local /logs/crm_dlq storage for anything that failed initial upload."""
        if not os.path.exists(self.dlq_dir):
            return
            
        files = [f for f in os.listdir(self.dlq_dir) if f.endswith(".json")]
        if not files:
            return
            
        logger.info(f"Found {len(files)} unsynced tickets on local storage. Attempting S3 migration...")
        
        for filename in files:
            path = os.path.join(self.dlq_dir, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                
                # Check for "Pending CRM Sync" status marker
                if entry.get("status") != "Pending CRM Sync":
                    entry["status"] = "Pending CRM Sync"
                
                # Attempt S3 upload
                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=f"dlq_tickets/{filename}",
                    Body=json.dumps(entry)
                )
                logger.info(f"Successfully migrated {filename} to S3.")
                os.remove(path)
            except Exception as e:
                logger.error(f"Migration of {filename} failed: {e}")

    def check_alert_threshold(self, unsynced_count):
        """PRD Requirement: If >50 unsynced entries: alert Admin/IT."""
        if unsynced_count > 50:
            logger.critical(f"🚨 ADMIN/IT ALERT 🚨: Severe CRM outage detected! {unsynced_count} pending CRM tickets in S3 failover queue.")
            self._fire_external_alert(unsynced_count)

    def _fire_external_alert(self, count):
        """
        Integration point for Slack/PagerDuty/Email.
        PRD: Needs to wake someone up.
        """
        # TODO: Implement actual Slack/PagerDuty webhook call here
        pass

    async def run_sync_cycle(self):
        # 0. Check Configuration (CRITICAL: Stop log spam in Dev)
        if not self.crm_client.is_configured:
            logger.debug("CRM Reconciler: Skipping sync cycle (CRM_API_KEY is using default/test value).")
            return

        logger.info("Starting CRM DLQ Reconciliation Check...")
        try:
            # 1. Migrate Local Storage entries FIRST (Only if NOT local-only)
            if not self.is_local_only:
                await self.sync_local_to_s3()

            # 2. Pull objects from S3
            s3_count = 0
            objects = []
            if not self.is_local_only:
                try:
                    response = self.s3.list_objects_v2(Bucket=self.bucket, Prefix="dlq_tickets/")
                    if 'Contents' in response:
                        objects = response['Contents']
                        s3_count = len(objects)
                except Exception as s3_err:
                    logger.warning(f"Failed to list S3 objects: {s3_err}. Proceeding with local-only check.")

            # Count local files too
            local_files = [f for f in os.listdir(self.dlq_dir) if f.endswith(".json")] if os.path.exists(self.dlq_dir) else []
            local_count = len(local_files)
            total_unsynced = local_count + s3_count
            
            # 3. Check the 50-entry threshold (Pillar 2: Reliability)
            self.check_alert_threshold(total_unsynced)

            # 4. Handle Local leftovers (if S3 sync failed or they were just added)
            for filename in local_files:
                path = os.path.join(self.dlq_dir, filename)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        entry = json.load(f)
                    success = await self._process_entry(entry, path, is_s3=False)
                    if success:
                        os.remove(path)
                except Exception as e:
                    logger.error(f"Failed to process local DLQ entry {filename}: {e}")

            if s3_count == 0:
                return

            now = datetime.datetime.now()

            # 5. Process each S3 ticket
            for obj in objects:
                key = obj['Key']
                try:
                    # Fetch ticket
                    obj_data = self.s3.get_object(Bucket=self.bucket, Key=key)
                    ticket_entry = json.loads(obj_data['Body'].read().decode('utf-8'))
                    
                    # Expiration calculation (Max Window: 24 Hours)
                    ticket_time = datetime.datetime.fromisoformat(ticket_entry['created_at'])
                    age_hours = (now - ticket_time).total_seconds() / 3600
                    
                    if age_hours > self.max_age_hours:
                        logger.error(f"Ticket {key} has exceeded max 24-hour window ({age_hours:.1f} hours old). Moving to 'expired_tickets'.")
                        expired_key = key.replace("dlq_tickets/", "expired_tickets/")
                        self.s3.copy_object(
                            Bucket=self.bucket,
                            CopySource={'Bucket': self.bucket, 'Key': key},
                            Key=expired_key
                        )
                        self.s3.delete_object(Bucket=self.bucket, Key=key)
                        continue
                    
                    # Exponential Backoff logic
                    retry_count = ticket_entry.get("retry_count", 0)
                    backoff_seconds = min((2 ** retry_count) * 60, 3600) # Cap at 1 hour
                    seconds_since_last = (now - ticket_time).total_seconds()
                    
                    if seconds_since_last < backoff_seconds and retry_count > 0:
                        continue
                    
                    success = await self._process_entry(ticket_entry, key, is_s3=True)
                    if success:
                        self.s3.delete_object(Bucket=self.bucket, Key=key)
                    else:
                        # Update backoff and put back
                        ticket_entry["retry_count"] = retry_count + 1
                        ticket_entry["created_at"] = datetime.datetime.now().isoformat()
                        self.s3.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(ticket_entry))
                except Exception as e:
                    logger.error(f"Failed to process S3 entry {key}: {e}")
        except Exception as e:
            logger.error(f"Reconciliation job cycle failed: {e}")

    async def _process_entry(self, entry, identifier, is_s3=True):
        """Helper to process a single DLQ entry (S3 or Local)."""
        try:
            payload = entry['payload']
            id_key = entry['ticket_id']
            
            logger.info(f"Retrying ticket sync for {id_key} ({'S3' if is_s3 else 'Local'})")
            
            ticket_id = await self.crm_client._send_request_safe(
                endpoint="tickets", 
                data=payload,
                idempotency_key=id_key
            )
            logger.info(f"✅ Successfully synced recovered ticket! CRM ID: {ticket_id}")
            return True
        except Exception as e:
            logger.warning(f"Retry failed for {identifier}: {e}")
            return False

def start_background_worker():
    """Starts the reconciler as a separate asyncio task within the FastAPI loop."""
    async def worker_loop():
        reconciler = CRMReconciler()
        logger.info("CRM Background Reconciler Thread STARTED (5m interval).")
        while True:
            try:
                await reconciler.run_sync_cycle()
            except Exception as e:
                logger.error(f"CRM Reconciler Cycle Crashed: {e}")
            await asyncio.sleep(300) # Every 5 minutes
            
    # Schedule it
    asyncio.create_task(worker_loop())

if __name__ == "__main__":
    reconciler = CRMReconciler()
    logger.info("Starting standalone CRM Reconciler Job. Press CTRL+C to cleanly exit.")
    
    # Run the worker loop independently
    try:
        while True:
            asyncio.run(reconciler.run_sync_cycle())
            logger.info("Cycle complete. Sleeping for 5 minutes...")
            time.sleep(300) # Run every 5 minutes
    except KeyboardInterrupt:
        logger.info("Standalone CRM Reconciler Job terminated gracefully.")
