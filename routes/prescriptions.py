from fastapi import APIRouter, HTTPException, Header, UploadFile, File
from database import supabase
import anthropic
import base64
import os
import json

router = APIRouter()

@router.post("/read")
async def read_prescription(
    patient_id: str,
    file: UploadFile = File(...),
    pharmacy_id: str = Header(...)
):
    try:
        # Read image file
        image_data = await file.read()
        base64_image = base64.b64encode(image_data).decode("utf-8")

        # Determine media type
        content_type = file.content_type or "image/jpeg"

        # Call Claude to read prescription
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": content_type,
                                "data": base64_image
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

        # Parse response
        response_text = message.content[0].text.strip()

        # Clean up JSON if needed
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]

        medicines = json.loads(response_text)

        # Save prescription record to Supabase
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
    patient_id: str,
    medicines: list,
    pharmacy_id: str = Header(...)
):
    """After prescription is read, chemist confirms and all medicines are added at once"""
    try:
        from datetime import date
        records = []

        for med in medicines:
            records.append({
                "pharmacy_id": pharmacy_id,
                "patient_id": patient_id,
                "name": med["name"],
                "dosage": med.get("dosage", "as prescribed"),
                "refill_days": med.get("refill_days", 30),
                "last_purchase_date": date.today().isoformat(),
                "notes": med.get("instructions", "")
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
