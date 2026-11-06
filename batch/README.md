# Batch Pipeline

Batch pipeline using Airflow 3 orchestration with TOML-driven DAG generation.

## Runtime

```bash
docker compose -f docker-compose.batch.yaml up -d
```

Airflow UI: `http://localhost:8080`

Adhoc simulation DAGs:
- `adhoc_push_batch_data`: inserts new synthetic `users/products/transactions` into MySQL and PostgreSQL sources.
- `adhoc_push_realtime_data`: inserts new synthetic MySQL transaction data to trigger the CDC realtime path.

## Layout

- `dags/factory/dag_generator.py`: DAG factory that loads definitions from TOML.
- `dags/definitions/*.toml`: schedule + source/target/clickhouse configs.
- `src/extractors/`: PostgreSQL/MySQL extractors with server-side cursors.
- `src/loaders/parquet_loader.py`: chunked Parquet writer with date partitions.
- `src/transformers/clickhouse_transformer.py`: staging and DWH load SQL.
- `src/pipeline.py`: end-to-end orchestration for one batch run.

## Data Model

Source entities:
- `users`
- `products`
- `transactions`

Star schema output:
- `dwh.dim_user`
- `dwh.dim_product`
- `dwh.dim_payment_method`
- `dwh.dim_date`
- `dwh.fact_transactions`

## Environment Variables

- `MYSQL_DSN` e.g. `root:root@mysql:3306/app`
- `POSTGRES_DSN` e.g. `postgresql://postgres:postgres@postgres:5432/app`
- `CLICKHOUSE_URL` e.g. `http://clickhouse:8123`
- `CLICKHOUSE_USER` default `default`
- `CLICKHOUSE_PASSWORD` default empty
