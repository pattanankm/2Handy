# 2Handy - Secondhand Marketplace API

This project is a Backend API for the "2Handy" secondhand marketplace platform, developed as part of Checkpoint 1 (CP1) for the course **269340: Data Centric Application Development**. The project implements a Dual-Database architecture to separate transactional data management from highly flexible data storage.

## 👥 Team Roster (the smachik gang)
* **1:** 670615108 Prempracha Numchai - PostgreSQL (Users + Orders)
* **2:** 670615036 Apiluk Bualuang - MongoDB (Products + Reviews)
* **3:** 670615031 Peeratchai Matunboon - REST API + Backend Integration
* **4:** 670615030 Pattanan Siriworakul - Docker + Seed Data + Documentation

---

## System Architecture

![Architecture Diagram](ArchitectureDiagram.jpg)

The system is designed with separated responsibilities within a Docker Compose environment:
* **Web Frontend:** The "2Handy" user interface connecting via REST API.
* **Backend API:** Built with Python (FastAPI), serving as the core engine to process and route data.
* **PostgreSQL (Transactional Data):** A relational database managing core transactional domain data (`Users`, `Orders`, and `Order Items`).
* **MongoDB (Flexible Data):** A document store managing flexible, semi-structured data (`Products` and `Reviews`).

## Checkpoint 3: MongoDB Concurrency and Query Performance

The MongoDB layer uses optimistic concurrency control for products. Each product has
a `version` token, and updates match both the product ID and the version read by the
caller. A successful update increments the token atomically; stale writers are
rejected and the API returns `409 Conflict`.

MongoDB reads use projections for product, product-state, and review endpoints.
Product summaries use one aggregation with `$lookup` instead of one review query
per product. Run the migration once for products created before Checkpoint 3:

```powershell
python migrations/006_mongo_add_version.py
```

The focused verification uses a throwaway database and 20 concurrent writers:

```powershell
python test_mongo_concurrency.py
```

It verifies one successful writer, 19 rejected stale writers, legacy-version
compatibility, projection output, and one aggregation query for summaries.

---

## ⚙️ Prerequisites
Before running the project, ensure you have the following installed on your machine:
* [Docker](https://www.docker.com/products/docker-desktop/) and Docker Compose
* [Python 3.9+](https://www.python.org/downloads/)
* Git

---

## 🚀 Setup Instructions

**1. Clone the repository**
```bash
git clone [https://github.com/pattanankm/2Handy.git](https://github.com/pattanankm/2Handy.git)
cd 2Handy