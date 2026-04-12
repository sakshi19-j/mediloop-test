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


def extract_medicines_from_image(
    image_data: bytes,
    content_type: str,
    prompt: str
) -> list:
    """Shared helper — calls GPT-4o-mini with the given image and prompt."""
    base64_image = base64.b64encode(image_data).decode("utf-8")
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
                        "text": prompt
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

    return json.loads(response_text)


PRESCRIPTION_PROMPT = """You are a medical prescription reader. Extract all medicines from this prescription image.

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


BILL_PROMPT = """You are a pharmacy billing assistant. Extract all medicines from this pharmacy bill or invoice image.

Return ONLY a JSON array with no other text, in this exact format:
[
  {
    "name": "Medicine name",
    "dosage": "dosage like 500mg or 10mg",
    "refill_days": 30,
    "instructions": ""
  }
]

Rules:
- This is a billing printout, not a prescription. Extract medicine/product names from the bill line items.
- Ignore non-medicine items (surgical supplies, cosmetics, baby products, etc.)
- refill_days should be estimated from quantity purchased: if qty is 30 tablets set 30, if 60 set 60, if 10 set 10. Default to 30 if unclear.
- Extract dosage from the medicine name itself (e.g. "Metformin 500mg" → dosage is "500mg")
- If dosage is not in the name, write "as prescribed"
- Return only the JSON array, nothing else"""


@router.post("/read")
async def read_prescription(
    patient_id: str,
    file: UploadFile = File(...),
    pharmacy_id: str = Header(...)
):
    """Scan a doctor prescription image and extract medicines."""
    try:
        image_data = await file.read()
        content_type = file.content_type or "image/jpeg"

        medicines = extract_medicines_from_image(image_data, content_type, PRESCRIPTION_PROMPT)

        supabase.table("prescriptions").insert({
            "patient_id": patient_id,
            "pharmacy_id": pharmacy_id,
            "medicines_extracted": medicines,
            "file_name": file.filename,
            "source": "prescription"
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


@router.post("/read-bill")
async def read_bill(
    patient_id: str,
    file: UploadFile = File(...),
    pharmacy_id: str = Header(...)
):
    """Scan a pharmacy billing printout and extract medicines from line items."""
    try:
        image_data = await file.read()
        content_type = file.content_type or "image/jpeg"

        medicines = extract_medicines_from_image(image_data, content_type, BILL_PROMPT)

        # Filter out any empty names that GPT might return
        medicines = [m for m in medicines if m.get("name", "").strip()]

        supabase.table("prescriptions").insert({
            "patient_id": patient_id,
            "pharmacy_id": pharmacy_id,
            "medicines_extracted": medicines,
            "file_name": file.filename,
            "source": "bill"
        }).execute()

        return {
            "medicines_found": len(medicines),
            "medicines": medicines,
            "message": f"Found {len(medicines)} medicines in bill"
        }

    except json.JSONDecodeError:
        raise HTTPException(
            status_code=422,
            detail="Could not read bill clearly. Please upload a clearer image."
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/confirm")
async def confirm_and_add_medicines(
    body: ConfirmRequest,
    pharmacy_id: str = Header(...)
):
    """After prescription or bill is read, chemist confirms and medicines are added at once."""
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