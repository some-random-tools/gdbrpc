#include <stdio.h>
#include <stdlib.h>

static void *chunks[16];

static int work(int i)
{
    return i * 2;
}

static void heap_pause(void)
{
    /* breakpoint marker: heap state is set up here */
}

int main(void)
{
    int total = 0;

    /* heap allocations of various sizes (tcache / fastbin / smallbin
     * territory: 0x30..0xa0 chunks) — no work() calls here so the
     * first work breakpoint hit lands AFTER the frees (tcache filled)
     */
    for (int i = 0; i < 8; i++) {
        chunks[i] = malloc(0x20 + i * 0x10);
    }

    /* frees populate tcache bins */
    free(chunks[0]);
    free(chunks[1]);
    free(chunks[2]);

    heap_pause();

    /* allocate again (partially drains tcache) + bigger chunks */
    for (int i = 8; i < 12; i++) {
        chunks[i] = malloc(0x80 + (i - 8) * 0x40);
    }
    free(chunks[10]);
    free(chunks[11]);

    heap_pause();

    for (int i = 0; i < 50; i++) {
        total += work(i);
        printf("i=%d total=%d\n", i, total);
    }
    return total;
}