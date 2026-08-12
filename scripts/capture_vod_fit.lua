local MARIO = 0x80339E00
local GLOBAL_TIMER = 0x8032C694
local LAKITU = 0x8033B328
local CAMERA_PTR = 0x8033B860
local ACT_FREEFALL = 0x0100088C
local SOURCE_Y_WORD = 0xC5837800
local FLIPPED_Y_WORD = 0xC4837800
local RDRAM_BASE = 0x80000000
local RDRAM_LIMIT = 0x80800000

local SCENARIO = os.getenv("SCENARIO") or "reachable_no_mutation"
local MAX_VI = tonumber(os.getenv("MAX_VI") or "220")
local CAPTURE_START_VI = tonumber(os.getenv("CAPTURE_START_VI") or "1")
local CAPTURE_END_VI = tonumber(os.getenv("CAPTURE_END_VI") or tostring(MAX_VI))
local PLACE_VI = tonumber(os.getenv("PLACE_VI") or "100")
local MUTATE_VI = tonumber(os.getenv("MUTATE_VI") or "101")
local X = tonumber(os.getenv("EVENT_X") or "800")
local Y = tonumber(os.getenv("EVENT_Y") or "-4207")
local Z = tonumber(os.getenv("EVENT_Z") or "1900")
local movie
local vi = 0
local screenshots = 0
local writes = 0
local saw_place = false
local saw_mutation = false

assert(SCENARIO == "reachable_no_mutation"
    or SCENARIO == "synthetic_no_flip"
    or SCENARIO == "synthetic_bit_clear",
    "unsupported SCENARIO: " .. SCENARIO)
assert(MAX_VI >= CAPTURE_END_VI and CAPTURE_START_VI >= 1
    and CAPTURE_START_VI <= CAPTURE_END_VI, "invalid capture interval")
if SCENARIO ~= "reachable_no_mutation" then
    assert(PLACE_VI >= 1 and MUTATE_VI > PLACE_VI, "invalid synthetic event VIs")
end

local function u32(address)
    return Fuzzer:getUInt(address)
end

local function f32(address)
    return Fuzzer:getFloat(address)
end

local function valid_ptr(address)
    return address >= RDRAM_BASE and address < RDRAM_LIMIT
end

local function set_position(x, y, z)
    Fuzzer:setFloat(MARIO + 0x3C, x)
    Fuzzer:setFloat(MARIO + 0x40, y)
    Fuzzer:setFloat(MARIO + 0x44, z)
    local object = u32(MARIO + 0x88)
    if valid_ptr(object) then
        Fuzzer:setFloat(object + 0xA0, x)
        Fuzzer:setFloat(object + 0xA4, y)
        Fuzzer:setFloat(object + 0xA8, z)
    end
end

local function inject_freefall()
    Fuzzer:setUInt(MARIO + 0x0C, ACT_FREEFALL)
    Fuzzer:setFloat(MARIO + 0x48, 0)
    Fuzzer:setFloat(MARIO + 0x4C, -20)
    Fuzzer:setFloat(MARIO + 0x50, 0)
    Fuzzer:setFloat(MARIO + 0x54, 0)
    set_position(X, Y, Z)
    assert(u32(MARIO + 0x0C) == ACT_FREEFALL, "ACT_FREEFALL write did not stick")
    assert(u32(MARIO + 0x40) == SOURCE_Y_WORD, "source Y did not encode as C5837800")
    writes = writes + 1
    saw_place = true
end

local function camera_row()
    local camera = u32(CAMERA_PTR)
    if not valid_ptr(camera) then
        return 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
    end
    local raw0 = u32(camera + 0x00)
    local camera_mode = raw0 % 256
    local camera_yaw = math.floor(raw0 / 65536) % 65536
    if camera_yaw >= 32768 then
        camera_yaw = camera_yaw - 65536
    end
    return camera,
        camera_mode,
        camera_yaw,
        f32(camera + 0x04), f32(camera + 0x08), f32(camera + 0x0C),
        f32(camera + 0x10), f32(camera + 0x14), f32(camera + 0x18), f32(camera + 0x1C)
end

