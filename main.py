# main.py
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Dict, Any, List
from bson import ObjectId

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

class OrderItemCreate(BaseModel):
    product_id: str
    quantity: int = 1

class OrderCreate(BaseModel):
    user_id: int
    items: List[OrderItemCreate]

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
@app.post("/api/v1/orders", status_code=201)
def create_order(order: OrderCreate, db: Session = Depends(get_pg_db), mongo_db = Depends(get_mongo_db)):
    # 1. ตรวจสอบข้อมูล Product จาก MongoDB (ดึงราคา, สต็อก)
    # 2. บันทึก Transaction การสั่งซื้อลง PostgreSQL (ตาราง orders, order_items)
    # 3. อัปเดตข้อมูลหรือทำ Audit log กลับไปที่ MongoDB
    if not hasattr(models, "Order") or not hasattr(models, "OrderItem"):
        raise HTTPException(
            status_code=503,
            detail="Order and OrderItem models are not available yet",
        )

    if not order.items:
        raise HTTPException(status_code=422, detail="Order must contain at least one item")

    user = db.query(models.User).filter(models.User.id == order.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    products_to_order = []
    requested_product_ids = set()

    for item in order.items:
        if item.quantity < 1:
            raise HTTPException(status_code=422, detail="Quantity must be at least 1")

        if item.product_id in requested_product_ids:
            raise HTTPException(status_code=422, detail="A product can appear only once in an order")
        requested_product_ids.add(item.product_id)

        if not ObjectId.is_valid(item.product_id):
            raise HTTPException(status_code=422, detail=f"Invalid product id: {item.product_id}")

        product_object_id = ObjectId(item.product_id)
        product = mongo_db["products"].find_one({"_id": product_object_id})
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.product_id}")

        if product.get("status") not in (None, "available"):
            raise HTTPException(status_code=409, detail=f"Product is not available: {item.product_id}")

        products_to_order.append({
            "item": item,
            "object_id": product_object_id,
            "previous_status": product.get("status"),
            "had_status": "status" in product,
            "previous_order_id": product.get("order_id"),
            "had_order_id": "order_id" in product,
        })

    updated_products = []
    db_order = None

    def restore_products():
        if not db_order:
            return

        for product_data in updated_products:
            restore_set = {}
            restore_unset = {}

            if product_data["had_status"]:
                restore_set["status"] = product_data["previous_status"]
            else:
                restore_unset["status"] = ""

            if product_data["had_order_id"]:
                restore_set["order_id"] = product_data["previous_order_id"]
            else:
                restore_unset["order_id"] = ""

            restore_update = {}
            if restore_set:
                restore_update["$set"] = restore_set
            if restore_unset:
                restore_update["$unset"] = restore_unset

            mongo_db["products"].update_one(
                {"_id": product_data["object_id"], "order_id": db_order.id},
                restore_update,
            )

    try:
        db_order = models.Order(user_id=order.user_id)
        db.add(db_order)
        db.flush()

        for product_data in products_to_order:
            item = product_data["item"]
            if item.quantity != 1 and not hasattr(models.OrderItem, "quantity"):
                raise HTTPException(
                    status_code=422,
                    detail="OrderItem model does not support quantities greater than 1",
                )
            order_item_data = {
                "order_id": db_order.id,
                "product_id": item.product_id,
            }
            if hasattr(models.OrderItem, "quantity"):
                order_item_data["quantity"] = item.quantity
            db.add(models.OrderItem(**order_item_data))

        for product_data in products_to_order:
            product_filter = {"_id": product_data["object_id"]}
            previous_status = product_data["previous_status"]
            if product_data["had_status"]:
                product_filter["status"] = previous_status
            else:
                product_filter["status"] = {"$exists": False}

            result = mongo_db["products"].update_one(
                product_filter,
                {"$set": {"status": "sold", "order_id": db_order.id}},
            )
            if result.modified_count != 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"Product is no longer available: {product_data['item'].product_id}",
                )
            updated_products.append(product_data)

        db.commit()
    except HTTPException:
        db.rollback()
        restore_products()
        raise
    except Exception:
        db.rollback()
        restore_products()
        raise HTTPException(status_code=500, detail="Could not create order")

    return {
        "message": "Order created successfully",
        "order_id": db_order.id,
        "user_id": order.user_id,
        "items": [
            item.model_dump() if hasattr(item, "model_dump") else item.dict()
            for item in order.items
        ],
    }
