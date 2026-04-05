from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scheduler import start_scheduler
from routes import auth, patients, medicines, dashboard

app = FastAPI(title="MediLoop API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

app.include_router(auth.router, prefix="/auth")
app.include_router(patients.router, prefix="/patients")
app.include_router(medicines.router, prefix="/medicines")
app.include_router(dashboard.router, prefix="/dashboard")

@app.on_event("startup")
async def startup():
    start_scheduler()

@app.get("/")
def root():
    return {"status": "MediLoop running"}