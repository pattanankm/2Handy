# seed.py
from faker import Faker
import random
from database import SessionLocal, engine, get_mongo_db
import models

# ลบตารางเก่า (ถ้ามี) เพื่อเริ่มต้นใหม่
models.Base.metadata.drop_all(bind=engine)
# 1. สั่งให้ SQLAlchemy สร้างตารางใน PostgreSQL (ถ้ายังไม่มี)
models.Base.metadata.create_all(bind=engine)

fake = Faker(['th_TH', 'en_US'])

def seed_postgres(num_users=1000, num_orders=1000):
    db = SessionLocal()
    try:
        print("Creating Users...")
        users = []
        for _ in range(num_users):
            user = models.User(
                name=fake.name(),
                email=fake.unique.email()
            )
            db.add(user)
        
        db.commit()
        
        print("Creating Orders and Order Items...")
        all_users = db.query(models.User).all()
        
        for _ in range(num_orders):
            random_user = random.choice(all_users)
            
            order = models.Order(
                user_id=random_user.id,
                total_amount=0,
                status=random.choice(["pending", "completed", "cancelled"])
            )
            db.add(order)
            db.flush()
            
            total_amount = 0
            for _ in range(random.randint(1, 3)):
                unit_price = random.randint(100, 5000)
                quantity = random.randint(1, 5)
                total_amount += (unit_price * quantity)
                
                item = models.OrderItem(
                    order_id=order.id,
                    product_id=fake.uuid4(), # จำลองรหัส MongoDB ID เป็น String
                    quantity=quantity,
                    unit_price=unit_price
                )
                db.add(item)
            
            order.total_amount = total_amount
        
        db.commit()
        print("✅ Create data to PostgreSQL completed!")
        
    except Exception as e:
        print(f"❌ Error in PostgreSQL seed: {e}")
        db.rollback()
    finally:
        db.close()

def seed_mongo(num_products=1000):
    try:
        # เรียกใช้ connection ของ MongoDB
        db_conn = get_mongo_db()
        
        import types
        if isinstance(db_conn, types.GeneratorType):
            mongo_db = next(db_conn)
        else:
            mongo_db = db_conn
            
        print("Creating Products in MongoDB...")
        # ล้างข้อมูลเก่าทิ้งก่อนเพื่อไม่ให้ซ้ำซ้อน
        mongo_db["products"].drop() 
        
        products = []
        categories = ["Electronics", "Clothing", "Books", "Furniture", "Vehicles"]
        conditions = ["New", "Like New", "Good", "Fair"]
        
        for _ in range(num_products):
            category = random.choice(categories)
            product = {
                "name": fake.catch_phrase(),
                "category": category,
                "status": "available", 
                "attributes": {
                    "condition": random.choice(conditions),
                    "price": round(random.uniform(50.0, 10000.0), 2),
                    "brand": fake.company() if category in ["Electronics", "Vehicles"] else None
                }
            }
            # ลบค่า None ออกจาก attributes
            product["attributes"] = {k: v for k, v in product["attributes"].items() if v is not None}
            
            products.append(product)
            
        mongo_db["products"].insert_many(products)
        print("✅ Create data to MongoDB completed!")
        
    except Exception as e:
        print(f"❌ Error in MongoDB seed: {e}")

if __name__ == "__main__":
    print("--- 🚀 Starting Database Seeding ---")
    seed_postgres()
    seed_mongo()
    print("--- 🎉 All Seeding Completed! ---")