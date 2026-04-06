from twilio.rest import Client
import os

def get_client():
    return Client(
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN")
    )

async def send_reminder(
    phone: str,
    patient_name: str,
    medicine_name: str,
    pharmacy_name: str
) -> dict:
    
    message_body = (
        f"Hello {patient_name}, this is a reminder from {pharmacy_name}. "
        f"Your medicine {medicine_name} is due in 2 days. "
        f"Please visit us or reply to order. Thank you!"
    )

    # Try WhatsApp first
    try:
        client = get_client()
        msg = client.messages.create(
            from_=os.getenv("TWILIO_WHATSAPP_FROM"),
            to=f"whatsapp:+{phone}",
            body=message_body
        )
        return {
            "channel": "whatsapp",
            "sid": msg.sid,
            "status": msg.status
        }

    except Exception as whatsapp_error:
        print(f"WhatsApp failed for {phone}: {whatsapp_error}")

        # Fallback to SMS
        try:
            client = get_client()
            msg = client.messages.create(
                from_=os.getenv("TWILIO_SMS_FROM"),
                to=f"+{phone}",
                body=message_body
            )
            return {
                "channel": "sms_fallback",
                "sid": msg.sid,
                "status": msg.status
            }

        except Exception as sms_error:
            print(f"SMS also failed for {phone}: {sms_error}")
            return {
                "channel": "failed",
                "error": str(sms_error)
            }