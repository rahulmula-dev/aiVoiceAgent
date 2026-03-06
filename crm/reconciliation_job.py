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
        
    def check_alert_threshold(self, unsynced_count):
        """PRD Requirement: If >50 unsynced entries: alert Admin/IT."""
        if unsynced_count > 50:
            logger.critical(f"🚨 ADMIN/IT ALERT 🚨: Severe CRM outage detected! {unsynced_count} pending CRM tickets in S3 failover queue.")
            # Real-world: Insert SendGrid/Slack API call here

    async def run_sync_cycle(self):
        logger.info("Starting CRM DLQ Reconciliation Check...")
        try:
            # 1. Pull objects from S3
            response = self.s3.list_objects_v2(Bucket=self.bucket, Prefix="dlq_tickets/")
            if 'Contents' not in response:
                logger.info("Queue is clear. No pending tickets found in S3 DLQ.")
                return

            objects = response['Contents']
            unsynced_count = len(objects)
            
            # 2. Check the 50-entry threshold
            self.check_alert_threshold(unsynced_count)

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
                
                # Attempt recovery sync
                try:
                    payload = ticket_entry['payload']
                    # 4. IDEMPOTENCY INJECTION: Pass the original unique DLQ UUID directly to CRM
                    payload['ticket_id'] = ticket_entry['ticket_id']
                    
                    # Using the safe internal method of CRM client to prevent circular DLQ loops
                    ticket_id = await self.crm_client._send_request_safe("tickets", payload)
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
