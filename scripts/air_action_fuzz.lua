local MARIO = 0x80339E00
local BOOT_VI = tonumber(os.getenv("BOOT_VI") or "100")
local MAX_VI = tonumber(os.getenv("MAX_VI") or "10000")
local TRIAL_VI = tonumber(os.getenv("TRIAL_VI") or "24")
local SEED = tonumber(os.getenv("FUZZ_SEED") or "1")
local SOURCE_Y_WORD = 0xC5837800 -- -4207.0f
local movie
local vi = 0
local trial = 0
local age = 0
local last_y = nil
local max_step = -1e30
local max_y = -1e30
local max_step_trial = 0
local max_y_trial = 0
local input_x = 0
local input_y = 0
local input_buttons = 0

math.randomseed(SEED)

local sites = {
    { 800, 1900 }, { 200, 2000 },
    { 1102, 1682 }, { 424, 1959 },
}

-- Normal airborne actions visible or plausible around the incident's spinner contact.
local actions = {
    0x03000880, -- ACT_JUMP
    0x03000886, -- ACT_WALL_KICK_AIR
    0x01000887, -- ACT_SIDE_FLIP
    0x0100088C, -- ACT_FREEFALL
    0x000008A7, -- ACT_AIR_HIT_WALL
    0x018008AC, -- ACT_JUMP_KICK
    0x010208B6, -- ACT_SOFT_BONK
}

local function u32(address)
    return Fuzzer:getUInt(address)
end

local function f32(address)
    return Fuzzer:getFloat(address)
end

local function set_position(x, y_word, z)
    Fuzzer:setFloat(MARIO + 0x3C, x)
    Fuzzer:setUInt(MARIO + 0x40, y_word)
    Fuzzer:setFloat(MARIO + 0x44, z)
    local object = u32(MARIO + 0x88)
    if object >= 0x80000000 and object < 0x80800000 then
        Fuzzer:setFloat(object + 0xA0, x)
        Fuzzer:setUInt(object + 0xA4, y_word)
        Fuzzer:setFloat(object + 0xA8, z)
    end
end

local function begin_trial()
    trial = trial + 1
    age = 0
    local site = sites[math.random(#sites)]
    local x = site[1] + math.random(-220, 220)
    local z = site[2] + math.random(-220, 220)
    local vy = math.random(-75, 55)
    local speed = math.random(0, 55)
    local action = actions[math.random(#actions)]

    Fuzzer:setUInt(MARIO + 0x0C, action)
    Fuzzer:setUInt(MARIO + 0x10, action)
    Fuzzer:setShort(MARIO + 0x18, 0)
    Fuzzer:setShort(MARIO + 0x1A, 0)
    Fuzzer:setShort(MARIO + 0x2E, math.random(0, 65535))
    Fuzzer:setFloat(MARIO + 0x48, 0)
    Fuzzer:setFloat(MARIO + 0x4C, vy)
    Fuzzer:setFloat(MARIO + 0x50, 0)
    Fuzzer:setFloat(MARIO + 0x54, speed)
    Fuzzer:setUInt(MARIO + 0x60, 0)
    Fuzzer:setUInt(MARIO + 0x64, 0)
    Fuzzer:setUInt(MARIO + 0x68, 0)
    Fuzzer:setFloat(MARIO + 0x6C, 20000)
    Fuzzer:setFloat(MARIO + 0x70, -8191)
    Fuzzer:setUInt(MARIO + 0x78, 0)
    Fuzzer:setUInt(MARIO + 0x7C, 0)
    Fuzzer:setUInt(MARIO + 0x80, 0)
    Fuzzer:setUInt(MARIO + 0x84, 0)
    set_position(x, SOURCE_Y_WORD, z)
    last_y = -4207
end

function Fuzzer.start(_)
    local path = assert(os.getenv("M64_PATH"), "M64_PATH is required")
    movie = assert(Fuzzer:openM64(path), "could not open M64 movie: " .. path)
end

function Fuzzer.get_inputs(_, inputs)
    if vi < BOOT_VI then
        inputs:setRaw(movie:getNextInput())
        return
    end

    if math.random(3) == 1 then
        input_x = math.random(-128, 127)
        input_y = math.random(-128, 127)
        input_buttons = math.random(0, 15)
    end
    inputs:setRaw(0)
    inputs:setAnalogX(input_x)
    inputs:setAnalogY(input_y)
    if input_buttons % 2 >= 1 then inputs:setA() end
    if math.floor(input_buttons / 2) % 2 >= 1 then inputs:setB() end
    if math.floor(input_buttons / 4) % 2 >= 1 then inputs:setZ() end
    if math.floor(input_buttons / 8) % 2 >= 1 then inputs:setR() end
end

function Fuzzer.update(_)
    vi = vi + 1
    if vi == BOOT_VI then
        begin_trial()
    elseif vi > BOOT_VI then
        age = age + 1
        local y = f32(MARIO + 0x40)
        local step = y - last_y
        if step > max_step then
            max_step = step
            max_step_trial = trial
        end
        if y > max_y then
            max_y = y
            max_y_trial = trial
        end
        if step > 500 or y > -2000 then
            print(string.format(
                "SOFTWARE_UPWARP,seed=%d,trial=%d,age=%d,vi=%d,step=%.9g,x=%.9g,y=%.9g,z=%.9g,vy=%.9g,action=%08X,floor=%.9g,ceil=%.9g",
                SEED, trial, age, vi, step, f32(MARIO + 0x3C), y,
                f32(MARIO + 0x44), f32(MARIO + 0x4C), u32(MARIO + 0x0C),
                f32(MARIO + 0x70), f32(MARIO + 0x6C)))
            io.stdout:flush()
            os.exit(2)
        end
        last_y = y
        if age >= TRIAL_VI then
            begin_trial()
        end
    end

    if vi >= MAX_VI then
        print(string.format(
            "FUZZ_DONE,seed=%d,trials=%d,vis=%d,max_step=%.9g,max_step_trial=%d,max_y=%.9g,max_y_trial=%d",
            SEED, trial, vi - BOOT_VI, max_step, max_step_trial, max_y, max_y_trial))
        io.stdout:flush()
        os.exit(0)
    end
end
