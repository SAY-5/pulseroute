-- ClickHouse hot-path analytics schema.
-- Partitioned by month so we can drop old partitions without expensive deletes.
-- Primary key (tenant_id, timestamp) makes the most common admin query
-- (recent rows for a tenant) a single-partition contiguous read.

CREATE DATABASE IF NOT EXISTS pulseroute;

CREATE TABLE IF NOT EXISTS pulseroute.request_log (
    timestamp     DateTime64(3, 'UTC'),
    request_id    String,
    tenant_id     LowCardinality(String),
    model         LowCardinality(String),
    provider      LowCardinality(String),
    route_reason  LowCardinality(String),
    latency_ms    UInt32,
    ttft_ms       UInt32,
    tokens_in     UInt32,
    tokens_out    UInt32,
    cost_usd      Float32,
    cache_hit     UInt8,
    error_code    LowCardinality(String) DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (tenant_id, timestamp)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS pulseroute.eval_results (
    timestamp     DateTime64(3, 'UTC'),
    suite_id      LowCardinality(String),
    model         LowCardinality(String),
    task_id       String,
    score         Float32,
    latency_ms    UInt32,
    cost_usd      Float32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (suite_id, model, timestamp);

-- Hourly rollup for the overview dashboard. Refreshed by the materialised view.
CREATE TABLE IF NOT EXISTS pulseroute.request_log_hourly (
    hour          DateTime,
    tenant_id     LowCardinality(String),
    model         LowCardinality(String),
    requests      AggregateFunction(count),
    cache_hits    AggregateFunction(sum, UInt32),
    cost_usd      AggregateFunction(sum, Float32),
    avg_latency   AggregateFunction(avg, UInt32)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(hour)
ORDER BY (tenant_id, hour, model);

CREATE MATERIALIZED VIEW IF NOT EXISTS pulseroute.request_log_hourly_mv
TO pulseroute.request_log_hourly AS
SELECT
    toStartOfHour(timestamp) AS hour,
    tenant_id,
    model,
    countState() AS requests,
    sumState(toUInt32(cache_hit)) AS cache_hits,
    sumState(cost_usd) AS cost_usd,
    avgState(latency_ms) AS avg_latency
FROM pulseroute.request_log
GROUP BY hour, tenant_id, model;
