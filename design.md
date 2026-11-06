# General
package python: uv.
container: docker.
OLAP: clickhouse.
Language: python >= 3.12, yaml / toml preferred, config file.
Safety: Ruff, pyrefly, Google docstring, OOP.
Memory usage: should fit 8 GB RAM for personal PC. only run 1 case if too much memory, if batch then dont streaming, vice versa.
Compatibility: Should run on linux / windows WSL2 and MacOS ARM.

# Batch
async source connection: mysql, postgres source. ss cursor.
airflow 3: dag factory generator. Daily & hourly schedule.
filesystem: garage fs.
Process: ELT from source, pyarrow store to parquet in fs, create external table clickhouse, create DWH star schema for analysis.

# Streaming
stream: redpanda, including schema registry.
Source: mysql db, mastodon.
db connector: redpanda connect. mysql source. Clickhouse target.
mastodon connector: Flink realtime, stream chat, pii redacted.
