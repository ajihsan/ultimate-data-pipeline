CREATE DATABASE IF NOT EXISTS app;
USE app;

CREATE TABLE IF NOT EXISTS users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(120) NOT NULL,
  email VARCHAR(255) NOT NULL UNIQUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(120) NOT NULL,
  category VARCHAR(80) NOT NULL,
  unit_price DECIMAL(12,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  product_id BIGINT NOT NULL,
  quantity INT NOT NULL,
  payment_method VARCHAR(40) NOT NULL,
  status VARCHAR(40) NOT NULL,
  currency CHAR(3) NOT NULL DEFAULT 'USD',
  transaction_ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (product_id) REFERENCES products(id)
);

INSERT INTO users (name, email)
SELECT 'Alice Johnson', 'alice@example.com'
WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = 'alice@example.com');

INSERT INTO users (name, email)
SELECT 'Bob Lee', 'bob@example.com'
WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = 'bob@example.com');

INSERT INTO products (name, category, unit_price)
SELECT 'Wireless Mouse', 'Electronics', 25.50
WHERE NOT EXISTS (SELECT 1 FROM products WHERE name = 'Wireless Mouse');

INSERT INTO products (name, category, unit_price)
SELECT 'Office Chair', 'Furniture', 180.00
WHERE NOT EXISTS (SELECT 1 FROM products WHERE name = 'Office Chair');

INSERT INTO transactions (user_id, product_id, quantity, payment_method, status, currency, transaction_ts)
SELECT
  (SELECT id FROM users WHERE email = 'alice@example.com'),
  (SELECT id FROM products WHERE name = 'Wireless Mouse'),
  2,
  'credit_card',
  'completed',
  'USD',
  NOW()
WHERE NOT EXISTS (
  SELECT 1 FROM transactions
  WHERE user_id = (SELECT id FROM users WHERE email = 'alice@example.com')
    AND product_id = (SELECT id FROM products WHERE name = 'Wireless Mouse')
    AND quantity = 2
);

INSERT INTO transactions (user_id, product_id, quantity, payment_method, status, currency, transaction_ts)
SELECT
  (SELECT id FROM users WHERE email = 'bob@example.com'),
  (SELECT id FROM products WHERE name = 'Office Chair'),
  1,
  'bank_transfer',
  'completed',
  'USD',
  NOW()
WHERE NOT EXISTS (
  SELECT 1 FROM transactions
  WHERE user_id = (SELECT id FROM users WHERE email = 'bob@example.com')
    AND product_id = (SELECT id FROM products WHERE name = 'Office Chair')
    AND quantity = 1
);
