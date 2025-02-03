# Realtime Data Pipeline

In this part, we try to emulate common use case for realtime data pipeline:
1. CDC Ingestion
2. Realtime data processing

# CDC
docker-compose up -d

curl -i -X POST -H "Accept:application/json" -H "Content-Type:application/json" localhost:8083/connectors --data @postgres-cdc.json

curl -i -X POST -H "Accept:application/json" -H "Content-Type:application/json" localhost:8083/connectors --data @clickhouse-sinker-cdc.json

check data in both database

## Realtime analytics

### mastodon producer
docker build -t pyflink:1.14.0 .
python code/mastodon_listener.py --baseURL https://mastodon.social/public/local --public --enableKafka --quiet --watchdog 3

### flink
docker build -t pyflink:1.14.0 .

docker-compose up -d

python flink_sentiment.py

curl -i -X POST -H "Accept:application/json" -H "Content-Type:application/json" localhost:8083/connectors --data @clickhouse-sinker-flink.json

### ollama with deepseek
docker-compose up -d