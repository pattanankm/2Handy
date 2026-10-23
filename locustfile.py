"""locustfile.py - Checkpoint 3 Automated Load Testing & Benchmarking.

Load Profile:
  - 50 Virtual Users (VUs)
  - Concurrent Read/Write transactions
  - Duration: 3 minutes

Target Performance Metrics:
  - Throughput: Stable RPS
  - Latency: p95 < 200ms, p99 < 500ms
  - Error Rate: < 1%

Run Command:
  locust --headless -u 50 -r 10 --run-time 3m --host http://127.0.0.1:8000 \
         --html Performance_Audit_Report.html --csv performance_results
"""

import random
from uuid import uuid4
from locust import HttpUser, task, between, events


class MarketplaceUser(HttpUser):
    # Simulated think time between user actions (0.1 to 0.4 seconds)
    wait_time = between(0.1, 0.4)

    def on_start(self):
        """Initialize user session: fetch existing product IDs and a valid user ID."""
        self.product_ids = []
        self.user_id = random.randint(1, 1000)

        try:
            res = self.client.get("/api/v1/products?limit=50", name="Setup: Fetch Initial Products")
            if res.status_code == 200:
                products = res.json()
                self.product_ids = [p["_id"] for p in products if "_id" in p]
        except Exception:
            pass

    # ==========================================
    # Read Transactions (Audit: No N+1, Projected)
    # ==========================================

    @task(6)
    def view_products_list(self):
        """Read: list products with projection and index (O(1) find)."""
        category = random.choice([None, "Electronics", "Furniture", "Clothing", "Books"])
        params = {"limit": 20}
        if category:
            params["category"] = category
        self.client.get("/api/v1/products", params=params, name="Read: List Products (Projected)")

    @task(4)
    def view_product_detail(self):
        """Read: single product detail view."""
        if self.product_ids:
            pid = random.choice(self.product_ids)
            self.client.get(f"/api/v1/products/{pid}", name="Read: Get Product Detail")

    @task(4)
    def view_product_summaries(self):
        """Read: product list with review aggregation ($lookup, zero N+1)."""
        self.client.get("/api/v1/products/summary?limit=20", name="Read: Product Summaries ($lookup)")

    @task(3)
    def view_product_reviews(self):
        """Read: product reviews with projection."""
        if self.product_ids:
            pid = random.choice(self.product_ids)
            self.client.get(f"/api/v1/products/{pid}/reviews", name="Read: Product Reviews")

    @task(5)
    def view_orders_list(self):
        """Read: list orders with items in 1 SQL statement (JOIN + jsonb_agg, zero N+1)."""
        params = {"limit": 20}
        if random.random() < 0.5:
            params["user_id"] = self.user_id
        self.client.get("/api/v1/orders", params=params, name="Read: List Orders (1 SQL)")

    # ==========================================
    # Write Transactions (OCC & Concurrency Control)
    # ==========================================

    @task(2)
    def create_new_product(self):
        """Write: create an available product with OCC version=1."""
        payload = {
            "name": f"Product-{uuid4().hex[:6]}",
            "category": random.choice(["Electronics", "Furniture", "Clothing", "Books", "Sports"]),
            "attributes": {
                "price": round(random.uniform(50.0, 5000.0), 2),
                "condition": random.choice(["New", "Like New", "Good", "Used"]),
                "in_stock": True,
            },
        }
        with self.client.post("/api/v1/products", json=payload, catch_response=True, name="Write: Create Product (OCC v1)") as res:
            if res.status_code == 201:
                pid = res.json().get("product", {}).get("_id")
                if pid:
                    self.product_ids.append(pid)
                    if len(self.product_ids) > 100:
                        self.product_ids.pop(0)
                res.success()
            else:
                res.failure(f"Create product failed: {res.status_code}")

    @task(2)
    def create_order_transaction(self):
        """Write: dual-DB order creation with OCC inventory lock."""
        if not self.product_ids:
            return

        pid = random.choice(self.product_ids)
        payload = {
            "user_id": self.user_id,
            "items": [
                {
                    "product_id": pid,
                    "quantity": 1,
                }
            ],
        }

        with self.client.post("/api/v1/orders", json=payload, catch_response=True, name="Write: Create Order (Dual-DB)") as res:
            if res.status_code == 201:
                res.success()
            elif res.status_code == 409:
                # 409 Conflict is expected behavior when another VU bought the item first (OCC)
                res.success()
            else:
                res.failure(f"Unexpected status: {res.status_code} - {res.text}")