#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "capture_buffer.h"

typedef struct {
    cb_buffer *buffer;
    HANDLE thread;
    uint32_t count, frames, channels, interval, backpressure;
    float *samples;
    uint64_t *entries;
    int result, priority;
} driver;

static DWORD WINAPI produce(void *arg) {
    driver *d = (driver *)arg;
    uint32_t i, j;
    for (i = 0; i < d->count; ++i) {
        cb_stats stats;
        cb_time timing = { 1.25, 2.5, 3.75 };
        uint32_t frames = d->frames - i % 3;
        if (d->backpressure) {
            do {
                cb_snapshot(d->buffer, &stats);
                if (stats.stop_requested) { d->result = 2; return 0; }
                if (stats.error) { d->result = 3; return 0; }
                if (stats.sequence - stats.consumed < stats.capacity) break;
                Sleep(1);
            } while (1);
        }
        for (j = 0; j < frames * d->channels; ++j) d->samples[j] = (float)(i * 10000 + j);
        d->result = cb_callback(d->samples, NULL, frames, &timing, 0, d->buffer);
        d->priority = GetThreadPriority(GetCurrentThread());
        if (d->result) return 0;
        cb_snapshot(d->buffer, &stats);
        d->entries[i] = stats.heartbeat_ns;
        if (d->interval) Sleep(d->interval);
    }
    return 0;
}
__declspec(dllexport) void *driver_start(cb_buffer *b, uint32_t count, uint32_t frames,
    uint32_t channels, uint32_t interval, uint32_t backpressure) {
    driver *d;
    if (!b || !count || frames < 3 || !channels) return NULL;
    d = (driver *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*d));
    if (!d) return NULL;
    d->buffer = b; d->count = count; d->frames = frames; d->channels = channels;
    d->interval = interval; d->backpressure = backpressure;
    d->samples = (float *)HeapAlloc(GetProcessHeap(), 0, (size_t)frames * channels * sizeof(float));
    d->entries = (uint64_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, (size_t)count * sizeof(uint64_t));
    if (d->samples && d->entries) d->thread = CreateThread(NULL, 0, produce, d, 0, NULL);
    if (!d->thread) {
        if (d->samples) HeapFree(GetProcessHeap(), 0, d->samples);
        if (d->entries) HeapFree(GetProcessHeap(), 0, d->entries);
        HeapFree(GetProcessHeap(), 0, d); return NULL;
    }
    return d;
}
__declspec(dllexport) int driver_wait(driver *d, uint32_t timeout) {
    return WaitForSingleObject(d->thread, timeout) == WAIT_OBJECT_0;
}
__declspec(dllexport) uint64_t driver_entry(driver *d, uint32_t index) {
    return index < d->count ? d->entries[index] : 0;
}
__declspec(dllexport) int driver_result(driver *d) { return d->result; }
__declspec(dllexport) int driver_priority(driver *d) { return d->priority; }
__declspec(dllexport) void driver_destroy(driver *d) {
    if (!d || WaitForSingleObject(d->thread, 0) != WAIT_OBJECT_0) return;
    CloseHandle(d->thread);
    HeapFree(GetProcessHeap(), 0, d->samples);
    HeapFree(GetProcessHeap(), 0, d->entries);
    HeapFree(GetProcessHeap(), 0, d);
}
