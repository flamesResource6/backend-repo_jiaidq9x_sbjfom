"""
Database Schemas for Servisca

Each Pydantic model represents a MongoDB collection. The collection name is the
lowercased class name (e.g., User -> "user").

These schemas focus on key fields required by the product spec. Optional fields
allow gradual enrichment without breaking validation.
"""
from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field, EmailStr
from datetime import datetime

# Core collections
class User(BaseModel):
    name: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    payment_methods: Optional[List[Dict[str, Any]]] = None  # tokenized methods (Stripe PM ids)
    avatar: Optional[str] = None
    rating: Optional[float] = Field(default=None, ge=0, le=5)
    role: Literal["user", "tasker", "admin"] = "user"
    created_at: Optional[datetime] = None

class Tasker(BaseModel):
    name: str
    skills: List[str] = []
    hourly_rate: Optional[float] = Field(default=None, ge=0)
    vehicle: Optional[str] = None
    status: Literal["online", "offline"] = "offline"
    location: Optional[Dict[str, float]] = None  # { lat: float, lng: float }
    rating: Optional[float] = Field(default=None, ge=0, le=5)
    background_check_status: Optional[Literal["pending", "verified", "rejected"]] = "pending"
    acceptance_rate: Optional[float] = Field(default=None, ge=0, le=1)
    last_seen: Optional[datetime] = None

class Task(BaseModel):
    user_id: str
    address: Optional[str] = None
    lat: float
    lng: float
    category: str
    subcategory: Optional[str] = None
    description: Optional[str] = None
    images: Optional[List[str]] = None
    preferred_time: Optional[datetime] = None
    duration_est: Optional[int] = Field(default=None, description="Estimated minutes")
    price_est: Optional[float] = None
    status: Literal[
        "draft",
        "searching",
        "accepted",
        "en_route",
        "arrived",
        "in_progress",
        "completed",
        "canceled",
        "failed"
    ] = "searching"
    quick_match: bool = False
    assigned_tasker_id: Optional[str] = None
    created_at: Optional[datetime] = None

class Booking(BaseModel):
    task_id: str
    tasker_id: str
    price: float
    payment_status: Literal["init", "hold", "captured", "refunded", "failed"] = "init"
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    status: Literal["reserved", "in_progress", "completed", "canceled"] = "reserved"

class Message(BaseModel):
    from_id: str
    to_id: str
    task_id: str
    text: Optional[str] = None
    media: Optional[List[str]] = None
    created_at: Optional[datetime] = None

class Review(BaseModel):
    task_id: str
    from_id: str
    to_id: str
    rating: int = Field(..., ge=1, le=5)
    comment: Optional[str] = None

class AdminAction(BaseModel):
    actor_id: str
    action: str
    target_type: str
    target_id: str
    details: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None

class Cancellation(BaseModel):
    task_id: str
    who: Literal["user", "tasker", "admin"]
    reason: Optional[str] = None
    fee: Optional[float] = 0.0

class Dispute(BaseModel):
    task_id: str
    opened_by: Literal["user", "tasker", "admin"]
    reason: str
    status: Literal["open", "review", "resolved", "rejected"] = "open"
    evidence: Optional[List[str]] = None

class Payout(BaseModel):
    tasker_id: str
    amount: float
    status: Literal["pending", "processing", "paid", "failed"] = "pending"
    period: Optional[str] = None  # e.g., "2025-W45"

# Lightweight OTP session for auth (non-persistent secrets should be handled by a real provider)
class OtpSession(BaseModel):
    contact: str
    channel: Literal["sms", "email"]
    otp: str
    expires_at: datetime

# Simple category structure
class Category(BaseModel):
    id: str
    name: str
    subcategories: List[str] = []
