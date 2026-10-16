/**
 * nff_sha256_fallback.h — Minimal SHA-256 implementation (public domain).
 *
 * Used only when neither mbedTLS nor OpenSSL are available (e.g., bare MinGW).
 * Based on the reference FIPS 180-4 algorithm.
 */

#ifndef NFF_SHA256_FALLBACK_H
#define NFF_SHA256_FALLBACK_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>

typedef struct {
    uint32_t state[8];
    uint64_t count;
    uint8_t  buf[64];
} nff_sha256_fallback_t;

static const uint32_t SHA256_K[64] = {
    0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,
    0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
    0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,
    0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
    0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,
    0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
    0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,
    0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
    0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,
    0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
    0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,
    0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
    0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,
    0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
    0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,
    0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u
};

#define RR(x,n) (((x)>>(n))|((x)<<(32-(n))))
#define CH(x,y,z)  (((x)&(y))^(~(x)&(z)))
#define MAJ(x,y,z) (((x)&(y))^((x)&(z))^((y)&(z)))
#define EP0(x) (RR(x,2)^RR(x,13)^RR(x,22))
#define EP1(x) (RR(x,6)^RR(x,11)^RR(x,25))
#define SIG0(x) (RR(x,7)^RR(x,18)^((x)>>3))
#define SIG1(x) (RR(x,17)^RR(x,19)^((x)>>10))

static void nff_sha256_transform(nff_sha256_fallback_t *ctx, const uint8_t *d) {
    uint32_t a,b,c,e,f,g,h,t1,t2,m[64];
    uint32_t *s = ctx->state;
    for (int i=0,j=0; i<16; i++,j+=4)
        m[i] = ((uint32_t)d[j]<<24)|((uint32_t)d[j+1]<<16)|((uint32_t)d[j+2]<<8)|d[j+3];
    for (int i=16; i<64; i++)
        m[i] = SIG1(m[i-2])+m[i-7]+SIG0(m[i-15])+m[i-16];
    a=s[0]; b=s[1]; c=s[2]; uint32_t dd=s[3];
    e=s[4]; f=s[5]; g=s[6]; h=s[7];
    for (int i=0; i<64; i++) {
        t1 = h+EP1(e)+CH(e,f,g)+SHA256_K[i]+m[i];
        t2 = EP0(a)+MAJ(a,b,c);
        h=g; g=f; f=e; e=dd+t1;
        dd=c; c=b; b=a; a=t1+t2;
    }
    s[0]+=a; s[1]+=b; s[2]+=c; s[3]+=dd;
    s[4]+=e; s[5]+=f; s[6]+=g; s[7]+=h;
}

static inline void nff_sha256_fallback_init(nff_sha256_fallback_t *ctx) {
    ctx->count = 0;
    ctx->state[0]=0x6a09e667u; ctx->state[1]=0xbb67ae85u;
    ctx->state[2]=0x3c6ef372u; ctx->state[3]=0xa54ff53au;
    ctx->state[4]=0x510e527fu; ctx->state[5]=0x9b05688cu;
    ctx->state[6]=0x1f83d9abu; ctx->state[7]=0x5be0cd19u;
}

static inline void nff_sha256_fallback_update(nff_sha256_fallback_t *ctx,
                                               const uint8_t *data, size_t len) {
    uint32_t used = (uint32_t)(ctx->count & 63);
    ctx->count += len;
    if (used) {
        uint32_t avail = 64 - used;
        if (len < avail) { memcpy(ctx->buf + used, data, len); return; }
        memcpy(ctx->buf + used, data, avail);
        nff_sha256_transform(ctx, ctx->buf);
        data += avail; len -= avail;
    }
    while (len >= 64) {
        nff_sha256_transform(ctx, data);
        data += 64; len -= 64;
    }
    if (len) memcpy(ctx->buf, data, len);
}

static inline void nff_sha256_fallback_finish(nff_sha256_fallback_t *ctx, uint8_t out[32]) {
    uint32_t used = (uint32_t)(ctx->count & 63);
    ctx->buf[used++] = 0x80;
    if (used > 56) {
        memset(ctx->buf + used, 0, 64 - used);
        nff_sha256_transform(ctx, ctx->buf);
        used = 0;
    }
    memset(ctx->buf + used, 0, 56 - used);
    uint64_t bits = ctx->count * 8;
    ctx->buf[56] = (uint8_t)(bits >> 56); ctx->buf[57] = (uint8_t)(bits >> 48);
    ctx->buf[58] = (uint8_t)(bits >> 40); ctx->buf[59] = (uint8_t)(bits >> 32);
    ctx->buf[60] = (uint8_t)(bits >> 24); ctx->buf[61] = (uint8_t)(bits >> 16);
    ctx->buf[62] = (uint8_t)(bits >> 8);  ctx->buf[63] = (uint8_t)(bits);
    nff_sha256_transform(ctx, ctx->buf);
    for (int i = 0, j = 0; i < 8; i++, j += 4) {
        out[j]   = (uint8_t)(ctx->state[i] >> 24);
        out[j+1] = (uint8_t)(ctx->state[i] >> 16);
        out[j+2] = (uint8_t)(ctx->state[i] >> 8);
        out[j+3] = (uint8_t)(ctx->state[i]);
    }
}

#undef RR
#undef CH
#undef MAJ
#undef EP0
#undef EP1
#undef SIG0
#undef SIG1

#endif /* NFF_SHA256_FALLBACK_H */
