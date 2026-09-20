# main.py
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Dict, Any

import models
from database import engine, get_pg_db, get_mongo_db


# สั่งให้สร้างตารางใน PostgreSQL ถ้ายังไม่มี
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="2Handy API")


# ==========================================
# Pydantic Schemas
# ==========================================

# PostgreSQL - User
class UserCreate(BaseModel):
    name: str
    email: str


# MongoDB - Product
# รองรับ Dynamic Attributes
class ProductCreate(BaseModel):
    name: str
    category: str
    attributes: Dict[str, Any]


# MongoDB - Review
class ReviewCreate(BaseModel):
    product_id: str
    user_id: int
    rating: int
    comment: str


# ==========================================
# PostgreSQL - Users
# ==========================================

@app.post("/api/v1/users", status_code=201)
def create_user(
    user: UserCreate,
    db: Session = Depends(get_pg_db)
):
    db_user = models.User(
        name=user.name,
        email=user.email
    )

    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    return {
        "message": "User created",
        "user": db_user
    }


@app.get("/api/v1/users/{user_id}")
def get_user(
    user_id: int,
    db: Session = Depends(get_pg_db)
):
    user = db.query(models.User).filter(
        models.User.id == user_id
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    return user


# ==========================================
# MongoDB - Products
# ==========================================

@app.post("/api/v1/products", status_code=201)
def create_product(
    product: ProductCreate,
    mongo_db=Depends(get_mongo_db)
):
    # แปลง Pydantic Model เป็น Dictionary
    product_dict = product.dict()

    # บันทึกลง MongoDB collection: products
    result = mongo_db["products"].insert_one(product_dict)

    # แปลง MongoDB ObjectId เป็น String
    product_dict["_id"] = str(result.inserted_id)

    return {
        "message": "Product created",
        "product": product_dict
    }


@app.get("/api/v1/products")
def get_products(
    mongo_db=Depends(get_mongo_db)
):
    # ดึงข้อมูลทั้งหมดจาก MongoDB
    products = list(
        mongo_db["products"].find()
    )

    # ObjectId ไม่สามารถส่งผ่าน JSON ได้
    # จึงแปลงเป็น String
    for product in products:
        product["_id"] = str(product["_id"])

    return products


# ==========================================
# Dual-DB - Orders
# ==========================================

@app.post("/api/v1/orders")
def create_order(
    db: Session = Depends(get_pg_db),
    mongo_db=Depends(get_mongo_db)
):
    # 1. ตรวจสอบ Product จาก MongoDB
    # 2. บันทึก Order ลง PostgreSQL
    # 3. ทำงานร่วมกันระหว่าง PostgreSQL และ MongoDB

    return {
        "message": "Order created successfully (Dual-DB transaction implemented here)"
    }