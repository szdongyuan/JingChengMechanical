#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <string.h>
#include "capture_buffer.h"

#define CB_MAX_BYTES ((uint64_t)512 * 1024 * 1024)
struct cb_buffer {
    volatile LONG64 written, consumed, heartbeat;
    volatile LONG error, priority, state, stop, active, leased;
    uint32_t channels, max_frames, slots;
    uint64_t frequency;
    int64_t clock_offset;
    cb_block *blocks;
    float *data;
};
static uint64_t load64(volatile LONG64 *p) { return (uint64_t)InterlockedCompareExchange64(p, 0, 0); }
static LONG load32(volatile LONG *p) { return InterlockedCompareExchange(p, 0, 0); }
static uint64_t now_ns(cb_buffer *b) {
    LARGE_INTEGER q;
    QueryPerformanceCounter(&q);
    return (uint64_t)((int64_t)(((uint64_t)q.QuadPart / b->frequency) * 1000000000ULL
        + ((uint64_t)q.QuadPart % b->frequency) * 1000000000ULL / b->frequency) + b->clock_offset);
}
CB_API uint32_t cb_abi_version(void) { return 1; }
CB_API cb_buffer *cb_create(uint32_t channels, uint32_t max_frames, uint32_t slots) {
    cb_buffer *b;
    LARGE_INTEGER frequency;
    uint64_t stride, bytes, metadata;
    size_t i;
    if (!channels || !max_frames || !slots) return NULL;
    stride = (uint64_t)channels * max_frames;
    if (stride > CB_MAX_BYTES / sizeof(float)) return NULL;
    bytes = stride * sizeof(float);
    if (slots > CB_MAX_BYTES / bytes) return NULL;
    bytes *= slots;
    metadata = (uint64_t)slots * sizeof(cb_block);
    if (bytes + metadata + sizeof(cb_buffer) > CB_MAX_BYTES) return NULL;
    if (!QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0
        || frequency.QuadPart > 1000000000LL) return NULL;
    b = (cb_buffer *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*b));
    if (!b) return NULL;
    b->blocks = (cb_block *)VirtualAlloc(NULL, (SIZE_T)metadata, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    b->data = (float *)VirtualAlloc(NULL, (SIZE_T)bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!b->blocks || !b->data) { cb_destroy(b); return NULL; }
    /* Commit/touch all pages before the producer starts, including the last page. */
    for (i = 0; i < (size_t)bytes; i += 4096) ((volatile char *)b->data)[i] = 0;
    for (i = 0; i < (size_t)metadata; i += 4096) ((volatile char *)b->blocks)[i] = 0;
    b->channels = channels; b->max_frames = max_frames; b->slots = slots;
    b->frequency = (uint64_t)frequency.QuadPart;
    for (i = 0; i < slots; ++i) b->blocks[i].samples = b->data + i * (size_t)stride;
    return b;
}
CB_API int cb_arm(cb_buffer *b) {
    return b && InterlockedCompareExchange(&b->state, 1, 0) == 0;
}
CB_API int cb_set_clock_offset(cb_buffer *b, int64_t offset_ns) {
    if (!b || load32(&b->state) != 0) return 0;
    b->clock_offset = offset_ns;
    return 1;
}
CB_API void cb_request_stop(cb_buffer *b) { if (b) InterlockedExchange(&b->stop, 1); }
CB_API int cb_producer_stopped(cb_buffer *b) {
    if (!b || load32(&b->active)) return 0;
    InterlockedExchange(&b->state, 2);
    return 1;
}
CB_API int cb_destroy(cb_buffer *b) {
    if (!b) return 1;
    if (load32(&b->state) == 1 || load32(&b->active) || load32(&b->leased)) return 0;
    if (b->data) VirtualFree(b->data, 0, MEM_RELEASE);
    if (b->blocks) VirtualFree(b->blocks, 0, MEM_RELEASE);
    HeapFree(GetProcessHeap(), 0, b);
    return 1;
}
CB_API int cb_callback(const void *input, void *output, unsigned long frames,
    const cb_time *timing, unsigned long status, void *userdata) {
    cb_buffer *b = (cb_buffer *)userdata;
    cb_block *block;
    uint64_t entry, written;
    int result = 2;
    (void)output;
    if (!b) return 2;
    InterlockedIncrement(&b->active);
    entry = now_ns(b);
    InterlockedExchange64(&b->heartbeat, (LONG64)entry);
    if (load32(&b->state) != 1 || load32(&b->stop) || load32(&b->error)) goto done;
    if (!load32(&b->priority)) {
        int ok = SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);
        InterlockedExchange(&b->priority, ok ? 1 : 2);
    }
    if (!input || !frames || frames > b->max_frames) {
        InterlockedCompareExchange(&b->error, 2, 0); goto done;
    }
    written = load64(&b->written);
    if (written - load64(&b->consumed) >= b->slots) {
        InterlockedCompareExchange(&b->error, 1, 0); goto done;
    }
    block = &b->blocks[written % b->slots];
    memcpy((void *)block->samples, input, (size_t)frames * b->channels * sizeof(float));
    block->sequence = written + 1; block->entry_ns = entry;
    block->frames = frames; block->channels = b->channels; block->status = status;
    block->adc_time = timing ? timing->inputBufferAdcTime : 0;
    block->current_time = timing ? timing->currentTime : 0;
    block->dac_time = timing ? timing->outputBufferDacTime : 0;
    /* Interlocked publication is a full barrier (stronger than release/acquire). */
    InterlockedExchange64(&b->written, (LONG64)(written + 1));
    result = 0;
done:
    InterlockedDecrement(&b->active);
    return result;
}
CB_API void *cb_callback_address(void) { return (void *)&cb_callback; }
CB_API int cb_peek(cb_buffer *b, cb_block *out) {
    uint64_t consumed;
    if (!b || !out || InterlockedCompareExchange(&b->leased, 1, 0)) return -1;
    consumed = load64(&b->consumed);
    if (consumed == load64(&b->written)) { InterlockedExchange(&b->leased, 0); return 0; }
    *out = b->blocks[consumed % b->slots];
    return 1;
}
CB_API int cb_release(cb_buffer *b) {
    if (!b || !load32(&b->leased)) return 0;
    InterlockedIncrement64(&b->consumed);
    InterlockedExchange(&b->leased, 0);
    return 1;
}
CB_API void cb_snapshot(cb_buffer *b, cb_stats *out) {
    if (!b || !out) return;
    out->consumed = load64(&b->consumed); out->sequence = load64(&b->written);
    out->heartbeat_ns = load64(&b->heartbeat);
    out->error = (uint32_t)load32(&b->error); out->priority_applied = (uint32_t)load32(&b->priority);
    out->capacity = b->slots; out->armed = load32(&b->state) == 1;
    out->stop_requested = (uint32_t)load32(&b->stop);
}
