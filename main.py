# main.py
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict
from typing import Dict, Any, List

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
    attributes: Dict[str, Any]  # รับค่า Dynamic attributes แบบยืดหยุ่นสำหรับ MongoDB

class OrderItemCreate(BaseModel):
    product_id: str
    quantity: int = 1
    unit_price: int = 0

class OrderCreate(BaseModel):
    user_id: int
    items: List[OrderItemCreate]

class OrderItemRead(BaseModel):
    id: int
    order_id: int
    product_id: str
    quantity: int
    unit_price: int

    model_config = ConfigDict(from_attributes=True)

class OrderRead(BaseModel):
    id: int
    user_id: int
    total_amount: int
    status: str
    items: List[OrderItemRead]

    model_config = ConfigDict(from_attributes=True)

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
@app.post("/api/v1/orders", status_code=201)
def create_order(order: OrderCreate, db: Session = Depends(get_pg_db), mongo_db = Depends(get_mongo_db)):
    user = db.query(models.User).filter(models.User.id == order.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not order.items:
        raise HTTPException(status_code=400, detail="Order must include at least one item")

    total_amount = 0
    order_items = []

    for item in order.items:
        product = mongo_db["products"].find_one({"_id": item.product_id})
        if product is None:
            product = mongo_db["products"].find_one({"product_id": item.product_id})
        if product is None:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found in MongoDB")

        unit_price = int(product.get("price", item.unit_price))
        quantity = int(item.quantity)
        item_total = unit_price * quantity
        total_amount += item_total
        order_items.append({
            "product_id": item.product_id,
            "quantity": quantity,
            "unit_price": unit_price,
        })

    db_order = models.Order(user_id=order.user_id, total_amount=total_amount, status="pending")
    db.add(db_order)
    db.commit()
    db.refresh(db_order)

    for item in order_items:
        db_order_item = models.OrderItem(
            order_id=db_order.id,
            product_id=item["product_id"],
            quantity=item["quantity"],
            unit_price=item["unit_price"],
        )
        db.add(db_order_item)

    db.commit()

    mongo_db["orders"].insert_one({
        "order_id": db_order.id,
        "user_id": order.user_id,
        "status": "pending",
        "total_amount": total_amount,
        "items": order_items,
    })

    return {
        "message": "Order created successfully",
        "order": {
            "id": db_order.id,
            "user_id": db_order.user_id,
            "total_amount": db_order.total_amount,
            "status": db_order.status,
            "items": order_items,
        },
    }


@app.get("/api/v1/orders")
def get_orders(db: Session = Depends(get_pg_db)):
    orders = db.query(models.Order).all()
    result = []
    for order in orders:
        result.append({
            "id": order.id,
            "user_id": order.user_id,
            "total_amount": order.total_amount,
            "status": order.status,
            "items": [
                {
                    "id": item.id,
                    "order_id": item.order_id,
                    "product_id": item.product_id,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                }
                for item in order.items
            ],
        })
    return result


@app.get("/api/v1/orders/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_pg_db)):
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return {
        "id": order.id,
        "user_id": order.user_id,
        "total_amount": order.total_amount,
        "status": order.status,
        "items": [
            {
                "id": item.id,
                "order_id": item.order_id,
                "product_id": item.product_id,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
            }
            for item in order.items
        ],
    }