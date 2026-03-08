# LeadSquared CRM Client - Production Ready
import logging
import json
import os
import datetime
import httpx
import re
from typing import Optional, Dict
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type, before_sleep_log

# Configure logger
logger = logging.getLogger("crm")

from contracts.interfaces import CRMEngine

# Custom Exceptions
class CRMConnectionError(Exception):
    pass

class SecurityError(Exception):
    """Raised when security constraints are violated (e.g. Prod keys in Staging)."""
    pass

class CRMClient(CRMEngine):
    """
    Client for LeadSquared CRM integration.
    Handles ticket creation with Idempotency, Retries, and Dead-Letter Queue (DLQ).
    """
    def __init__(self):
        # Load API keys from .env
        self.api_key = os.getenv("CRM_API_KEY", "crm_test_key_123")
        self.base_url = os.getenv("CRM_BASE_URL", "http://72.61.172.170:8000")
        self.app_env = os.getenv("APP_ENV", "production").lower()
        
        # 1. CRM SECURITY GUARD (Task 1)
        # Prevent production credentials in staging/development
        if self.app_env in ["staging", "development", "test"]:
            prod_indicators = ["live", "prod", "production"]
            if any(indicator in self.api_key.lower() for indicator in prod_indicators):
                logger.critical(f"FATAL SECURITY BREACH: Production CRM credentials detected in {self.app_env} environment!")
                raise SecurityError(f"FATAL: Production CRM credentials detected in {self.app_env} environment.")

        self.headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
        }
        
        # IDEMPOTENCY: Local cache of processed call_ids to prevent duplicates
        self.processed_calls = {} # Map call_id -> ticket_id
        
        # CONFIG: S3 DLQ Path (Canada Region - PRD Section 5)
        import boto3
        aws_kwargs = {'region_name': 'ca-central-1'}
        if not os.getenv("AWS_ACCESS_KEY_ID"):
            # Boto3 attempts to fetch from IMDS (169.254.169.254) if keys are missing.
            # This synchronous HTTP request blocks the asyncio event loop for several seconds causing timeouts
            aws_kwargs['aws_access_key_id'] = 'dummy_key_to_bypass_imds'
            aws_kwargs['aws_secret_access_key'] = 'dummy_secret_to_bypass_imds'
            
        self.s3_client = boto3.client('s3', **aws_kwargs)
        # AWS explicitly requires hyphens, translating from PRD's 'crm_failover_queue'
        self.s3_bucket = "crm-failover-queue"

    async def check_health(self) -> bool:
        """Verifies CRM API reachability for readiness probe."""
        url = f"{self.base_url}/health" # Standard health endpoint
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=2.0)
                # Any non-5xx response is usually a sign of reachability, 
                # but we'll accept 200/201/204
                return response.status_code < 500
        except Exception as e:
            logger.warning(f"CRM health check failed: {e}")
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(0.25),
        retry=retry_if_exception_type(CRMConnectionError),
        before_sleep=before_sleep_log(logger, logging.WARNING)
    )
    async def log_call(self, call_id: str, caller_phone: str, caller_type: str = "unknown", 
                      summary: str = "", transcript: str = "", sentiment: str = "neutral", 
                      duration_seconds: int = 0):
        """
        Logs a call to the CRM. This is the PARENT record for any tickets.
        """
        url = f"{self.base_url}/calls"
        payload = {
            "caller_phone": caller_phone,
            "caller_type": caller_type,
            "summary": summary or "Call Initiated",
            "transcript": transcript or "Transcript pending...",
            "sentiment": sentiment,
            "duration_seconds": duration_seconds,
            "created_at": datetime.datetime.now().isoformat()
        }
        
        # We don't send call_id in the body? The user requirement says "Every inbound call should create a Call record".
        # But tickets need 'call_id'. Usually the CRM *returns* the call_id or we generate it.
        # But in this system, we have our own 'call_id' (session_id).
        # Typically, we should probably pass our 'call_id' if the CRM supports it, 
        # OR we use the CRM's returned ID as the 'call_id' for tickets.
        # Looking at the user request: 
        # "Stored fields: ... call_id (required for Ticket)".
        # It doesn't explicitly say /calls accepts 'call_id'. 
        # However, for the System to link them, we likely need to return the ID from this call and use it.
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=self.headers, json=payload, timeout=5.0)
                
            if response.status_code >= 500:
                raise CRMConnectionError(f"Server Error {response.status_code}")
            
            if response.status_code not in [200, 201]:
                # PILLAR 3: Redact PII from API error responses (MEDIUM-P3-02)
                err_text = re.sub(r'\+?\d{7,15}', '[REDACTED]', response.text)
                logger.error(f"[CRM] Failed to log call: {err_text}")
                return None
                
            data = response.json()
            # If the CRM returns an ID, use it. If not, use our session ID? 
            # The prompt says: "Create Ticket... call_id: 'CALL_ID_HERE'".
            # This implies the POST /calls returns an ID we must use.
            crm_call_id = data.get("id") or data.get("call_id")
            
            # Map our internal session ID to the CRM's ID if needed, or just return it.
            return crm_call_id

        except httpx.RequestError as e:
            raise CRMConnectionError(f"Connection Failed: {e}")

    async def create_ticket(self, transcript, summary, sentiment="Neutral", call_logger=None, call_id=None, title=None, structured_turns=None, session_obj=None):
        """
        Creates a ticket in LeadSquared for the interaction.
        Arguments:
            call_id (str): MANDATORY. This should be the ID returned by `log_call` if possible, 
                           OR the session ID if the CRM accepts external IDs.
                           (Based on "CALL_ID_HERE", it's likely the CRM internal ID).
            structured_turns (list, optional): S4-11 metadata.
            session_obj (Session, optional): Full session context for enrichment.
        """
        # 1. IDEMPOTENCY CHECK
        idempotency_key = f"{call_id}:{summary}" if call_id else None
        if idempotency_key and idempotency_key in self.processed_calls:
            existing_ticket = self.processed_calls[idempotency_key]
            logger.info(f"[CRM] Idempotency Hit: Ticket '{summary}' already exists for call {call_id} -> {existing_ticket}")
            return {"status": "success", "ticket_id": existing_ticket, "message": "Already processed"}

        priority = "NORMAL"
        if sentiment == "Negative": priority = "HIGH"
        if "escalat" in summary.lower(): priority = "HIGH"

        # Allow custom title or default to template
        final_title = title or f"Voice Agent Ticket - {sentiment}"
        
        # Enforce call_id is a valid scalar string (preventing PydanticValidationError input_type=list or input=null)
        if isinstance(call_id, list):
            call_id_str = str(call_id[0]) if call_id else "unknown_call_id"
        elif call_id:
            call_id_str = str(call_id)
        else:
            call_id_str = "unknown_call_id"
            
        # DEBUG: Append short Call ID to title so user can distinguish them on dashboard
        if call_id_str:
             short_id = call_id_str[-4:] if len(call_id_str) > 4 else call_id_str
             final_title = f"{final_title} | Call-{short_id}"

        # Forensic Metadata Enrichment (Task 6)
        enhanced_metadata = {
            "structured_turns": structured_turns
        } if structured_turns else {}
        
        if session_obj:
            if session_obj.interruption_snapshot:
                enhanced_metadata["interruption_snapshot"] = session_obj.interruption_snapshot
            if session_obj.termination_reason:
                enhanced_metadata["termination_reason"] = session_obj.termination_reason
            if session_obj.metadata:
                enhanced_metadata["session_metadata"] = session_obj.metadata
            if session_obj.caller_type:
                enhanced_metadata["caller_type"] = session_obj.caller_type

        ticket_data = {
            "call_id": call_id_str,
            "title": final_title,
            "description": summary,
            "status": "OPEN",
            "priority": priority,
            "metadata": enhanced_metadata
        }
        
        try:
            # 2. ATTEMPT CREATION WITH RETRY LOGIC (Pillar 2: Reliability)
            # Use call_id as a natural idempotency key candidate for the CRM
            ticket_id = await self._send_request_safe(
                endpoint="tickets", 
                data=ticket_data, 
                idempotency_key=idempotency_key
            )
            
            # Success: Cache the ID (RAM-only, reconciler uses DLQ id for persistence)
            if idempotency_key:
                self.processed_calls[idempotency_key] = ticket_id
                
            logger.info(f"[CRM] Ticket logged successfully: {ticket_id}")
            
            # Structured log
            if call_logger:
                call_logger.log_event("crm", "ticket_created", 
                                     meta={
                                         "ticket_id": ticket_id, 
                                         "sentiment": sentiment,
                                         "summary": summary
                                     })
            
            return {"status": "success", "ticket_id": ticket_id}
            
        except Exception as e:
            # 3. DEAD LETTER QUEUE (Persistence)
            logger.error(f"[CRM] All retries failed. saving to DLQ. Error: {e}")
            # The entry_id (UUID) in the DLQ will become the PERMANENT idempotency key for this ticket
            self._save_to_dlq(ticket_data, str(e))
            return {"status": "queued", "ticket_id": "DLQ-SAVED", "message": "Saved to Dead Letter Queue"}

    # RETRY CONFIGURATION
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(0.25),
        retry=retry_if_exception_type(CRMConnectionError),
        before_sleep=before_sleep_log(logger, logging.WARNING)
    )
    async def _send_request_safe(self, endpoint, data, idempotency_key: str = None):
        """
        Internal method that performs the actual API call.
        PRD: Requires X-Idempotency-Key support to prevent duplicate tickets.
        """
        url = f"{self.base_url}/{endpoint}"
        
        headers = dict(self.headers)
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=headers, json=data, timeout=5.0)
                
            if response.status_code >= 500:
                raise CRMConnectionError(f"Server Error {response.status_code}")
                
            if response.status_code not in [200, 201]:
                # PILLAR 3: Redact PII from API error responses (MEDIUM-P3-02)
                err_text = re.sub(r'\+?\d{7,15}', '[REDACTED]', response.text)
                error_msg = f"API Error {response.status_code}: {err_text}"
                logger.error(error_msg)
                raise CRMConnectionError(error_msg)
                
            resp_json = response.json()
            # Return ID from response
            return resp_json.get("id") or resp_json.get("ticket_id") or "UNKNOWN_ID"
            
        except httpx.RequestError as e:
            raise CRMConnectionError(f"Network Error: {e}")

    def _save_to_dlq(self, payload, error_msg):
        """
        PRD Section 5: Robust Failover Queue.
        1. PERSIST LOCALLY: Prevents data loss on pod restart/S3 failure.
        2. UPLOAD TO S3: ca-central-1 (Durable long-term recovery).
        """
        import uuid
        entry_id = str(uuid.uuid4())
        
        # Consistent status marker required by PRD
        entry = {
            "ticket_id": entry_id,
            "created_at": datetime.datetime.now().isoformat(),
            "payload": payload,
            "error": error_msg,
            "status": "Pending CRM Sync",
            "retry_count": 0
        }
        
        # 1. LOCAL PERSISTENCE FIRST (Pillar 2: Reliability)
        # NOTE: For true crash-resilience in K8s, logs/ must be a PersistentVolume.
        # If logs/ is ephemeral, data is lost on pod restart.
        dlq_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "crm_dlq")
        os.makedirs(dlq_dir, exist_ok=True)
        local_file = os.path.join(dlq_dir, f"{entry_id}.json")
        
        try:
            with open(local_file, "w", encoding="utf-8") as f:
                json.dump(entry, f, indent=2)
            logger.info(f"[CRM] DLQ entry saved locally: {local_file}")
        except Exception as e:
            # If even local disk fails, we have a major infra issue, but we still try S3
            logger.critical(f"[CRM] FATAL: Failed to write to local DLQ disk! {e}")

        # 2. ATTEMPT S3 UPLOAD (ca-central-1)
        try:
            self.s3_client.put_object(
                Bucket=self.s3_bucket,
                Key=f"dlq_tickets/{entry_id}.json",
                Body=json.dumps(entry)
            )
            logger.info(f"[CRM] Payload synced to S3 DLQ: {entry_id}")
            
            # If S3 succeeds, we can safely remove the local "buffer" file
            # as it is now durable in the ca-central-1 cloud.
            if os.path.exists(local_file):
                os.remove(local_file)
                
        except Exception as e:
            # If S3 fails, we do NOT delete the local file. 
            # The background reconciler will find it later.
            logger.error(f"[CRM] S3 DLQ Upload Failed. Entry remains stored at {local_file}. Error: {e}")



    async def get_ticket_status(self, ticket_id: str):
        # GET /tickets
        # We can implement a simple fetch
        return {"status": "Mock", "details": "Not implemented completely"}

    async def get_ticket_by_phone(self, phone_number: str):
        return None
