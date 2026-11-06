CREATE DATABASE IF NOT EXISTS staging;

CREATE TABLE IF NOT EXISTS staging.staging_transactions
(
    transaction_id UInt64,
    user_id UInt64,
    user_name String,
    user_email String,
    product_id UInt64,
    product_name String,
    product_category String,
    quantity UInt32,
    unit_price Decimal(18, 2),
    gross_amount Decimal(18, 2),
    currency FixedString(3),
    payment_method String,
    transaction_status String,
    transaction_ts DateTime,
    source_system String,
    extracted_at DateTime,
    batch_date Date
)
ENGINE = MergeTree
ORDER BY (batch_date, source_system, transaction_id)
PARTITION BY batch_date;
