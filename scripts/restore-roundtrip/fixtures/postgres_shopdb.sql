-- Run as app_owner (the database owner, not a superuser).
CREATE TABLE products (id serial PRIMARY KEY, name text NOT NULL, price numeric(10, 2));
INSERT INTO products (name, price) SELECT 'product ' || g, g * 1.25 FROM generate_series(1, 100) g;
CREATE TABLE orders (id serial PRIMARY KEY, product_id int REFERENCES products (id), qty int);
INSERT INTO orders (product_id, qty) SELECT 1 + (g % 100), g FROM generate_series(1, 300) g;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_reader;
