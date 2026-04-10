from fastapi import APIRouter, HTTPException, Header, UploadFile, File
from pydantic import BaseModel
from database import supabase
from openai import OpenAI
import base64
import os
import json
from datetime import date
from typing import List, Optional

router = APIRouter()


class MedicineConfirm(BaseModel):
    name: str
    dosage: Optional[str] = "as prescribed"
    refill_days: Optional[int] = 30
    instructions: Optional[str] = ""


class ConfirmRequest(BaseModel):
    patient_id: str
    medicines: List[MedicineConfirm]


@router.post("/read")
async def read_prescription(
    patient_id: str,
    file: UploadFile = File(...),
    pharmacy_id: str = Header(...)
):
    try:
        image_data = await file.read()
        base64_image = base64.b64encode(image_data).decode("utf-8")
        content_type = file.content_type or "image/jpeg"

        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        message = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{content_type};base64,{base64_image}"
                            }
                        },
                        {
                            "type": "text",
                            "text": """You are a medical prescription reader. Extract all medicines from this prescription image.

Return ONLY a JSON array with no other text, in this exact format:
[
  {
    "name": "Medicine name",
    "dosage": "dosage like 500mg or 10mg",
    "refill_days": 30,
    "instructions": "any instructions like twice daily"
  }
]

Rules:
- refill_days should be 30 for most chronic medicines, 7 for antibiotics, 90 for long-term
- If dosage is not clear, write "as prescribed"
- Include every medicine visible on the prescription
- Return only the JSON array, nothing else"""
                        }
                    ]
                }
            ]
        )

        response_text = message.choices[0].message.content.strip()

        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]

        medicines = json.loads(response_text)

        supabase.table("prescriptions").insert({
            "patient_id": patient_id,
            "pharmacy_id": pharmacy_id,
            "medicines_extracted": medicines,
            "file_name": file.filename
        }).execute()

        return {
            "medicines_found": len(medicines),
            "medicines": medicines,
            "message": f"Found {len(medicines)} medicines in prescription"
        }

    except json.JSONDecodeError:
        raise HTTPException(
            status_code=422,
            detail="Could not read prescription clearly. Please upload a clearer image."
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/confirm")
async def confirm_and_add_medicines(
    body: ConfirmRequest,
    pharmacy_id: str = Header(...)
):
    """After prescription is read, chemist confirms and all medicines are added at once"""
    try:
        records = []

        for med in body.medicines:
            records.append({
                "pharmacy_id": pharmacy_id,
                "patient_id": body.patient_id,
                "name": med.name,
                "dosage": med.dosage,
                "refill_days": med.refill_days,
                "last_purchase_date": date.today().isoformat(),
                "notes": med.instructions
            })

        result = supabase.table("medicines")\
            .insert(records)\
            .execute()

        return {
            "added": len(result.data),
            "medicines": result.data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))