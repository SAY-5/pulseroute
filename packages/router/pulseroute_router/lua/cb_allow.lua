-- cb_allow.lua
-- Implements the "allow this request through?" check, with the side-effect
-- that an OPEN breaker whose cooldown has elapsed transitions atomically to
-- HALF_OPEN so exactly one probe slips through across all pods.
--
-- KEYS[1] = state key
-- KEYS[2] = opened_at key
--
-- ARGV[1] = now (float seconds)
-- ARGV[2] = half_open_after_s
--
-- Returns: ARRAY[state, allowed]   allowed = "1" | "0"

local now = tonumber(ARGV[1])
local half_open_after_s = tonumber(ARGV[2])

local state_key = KEYS[1]
local opened_at_key = KEYS[2]

local state = redis.call("GET", state_key)
if not state or state == false or state == "" then
  state = "closed"
end

if state == "open" then
  local opened_at = tonumber(redis.call("GET", opened_at_key) or "0")
  if (now - opened_at) >= half_open_after_s then
    redis.call("SET", state_key, "half_open")
    state = "half_open"
    return { state, "1" }
  end
  return { state, "0" }
end

-- closed and half_open both allow. (half_open allows the probe.)
return { state, "1" }