local function log_row(event)
    local camera, camera_mode, camera_yaw, camera_focus_x, camera_focus_y,
        camera_focus_z, camera_pos_x, camera_pos_y, camera_pos_z, camera_unused = camera_row()
    local object = u32(MARIO + 0x88)
    local gfx_x, gfx_y, gfx_z = 0 / 0, 0 / 0, 0 / 0
    if valid_ptr(object) then
        gfx_x = f32(object + 0xA0)
        gfx_y = f32(object + 0xA4)
        gfx_z = f32(object + 0xA8)
    end
    local lakitu_focus_x = f32(LAKITU + 0x80)
    local lakitu_focus_y = f32(LAKITU + 0x84)
    local lakitu_focus_z = f32(LAKITU + 0x88)
    local lakitu_pos_x = f32(LAKITU + 0x8C)
    local lakitu_pos_y = f32(LAKITU + 0x90)
    local lakitu_pos_z = f32(LAKITU + 0x94)
    print(string.format(
        "RENDER_ROW,%d,%d,%d,%s,%08X,%08X,%08X,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%08X,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%08X,%d,%d,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%08X,%08X,%d,%d,%.9g",
        vi,
        vi - CAPTURE_START_VI,
        movie:getPosition(),
        event,
        u32(GLOBAL_TIMER),
        u32(MARIO + 0x0C),
        u32(MARIO + 0x40),
        f32(MARIO + 0x3C), f32(MARIO + 0x40), f32(MARIO + 0x44),
        f32(MARIO + 0x48), f32(MARIO + 0x4C), f32(MARIO + 0x50),
        f32(MARIO + 0x54), f32(MARIO + 0x70), f32(MARIO + 0x6C),
        u32(MARIO + 0x68),
        gfx_x, gfx_y, gfx_z,
        lakitu_focus_x, lakitu_focus_y, lakitu_focus_z,
        lakitu_pos_x, lakitu_pos_y, lakitu_pos_z,
        camera,
        camera_mode,
        camera_yaw,
        camera_focus_x, camera_focus_y, camera_focus_z,
        camera_pos_x, camera_pos_y, camera_pos_z,
        u32(GLOBAL_TIMER),
        u32(MARIO + 0x88),
        saw_place and 1 or 0,
        saw_mutation and 1 or 0,
        camera_unused
    ))
    io.stdout:flush()
end

function Fuzzer.start(_)
    local path = assert(os.getenv("M64_PATH"), "M64_PATH is required")
    movie = assert(Fuzzer:openM64(path), "could not open M64 movie: " .. path)
    print(string.format(
        "RENDER_HEADER,scenario=%s,capture_start_vi=%d,capture_end_vi=%d,max_vi=%d,place_vi=%d,mutate_vi=%d,event_x=%.9g,event_y=%.9g,event_z=%.9g,act_freefall=%08X,source_y_word=%08X,flipped_y_word=%08X,declared_writes=%s",
        SCENARIO, CAPTURE_START_VI, CAPTURE_END_VI, MAX_VI, PLACE_VI, MUTATE_VI,
        X, Y, Z, ACT_FREEFALL, SOURCE_Y_WORD, FLIPPED_Y_WORD,
        SCENARIO == "reachable_no_mutation" and "controller_input_only" or "placement_and_optional_bit_clear"))
    print("RENDER_COLUMNS,vi,frame_id,input_position,event,timer,action,y_word,x,y,z,vx,vy,vz,forward_vel,floor_height,ceil_height,floor_ptr,gfx_x,gfx_y,gfx_z,lakitu_focus_x,lakitu_focus_y,lakitu_focus_z,lakitu_pos_x,lakitu_pos_y,lakitu_pos_z,camera_ptr,camera_mode,camera_yaw,camera_focus_x,camera_focus_y,camera_focus_z,camera_pos_x,camera_pos_y,camera_pos_z,timer_repeat,mario_object,placed,mutated,camera_unused")
    io.stdout:flush()
end

function Fuzzer.get_inputs(_, inputs)
    inputs:setRaw(movie:getNextInput())
end

function Fuzzer.update(_)
    vi = vi + 1
    local event = "tick"
    if SCENARIO ~= "reachable_no_mutation" and vi == PLACE_VI then
        inject_freefall()
        event = "place_ACT_FREEFALL_C5837800"
    elseif SCENARIO == "synthetic_bit_clear" and vi == MUTATE_VI then
        local before = u32(MARIO + 0x40)
        assert(before == SOURCE_Y_WORD,
            string.format("unexpected pre-mutation Y word: %08X", before))
        Fuzzer:setUInt(MARIO + 0x40, before - 0x01000000)
        assert(u32(MARIO + 0x40) == FLIPPED_Y_WORD,
            "bit 24 did not clear to C4837800")
        writes = writes + 1
        saw_mutation = true
        event = "clear_bit_24_C5837800_to_C4837800"
    end
    if vi >= CAPTURE_START_VI and vi <= CAPTURE_END_VI then
        log_row(event)
        Fuzzer:takeScreenshot()
        screenshots = screenshots + 1
    end
    if vi >= MAX_VI then
        assert(SCENARIO == "reachable_no_mutation" or saw_place,
            "synthetic placement was not reached")
        assert(SCENARIO ~= "synthetic_bit_clear" or saw_mutation,
            "synthetic bit clear was not reached")
        print(string.format(
            "RENDER_DONE,scenario=%s,first_vi=%d,last_vi=%d,captured_vis=%d,screenshot_requests=%d,field_write_events=%d,place_seen=%s,mutation_seen=%s,movie_position=%d",
            SCENARIO, CAPTURE_START_VI, math.min(vi, CAPTURE_END_VI),
            math.max(0, math.min(vi, CAPTURE_END_VI) - CAPTURE_START_VI + 1),
            screenshots, writes, tostring(saw_place), tostring(saw_mutation), movie:getPosition()))
        io.stdout:flush()
        os.exit(0)
    end
end
