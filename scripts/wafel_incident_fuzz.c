#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef void (*game_fn)(void);
typedef float (*collision_fn)(float, float, float, void **);

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
    OBJECT_POS_OFFSET = 240,
    OBJECT_VEL_OFFSET = 252,
    OBJECT_FORWARD_VEL_OFFSET = 264,
    OBJECT_MOVE_YAW_OFFSET = 280,
    OBJECT_FACE_YAW_OFFSET = 292,
    LEVEL_COMMAND_ADDR_RVA = 12737304,
    DLL_DATA_RVA = 958464,
    DLL_DATA_SIZE = 2388720,
    DLL_BSS_RVA = 12705792,
    DLL_BSS_SIZE = 4888624,
    PHASE_BLOCK_EPISODES = 100,
    PHASE_ADVANCE_FRAMES = 17
};

static const uint32_t AIRBORNE_ACTIONS[] = {
    16779404,  /* ACT_FREEFALL */
    50333824,  /* ACT_JUMP */
    50333825,  /* ACT_DOUBLE_JUMP */
    16779394,  /* ACT_TRIPLE_JUMP */
    16779395,  /* ACT_BACKFLIP */
    16779399,  /* ACT_SIDE_FLIP */
    50333830,  /* ACT_WALL_KICK_AIR */
    50333832,  /* ACT_LONG_JUMP */
    25692298,   /* ACT_DIVE */
    8914006,    /* ACT_DIVE_SLIDE */
    25168044,   /* ACT_JUMP_KICK */
    2215,       /* ACT_AIR_HIT_WALL */
    16910518,   /* ACT_SOFT_BONK */
    16910512,   /* ACT_BACKWARD_AIR_KB */
    16910513,   /* ACT_FORWARD_AIR_KB */
    8390825,    /* ACT_GROUND_POUND */
    276826276   /* ACT_TWIRLING */
};

