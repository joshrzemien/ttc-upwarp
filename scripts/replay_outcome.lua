local MARIO = 0x80339E00
local GLOBAL_TIMER = 0x8032C694
local CURR_LEVEL_NUM = 0x8032CE98
local G_MARIO_PLATFORM = 0x8032FED4
local TTC_LEVEL_NUM = 14
local SPINNER_X = 1102.0
local SPINNER_Y = -4207.0
local SPINNER_Z = 1682.0

local movie = nil
local vi = 0
local polls = 0
local done_vis = 0
local max_vis = tonumber(os.getenv("MAX_VIS") or "1200")
local previous_timer = nil
local previous_y = nil
local entered_ttc = false
local exited_ttc = false
local ttc_updates = 0
local min_y = math.huge
local max_y = -math.huge
local max_upward_step = -math.huge
local nearest_distance = math.huge
local nearest = nil
local upper_platform_updates = 0

local function u32(address)
    return Fuzzer:getUInt(address)
end

local function f32(address)
    return Fuzzer:getFloat(address)
end

local function current_level()
    return math.floor(u32(CURR_LEVEL_NUM) / 0x10000)
end

local function observe_update()
    local timer = u32(GLOBAL_TIMER)
    if timer == previous_timer then
        return
    end
    previous_timer = timer

    local level = current_level()
    if level ~= TTC_LEVEL_NUM then
        if entered_ttc then
            exited_ttc = true
        end
        previous_y = nil
        return
    end

    entered_ttc = true
    ttc_updates = ttc_updates + 1

    local x = f32(MARIO + 0x3C)
    local y = f32(MARIO + 0x40)
    local z = f32(MARIO + 0x44)
    min_y = math.min(min_y, y)
    max_y = math.max(max_y, y)

    if previous_y ~= nil then
        max_upward_step = math.max(max_upward_step, y - previous_y)
    end
    previous_y = y

    if y >= -2520.0 and y <= -2450.0 then
        upper_platform_updates = upper_platform_updates + 1
    end

    local dx = x - SPINNER_X
    local dy = y - SPINNER_Y
    local dz = z - SPINNER_Z
    local distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance < nearest_distance then
        nearest_distance = distance
        nearest = {
            vi = vi,
            polls = polls,
            timer = timer,
            action = u32(MARIO + 0x0C),
            x = x,
            y = y,
            z = z,
            vx = f32(MARIO + 0x48),
            vy = f32(MARIO + 0x4C),
            vz = f32(MARIO + 0x50),
            floor_height = f32(MARIO + 0x70),
            ceil_height = f32(MARIO + 0x6C),
            floor = u32(MARIO + 0x68),
            platform = u32(G_MARIO_PLATFORM),
        }
    end
end

local function finite_or_zero(value)
    if value == math.huge or value == -math.huge then
        return 0.0
    end
    return value
end

local function finish()
    local input_position = movie:getPosition()
    local level = current_level()
    local nearest_row = nearest or {
        vi = 0,
        polls = 0,
        timer = 0,
        action = 0,
        x = 0.0,
        y = 0.0,
        z = 0.0,
        vx = 0.0,
        vy = 0.0,
        vz = 0.0,
        floor_height = 0.0,
        ceil_height = 0.0,
        floor = 0,
        platform = 0,
    }
    print(string.format(
        "OUTCOME,vi=%d,polls=%d,input=%d,finished=%s,level=%d,entered_ttc=%s,exited_ttc=%s,ttc_updates=%d,min_y=%.9g,max_y=%.9g,max_upward_step=%.9g,upper_platform_updates=%d,nearest_distance=%.9g,nearest_vi=%d,nearest_polls=%d,nearest_timer=%d,nearest_action=%08X,nearest_x=%.9g,nearest_y=%.9g,nearest_z=%.9g,nearest_vx=%.9g,nearest_vy=%.9g,nearest_vz=%.9g,nearest_floor_height=%.9g,nearest_ceil_height=%.9g,nearest_floor=%08X,nearest_platform=%08X,final_action=%08X,final_x=%.9g,final_y=%.9g,final_z=%.9g",
        vi,
        polls,
        input_position,
        tostring(movie:isFinished()),
        level,
        tostring(entered_ttc),
        tostring(exited_ttc),
        ttc_updates,
        finite_or_zero(min_y),
        finite_or_zero(max_y),
        finite_or_zero(max_upward_step),
        upper_platform_updates,
        finite_or_zero(nearest_distance),
        nearest_row.vi,
        nearest_row.polls,
        nearest_row.timer,
        nearest_row.action,
        nearest_row.x,
        nearest_row.y,
        nearest_row.z,
        nearest_row.vx,
        nearest_row.vy,
        nearest_row.vz,
        nearest_row.floor_height,
        nearest_row.ceil_height,
        nearest_row.floor,
        nearest_row.platform,
        u32(MARIO + 0x0C),
        f32(MARIO + 0x3C),
        f32(MARIO + 0x40),
        f32(MARIO + 0x44)
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
    polls = polls + 1
end

function Fuzzer.update(_)
    vi = vi + 1
    observe_update()

    if movie:isFinished() then
        done_vis = done_vis + 1
    end
    if done_vis >= 12 or vi >= max_vis then
        finish()
    end
end
