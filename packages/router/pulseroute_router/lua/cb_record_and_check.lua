-- cb_record_and_check.lua
-- Atomically records a (timestamp, success_bool) event into the rolling
-- window, evicts events older than the window, evaluates the OPEN predicate
-- on a failure, and returns the (possibly-updated) state as a string.
--
-- Single round trip per check — the Python client passes
-- (events_key, state_key, opened_at_key) as KEYS and tunables as ARGV.
--
-- KEYS[1] = events sorted-set:   members "<ts>:<is_error>" with score = ts
-- KEYS[2] = state key:           string "closed" | "open" | "half_open"
-- KEYS[3] = opened_at key:       string (numeric ts) when state = open
--
-- ARGV[1] = now (float seconds, e.g. 1714000000.123)
-- ARGV[2] = is_error (0 | 1)
-- ARGV[3] = window_s
-- ARGV[4] = min_requests
-- ARGV[5] = error_rate_threshold
-- ARGV[6] = half_open_after_s
--
-- Returns: ARRAY[state, n_events, n_errors]

local now = tonumber(ARGV[1])
local is_error = tonumber(ARGV[2])
local window_s = tonumber(ARGV[3])
local min_requests = tonumber(ARGV[4])
local threshold = tonumber(ARGV[5])
local half_open_after_s = tonumber(ARGV[6])

local events_key = KEYS[1]
local state_key = KEYS[2]
local opened_at_key = KEYS[3]

-- Append the new event with a tiny disambiguator so two events at the same
-- timestamp do not stomp each other in the sorted set. We use INCR on a
-- small counter for the disambiguator (cheap, unique per process, safe
-- against same-microsecond collisions across pods).
local seq = redis.call("INCR", events_key .. ":seq")
local member = string.format("%.6f:%d:%d", now, is_error, seq)
redis.call("ZADD", events_key, now, member)

-- Evict stale events.
redis.call("ZREMRANGEBYSCORE", events_key, "-inf", now - window_s)
-- Cap retention so a buggy caller cannot grow the set without bound.
local hard_cap = 10000
local card = redis.call("ZCARD", events_key)
if card > hard_cap then
  redis.call("ZREMRANGEBYRANK", events_key, 0, card - hard_cap - 1)
end

-- Read current state. Default to "closed" if missing.
local state = redis.call("GET", state_key)
if not state or state == false or state == "" then
  state = "closed"
end

-- A pending OPEN -> HALF_OPEN transition is handled by the dedicated
-- allow_lua script. Here we only handle CLOSED -> OPEN and HALF_OPEN -> OPEN.

local n_events = redis.call("ZCARD", events_key)
local n_errors = 0
local members = redis.call("ZRANGE", events_key, 0, -1)
for i = 1, #members do
  -- member shape: "<ts>:<is_error>:<sha>"
  local _, _, e = string.find(members[i], "^[^:]+:(%d+):")
  if e == "1" then
    n_errors = n_errors + 1
  end
end

if state == "half_open" then
  if is_error == 1 then
    redis.call("SET", state_key, "open")
    redis.call("SET", opened_at_key, tostring(now))
    state = "open"
  else
    redis.call("SET", state_key, "closed")
    redis.call("DEL", opened_at_key)
    state = "closed"
  end
elseif state == "closed" and is_error == 1 then
  if n_events >= min_requests and (n_errors / n_events) >= threshold then
    redis.call("SET", state_key, "open")
    redis.call("SET", opened_at_key, tostring(now))
    state = "open"
  end
end

return { state, tostring(n_events), tostring(n_errors) }
