# main.py
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Dict, Any

import models
from database import engine, get_pg_db, get_mongo_db

# สั่งให้สร้างตารางใน PostgreSQL (ถ้ายังไม่มี)
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="2Handy API")

# --- Pydantic Schemas สำหรับรับข้อมูลจาก Request ---
class UserCreate(BaseModel):
    name: str
    email: str

class ProductCreate(BaseModel):
    name: str
    category: str
    attributes: Dict[str, Any] # รับค่า Dynamic attributes แบบยืดหยุ่นสำหรับ MongoDB

# ==========================================
# Endpoints สำหรับ PostgreSQL (Users)
# ==========================================

@app.post("/api/v1/users", status_code=201)
def create_user(user: UserCreate, db: Session = Depends(get_pg_db)):
    # สร้าง User ลงใน PostgreSQL
    db_user = models.User(name=user.name, email=user.email)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return {"message": "User created", "user": db_user}

@app.get("/api/v1/users/{user_id}")
def get_user(user_id: int, db: Session = Depends(get_pg_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

# ==========================================
# Endpoints สำหรับ MongoDB (Products)
# ==========================================

@app.post("/api/v1/products", status_code=201)
def create_product(product: ProductCreate, mongo_db = Depends(get_mongo_db)):
    # บันทึกข้อมูลลง Collection "products" ใน MongoDB
    product_dict = product.dict()
    result = mongo_db["products"].insert_one(product_dict)
    
    # ดึง ID ที่ MongoDB สร้างให้แปลงเป็น String
    product_dict["_id"] = str(result.inserted_id)
    return {"message": "Product created", "product": product_dict}

@app.get("/api/v1/products")
def get_products(mongo_db = Depends(get_mongo_db)):
    # ดึงข้อมูลทั้งหมดจาก MongoDB (ในงานจริงควรทำ Pagination)
    products = list(mongo_db["products"].find())
    for prod in products:
        prod["_id"] = str(prod["_id"]) # แปลง ObjectId ให้เป็น String
    return products

# ==========================================
# Endpoint แบบ Dual-DB (Orders)
# ==========================================
@app.post("/api/v1/orders")
def create_order(db: Session = Depends(get_pg_db), mongo_db = Depends(get_mongo_db)):
    # 1. ตรวจสอบข้อมูล Product จาก MongoDB (ดึงราคา, สต็อก)
    # 2. บันทึก Transaction การสั่งซื้อลง PostgreSQL (ตาราง orders, order_items)
    # 3. อัปเดตข้อมูลหรือทำ Audit log กลับไปที่ MongoDB
    return {"message": "Order created successfully (Dual-DB transaction implemented here)"}