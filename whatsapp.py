from twilio.rest import Client
import os

def send_reminder_sync(phone: str, patient_name: str, medicine_name: str, pharmacy_name: str):
    client = Client(
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN")
    )
    
    message = client.messages.create(
        from_=os.getenv("TWILIO_WHATSAPP_FROM"),
        to=f"whatsapp:+{phone}",
        body=f"Hello {patient_name}, this is a reminder from {pharmacy_name}. Your medicine {medicine_name} is due in 2 days. Please visit us or reply to order. Thank you!"
    )
    
    return {"sid": message.sid, "status": message.status}

async def send_reminder(phone: str, patient_name: str, medicine_name: str, pharmacy_name: str):
    return send_reminder_sync(phone, patient_name, medicine_name, pharmacy_name)