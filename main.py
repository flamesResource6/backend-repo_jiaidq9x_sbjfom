import os
import math
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import db, create_document, get_documents
from schemas import User, Tasker, Task, Booking, Message, Review, AdminAction, Cancellation, Dispute, Payout

app = FastAPI(title="Servisca API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------
# Utilities
# ---------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# ---------------------------
# Simple WebSocket channel manager
# ---------------------------
class WSManager:
    def __init__(self):
        self.topics: Dict[str, List[WebSocket]] = {}
        self.lock = asyncio.Lock()

    async def connect(self, topic: str, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.topics.setdefault(topic, []).append(ws)

    async def disconnect(self, topic: str, ws: WebSocket):
        async with self.lock:
            if topic in self.topics and ws in self.topics[topic]:
                self.topics[topic].remove(ws)
                if not self.topics[topic]:
                    self.topics.pop(topic, None)

    async def broadcast(self, topic: str, message: Dict[str, Any]):
        conns = list(self.topics.get(topic, []))
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                # Drop broken connections
                await self.disconnect(topic, ws)

ws_manager = WSManager()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, topic: str):
    # topic examples: task:t123, user:u123, tasker:tsk123
    await ws_manager.connect(topic, ws)
    try:
        while True:
            # Keep alive; clients may send pings or ignore
            _ = await ws.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(topic, ws)

# ---------------------------
# Config & basic endpoints
# ---------------------------
class AppConfig(BaseModel):
    categories: List[Dict[str, Any]]
    promotions: List[Dict[str, Any]] = []
    quick_match: Dict[str, Any]


@app.get("/config")
async def get_config() -> AppConfig:
    categories = [
        {"id": "plumbing", "name": "Plumbing", "subcategories": ["Leak", "Install", "Clog"]},
        {"id": "electrical", "name": "Electrical", "subcategories": ["Wiring", "Outlet", "Lighting"]},
        {"id": "moving", "name": "Moving", "subcategories": ["Small Move", "Truck", "Packing"]},
        {"id": "cleaning", "name": "Cleaning", "subcategories": ["Home", "Office", "Deep Clean"]},
        {"id": "furniture", "name": "Furniture", "subcategories": ["Assembly", "Mounting", "Repair"]},
    ]
    return AppConfig(
        categories=categories,
        promotions=[],
        quick_match={
            "enabled": True,
            "default_radius_km": 5,
            "offer_timeout_sec": 20,
            "global_timeout_sec": 120,
            "fee": 1.5,
        },
    )

# ---------------------------
# OTP Auth (demo)
# ---------------------------
class RequestOtpBody(BaseModel):
    contact: str  # phone or email
    channel: str = Field(pattern="^(sms|email)$")

class VerifyOtpBody(BaseModel):
    contact: str
    otp: str

# In-memory ephemeral store: acceptable for demo OTP
_otp_store: Dict[str, Dict[str, Any]] = {}

@app.post("/auth/request-otp")
async def request_otp(body: RequestOtpBody):
    otp = "123456"  # For demo only. Integrate real provider in production.
    _otp_store[body.contact] = {"otp": otp, "expires": now_utc() + timedelta(minutes=5)}
    return {"sent": True, "channel": body.channel}

@app.post("/auth/verify-otp")
async def verify_otp(body: VerifyOtpBody):
    rec = _otp_store.get(body.contact)
    if not rec or rec["expires"] < now_utc() or rec["otp"] != body.otp:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    # Demo token
    token = f"demo-token-{body.contact}"
    return {"ok": True, "token": token, "user_id": f"u_{abs(hash(body.contact)) % 10_000}"}

# ---------------------------
# Tasks & Quick Match
# ---------------------------
class CreateTaskBody(BaseModel):
    user_id: str
    category: str
    description: Optional[str] = None
    lat: float
    lng: float
    address: Optional[str] = None
    quick_match: bool = False
    payment_method_id: Optional[str] = None
    estimated_duration: Optional[int] = None

class TaskAcceptBody(BaseModel):
    task_id: str

class StatusPatchBody(BaseModel):
    status: Task.__annotations__["status"]  # reuse literals

@app.post("/tasks/create")
async def create_task(body: CreateTaskBody, background: BackgroundTasks):
    task_doc = Task(
        user_id=body.user_id,
        address=body.address,
        lat=body.lat,
        lng=body.lng,
        category=body.category,
        description=body.description,
        duration_est=body.estimated_duration,
        quick_match=body.quick_match,
        status="searching" if body.quick_match else "draft",
        price_est=None,
        created_at=now_utc(),
    ).model_dump()

    task_id = create_document("task", task_doc)

    # Notify creation
    await ws_manager.broadcast(f"user:{body.user_id}", {"event": "task:created", "task_id": task_id})

    match_estimate = 60
    quick_match_fee = 1.5

    if body.quick_match:
        background.add_task(run_quick_match, task_id, body.lat, body.lng, body.category, body.user_id)

    return {"task_id": task_id, "status": task_doc["status"], "match_estimate": match_estimate, "quick_match_fee": quick_match_fee}

@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    doc = db["task"].find_one({"_id": {"$oid": task_id}}) if isinstance(task_id, dict) else db["task"].find_one({"_id": db.client.get_default_database().codec_options.document_class.object_hook if False else None})
    # Simpler fetch by _id string support using $toString mirror
    from bson import ObjectId
    try:
        obj = ObjectId(task_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Invalid task id")
    doc = db["task"].find_one({"_id": obj})
    if not doc:
        raise HTTPException(status_code=404, detail="Task not found")
    doc["_id"] = str(doc["_id"]) 
    return doc

@app.patch("/tasks/{task_id}/status")
async def patch_task_status(task_id: str, body: StatusPatchBody):
    from bson import ObjectId
    obj = ObjectId(task_id)
    res = db["task"].update_one({"_id": obj}, {"$set": {"status": body.status, "updated_at": now_utc()}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    await ws_manager.broadcast(f"task:{task_id}", {"event": "task:status_update", "status": body.status})
    return {"ok": True}

# Tasker accepts an offer
@app.post("/taskers/{tasker_id}/accept")
async def tasker_accept(tasker_id: str, body: TaskAcceptBody):
    from bson import ObjectId
    obj = ObjectId(body.task_id)
    # Assign tasker if still searching
    task = db["task"].find_one({"_id": obj})
    if not task or task.get("status") != "searching":
        raise HTTPException(status_code=400, detail="Task not available for assignment")
    db["task"].update_one({"_id": obj}, {"$set": {"assigned_tasker_id": tasker_id, "status": "accepted", "updated_at": now_utc()}})
    await ws_manager.broadcast(f"task:{body.task_id}", {"event": "task:assigned", "tasker_id": tasker_id})
    await ws_manager.broadcast(f"user:{task.get('user_id')}", {"event": "task:assigned", "task_id": body.task_id, "tasker_id": tasker_id})
    return {"ok": True}

# Nearby taskers
@app.get("/taskers/nearby")
async def nearby_taskers(lat: float, lng: float, radius: float = 5.0, skill: Optional[str] = None):
    # Basic scan; in production use geo indexes or GeoJSON queries
    taskers = get_documents("tasker", {"status": "online"})
    results = []
    for t in taskers:
        loc = (t.get("location") or {})
        tlat, tlng = loc.get("lat"), loc.get("lng")
        if tlat is None or tlng is None:
            continue
        if skill and skill not in (t.get("skills") or []):
            continue
        dist = haversine_km(lat, lng, tlat, tlng)
        if dist <= radius:
            t_copy = dict(t)
            t_copy["_id"] = str(t_copy["_id"]) 
            t_copy["distance_km"] = round(dist, 3)
            results.append(t_copy)
    results.sort(key=lambda x: (x.get("distance_km", 999), -(x.get("rating") or 0)))
    return {"count": len(results), "items": results}

# Payments mock
@app.post("/payments/hold")
async def payments_hold(task_id: Optional[str] = None, amount: Optional[float] = None):
    return {"ok": True, "hold_id": f"hold_{int(now_utc().timestamp())}"}

@app.post("/payments/capture")
async def payments_capture(hold_id: str, amount: float):
    return {"ok": True, "captured": amount}

# ---------------------------
# Quick Match background process
# ---------------------------
async def run_quick_match(task_id: str, lat: float, lng: float, category: str, user_id: str):
    # Config
    default_radius = 5.0
    offer_timeout_sec = 20
    global_timeout_sec = 120
    radius = default_radius
    deadline = now_utc() + timedelta(seconds=global_timeout_sec)

    from bson import ObjectId
    obj = ObjectId(task_id)

    attempt = 0
    while now_utc() < deadline:
        attempt += 1
        # Fetch candidates
        candidates_resp = await nearby_taskers(lat=lat, lng=lng, radius=radius)
        candidates = candidates_resp["items"]
        if not candidates:
            radius *= 1.5
            await asyncio.sleep(2)
            continue
        # Iterate candidates
        for cand in candidates:
            tasker_id = str(cand.get("_id"))
            eta_min = max(3, int(haversine_km(lat, lng, cand["location"]["lat"], cand["location"]["lng"]) / 0.4))
            offer = {
                "event": "match_offer",
                "task_id": task_id,
                "tasker_id": tasker_id,
                "eta_minutes": eta_min,
                "offer_expires_at": (now_utc() + timedelta(seconds=offer_timeout_sec)).isoformat(),
            }
            await ws_manager.broadcast(f"tasker:{tasker_id}", offer)
            # Wait for acceptance window
            for _ in range(offer_timeout_sec):
                # Check if task already assigned
                cur = db["task"].find_one({"_id": obj})
                if cur and cur.get("status") == "accepted":
                    return
                await asyncio.sleep(1)
        # Expand search if nobody accepted
        radius *= 1.5
    # Timeout reached: notify user
    await ws_manager.broadcast(f"user:{user_id}", {"event": "match_failed", "task_id": task_id, "reason": "timeout"})


@app.get("/")
def read_root():
    return {"message": "Servisca API ready"}

@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}

@app.get("/test")
def test_database():
    """Test endpoint to check if database is available and accessible"""
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"

    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
