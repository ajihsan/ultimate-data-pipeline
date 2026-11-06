CREATE DATABASE IF NOT EXISTS dwh;

CREATE TABLE IF NOT EXISTS dwh.dim_user
(
    user_key UInt64,
    user_id UInt64,
    source_system String,
    user_name String,
    user_email String,
    _updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(_updated_at)
ORDER BY user_key;

CREATE TABLE IF NOT EXISTS dwh.dim_product
(
    product_key UInt64,
    product_id UInt64,
    source_system String,
    product_name String,
    product_category String,
    unit_price Decimal(18, 2),
    _updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(_updated_at)
ORDER BY product_key;

CREATE TABLE IF NOT EXISTS dwh.dim_payment_method
(
    payment_method_key UInt64,
    payment_method String,
    _updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(_updated_at)
ORDER BY payment_method_key;

CREATE TABLE IF NOT EXISTS dwh.dim_date
(
    date_key UInt32,
    full_date Date,
    year UInt16,
    quarter UInt8,
    month UInt8,
    day UInt8,
    day_of_week UInt8
)
ENGINE = ReplacingMergeTree
ORDER BY date_key;

CREATE TABLE IF NOT EXISTS dwh.fact_transactions
(
    transaction_key UInt64,
    transaction_id UInt64,
    user_key UInt64,
    product_key UInt64,
    payment_method_key UInt64,
    date_key UInt32,
    quantity UInt32,
    unit_price Decimal(18, 2),
    gross_amount Decimal(18, 2),
    currency FixedString(3),
    transaction_status String,
    source_system String,
    transaction_ts DateTime,
    extracted_at DateTime,
    _inserted_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(_inserted_at)
ORDER BY (source_system, transaction_id)
PARTITION BY toYYYYMM(transaction_ts);
