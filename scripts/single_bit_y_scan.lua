local MARIO = 0x80339E00
local GLOBAL_TIMER = 0x8032C694
local SOURCE_Y_WORD = 0xC5837800
local SOURCE_Y = -4207.0

local place_vi = tonumber(os.getenv("PLACE_VI") or "100")
local mutate_vi = tonumber(os.getenv("MUTATE_VI") or "101")
local stop_vi = tonumber(os.getenv("STOP_VI") or "190")
local bit_index = assert(tonumber(os.getenv("BIT_INDEX")), "BIT_INDEX is required")
local event_x = tonumber(os.getenv("EVENT_X") or "800")
local event_z = tonumber(os.getenv("EVENT_Z") or "1900")
local movie = nil
local vi = 0
local mutation_timer = nil
local previous_timer = nil
local previous_y = nil
local mutated_word = nil
local mutated_y = nil
local updates = 0
local min_y = math.huge
local max_y = -math.huge
local max_upward_step = -math.huge
local first_physics = nil
local landing = nil
local saw_nan = false

assert(bit_index >= 0 and bit_index <= 31 and bit_index == math.floor(bit_index), "BIT_INDEX must be 0..31")
assert(mutate_vi > place_vi, "MUTATE_VI must follow PLACE_VI")

local function u32(address)
    return Fuzzer:getUInt(address)
end

local function f32(address)
    return Fuzzer:getFloat(address)
end

local function set_position(x, y, z)
    Fuzzer:setFloat(MARIO + 0x3C, x)
    Fuzzer:setFloat(MARIO + 0x40, y)
    Fuzzer:setFloat(MARIO + 0x44, z)
    local object = u32(MARIO + 0x88)
    if object >= 0x80000000 and object < 0x80800000 then
        Fuzzer:setFloat(object + 0xA0, x)
        Fuzzer:setFloat(object + 0xA4, y)
        Fuzzer:setFloat(object + 0xA8, z)
    end
end

local function xor_single_bit(word, bit)
    local mask = 2 ^ bit
    if math.floor(word / mask) % 2 == 1 then
        return word - mask
    end
    return word + mask
end

local function observe_physics()
    if mutated_word == nil then
        return
    end
    local timer = u32(GLOBAL_TIMER)
    if timer == previous_timer then
        return
    end
    previous_timer = timer
    updates = timer - mutation_timer

    local y = f32(MARIO + 0x40)
    local floor_height = f32(MARIO + 0x70)
    local ceil_height = f32(MARIO + 0x6C)
    local vy = f32(MARIO + 0x4C)
    if y ~= y or floor_height ~= floor_height or ceil_height ~= ceil_height or vy ~= vy then
        saw_nan = true
    else
        min_y = math.min(min_y, y)
        max_y = math.max(max_y, y)
        if previous_y ~= nil then
            max_upward_step = math.max(max_upward_step, y - previous_y)
        end
        previous_y = y
    end

    if first_physics == nil and u32(MARIO + 0x40) ~= mutated_word then
        first_physics = {
            updates = updates,
            vi = vi,
            action = u32(MARIO + 0x0C),
            y = y,
            y_word = u32(MARIO + 0x40),
            vy = vy,
            floor_height = floor_height,
            ceil_height = ceil_height,
        }
    end
    if landing == nil and updates > 0 and y == y and floor_height == floor_height
        and math.abs(y - floor_height) < 0.01 then
        landing = {
            updates = updates,
            vi = vi,
            action = u32(MARIO + 0x0C),
            y = y,
            y_word = u32(MARIO + 0x40),
            vy = vy,
            floor_height = floor_height,
            ceil_height = ceil_height,
        }
    end
end

local function finite_or_zero(value)
    if value == math.huge or value == -math.huge or value ~= value then
        return 0.0
    end
    return value
end

local function row_or_default(row)
    return row or {
        updates = -1,
        vi = -1,
        action = 0,
        y = 0.0,
        y_word = 0,
        vy = 0.0,
        floor_height = 0.0,
        ceil_height = 0.0,
    }
end

local function finish()
    local first = row_or_default(first_physics)
    local land = row_or_default(landing)
    print(string.format(
        "BIT_SCAN,bit=%d,source_word=%08X,mutated_word=%08X,mutated_y=%.9g,instantaneous_delta=%.9g,saw_nan=%s,updates=%d,min_y=%.9g,max_y=%.9g,max_upward_step=%.9g,first_updates=%d,first_vi=%d,first_action=%08X,first_y=%.9g,first_y_word=%08X,first_vy=%.9g,first_floor=%.9g,first_ceil=%.9g,landing_updates=%d,landing_vi=%d,landing_action=%08X,landing_y=%.9g,landing_y_word=%08X,landing_vy=%.9g,landing_floor=%.9g,landing_ceil=%.9g",
        bit_index,
        SOURCE_Y_WORD,
        mutated_word or 0,
        finite_or_zero(mutated_y),
        finite_or_zero((mutated_y or SOURCE_Y) - SOURCE_Y),
        tostring(saw_nan),
        updates,
        finite_or_zero(min_y),
        finite_or_zero(max_y),
        finite_or_zero(max_upward_step),
        first.updates,
        first.vi,
        first.action,
        first.y,
        first.y_word,
        first.vy,
        first.floor_height,
        first.ceil_height,
        land.updates,
        land.vi,
        land.action,
        land.y,
        land.y_word,
        land.vy,
        land.floor_height,
        land.ceil_height
    ))
    io.stdout:flush()
    os.exit(0)
end

function Fuzzer.start(_)
    local path = assert(os.getenv("M64_PATH"), "M64_PATH is required")
    movie = assert(Fuzzer:openM64(path), "could not open M64 movie: " .. path)
end

function Fuzzer.get_inputs(_, inputs)
    inputs:setRaw(movie:getNextInput())
end

function Fuzzer.update(_)
    vi = vi + 1
    if vi == place_vi then
        Fuzzer:setUInt(MARIO + 0x0C, 0x0100088C)
        Fuzzer:setFloat(MARIO + 0x48, 0.0)
        Fuzzer:setFloat(MARIO + 0x4C, -20.0)
        Fuzzer:setFloat(MARIO + 0x50, 0.0)
        Fuzzer:setFloat(MARIO + 0x54, 0.0)
        set_position(event_x, SOURCE_Y, event_z)
        assert(u32(MARIO + 0x40) == SOURCE_Y_WORD, "source Y did not encode as C5837800")
    elseif vi == mutate_vi then
        local before = u32(MARIO + 0x40)
        assert(before == SOURCE_Y_WORD, string.format("unexpected pre-mutation Y word: %08X", before))
        mutated_word = xor_single_bit(before, bit_index)
        Fuzzer:setUInt(MARIO + 0x40, mutated_word)
        assert(u32(MARIO + 0x40) == mutated_word, "Y mutation did not persist")
        mutated_y = f32(MARIO + 0x40)
        mutation_timer = u32(GLOBAL_TIMER)
        previous_timer = mutation_timer
        previous_y = mutated_y
        min_y = mutated_y
        max_y = mutated_y
    end

    observe_physics()
    if vi >= stop_vi then
        finish()
    end
end
