#define WIN32_LEAN_AND_MEAN

#include <windows.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "wafel_jp_dll.h"

/* Wafel's 64-bit libsm64 keeps MarioState's scalar layout but widens object
 * pointers.  These offsets are checked against the JP DLL disassembly in the
 * accompanying result. */
enum {
    LEVEL_TTC = 14,
    TTC_SPEED_STOPPED = 3,
    MARIO_ACTION_OFFSET = 12,
    MARIO_PREV_ACTION_OFFSET = 16,
    MARIO_ACTION_STATE_OFFSET = 24,
    MARIO_ACTION_TIMER_OFFSET = 26,
    MARIO_ACTION_ARG_OFFSET = 28,
    MARIO_FACE_ANGLE_OFFSET = 44,
    MARIO_POS_OFFSET = 60,
    MARIO_VEL_OFFSET = 72,
    MARIO_FORWARD_VEL_OFFSET = 84,
    MARIO_WALL_OFFSET = 96,
    MARIO_CEIL_OFFSET = 104,
    MARIO_FLOOR_OFFSET = 112,
    MARIO_CEIL_HEIGHT_OFFSET = 120,
    MARIO_FLOOR_HEIGHT_OFFSET = 124,
    MARIO_WATER_LEVEL_OFFSET = 130,
    MARIO_HELD_OBJ_OFFSET = 144,
    MARIO_USED_OBJ_OFFSET = 152,
    MARIO_RIDDEN_OBJ_OFFSET = 160,
    MARIO_PEAK_HEIGHT_OFFSET = 248,
    MARIO_OBJECT_OFFSET = 168,
    OBJECT_GFX_POS_OFFSET = 56,
    OBJECT_POS_OFFSET = 240,
    OBJECT_VEL_OFFSET = 252,
    OBJECT_FORWARD_VEL_OFFSET = 264,
    OBJECT_MOVE_YAW_OFFSET = 280,
    OBJECT_FACE_YAW_OFFSET = 292,
    LEVEL_COMMAND_ADDR_RVA = 12737304,
    ACT_FREEFALL = 16779404,
    PREV_ACTION = 205521409
};

typedef void (*game_fn)(void);
typedef void (*geometry_fn)(void *mario_state);
typedef float (*collision_fn)(float, float, float, void **);


static volatile uint64_t g_branch_hits;
static uint8_t *g_branch_target;
static uint8_t g_branch_original[14];
static uint8_t *g_branch_trampoline;
static DWORD g_branch_original_protection;

static FARPROC require_symbol(HMODULE module, const char *name) {
    FARPROC symbol = GetProcAddress(module, name);
    if (symbol == NULL) {
        fprintf(stderr, "missing symbol: %s (error %lu)\n", name,
                GetLastError());
        ExitProcess(2);
    }
    return symbol;
}

static game_fn require_game_fn(HMODULE module, const char *name) {
    FARPROC symbol = require_symbol(module, name);
    game_fn function;
    memcpy(&function, &symbol, sizeof(function));
    return function;
}

static geometry_fn require_geometry_fn(HMODULE module, const char *name) {
    FARPROC symbol = require_symbol(module, name);
    geometry_fn function;
    memcpy(&function, &symbol, sizeof(function));
    return function;
}

static collision_fn require_collision_fn(HMODULE module, const char *name) {
    FARPROC symbol = require_symbol(module, name);
    collision_fn function;
    memcpy(&function, &symbol, sizeof(function));
    return function;
}

static void store_u16(uint8_t *p, uint16_t value) {
    memcpy(p, &value, sizeof(value));
}

static void store_s16(uint8_t *p, int16_t value) {
    memcpy(p, &value, sizeof(value));
}

static void store_u32(uint8_t *p, uint32_t value) {
    memcpy(p, &value, sizeof(value));
}

static void store_f32(uint8_t *p, float value) {
    memcpy(p, &value, sizeof(value));
}

static void store_ptr(uint8_t *p, void *value) {
    memcpy(p, &value, sizeof(value));
}


