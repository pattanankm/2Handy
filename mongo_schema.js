use main_db;

// Create collections
db.createCollection("products");
db.createCollection("reviews");

// Indexes
db.products.createIndex({ category: 1 });
db.reviews.createIndex({ product_id: 1 });
db.reviews.createIndex({ user_id: 1 });


// ===== ข้อ 7: Products =====

db.products.insertMany([
    {
        _id: "P001",
        name: "iPhone 17",
        category: "electronics",
        price: 39900,
        attributes: {
            brand: "Apple",
            storage: "256GB",
            color: "Black"
        }
    },
    {
        _id: "P002",
        name: "T-Shirt",
        category: "clothing",
        price: 590,
        attributes: {
            brand: "Example",
            size: "L",
            material: "Cotton"
        }
    }
]);


// ===== ข้อ 8: Reviews =====

db.reviews.insertMany([
    {
        _id: "R001",
        product_id: "P001",
        user_id: 1,
        rating: 5,
        comment: "Very good product",
        created_at: new Date()
    },
    {
        _id: "R002",
        product_id: "P001",
        user_id: 2,
        rating: 4,
        comment: "Good product",
        created_at: new Date()
    }
]);