local MARIO = 0x80339E00
local GLOBAL_TIMER = 0x8032C694
local CURR_LEVEL_NUM = 0x8032CE98
local G_MARIO_PLATFORM = 0x8032FED4
local TARGET_X = 1102.0
local TARGET_Y = -4207.0
local TARGET_Z = 1682.0
local TTC_LEVEL_NUM = 14

local movie = nil
local candidates = {}
local candidate_index = 0
local phase = "initializing"
local baseline = nil
local vi = 0
local polls = 0
local load_requests = 0
local step_index = 0
local observed_steps = 0
local last_timer = nil
local nearest = nil
local max_y = -math.huge
local max_upward_step = -math.huge
local previous_y = nil
local top_contact_updates = 0
local exited_ttc = false

local function u32(address)
    return Fuzzer:getUInt(address)
end

local function f32(address)
    return Fuzzer:getFloat(address)
end

local function low_s16(value)
    local result = value % 0x10000
    if result >= 0x8000 then
        result = result - 0x10000
    end
    return result
end

local function current_level()
    return math.floor(u32(CURR_LEVEL_NUM) / 0x10000)
end

local function same_baseline()
    return u32(GLOBAL_TIMER) == baseline.timer
        and u32(MARIO + 0x0C) == baseline.action
        and u32(MARIO + 0x3C) == baseline.x_word
        and u32(MARIO + 0x40) == baseline.y_word
        and u32(MARIO + 0x44) == baseline.z_word
end

