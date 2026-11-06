CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS products (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  unit_price NUMERIC(12,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  product_id BIGINT NOT NULL REFERENCES products(id),
  quantity INTEGER NOT NULL,
  payment_method TEXT NOT NULL,
  status TEXT NOT NULL,
  currency CHAR(3) NOT NULL DEFAULT 'USD',
  transaction_ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO users (name, email)
SELECT 'Carla Nguyen', 'carla@example.com'
WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = 'carla@example.com');

INSERT INTO users (name, email)
SELECT 'Dimas Pratama', 'dimas@example.com'
WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = 'dimas@example.com');

INSERT INTO products (name, category, unit_price)
SELECT 'Mechanical Keyboard', 'Electronics', 89.90
WHERE NOT EXISTS (SELECT 1 FROM products WHERE name = 'Mechanical Keyboard');

INSERT INTO products (name, category, unit_price)
SELECT 'Notebook', 'Stationery', 5.25
WHERE NOT EXISTS (SELECT 1 FROM products WHERE name = 'Notebook');

INSERT INTO transactions (user_id, product_id, quantity, payment_method, status, currency, transaction_ts)
SELECT
  (SELECT id FROM users WHERE email = 'carla@example.com'),
  (SELECT id FROM products WHERE name = 'Mechanical Keyboard'),
  1,
  'credit_card',
  'completed',
  'USD',
  now()
WHERE NOT EXISTS (
  SELECT 1 FROM transactions
  WHERE user_id = (SELECT id FROM users WHERE email = 'carla@example.com')
    AND product_id = (SELECT id FROM products WHERE name = 'Mechanical Keyboard')
    AND quantity = 1
);

INSERT INTO transactions (user_id, product_id, quantity, payment_method, status, currency, transaction_ts)
SELECT
  (SELECT id FROM users WHERE email = 'dimas@example.com'),
  (SELECT id FROM products WHERE name = 'Notebook'),
  10,
  'e_wallet',
  'completed',
  'USD',
  now()
WHERE NOT EXISTS (
  SELECT 1 FROM transactions
  WHERE user_id = (SELECT id FROM users WHERE email = 'dimas@example.com')
    AND product_id = (SELECT id FROM products WHERE name = 'Notebook')
    AND quantity = 10
);