static FARPROC require_symbol(HMODULE module, const char *name) {
    FARPROC symbol = GetProcAddress(module, name);
    if (symbol == NULL) {
        fprintf(stderr, "missing symbol: %s (error %lu)\n", name, GetLastError());
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

static collision_fn require_collision_fn(HMODULE module, const char *name) {
    FARPROC symbol = require_symbol(module, name);
    collision_fn function;
    memcpy(&function, &symbol, sizeof(function));
    return function;
}

static uint64_t prng_next(uint64_t *state) {
    uint64_t x = *state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    *state = x;
    return x * UINT64_C(2685821657736338717);
}

static float random_range(uint64_t *state, float low, float high) {
    uint32_t bits = (uint32_t)(prng_next(state) >> 40);
    return low + (high - low) * ((float)bits / 16777215.0f);
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

int main(int argc, char **argv) {
    if (argc < 9) {
        fprintf(stderr,
                "usage: %s DLL SEED TRIALS WARMUP SPEED_MODE HORIZON RESET_MODE PROFILE\n",
                argv[0]);
        return 2;
    }

    const char *module_path = argv[1];
    uint64_t seed = strtoull(argv[2], NULL, 0);
    uint64_t trials = strtoull(argv[3], NULL, 0);
    uint64_t warmup = strtoull(argv[4], NULL, 0);
    int speed_mode = atoi(argv[5]);
    int horizon = atoi(argv[6]);
    int reset_mode = atoi(argv[7]);
    int profile = atoi(argv[8]);
    if (profile < 0 || profile > 4) {
        fprintf(stderr,
                "PROFILE must be 0 (broad), 1 (exact), 2 (local), 3 (grid),"
                " or 4 (bitflip self-test)\n");
        return 2;
    }
    if (reset_mode < 0 || reset_mode > 1) {
        fprintf(stderr, "RESET_MODE must be 0 (continuous) or 1 (snapshot)\n");
        return 2;
    }
    if (horizon < 1 || horizon > 256) {
        fprintf(stderr, "HORIZON must be 1..256\n");
        return 2;
    }
    if (speed_mode < -1 || speed_mode > 3) {
        fprintf(stderr, "SPEED_MODE must be -1 (cycling) or 0..3\n");
        return 2;
    }
    uint64_t random_state = seed == 0 ? UINT64_C(0x9E3779B97F4A7C15) : seed;

    HMODULE module = LoadLibraryA(module_path);
    if (module == NULL) {
        fprintf(stderr, "LoadLibrary failed for %s (error %lu)\n", module_path, GetLastError());
        return 1;
    }

    game_fn init = require_game_fn(module, "sm64_init");
    game_fn update = require_game_fn(module, "sm64_update");
    collision_fn find_floor = require_collision_fn(module, "find_floor");
    collision_fn find_ceil = require_collision_fn(module, "find_ceil");
    uint8_t *mario = (uint8_t *)require_symbol(module, "gMarioStates");
    void **mario_object_ptr = (void **)require_symbol(module, "gMarioObject");
    void **mario_platform = (void **)require_symbol(module, "gMarioPlatform");
    int16_t *ttc_speed = (int16_t *)require_symbol(module, "gTTCSpeedSetting");
    uint32_t *timer = (uint32_t *)require_symbol(module, "gGlobalTimer");
    uint8_t *controller_pads =
        (uint8_t *)require_symbol(module, "gControllerPads");

    bootstrap_ttc(module, init, update);
    if (speed_mode >= 0) {
        *ttc_speed = (int16_t)speed_mode;
    }
    for (uint64_t i = 0; i < warmup; i++) {
        update();
    }
    uint8_t *module_base = (uint8_t *)module;
    uint8_t *data_snapshot = NULL;
    uint8_t *bss_snapshot = NULL;
    if (reset_mode != 0) {
        data_snapshot = (uint8_t *)malloc(DLL_DATA_SIZE);
        bss_snapshot = (uint8_t *)malloc(DLL_BSS_SIZE);
        if (data_snapshot == NULL || bss_snapshot == NULL) {
            fprintf(stderr, "unable to allocate DLL state snapshots\n");
            return 1;
        }
        memcpy(data_snapshot, module_base + DLL_DATA_RVA, DLL_DATA_SIZE);
        memcpy(bss_snapshot, module_base + DLL_BSS_RVA, DLL_BSS_SIZE);
    }

    LARGE_INTEGER frequency;
    LARGE_INTEGER start;
    LARGE_INTEGER end;
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&start);

    float largest_delta = -INFINITY;
    uint64_t largest_trial = 0;
    int largest_step = 0;
    uint64_t candidates = 0;
    uint64_t valid_trials = 0;
    uint64_t simulated_frames = 0;

    for (uint64_t trial = 0; trial < trials; trial++) {
        if (data_snapshot != NULL) {
            if (trial != 0 && trial % PHASE_BLOCK_EPISODES == 0) {
                memcpy(module_base + DLL_DATA_RVA, data_snapshot, DLL_DATA_SIZE);
                memcpy(module_base + DLL_BSS_RVA, bss_snapshot, DLL_BSS_SIZE);
                for (int phase_step = 0;
                     phase_step < PHASE_ADVANCE_FRAMES;
                     phase_step++) {
                    update();
                }
                memcpy(data_snapshot, module_base + DLL_DATA_RVA, DLL_DATA_SIZE);
                memcpy(bss_snapshot, module_base + DLL_BSS_RVA, DLL_BSS_SIZE);
            } else {
                memcpy(module_base + DLL_DATA_RVA, data_snapshot, DLL_DATA_SIZE);
                memcpy(module_base + DLL_BSS_RVA, bss_snapshot, DLL_BSS_SIZE);
            }
        }
        uint64_t r = prng_next(&random_state);
        float x;
        float y;
        float z;
        float vx;
        float vy;
        float vz;
        float forward_vel;
        uint32_t action;

        if (profile == 1 || profile == 4
            || (profile == 0 && (r & 3) == 0)) {
            x = 800.0f;
            y = -4207.0f;
            z = 1900.0f;
            vx = 0.0f;
            vy = -20.0f;
            vz = 0.0f;
            forward_vel = 0.0f;
            action = AIRBORNE_ACTIONS[0];
        } else if (profile == 2) {
            x = random_range(&random_state, 700.0f, 1250.0f);
            y = random_range(&random_state, -4275.0f, -4100.0f);
            z = random_range(&random_state, 1450.0f, 2150.0f);
            vx = random_range(&random_state, -64.0f, 64.0f);
            vy = random_range(&random_state, -75.0f, 30.0f);
            vz = random_range(&random_state, -64.0f, 64.0f);
            forward_vel = random_range(&random_state, 0.0f, 64.0f);
            action = AIRBORNE_ACTIONS[
                prng_next(&random_state)
                % (sizeof(AIRBORNE_ACTIONS) / sizeof(AIRBORNE_ACTIONS[0]))];
        } else if (profile == 3) {
            uint64_t grid_index =
                (trial + seed * UINT64_C(104729)) % UINT64_C(386251);
            x = 700.0f + (float)(grid_index % UINT64_C(551));
            y = -4207.0f;
            z = 1450.0f + (float)(grid_index / UINT64_C(551));
            vx = 0.0f;
            vy = -20.0f;
            vz = 0.0f;
            forward_vel = 0.0f;
            action = AIRBORNE_ACTIONS[0];
        } else {
            x = random_range(&random_state, 300.0f, 1300.0f);
            z = random_range(&random_state, 1400.0f, 2400.0f);
            y = random_range(&random_state, -5000.0f, -3200.0f);
            vx = random_range(&random_state, -80.0f, 80.0f);
            vy = random_range(&random_state, -75.0f, 55.0f);
            vz = random_range(&random_state, -80.0f, 80.0f);
            forward_vel = random_range(&random_state, -80.0f, 80.0f);
            action = AIRBORNE_ACTIONS[
                prng_next(&random_state)
                % (sizeof(AIRBORNE_ACTIONS) / sizeof(AIRBORNE_ACTIONS[0]))];
        }

        void *floor = NULL;
        void *ceil = NULL;
        float floor_height = find_floor(x, y, z, &floor);
        float ceil_height = find_ceil(x, y, z, &ceil);
        if (floor == NULL || y < floor_height) {
            continue;
        }
        valid_trials++;
        int16_t face_yaw = (int16_t)prng_next(&random_state);

        store_u32(mario + MARIO_ACTION_OFFSET, action);
        store_u32(mario + MARIO_PREV_ACTION_OFFSET, 205521409);
        store_u16(mario + MARIO_ACTION_STATE_OFFSET, 0);
        store_u16(mario + MARIO_ACTION_TIMER_OFFSET, 0);
        store_u32(mario + MARIO_ACTION_ARG_OFFSET, 0);
        store_s16(mario + MARIO_FACE_ANGLE_OFFSET + 0, 0);
        store_s16(mario + MARIO_FACE_ANGLE_OFFSET + 2, face_yaw);
        store_s16(mario + MARIO_FACE_ANGLE_OFFSET + 4, 0);
        store_f32(mario + MARIO_POS_OFFSET + 0, x);
        store_f32(mario + MARIO_POS_OFFSET + 4, y);
        store_f32(mario + MARIO_POS_OFFSET + 8, z);
        store_f32(mario + MARIO_VEL_OFFSET + 0, vx);
        store_f32(mario + MARIO_VEL_OFFSET + 4, vy);
        store_f32(mario + MARIO_VEL_OFFSET + 8, vz);
        store_f32(mario + MARIO_FORWARD_VEL_OFFSET, forward_vel);
        store_ptr(mario + MARIO_WALL_OFFSET, NULL);
        store_ptr(mario + MARIO_CEIL_OFFSET, ceil);
        store_ptr(mario + MARIO_FLOOR_OFFSET, floor);
        store_f32(mario + MARIO_CEIL_HEIGHT_OFFSET, ceil_height);
        store_f32(mario + MARIO_FLOOR_HEIGHT_OFFSET, floor_height);
        store_s16(mario + MARIO_WATER_LEVEL_OFFSET, -11000);
        store_ptr(mario + MARIO_HELD_OBJ_OFFSET, NULL);
        store_ptr(mario + MARIO_USED_OBJ_OFFSET, NULL);
        store_ptr(mario + MARIO_RIDDEN_OBJ_OFFSET, NULL);
        store_f32(mario + MARIO_PEAK_HEIGHT_OFFSET, y);
        *mario_platform = NULL;

        uint8_t *mario_object = (uint8_t *)*mario_object_ptr;
        store_f32(mario_object + OBJECT_POS_OFFSET + 0, x);
        store_f32(mario_object + OBJECT_POS_OFFSET + 4, y);
        store_f32(mario_object + OBJECT_POS_OFFSET + 8, z);
        store_f32(mario_object + OBJECT_VEL_OFFSET + 0, vx);
        store_f32(mario_object + OBJECT_VEL_OFFSET + 4, vy);
        store_f32(mario_object + OBJECT_VEL_OFFSET + 8, vz);
        store_f32(mario_object + OBJECT_FORWARD_VEL_OFFSET, forward_vel);
        store_u32(mario_object + OBJECT_MOVE_YAW_OFFSET, (uint16_t)face_yaw);
        store_u32(mario_object + OBJECT_FACE_YAW_OFFSET, (uint16_t)face_yaw);

        static const uint16_t button_palette[] = {
            0, 0x8000, 0x4000, 0x2000, 0x0020, 0x0010, 0x0008,
            0xC000, 0x8020, 0x8010, 0x0030
        };
        for (int step = 0; step < horizon; step++) {
            uint16_t button =
                button_palette[prng_next(&random_state)
                               % (sizeof(button_palette) / sizeof(button_palette[0]))];
            int8_t stick_x =
                (int8_t)random_range(&random_state, -128.0f, 127.0f);
            int8_t stick_y =
                (int8_t)random_range(&random_state, -128.0f, 127.0f);
            if (profile == 4) {
                button = 0;
                stick_x = 0;
                stick_y = 0;
            }
            store_u16(controller_pads, button);
            controller_pads[2] = (uint8_t)stick_x;
            controller_pads[3] = (uint8_t)stick_y;
            *ttc_speed =
                (int16_t)(speed_mode < 0 ? (int)(trial & 3) : speed_mode);

            uint32_t before_action = load_u32(mario + MARIO_ACTION_OFFSET);
            float before_x = load_f32(mario + MARIO_POS_OFFSET + 0);
            float before_y = load_f32(mario + MARIO_POS_OFFSET + 4);
            float before_z = load_f32(mario + MARIO_POS_OFFSET + 8);
            float before_vx = load_f32(mario + MARIO_VEL_OFFSET + 0);
            float before_vy = load_f32(mario + MARIO_VEL_OFFSET + 4);
            float before_vz = load_f32(mario + MARIO_VEL_OFFSET + 8);
            float before_floor =
                load_f32(mario + MARIO_FLOOR_HEIGHT_OFFSET);
            float before_ceil =
                load_f32(mario + MARIO_CEIL_HEIGHT_OFFSET);
            if (profile == 4 && trial == 0 && step == 0) {
                uint32_t y_word =
                    load_u32(mario + MARIO_POS_OFFSET + 4);
                store_u32(mario + MARIO_POS_OFFSET + 4,
                          y_word & ~UINT32_C(0x01000000));
            }
            update();
            simulated_frames++;
            float after_y = load_f32(mario + MARIO_POS_OFFSET + 4);
            float delta_y = after_y - before_y;
            if (profile == 4) {
                printf("SELFTEST_FRAME step=%d timer=%" PRIu32
                       " y=%.9g vy=%.9g floor=%.9g ceil=%.9g"
                       " action=%08" PRIX32 "\n",
                       step, *timer, after_y,
                       load_f32(mario + MARIO_VEL_OFFSET + 4),
                       load_f32(mario + MARIO_FLOOR_HEIGHT_OFFSET),
                       load_f32(mario + MARIO_CEIL_HEIGHT_OFFSET),
                       load_u32(mario + MARIO_ACTION_OFFSET));
            }
            if (delta_y > largest_delta) {
                largest_delta = delta_y;
                largest_trial = trial;
                largest_step = step;
            }
            if (delta_y >= 500.0f) {
                candidates++;
                printf("FUZZ_CANDIDATE seed=%" PRIu64
                       " trial=%" PRIu64 " step=%d timer=%" PRIu32
                       " speed=%d action=%08" PRIX32
                       " x=%.9g y=%.9g z=%.9g vx=%.9g vy=%.9g vz=%.9g"
                       " forward_vel=%.9g face_yaw=%d button=%04X stick_x=%d stick_y=%d"
                       " floor=%.9g ceil=%.9g after_y=%.9g delta_y=%.9g after_action=%08" PRIX32 "\n",
                       seed, trial, step, *timer, *ttc_speed, before_action,
                       before_x, before_y, before_z, before_vx, before_vy,
                       before_vz, forward_vel, face_yaw, button, stick_x,
                       stick_y, before_floor, before_ceil, after_y, delta_y,
                       load_u32(mario + MARIO_ACTION_OFFSET));
            }
        }
    }

    QueryPerformanceCounter(&end);
    double seconds =
        (double)(end.QuadPart - start.QuadPart) / (double)frequency.QuadPart;
    printf("FUZZ_DONE seed=%" PRIu64 " speed_mode=%d horizon=%d"
           " reset_mode=%d profile=%d trials=%" PRIu64
           " valid_trials=%" PRIu64
           " simulated_frames=%" PRIu64 " candidates=%" PRIu64
           " largest_delta=%.9g largest_trial=%" PRIu64
           " largest_step=%d seconds=%.9f"
           " trials_per_second=%.3f frames_per_second=%.3f\n",
           seed, speed_mode, horizon, reset_mode, profile, trials,
           valid_trials,
           simulated_frames,
           candidates, largest_delta, largest_trial, largest_step, seconds,
           trials / seconds, simulated_frames / seconds);

    free(data_snapshot);
    free(bss_snapshot);
    FreeLibrary(module);
    return 0;
}
