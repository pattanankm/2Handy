# seed.py
from faker import Faker
import random
from database import SessionLocal, engine
import models

#ลบตารางเก่า (ถ้ามี) เพื่อเริ่มต้นใหม่
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
                email=fake.unique.email() # ใช้ fake.unique เพื่อไม่ให้อีเมลซ้ำกัน
            )
            db.add(user)
        
        # บันทึก Users ลง DB ก่อน เพื่อให้ระบบสร้าง ID ให้แต่ละคน
        db.commit()
        
        print("Creating Orders and Order Items...")
        # ดึงรายชื่อ User ทั้งหมดออกมาเพื่อสุ่มจับคู่กับ Order
        all_users = db.query(models.User).all()
        
        for _ in range(num_orders):
            # สุ่มลูกค้า 1 คน
            random_user = random.choice(all_users)
            
            # สร้างใบสั่งซื้อ
            order = models.Order(
                user_id=random_user.id,
                total_amount=0, # จะคำนวณทีหลัง
                status=random.choice(["pending", "completed", "cancelled"])
            )
            db.add(order)
            db.flush() # ใช้ flush เพื่อให้ได้ order.id มาใช้ต่อโดยยังไม่ commit
            
            # สร้างรายการสินค้าในใบสั่งซื้อ (สุ่ม 1-3 ชิ้นต่อออเดอร์)
            total_amount = 0
            for _ in range(random.randint(1, 3)):
                unit_price = random.randint(100, 5000)
                quantity = random.randint(1, 5)
                total_amount += (unit_price * quantity)
                
                item = models.OrderItem(
                    order_id=order.id,
                    product_id=fake.uuid4(), # จำลองรหัส MongoDB ID เป็น String ไปก่อน
                    quantity=quantity,
                    unit_price=unit_price
                )
                db.add(item)
            
            # อัปเดตราคารวมของใบสั่งซื้อ
            order.total_amount = total_amount
        
        # บันทึกข้อมูล Orders และ Items ทั้งหมดลง DB
        db.commit()
        print("✅ create data to PostgreSQL completed!")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    seed_postgres()