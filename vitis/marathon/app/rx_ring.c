#include <string.h>
#include "rx_ring.h"

#define RX_RING_SIZE 131072   /* 128 KB */

static uint8_t  rx_ring[RX_RING_SIZE];
static uint32_t rx_head = 0;  /* write index */
static uint32_t rx_tail = 0;  /* read index */

uint32_t rx_ring_used(void)
{
    if (rx_head >= rx_tail)
        return rx_head - rx_tail;
    else
        return RX_RING_SIZE - (rx_tail - rx_head);
}

uint32_t rx_ring_free(void)
{
    return RX_RING_SIZE - rx_ring_used() - 1;
}

void rx_ring_push(const uint8_t *data, uint32_t len)
{
    uint32_t first_chunk = RX_RING_SIZE - rx_head;
    if (first_chunk >= len) {
        memcpy(&rx_ring[rx_head], data, len);
        rx_head = (rx_head + len) % RX_RING_SIZE;
    } else {
        memcpy(&rx_ring[rx_head], data, first_chunk);
        memcpy(&rx_ring[0], data + first_chunk, len - first_chunk);
        rx_head = len - first_chunk;
    }
}

void rx_ring_peek(uint32_t offset, uint8_t *dst, uint32_t len)
{
    uint32_t start = (rx_tail + offset) % RX_RING_SIZE;
    uint32_t first_chunk = RX_RING_SIZE - start;
    if (first_chunk >= len) {
        memcpy(dst, &rx_ring[start], len);
    } else {
        memcpy(dst, &rx_ring[start], first_chunk);
        memcpy(dst + first_chunk, &rx_ring[0], len - first_chunk);
    }
}

void rx_ring_advance(uint32_t len)
{
    rx_tail = (rx_tail + len) % RX_RING_SIZE;
}

void rx_ring_reset(void)
{
    rx_head = 0;
    rx_tail = 0;
}
