/*
 * Hot loops for the PIR servers/clients. Built on first import by kernels.py
 * (gcc -O3 -march=native); numpy fallbacks exist if no compiler is available.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

/*
 * Two-server XOR PIR, server side.
 *   db:    n_rows x row_words uint64, row-major
 *   masks: n_q x mask_bytes, bit i (LSB-first within byte) selects row i
 *   out:   n_q x row_words; out[b] = XOR of the rows selected by masks[b]
 * Tiled over the row so every DB byte is read from RAM once per pass even
 * when several queries are answered together.
 */
#define XOR_TILE 2048 /* words = 16 KiB */
void xor_select_batch(const uint64_t *restrict db, size_t n_rows, size_t row_words,
                      const uint8_t *restrict masks, size_t mask_bytes, size_t n_q,
                      uint64_t *restrict out)
{
    memset(out, 0, n_q * row_words * sizeof(uint64_t));
    for (size_t w0 = 0; w0 < row_words; w0 += XOR_TILE) {
        size_t wn = row_words - w0 < XOR_TILE ? row_words - w0 : XOR_TILE;
        for (size_t i = 0; i < n_rows; i++) {
            const uint64_t *row = db + i * row_words + w0;
            for (size_t b = 0; b < n_q; b++) {
                if ((masks[b * mask_bytes + (i >> 3)] >> (i & 7)) & 1) {
                    uint64_t *restrict o = out + b * row_words + w0;
                    for (size_t w = 0; w < wn; w++) o[w] ^= row[w];
                }
            }
        }
    }
}

/*
 * SimplePIR-style server answer:  out[b] = D * Q[b]  (mod 2^32)
 *   D:   l x m uint8 (entries in Z_256), row-major
 *   Q:   n_q x m uint32 (each query contiguous)
 *   out: n_q x l uint32
 * Blocked over columns so the query block stays in cache while D streams by.
 */
#define LWE_JB 8192
void lwe_answer_batch(const uint8_t *restrict D, size_t l, size_t m,
                      const uint32_t *restrict Q, size_t n_q, uint32_t *restrict out)
{
    memset(out, 0, n_q * l * sizeof(uint32_t));
    for (size_t j0 = 0; j0 < m; j0 += LWE_JB) {
        size_t jn = m - j0 < LWE_JB ? m - j0 : LWE_JB;
        for (size_t r = 0; r < l; r++) {
            const uint8_t *restrict drow = D + r * m + j0;
            for (size_t b = 0; b < n_q; b++) {
                const uint32_t *restrict q = Q + b * m + j0;
                uint32_t acc = 0;
                for (size_t j = 0; j < jn; j++) acc += (uint32_t)drow[j] * q[j];
                out[b * l + r] += acc;
            }
        }
    }
}

/*
 * Generic mod-2^32 matrix * vector batch:  out[b*rows + i] = sum_j M[i*n+j] * V[b*n+j]
 * Used for A*s (query generation) and hint*s (decoding) on the client.
 */
void u32_matvec_batch(const uint32_t *restrict M, size_t rows, size_t n,
                      const uint32_t *restrict V, size_t n_v, uint32_t *restrict out)
{
    for (size_t i = 0; i < rows; i++) {
        const uint32_t *restrict mr = M + i * n;
        for (size_t b = 0; b < n_v; b++) {
            const uint32_t *restrict v = V + b * n;
            uint32_t acc = 0;
            for (size_t j = 0; j < n; j++) acc += mr[j] * v[j];
            out[b * rows + i] = acc;
        }
    }
}
