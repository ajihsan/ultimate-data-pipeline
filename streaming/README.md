# Streaming Pipeline - Step by Step

Real-time data pipeline using Redpanda, Flink, and ClickHouse.

## Architecture

```
Mastodon API → Redpanda Connect → Redpanda → Flink → ClickHouse
MySQL         → Redpanda Connect → Redpanda → ClickHouse
```

## Prerequisites

- Docker & Docker Compose
- Python 3.12+
- uv package manager

---

## Step 1: Environment Setup

Create `.env` file in project root:

```bash
touch .env
```

Update with your configuration:

```env
# MySQL (for CDC)
MYSQL_DSN=user:password@mysql:3306/mydb
MYSQL_DATABASE=mydb

# Redpanda
REDPANDA_BROKERS=redpanda:9092
SCHEMA_REGISTRY_URL=http://redpanda:8081

# ClickHouse
CLICKHOUSE_DSN=clickhouse://default:@clickhouse:8123/default
CLICKHOUSE_PASSWORD=

# Mastodon (optional - for testing)
MASTODON_STREAM_URL=https://mastodon.social/api/v1/streaming/public
MASTODON_ACCESS_TOKEN=your_access_token
```

---

## Step 2: Start Services

```bash
docker compose -f docker-compose.streaming.yaml up -d
```

Verify services are running:

```bash
docker ps
```

Expected containers:
- `redpanda` - Message broker on port 9092
- `redpanda-connect` - Connectors
- `clickhouse` - OLAP on port 8123
- `flink-jobmanager` - Flink UI on port 8081
- `flink-taskmanager` - Flink worker

---

## Step 3: Verify Redpanda

```bash
# Check Redpanda is healthy
curl -s http://localhost:9644/metrics | head -20

# List topics
docker exec redpanda rpk topic list
```

---

## Step 4: Test MySQL CDC

### 4.1 Start a test MySQL container

```bash
docker run -d \
  --name mysql-test \
  -e MYSQL_ROOT_PASSWORD=root \
  -e MYSQL_DATABASE=testdb \
  -p 3306:3306 \
  mysql:8.0
```

### 4.2 Enable binlog

```bash
docker exec mysql-test bash -c "
  echo 'server-id=1' >> /etc/mysql/conf.d/server-id.cnf
  echo 'log_bin=mysql-bin' >> /etc/mysql/conf.d/server-id.cnf
  echo 'binlog_format=ROW' >> /etc/mysql/conf.d/server-id.cnf
  mysql -proot -e \"GRANT REPLICATION SLAVE ON *.* TO 'root'@'%';\"
"
docker restart mysql-test
```

### 4.3 Create test table

```bash
docker exec mysql-test mysql -proot -e "
  CREATE TABLE users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100),
    email VARCHAR(100)
  );
"
```

### 4.4 Check topic created

```bash
docker exec redpanda rpk topic list
# Should see: db.cdc.users
```

### 4.5 Insert data

```bash
docker exec mysql-test mysql -proot -e "
  INSERT INTO testdb.users (name, email) VALUES ('John', 'john@test.com');
"
```

### 4.6 Verify data in ClickHouse

```bash
docker exec clickhouse clickhouse-client -q "
  SELECT * FROM streaming.staging_cdc_events;
"
```

---

## Step 5: Test Mastodon Stream

### 5.1 Update env with Mastodon token

```env
MASTODON_ACCESS_TOKEN=your_token_here
```

### 5.2 Restart connector

```bash
docker compose -f docker-compose.streaming.yaml restart redpanda-connect
```

### 5.3 Check topic

```bash
docker exec redpanda rpk topic list
# Should see: stream.mastodon.raw
```

### 5.4 Verify data in ClickHouse

```bash
docker exec clickhouse clickhouse-client -q "
  SELECT * FROM streaming.staging_mastodon LIMIT 1;
"
```

### 5.5 Sink raw Mastodon topic to ClickHouse for comparison

Use connector config:
- `streaming/connectors/mastodon_raw_clickhouse.yaml`

Run the sink from the connect container:

```bash
docker exec -it redpanda-connect rpk connect run /connectors/mastodon_raw_clickhouse.yaml
```

Then verify raw table and comparison view:

```bash
docker exec clickhouse clickhouse-client -q "
  SELECT event_id, content_raw
  FROM streaming.staging_mastodon_raw
  ORDER BY _inserted_at DESC
  LIMIT 5;
"

docker exec clickhouse clickhouse-client -q "
  SELECT event_id, content_raw, content_redacted, pii_detected
  FROM streaming.vw_mastodon_raw_vs_redacted
  ORDER BY coalesce(raw_timestamp_ms, redacted_timestamp_ms) DESC
  LIMIT 10;
"
```

---

## Step 6: Test Flink Job

### 6.1 Prepare Flink job code

```bash
ls streaming/flink
```

### 6.2 Submit job

```bash
docker exec -it flink-jobmanager bash
# Run your job from /opt/flink/usrlib or mount your Python job script as needed.
```

### 6.3 Check Flink UI

Open http://localhost:8084 to see running job.

---

## Step 7: Verify End-to-End

### 7.1 Check staging tables

```bash
docker exec clickhouse clickhouse-client -q "
  SELECT count() FROM streaming.staging_cdc_events;
  SELECT count() FROM streaming.staging_mastodon;
"
```

### 7.2 Check DWH tables

```bash
docker exec clickhouse clickhouse-client -q "
  SELECT * FROM dwh.dim_accounts LIMIT 5;
  SELECT * FROM dwh.fact_mastodon_events LIMIT 5;
"
```

---

## Stop Services

```bash
docker compose -f docker-compose.streaming.yaml down
```

To remove volumes:

```bash
docker compose -f docker-compose.streaming.yaml down -v
```

---

## Troubleshooting

### Check connector logs

```bash
docker logs redpanda-connect
```

### Check Redpanda topics

```bash
docker exec redpanda rpk topic describe db.cdc.users
```

### Check ClickHouse logs

```bash
docker logs clickhouse
```

### Restart specific service

```bash
docker compose -f docker-compose.streaming.yaml restart redpanda-connect
```

---

## Quick Test (Without MySQL/Mastodon)

Test with sample data:

```bash
# Produce test message to Redpanda
docker exec redpanda rpk topic create db.cdc.users

echo '{"operation":"insert","table":"users","after":{"id":1,"name":"test"}}' | \
docker exec -i redpanda rpk topic produce db.cdc.users
```
