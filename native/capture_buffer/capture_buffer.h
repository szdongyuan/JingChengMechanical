#pragma once
#include <stdint.h>
#ifdef CAPTURE_BUILD
#define CB_API __declspec(dllexport)
#else
#define CB_API __declspec(dllimport)
#endif

typedef struct cb_buffer cb_buffer;
typedef struct { double inputBufferAdcTime, currentTime, outputBufferDacTime; } cb_time;
typedef struct {
    uint64_t sequence, entry_ns;
    uint32_t frames, channels, status, reserved;
    double adc_time, current_time, dac_time;
    const float *samples;
} cb_block;
typedef struct {
    uint64_t sequence, consumed, heartbeat_ns;
    uint32_t error, priority_applied, capacity, armed, stop_requested;
} cb_stats;

CB_API uint32_t cb_abi_version(void);
CB_API cb_buffer *cb_create(uint32_t channels, uint32_t max_frames, uint32_t slots);
CB_API int cb_arm(cb_buffer *b);
CB_API int cb_set_clock_offset(cb_buffer *b, int64_t offset_ns);
CB_API void cb_request_stop(cb_buffer *b);
/* Call only AFTER the external stream/producer has stopped and cannot call back. */
CB_API int cb_producer_stopped(cb_buffer *b);
/* Refuses to free armed producers or outstanding read leases. */
CB_API int cb_destroy(cb_buffer *b);
CB_API int cb_peek(cb_buffer *b, cb_block *out);
CB_API int cb_release(cb_buffer *b);
CB_API void cb_snapshot(cb_buffer *b, cb_stats *out);
CB_API void *cb_callback_address(void);
CB_API int cb_callback(const void *input, void *output, unsigned long frames,
    const cb_time *timing, unsigned long status, void *userdata);
