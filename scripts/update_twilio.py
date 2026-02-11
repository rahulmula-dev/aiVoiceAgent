import os
from twilio.rest import Client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
NGROK_URL = "https://intervertebral-iker-revelatory.ngrok-free.dev"
VOICE_WEBHOOK = f"{NGROK_URL}/voice"

if __name__ == "__main__":
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print("Error: Twilio credentials not found in .env")
        exit(1)

    print(f"Connecting to Twilio...")
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    # 1. List Incoming Phone Numbers
    print("Fetching active phone numbers...")
    numbers = client.incoming_phone_numbers.list(limit=5)

    if not numbers:
        print("Error: No phone numbers found in this Twilio account.")
        exit(1)

    # 2. Update the first number (Assuming only one active for this agent)
    target_number = numbers[0]
    print(f"Found Number: {target_number.phone_number} ({target_number.friendly_name})")
    print(f"Current Voice URL: {target_number.voice_url}")

    print(f"Updating Voice URL to: {VOICE_WEBHOOK} ...")
    
    updated_number = client.incoming_phone_numbers(target_number.sid).update(
        voice_url=VOICE_WEBHOOK,
        voice_method='POST'
    )

    print("---------------------------------------------------")
    print("✅ SUCCESS: Webhook Updated!")
    print(f"Number: {updated_number.phone_number}")
    print(f"New Voice URL: {updated_number.voice_url}")
    print("---------------------------------------------------")