local function parse_sequence(spec)
    local sequence = {}
    for duration, buttons, x, y in string.gmatch(spec, "(%d+):(%d+):(-?%d+):(-?%d+)") do
        for _ = 1, tonumber(duration) do
            table.insert(sequence, {
                buttons = tonumber(buttons),
                x = tonumber(x),
                y = tonumber(y),
            })
        end
    end
    assert(#sequence > 0, "empty sequence: " .. spec)
    return sequence
end

local function load_candidates()
    local path = assert(os.getenv("SEQUENCES_FILE"), "SEQUENCES_FILE is required")
    local file = assert(io.open(path, "r"))
    for line in file:lines() do
        if line ~= "" and string.sub(line, 1, 1) ~= "#" then
            local separator = string.find(line, "|", 1, true)
            assert(separator ~= nil, "invalid sequence row: " .. line)
            local id = string.sub(line, 1, separator - 1)
            local spec = string.sub(line, separator + 1)
            table.insert(candidates, { id = id, sequence = parse_sequence(spec) })
        end
    end
    file:close()
    assert(#candidates > 0, "sequence file is empty")
end

local function set_input(inputs, value)
    inputs:setRaw(0)
    inputs:setAnalogX(value.x)
    inputs:setAnalogY(value.y)
    if value.buttons % 2 >= 1 then inputs:setA() end
    if math.floor(value.buttons / 2) % 2 >= 1 then inputs:setB() end
    if math.floor(value.buttons / 4) % 2 >= 1 then inputs:setZ() end
    if math.floor(value.buttons / 8) % 2 >= 1 then inputs:setR() end
end

local function reset_observation()
    step_index = 0
    observed_steps = 0
    last_timer = baseline.timer
    nearest = nil
    max_y = -math.huge
    max_upward_step = -math.huge
    previous_y = baseline.y
    top_contact_updates = 0
    exited_ttc = false
end

local function begin_next_branch()
    candidate_index = candidate_index + 1
    if candidate_index > #candidates then
        print(string.format(
            "SEQUENCE_DONE,candidates=%d,load_requests=%d,vi=%d,polls=%d",
            #candidates, load_requests, vi, polls
        ))
        io.stdout:flush()
        os.exit(0)
    end
    reset_observation()
    phase = "ready"
end

local function observe_state()
    if current_level() ~= TTC_LEVEL_NUM then
        exited_ttc = true
        previous_y = nil
        return false
    end
    local x = f32(MARIO + 0x3C)
    local y = f32(MARIO + 0x40)
    local z = f32(MARIO + 0x44)
    local dx = x - TARGET_X
    local dy = y - TARGET_Y
    local dz = z - TARGET_Z
    local distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    if nearest == nil or distance < nearest.distance then
        nearest = {
            distance = distance,
            step = observed_steps,
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
    max_y = math.max(max_y, y)
    if previous_y ~= nil then
        max_upward_step = math.max(max_upward_step, y - previous_y)
    end
    previous_y = y
    if math.abs(f32(MARIO + 0x70) - TARGET_Y) < 0.01
        and math.abs(y - TARGET_Y) < 0.01 then
        top_contact_updates = top_contact_updates + 1
    end
    return true
end

local function finish_branch()
    local candidate = candidates[candidate_index]
    local closest = nearest or {
        distance = math.huge,
        step = 0,
        action = 0,
        x = 0,
        y = 0,
        z = 0,
        vx = 0,
        vy = 0,
        vz = 0,
        floor_height = 0,
        ceil_height = 0,
        floor = 0,
        platform = 0,
    }
    print(string.format(
        "SEQUENCE,id=%s,index=%d,steps=%d,observed_steps=%d,exited_ttc=%s,final_level=%d,nearest_distance=%.9g,nearest_step=%d,nearest_action=%08X,nearest_x=%.9g,nearest_y=%.9g,nearest_z=%.9g,nearest_vx=%.9g,nearest_vy=%.9g,nearest_vz=%.9g,nearest_floor_height=%.9g,nearest_ceil_height=%.9g,nearest_floor=%08X,nearest_platform=%08X,max_y=%.9g,max_upward_step=%.9g,top_contact_updates=%d,final_action=%08X,final_x=%.9g,final_y=%.9g,final_z=%.9g,final_vx=%.9g,final_vy=%.9g,final_vz=%.9g,final_face_yaw=%d,final_floor_height=%.9g,final_ceil_height=%.9g,final_floor=%08X,final_platform=%08X",
        candidate.id,
        candidate_index,
        #candidate.sequence,
        observed_steps,
        tostring(exited_ttc),
        current_level(),
        closest.distance,
        closest.step,
        closest.action,
        closest.x,
        closest.y,
        closest.z,
        closest.vx,
        closest.vy,
        closest.vz,
        closest.floor_height,
        closest.ceil_height,
        closest.floor,
        closest.platform,
        max_y,
        max_upward_step,
        top_contact_updates,
        u32(MARIO + 0x0C),
        f32(MARIO + 0x3C),
        f32(MARIO + 0x40),
        f32(MARIO + 0x44),
        f32(MARIO + 0x48),
        f32(MARIO + 0x4C),
        f32(MARIO + 0x50),
        low_s16(u32(MARIO + 0x2C)),
        f32(MARIO + 0x70),
        f32(MARIO + 0x6C),
        u32(MARIO + 0x68),
        u32(G_MARIO_PLATFORM)
    ))
    io.stdout:flush()
end

local function request_restore()
    local snapshot_path = assert(os.getenv("SNAPSHOT_PATH"), "SNAPSHOT_PATH is required")
    Fuzzer:loadState(snapshot_path)
    load_requests = load_requests + 1
    phase = "loading"
end

function Fuzzer.start(_)
    local movie_path = assert(os.getenv("M64_PATH"), "M64_PATH is required")
    movie = assert(Fuzzer:openM64(movie_path), "could not open M64 movie: " .. movie_path)
    load_candidates()
end

function Fuzzer.get_inputs(_, inputs)
    polls = polls + 1
    if phase == "ready" or phase == "running" then
        local candidate = candidates[candidate_index]
        step_index = step_index + 1
        assert(step_index <= #candidate.sequence, "input poll exceeded sequence")
        set_input(inputs, candidate.sequence[step_index])
        phase = "running"
    else
        inputs:setRaw(0)
    end
end

function Fuzzer.update(_)
    vi = vi + 1
    if phase == "initializing" then
        baseline = {
            timer = u32(GLOBAL_TIMER),
            action = u32(MARIO + 0x0C),
            x_word = u32(MARIO + 0x3C),
            y_word = u32(MARIO + 0x40),
            y = f32(MARIO + 0x40),
            z_word = u32(MARIO + 0x44),
        }
        begin_next_branch()
        return
    end

    if phase == "running" and u32(GLOBAL_TIMER) ~= last_timer then
        last_timer = u32(GLOBAL_TIMER)
        observed_steps = observed_steps + 1
        local in_ttc = observe_state()
        if not in_ttc or observed_steps >= #candidates[candidate_index].sequence then
            finish_branch()
            if candidate_index >= #candidates then
                begin_next_branch()
            else
                request_restore()
            end
        end
        return
    end

    if phase == "loading" and same_baseline() then
        begin_next_branch()
    end
end
