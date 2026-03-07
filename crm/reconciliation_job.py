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
        logger.info("Initializing S3 Client in Region: ca-central-1")
        self.s3 = boto3.client('s3', region_name='ca-central-1')
        self.bucket = "crm-failover-queue" 
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
        logger.info("Starting CRM DLQ Reconciliation Check...")
            # 1. Migrate Local Storage entries FIRST
            await self.sync_local_to_s3()

            # 2. Pull objects from S3
            response = self.s3.list_objects_v2(Bucket=self.bucket, Prefix="dlq_tickets/")
            
            # Count both S3 and local files for alerting
            local_count = len(os.listdir(self.dlq_dir)) if os.path.exists(self.dlq_dir) else 0
            s3_count = len(response.get('Contents', []))
            total_unsynced = local_count + s3_count
            
            # 3. Check the 50-entry threshold (Pillar 2: Reliability)
            self.check_alert_threshold(total_unsynced)

            if s3_count == 0:
                return
            
            objects = response['Contents']

            now = datetime.datetime.now()

            # 3. Process each ticket
            for obj in objects:
                key = obj['Key']
                
                # Fetch ticket
                obj_data = self.s3.get_object(Bucket=self.bucket, Key=key)
                ticket_entry = json.loads(obj_data['Body'].read().decode('utf-8'))
                
                # Expiration calculation (Max Window: 24 Hours)
                ticket_time = datetime.datetime.fromisoformat(ticket_entry['created_at'])
                age_hours = (now - ticket_time).total_seconds() / 3600
                
                if age_hours > self.max_age_hours:
                    logger.error(f"Ticket {key} has exceeded max 24-hour window ({age_hours:.1f} hours old). Moving to 'expired_tickets' for manual review.")
                    
                    # Instead of deleting forever, move to a permanent dead-letter folder
                    expired_key = key.replace("dlq_tickets/", "expired_tickets/")
                    self.s3.copy_object(
                        Bucket=self.bucket,
                        CopySource={'Bucket': self.bucket, 'Key': key},
                        Key=expired_key
                    )
                    self.s3.delete_object(Bucket=self.bucket, Key=key)
                    continue
                
                # Exponential Backoff logic (1m, 2m, 4m, 8m, etc.)
                retry_count = ticket_entry.get("retry_count", 0)
                backoff_seconds = (2 ** retry_count) * 60
                seconds_since_last = (now - ticket_time).total_seconds()
                
                if seconds_since_last < backoff_seconds and retry_count > 0:
                    logger.debug(f"Ticket {key} in backoff period (waiting {backoff_seconds - seconds_since_last:.0f}s more). Skipping.")
                    continue
                
                logger.info(f"Retrying ticket sync from S3: {key} (Attempt {retry_count + 1})")
                
                # Attempt recovery sync (Durable Idempotency)
                try:
                    payload = ticket_entry['payload']
                    
                    # Using the DLQ Entry UUID as the PERMANENT idempotency key.
                    # This guarantees that even if this cycle is interrupted after the CRM 
                    # received it, the subsequent attempt will be a no-op.
                    id_key = ticket_entry['ticket_id'] 
                    
                    ticket_id = await self.crm_client._send_request_safe(
                        endpoint="tickets", 
                        data=payload,
                        idempotency_key=id_key
                    )
                    logger.info(f"✅ Successfully synced recovered ticket! New CRM ID: {ticket_id}")
                    
                    # Delete from S3 on success
                    self.s3.delete_object(Bucket=self.bucket, Key=key)
                    
                except Exception as e:
                    logger.warning(f"Retry failed for {key}: {e}")
                    # Update backoff counters and push back to S3
                    ticket_entry["retry_count"] = retry_count + 1
                    ticket_entry["created_at"] = datetime.datetime.now().isoformat()
                    self.s3.put_object(
                        Bucket=self.bucket,
                        Key=key,
                        Body=json.dumps(ticket_entry)
                    )
                    
        except Exception as e:
            logger.error(f"Reconciliation job cycle failed to query AWS S3: {e}")

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
