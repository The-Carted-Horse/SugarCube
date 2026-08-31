/*
 * See gc_synclog.h.
 */

#include "gc_synclog.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static gc_synclog_entry_t s_entries[GC_SYNCLOG_ENTRIES];
static int s_count;
static int s_head;
static SemaphoreHandle_t s_lock;
static StaticSemaphore_t s_lock_storage;

/* Created on first use rather than in an init(): a poller that logs before
 * anything called init would otherwise either crash or lose the event, and
 * the first event is usually the interesting one. Static storage so this
 * cannot fail. */
static SemaphoreHandle_t lock(void)
{
    if (s_lock == NULL) {
        s_lock = xSemaphoreCreateMutexStatic(&s_lock_storage);
    }
    return s_lock;
}

void gc_synclog_add(const char *source, const char *user, bool ok,
                    const char *fmt, ...)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);

    gc_synclog_entry_t entry = {
        .ms = (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000,
        .ok = ok,
    };
    snprintf(entry.source, sizeof(entry.source), "%s",
             source != NULL ? source : "");
    snprintf(entry.user, sizeof(entry.user), "%s",
             user != NULL ? user : "system");

    va_list args;
    va_start(args, fmt);
    vsnprintf(entry.message, sizeof(entry.message), fmt, args);
    va_end(args);

    xSemaphoreTake(lock(), portMAX_DELAY);
    s_entries[s_head] = entry;
    s_head = (s_head + 1) % GC_SYNCLOG_ENTRIES;
    if (s_count < GC_SYNCLOG_ENTRIES) {
        s_count++;
    }
    xSemaphoreGive(s_lock);
}

int gc_synclog_recent(gc_synclog_entry_t *out, int limit)
{
    if (out == NULL || limit <= 0) {
        return 0;
    }
    xSemaphoreTake(lock(), portMAX_DELAY);
    const int available = s_count < limit ? s_count : limit;
    for (int i = 0; i < available; i++) {
        int index = s_head - 1 - i;
        while (index < 0) {
            index += GC_SYNCLOG_ENTRIES;
        }
        out[i] = s_entries[index];
    }
    xSemaphoreGive(s_lock);
    return available;
}
