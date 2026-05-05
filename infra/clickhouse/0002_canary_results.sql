-- Canary sampler output. One row per sampled request_log row, per canary run.
--
-- ORDER BY (run_id, sampled_request_id) lets us pull a full run's rows in a
-- single contiguous read for win/loss/tie aggregation. Partitioning by
-- toYYYYMM(judged_at) matches request_log so retention windows align.

CREATE TABLE IF NOT EXISTS pulseroute.canary_results (
    judged_at           DateTime64(3, 'UTC'),
    run_id              String,
    sampled_request_id  String,
    tenant_id           LowCardinality(String),
    stable_model        LowCardinality(String),
    canary_model        LowCardinality(String),
    stable_score        Float32,
    canary_score        Float32,
    judgment            LowCardinality(String),
    judge_model         LowCardinality(String),
    window_start        DateTime64(3, 'UTC'),
    window_end          DateTime64(3, 'UTC'),
    sample_rate         Float32,
    seed                UInt32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(judged_at)
ORDER BY (run_id, sampled_request_id)
SETTINGS index_granularity = 8192;
