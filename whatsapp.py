import httpx
import os

WATI_URL = os.getenv("WATI_API_URL")
WATI_TOKEN = os.getenv("WATI_API_TOKEN")
TEMPLATE_NAME = os.getenv("WATI_TEMPLATE_NAME")

async def send_reminder(phone: str, patient_name: str, medicine_name: str, pharmacy_name: str):
    url = f"{WATI_URL}/api/v1/sendTemplateMessage"
    
    headers = {
        "Authorization": f"Bearer {WATI_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "whatsappNumber": phone,           # format: 919823XXXXXX
        "template_name": TEMPLATE_NAME,
        "broadcast_name": "medicine_reminder",
        "parameters": [
            {"name": "1", "value": patient_name},
            {"name": "2", "value": medicine_name},
            {"name": "3", "value": pharmacy_name}
        ]
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        return response.json()