static uint32_t load_u32(const uint8_t *p) {
    uint32_t value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static float load_f32(const uint8_t *p) {
    float value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static void *load_ptr(const uint8_t *p) {
    void *value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static uint64_t prng_next(uint64_t *state) {
    uint64_t x = *state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    *state = x;
    return x * UINT64_C(2685821657736338717);
}

static int32_t load_i32(const uint8_t *p) {
    int32_t value;
    memcpy(&value, p, sizeof(value));
    return value;
}


/* Locate the conditional jump whose target is the floor-null fallback.  The
 * byte pattern is a source-independent check against the JP x64 DLL, rather
 * than a fixed ASLR address. */
static void install_branch_probe(geometry_fn geometry) {
    uint8_t *function = (uint8_t *)geometry;
    uint8_t *target = NULL;
    for (size_t i = 0; i + 5 < 0x400 && target == NULL; i++) {
        if (function[i + 0] != 0x48 || function[i + 1] != 0x83
            || function[i + 2] != 0x7B || function[i + 3] != 0x70
            || function[i + 4] != 0x00) {
            continue;
        }
        for (size_t j = i + 5; j + 6 < i + 24; j++) {
            if (function[j] != 0x0F || function[j + 1] != 0x84) {
                continue;
            }
            int32_t relative = load_i32(function + j + 2);
            target = function + j + 6 + relative;
            break;
        }
    }
    if (target == NULL || target[0] != 0x48 || target[1] != 0x8B
        || target[2] != 0x83 || target[3] != 0xA8 || target[4] != 0x00
        || target[5] != 0x00 || target[6] != 0x00
        || target[7] != 0x48 || target[8] != 0x89
        || target[9] != 0xF1 || target[10] != 0x48
        || target[11] != 0x8D || target[12] != 0x50
        || target[13] != 0x38) {
        fprintf(stderr, "could not identify floor-null branch target\n");
        ExitProcess(2);
    }

    memcpy(g_branch_original, target, sizeof(g_branch_original));
    g_branch_trampoline =
        (uint8_t *)VirtualAlloc(NULL, 4096, MEM_COMMIT | MEM_RESERVE,
                                PAGE_EXECUTE_READWRITE);
    if (g_branch_trampoline == NULL) {
        fprintf(stderr, "VirtualAlloc failed (error %lu)\n", GetLastError());
        ExitProcess(2);
    }

    size_t offset = 0;
    memcpy(g_branch_trampoline + offset, g_branch_original,
           sizeof(g_branch_original));
    offset += sizeof(g_branch_original);
    g_branch_trampoline[offset++] = 0x48; /* mov rax, &g_branch_hits */
    g_branch_trampoline[offset++] = 0xB8;
    uintptr_t hit_address = (uintptr_t)&g_branch_hits;
    memcpy(g_branch_trampoline + offset, &hit_address, sizeof(hit_address));
    offset += sizeof(hit_address);
    g_branch_trampoline[offset++] = 0x48; /* inc qword ptr [rax] */
    g_branch_trampoline[offset++] = 0xFF;
    g_branch_trampoline[offset++] = 0x00;
    g_branch_trampoline[offset++] = 0x48; /* mov rax, target + 14 */
    g_branch_trampoline[offset++] = 0xB8;
    uintptr_t return_address = (uintptr_t)(target + sizeof(g_branch_original));
    memcpy(g_branch_trampoline + offset, &return_address,
           sizeof(return_address));
    offset += sizeof(return_address);
    g_branch_trampoline[offset++] = 0xFF; /* jmp rax */
    g_branch_trampoline[offset++] = 0xE0;
    FlushInstructionCache(GetCurrentProcess(), g_branch_trampoline, offset);

    uint8_t patch[14] = {0x48, 0xB8};
    uintptr_t trampoline_address = (uintptr_t)g_branch_trampoline;
    memcpy(patch + 2, &trampoline_address, sizeof(trampoline_address));
    patch[10] = 0xFF;
    patch[11] = 0xE0;
    patch[12] = 0x90;
    patch[13] = 0x90;
    if (!VirtualProtect(target, sizeof(patch), PAGE_EXECUTE_READWRITE,
                        &g_branch_original_protection)) {
        fprintf(stderr, "VirtualProtect writable failed (error %lu)\n",
                GetLastError());
        ExitProcess(2);
    }
    g_branch_target = target;
    memcpy(target, patch, sizeof(patch));
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    DWORD ignored_protection;
    if (!VirtualProtect(target, sizeof(patch), g_branch_original_protection,
                        &ignored_protection)) {
        fprintf(stderr, "VirtualProtect restore failed (error %lu)\n",
                GetLastError());
        ExitProcess(2);
    }
    printf("PROBE_PATCH function=%p branch_target=%p trampoline=%p"
           " original=%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X\n",
           (void *)function, (void *)target, (void *)g_branch_trampoline,
           g_branch_original[0], g_branch_original[1], g_branch_original[2],
           g_branch_original[3], g_branch_original[4], g_branch_original[5],
           g_branch_original[6], g_branch_original[7], g_branch_original[8],
           g_branch_original[9], g_branch_original[10], g_branch_original[11],
           g_branch_original[12], g_branch_original[13]);
}

static void uninstall_branch_probe(void) {
    if (g_branch_target != NULL) {
        DWORD ignored_protection;
        if (!VirtualProtect(g_branch_target, sizeof(g_branch_original),
                            PAGE_EXECUTE_READWRITE, &ignored_protection)) {
            fprintf(stderr, "VirtualProtect uninstall failed (error %lu)\n",
                    GetLastError());
            ExitProcess(2);
        }
        memcpy(g_branch_target, g_branch_original, sizeof(g_branch_original));
        FlushInstructionCache(GetCurrentProcess(), g_branch_target,
                              sizeof(g_branch_original));
        if (!VirtualProtect(g_branch_target, sizeof(g_branch_original),
                            g_branch_original_protection,
                            &ignored_protection)) {
            fprintf(stderr,
                    "VirtualProtect uninstall restore failed (error %lu)\n",
                    GetLastError());
            ExitProcess(2);
        }
    }
    if (g_branch_trampoline != NULL) {
        VirtualFree(g_branch_trampoline, 0, MEM_RELEASE);
    }
    g_branch_target = NULL;
    g_branch_trampoline = NULL;
}
static void bootstrap_ttc(HMODULE module, game_fn init, game_fn update) {
    game_fn init_mario_from_save_file =
        require_game_fn(module, "init_mario_from_save_file");
    void **level_command_addr =
        (void **)((uint8_t *)module + LEVEL_COMMAND_ADDR_RVA);
    void *ttc_entry = (void *)require_symbol(module, "level_ttc_entry");
    int16_t *level = (int16_t *)require_symbol(module, "gCurrLevelNum");
    int16_t *area = (int16_t *)require_symbol(module, "gCurrAreaIndex");
    int16_t *act = (int16_t *)require_symbol(module, "gCurrActNum");
    int16_t *ttc_speed = (int16_t *)require_symbol(module, "gTTCSpeedSetting");
    int8_t *debug_level_select =
        (int8_t *)require_symbol(module, "gDebugLevelSelect");

    init();
    init_mario_from_save_file();
    *level = LEVEL_TTC;
    *area = 1;
    *act = 4;
    *ttc_speed = TTC_SPEED_STOPPED;
    *debug_level_select = 1;
    *level_command_addr = ttc_entry;
    update();
}

static void set_controller(uint8_t *pads, uint16_t button, int8_t stick_x,
                           int8_t stick_y) {
    store_u16(pads, button);
    pads[2] = (uint8_t)stick_x;
    pads[3] = (uint8_t)stick_y;
}

static void set_controlled_state(uint8_t *mario, void **mario_object_ptr,
                                 void **mario_platform, float x, float y,
                                 float z, float gfx_x, float gfx_y,
                                 float gfx_z, float vx, float vy, float vz,
                                 uint32_t action) {
    store_u32(mario + MARIO_ACTION_OFFSET, action);
    store_u32(mario + MARIO_PREV_ACTION_OFFSET, PREV_ACTION);
    store_u16(mario + MARIO_ACTION_STATE_OFFSET, 0);
    store_u16(mario + MARIO_ACTION_TIMER_OFFSET, 0);
    store_u32(mario + MARIO_ACTION_ARG_OFFSET, 0);
    store_s16(mario + MARIO_FACE_ANGLE_OFFSET + 0, 0);
    store_s16(mario + MARIO_FACE_ANGLE_OFFSET + 2, 0);
    store_s16(mario + MARIO_FACE_ANGLE_OFFSET + 4, 0);
    store_f32(mario + MARIO_POS_OFFSET + 0, x);
    store_f32(mario + MARIO_POS_OFFSET + 4, y);
    store_f32(mario + MARIO_POS_OFFSET + 8, z);
    store_f32(mario + MARIO_VEL_OFFSET + 0, vx);
    store_f32(mario + MARIO_VEL_OFFSET + 4, vy);
    store_f32(mario + MARIO_VEL_OFFSET + 8, vz);
    store_f32(mario + MARIO_FORWARD_VEL_OFFSET, 0.0f);
    store_ptr(mario + MARIO_WALL_OFFSET, NULL);
    store_ptr(mario + MARIO_CEIL_OFFSET, NULL);
    store_ptr(mario + MARIO_FLOOR_OFFSET, NULL);
    store_f32(mario + MARIO_CEIL_HEIGHT_OFFSET, 0.0f);
    store_f32(mario + MARIO_FLOOR_HEIGHT_OFFSET, 0.0f);
    store_s16(mario + MARIO_WATER_LEVEL_OFFSET, -11000);
    store_ptr(mario + MARIO_HELD_OBJ_OFFSET, NULL);
    store_ptr(mario + MARIO_USED_OBJ_OFFSET, NULL);
    store_ptr(mario + MARIO_RIDDEN_OBJ_OFFSET, NULL);
    store_f32(mario + MARIO_PEAK_HEIGHT_OFFSET, y);
    *mario_platform = NULL;

    uint8_t *object = (uint8_t *)*mario_object_ptr;
    store_f32(object + OBJECT_GFX_POS_OFFSET + 0, gfx_x);
    store_f32(object + OBJECT_GFX_POS_OFFSET + 4, gfx_y);
    store_f32(object + OBJECT_GFX_POS_OFFSET + 8, gfx_z);
    store_f32(object + OBJECT_POS_OFFSET + 0, gfx_x);
    store_f32(object + OBJECT_POS_OFFSET + 4, gfx_y);
    store_f32(object + OBJECT_POS_OFFSET + 8, gfx_z);
    store_f32(object + OBJECT_VEL_OFFSET + 0, vx);
    store_f32(object + OBJECT_VEL_OFFSET + 4, vy);
    store_f32(object + OBJECT_VEL_OFFSET + 8, vz);
    store_f32(object + OBJECT_FORWARD_VEL_OFFSET, 0.0f);
    store_u32(object + OBJECT_MOVE_YAW_OFFSET, 0);
    store_u32(object + OBJECT_FACE_YAW_OFFSET, 0);
}

static void print_vec(const char *prefix, const float *v) {
    printf("%s_x=%.9g %s_y=%.9g %s_z=%.9g", prefix, v[0], prefix, v[1],
           prefix, v[2]);
}

static void load_state_vec(uint8_t *mario, void **mario_object_ptr,
                           float *physical, float *gfx) {
    physical[0] = load_f32(mario + MARIO_POS_OFFSET + 0);
    physical[1] = load_f32(mario + MARIO_POS_OFFSET + 4);
    physical[2] = load_f32(mario + MARIO_POS_OFFSET + 8);
    uint8_t *object = (uint8_t *)*mario_object_ptr;
    gfx[0] = load_f32(object + OBJECT_GFX_POS_OFFSET + 0);
    gfx[1] = load_f32(object + OBJECT_GFX_POS_OFFSET + 4);
    gfx[2] = load_f32(object + OBJECT_GFX_POS_OFFSET + 8);
}

static int floor_is_null(uint8_t *mario) {
    return load_ptr(mario + MARIO_FLOOR_OFFSET) == NULL;
}

static void run_geometry_positive(HMODULE module, game_fn init, game_fn update,
                                  geometry_fn geometry,
                                  collision_fn find_floor, uint8_t *mario,
                                  void **mario_object_ptr,
                                  void **mario_platform,
                                  int16_t *ttc_speed) {
    bootstrap_ttc(module, init, update);
    *ttc_speed = TTC_SPEED_STOPPED;
    set_controller((uint8_t *)require_symbol(module, "gControllerPads"), 0, 0,
                   0);

    const float physical[3] = {10000.0f, -4207.0f, 10000.0f};
    const float gfx[3] = {800.0f, -4207.0f, 1900.0f};
    set_controlled_state(mario, mario_object_ptr, mario_platform, physical[0],
                         physical[1], physical[2], gfx[0], gfx[1], gfx[2],
                         0.0f, -20.0f, 0.0f, ACT_FREEFALL);
    void *first_floor = (void *)(uintptr_t)1;
    float first_height = find_floor(physical[0], physical[1], physical[2],
                                    &first_floor);
    float before_physical[3];
    float before_gfx[3];
    load_state_vec(mario, mario_object_ptr, before_physical, before_gfx);
    uint64_t hits_before = g_branch_hits;
    geometry(mario);
    float after_physical[3];
    float after_gfx[3];
    load_state_vec(mario, mario_object_ptr, after_physical, after_gfx);
    printf("POSITIVE_GEOMETRY first_floor_null=%d first_height=%.9g ",
           first_floor == NULL, first_height);
    print_vec("before_physical", before_physical);
    printf(" ");
    print_vec("before_gfx", before_gfx);
    printf(" ");
    print_vec("copied_physical", after_physical);
    printf(" ");
    print_vec("after_gfx", after_gfx);
    printf(" second_floor_null=%d second_floor_height=%.9g action=%08" PRIX32
           " platform_null=%d branch_hits=%" PRIu64 "\n",
           floor_is_null(mario), load_f32(mario + MARIO_FLOOR_HEIGHT_OFFSET),
           load_u32(mario + MARIO_ACTION_OFFSET),
           load_ptr((const uint8_t *)mario_platform) == NULL,
           g_branch_hits - hits_before);

    /* Re-bootstrap so the one-update control starts from a clean game state.
     * The only post-bootstrap writes below are the synthetic Mario/object
     * divergence and controller input; this is deliberately not a naturality
     * claim. */
    bootstrap_ttc(module, init, update);
    *ttc_speed = TTC_SPEED_STOPPED;
    set_controller((uint8_t *)require_symbol(module, "gControllerPads"), 0, 0,
                   0);
    const float incident_physical[3] = {10000.0f, -4207.0f, 10000.0f};
    const float incident_gfx[3] = {800.0f, -1051.75f, 1900.0f};
    set_controlled_state(mario, mario_object_ptr, mario_platform,
                         incident_physical[0], incident_physical[1],
                         incident_physical[2], incident_gfx[0], incident_gfx[1],
                         incident_gfx[2], 0.0f, -20.0f, 0.0f, ACT_FREEFALL);
    void *update_first_floor = (void *)(uintptr_t)1;
    float update_first_height =
        find_floor(incident_physical[0], incident_physical[1],
                   incident_physical[2], &update_first_floor);
    float update_before_physical[3];
    float update_before_gfx[3];
    load_state_vec(mario, mario_object_ptr, update_before_physical,
                   update_before_gfx);
    hits_before = g_branch_hits;
    update();
    float update_after_physical[3];
    float update_after_gfx[3];
    load_state_vec(mario, mario_object_ptr, update_after_physical,
                   update_after_gfx);
    printf("POSITIVE_UPDATE first_floor_null=%d first_height=%.9g ",
           update_first_floor == NULL, update_first_height);
    print_vec("before_physical", update_before_physical);
    printf(" ");
    print_vec("before_gfx", update_before_gfx);
    printf(" ");
    print_vec("after_physical", update_after_physical);
    printf(" ");
    print_vec("after_gfx", update_after_gfx);
    printf(" one_update_y_delta=%.9g second_floor_null=%d second_floor_height=%.9g"
           " action=%08" PRIX32 " platform_null=%d branch_hits=%" PRIu64 "\n",
           update_after_physical[1] - update_before_physical[1],
           floor_is_null(mario), load_f32(mario + MARIO_FLOOR_HEIGHT_OFFSET),
           load_u32(mario + MARIO_ACTION_OFFSET),
           load_ptr((const uint8_t *)mario_platform) == NULL,
           g_branch_hits - hits_before);
}

static void run_synthetic_sweep(geometry_fn geometry, collision_fn find_floor,
                                uint8_t *mario, void **mario_object_ptr,
                                void **mario_platform) {
    static const float physical_y[] = {-10000.0f, -4207.0f, -2000.0f,
                                       0.0f, 1000.0f};
    static const float gfx_y[] = {-4207.0f, -3000.0f, -1051.75f,
                                  -500.0f, 0.0f, 500.0f};
    uint64_t rows = 0;
    uint64_t null_first = 0;
    uint64_t null_second = 0;
    float min_delta = 1.0e30f;
    float max_delta = -1.0e30f;
    float incident_delta = 0.0f;
    for (size_t i = 0; i < sizeof(physical_y) / sizeof(physical_y[0]); i++) {
        for (size_t j = 0; j < sizeof(gfx_y) / sizeof(gfx_y[0]); j++) {
            set_controlled_state(mario, mario_object_ptr, mario_platform,
                                 10000.0f, physical_y[i], 10000.0f, 800.0f,
                                 gfx_y[j], 1900.0f, 0.0f, -20.0f, 0.0f,
                                 ACT_FREEFALL);
            void *first_floor = (void *)(uintptr_t)1;
            float first_height =
                find_floor(10000.0f, physical_y[i], 10000.0f, &first_floor);
            float before[3];
            float before_gfx[3];
            load_state_vec(mario, mario_object_ptr, before, before_gfx);
            uint64_t hits_before = g_branch_hits;
            geometry(mario);
            float after[3];
            float after_gfx[3];
            load_state_vec(mario, mario_object_ptr, after, after_gfx);
            float delta = after[1] - before[1];
            int first_null = first_floor == NULL;
            int second_null = floor_is_null(mario);
            if (first_null) {
                null_first++;
            }
            if (second_null) {
                null_second++;
            }
            if (delta < min_delta) {
                min_delta = delta;
            }
            if (delta > max_delta) {
                max_delta = delta;
            }
            if (physical_y[i] == -4207.0f && gfx_y[j] == -1051.75f) {
                incident_delta = delta;
            }
            printf("SWEEP_ROW physical_y=%.9g gfx_y=%.9g first_floor_null=%d"
                   " first_height=%.9g copied_y=%.9g copied_x=%.9g copied_z=%.9g"
                   " second_floor_null=%d second_floor_height=%.9g"
                   " propagation_delta=%.9g branch_hits=%" PRIu64 "\n",
                   physical_y[i], gfx_y[j], first_null, first_height, after[1],
                   after[0], after[2], second_null,
                   load_f32(mario + MARIO_FLOOR_HEIGHT_OFFSET), delta,
                   g_branch_hits - hits_before);
            rows++;
        }
    }
    printf("SWEEP_DONE rows=%" PRIu64 " first_null=%" PRIu64
           " second_null=%" PRIu64 " min_delta=%.9g max_delta=%.9g"
           " incident_delta=%.9g branch_hits=%" PRIu64 "\n",
           rows, null_first, null_second, min_delta, max_delta, incident_delta,
           g_branch_hits);
}

static void run_natural(HMODULE module, game_fn init, game_fn update,
                        uint8_t *mario, int16_t *ttc_speed,
                        uint8_t *controller_pads, uint64_t frames,
                        int speed, uint64_t seed) {
    *ttc_speed = (int16_t)speed;
    bootstrap_ttc(module, init, update);
    *ttc_speed = (int16_t)speed;
    uint64_t start_hits = g_branch_hits;
    uint64_t completed = 0;
    uint64_t state = seed == 0 ? UINT64_C(0x9E3779B97F4A7C15) : seed;
    printf("NATURAL_START requested_frames=%" PRIu64 " speed=%d seed=%" PRIu64
           " controller_only=1\n",
           frames, speed, seed);
    for (uint64_t frame = 0; frame < frames; frame++) {
        uint64_t value = prng_next(&state);
        static const uint16_t button_palette[] = {
            0, 0x0020, 0x0010, 0x0008, 0x0030};
        uint16_t button =
            button_palette[value % (sizeof(button_palette) / sizeof(button_palette[0]))];
        int8_t stick_x = 0;
        int8_t stick_y = 0;
        set_controller(controller_pads, button, stick_x, stick_y);
        update();
        completed++;
        if (frame == 0 || frame + 1 == frames) {
            printf("NATURAL_FRAME frame=%" PRIu64 " timer=%" PRIu32
                   " action=%08" PRIX32 " x=%.9g y=%.9g z=%.9g"
                   " floor_height=%.9g floor_null=%d\n",
                   frame, load_u32((const uint8_t *)require_symbol(module,
                                                                    "gGlobalTimer")),
                   load_u32(mario + MARIO_ACTION_OFFSET),
                   load_f32(mario + MARIO_POS_OFFSET + 0),
                   load_f32(mario + MARIO_POS_OFFSET + 4),
                   load_f32(mario + MARIO_POS_OFFSET + 8),
                   load_f32(mario + MARIO_FLOOR_HEIGHT_OFFSET),
                   floor_is_null(mario));
        }
    }
    printf("NATURAL_DONE requested_frames=%" PRIu64
           " completed_updates=%" PRIu64 " branch_hits=%" PRIu64 "\n",
           frames, completed, g_branch_hits - start_hits);
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr,
                "usage: %s DLL positive|sweep|natural [frames] [speed] [seed]\n",
                argv[0]);
        return 2;
    }
    const char *module_path = argv[1];
    const char *mode = argv[2];
    uint64_t frames = argc > 3 ? strtoull(argv[3], NULL, 0) : 4096;
    int speed = argc > 4 ? atoi(argv[4]) : TTC_SPEED_STOPPED;
    uint64_t seed = argc > 5 ? strtoull(argv[5], NULL, 0) : 0x20260812;

    HMODULE module = load_wafel_jp_dll(module_path);
    if (module == NULL) {
        return 1;
    }
    game_fn init = require_game_fn(module, "sm64_init");
    game_fn update = require_game_fn(module, "sm64_update");
    geometry_fn geometry =
        require_geometry_fn(module, "update_mario_geometry_inputs");
    collision_fn find_floor = require_collision_fn(module, "find_floor");
    uint8_t *mario = (uint8_t *)require_symbol(module, "gMarioStates");
    void **mario_object_ptr =
        (void **)require_symbol(module, "gMarioObject");
    void **mario_platform =
        (void **)require_symbol(module, "gMarioPlatform");
    int16_t *ttc_speed =
        (int16_t *)require_symbol(module, "gTTCSpeedSetting");
    uint8_t *controller_pads =
        (uint8_t *)require_symbol(module, "gControllerPads");

    install_branch_probe(geometry);
    if (strcmp(mode, "positive") == 0) {
        run_geometry_positive(module, init, update, geometry, find_floor, mario,
                              mario_object_ptr, mario_platform, ttc_speed);
    } else if (strcmp(mode, "sweep") == 0) {
        bootstrap_ttc(module, init, update);
        run_synthetic_sweep(geometry, find_floor, mario, mario_object_ptr,
                            mario_platform);
    } else if (strcmp(mode, "natural") == 0) {
        run_natural(module, init, update, mario, ttc_speed, controller_pads,
                    frames, speed, seed);
    } else {
        fprintf(stderr, "unknown mode: %s\n", mode);
        uninstall_branch_probe();
        FreeLibrary(module);
        return 2;
    }

    uninstall_branch_probe();
    printf("PROBE_DONE total_branch_hits=%" PRIu64 "\n", g_branch_hits);
    FreeLibrary(module);
    return 0;
}
