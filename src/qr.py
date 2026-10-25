"""A tiny QR code generator (standard library only), so the dashboard can show a code your phone scans.

    svg = qr.svg("http://192.168.1.20:8788/?pin=123456")

Byte mode, error correction level M, versions 1 to 6 (up to 106 characters): plenty for a local address.
Written from the QR Code Model 2 specification (ISO/IEC 18004).
"""

from __future__ import annotations

# (total codewords, EC codewords per block, [(blocks, data codewords per block), ...]) for level M
_M = {1: (26, 10, [(1, 16)]), 2: (44, 16, [(1, 28)]), 3: (70, 26, [(1, 44)]),
      4: (100, 18, [(2, 32)]), 5: (134, 24, [(2, 43)]), 6: (172, 16, [(4, 27)])}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}

_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i], _LOG[_x] = _x, _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_remainder(data: list[int], n: int) -> list[int]:
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j] ^= c
            nxt[j + 1] ^= _mul(c, _EXP[i])
        gen = nxt
    rem = [0] * n
    for d in data:
        f = d ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(n):
            rem[i] ^= _mul(gen[i + 1], f)
    return rem


def _bits(value: int, n: int) -> list[int]:
    return [(value >> i) & 1 for i in range(n - 1, -1, -1)]


def _codewords(data: bytes, version: int) -> list[int]:
    total, ec, groups = _M[version]
    capacity = sum(b * d for b, d in groups)
    bits = _bits(0b0100, 4) + _bits(len(data), 8)
    for byte in data:
        bits += _bits(byte, 8)
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    while len(words) < capacity:
        words.append(pad[(len(words) - len(bits) // 8) % 2])
    blocks, pos = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[pos:pos + size])
            pos += size
    ecs = [_rs_remainder(b, ec) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec):
        out += [e[i] for e in ecs]
    assert len(out) == total
    return out


def _format_bits(mask: int) -> int:
    data = (0b00 << 3) | mask            # level M = 00
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _build(version: int, words: list[int], mask: int) -> list[list[bool]]:
    n = version * 4 + 17
    mod = [[False] * n for _ in range(n)]
    fn = [[False] * n for _ in range(n)]   # function patterns (not data)

    def put(r: int, c: int, v: bool) -> None:
        if 0 <= r < n and 0 <= c < n:
            mod[r][c] = v
            fn[r][c] = True

    for r, c in ((0, 0), (0, n - 7), (n - 7, 0)):   # finder patterns + separators
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                ring = dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                put(r + dr, c + dc, inside and ring)
    for i in range(8, n - 8):                      # timing
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    pos = _ALIGN[version]
    for r in pos:
        for c in pos:
            if fn[r][c]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
    put(n - 8, 8, True)                            # the dark module
    fmt = _format_bits(mask)
    for i in range(15):                            # format information, both copies
        bit = bool((fmt >> i) & 1)
        if i < 6:
            put(i, 8, bit)
        elif i < 8:
            put(i + 1, 8, bit)
        elif i == 8:
            put(8, 7, bit)
        else:
            put(8, 14 - i, bit)
        if i < 8:
            put(8, n - 1 - i, bit)
        else:
            put(n - 15 + i, 8, bit)
    bits = [b for w in words for b in _bits(w, 8)]
    k, up, col = 0, True, n - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(n - 1, -1, -1) if up else range(n)
        for r in rows:
            for c in (col, col - 1):
                if fn[r][c]:
                    continue
                bit = bits[k] if k < len(bits) else 0
                k += 1
                inv = [(r + c) % 2 == 0, r % 2 == 0, c % 3 == 0, (r + c) % 3 == 0,
                       (r // 2 + c // 3) % 2 == 0, (r * c) % 2 + (r * c) % 3 == 0,
                       ((r * c) % 2 + (r * c) % 3) % 2 == 0, ((r + c) % 2 + (r * c) % 3) % 2 == 0][mask]
                mod[r][c] = bool(bit) ^ inv
        up = not up
        col -= 2
    return mod


def _penalty(m: list[list[bool]]) -> int:
    n, score = len(m), 0
    for grid in (m, [list(r) for r in zip(*m)]):    # rows, then columns
        for row in grid:
            run = 1
            for i in range(1, n):
                if row[i] == row[i - 1]:
                    run += 1
                    if run == 5:
                        score += 3
                    elif run > 5:
                        score += 1
                else:
                    run = 1
            s = "".join("1" if v else "0" for v in row)
            score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(v for row in m for v in row)
    score += 10 * (abs(dark * 100 // (n * n) - 50) // 5)
    return score


def matrix(text: str) -> list[list[bool]]:
    data = text.encode("utf-8")
    for version in range(1, 7):
        if len(data) <= sum(b * d for b, d in _M[version][2]) - 2:
            break
    else:
        raise ValueError("too long for this QR generator (106 characters at most)")
    words = _codewords(data, version)
    best = min((_build(version, words, mk) for mk in range(8)), key=_penalty)
    return best


def svg(text: str, border: int = 4, dark: str = "#000", light: str = "#fff") -> str:
    m = matrix(text)
    n = len(m)
    cells = "".join(f"M{c + border},{r + border}h1v1h-1z" for r, row in enumerate(m) for c, v in enumerate(row) if v)
    size = n + 2 * border
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges" role="img" '
            f'aria-label="QR code"><rect width="{size}" height="{size}" fill="{light}"/><path d="{cells}" fill="{dark}"/></svg>')
