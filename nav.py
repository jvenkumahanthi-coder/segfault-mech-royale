"""Navigation: walkability grid (from navdata) + incremental A* + line-of-sight smoothing."""
import base64
import heapq
import math
from array import array

import navdata as D

C, X0, Y0, NX, NY = D.C, D.X0, D.Y0, D.NX, D.NY
N = NX * NY
SQ2 = math.sqrt(2.0)


def _decode():
    b = base64.b64decode(D.RLE_B64)
    runs, i = [], 0
    while i < len(b):
        v = s = 0
        while True:
            x = b[i]
            i += 1
            v |= (x & 127) << s
            s += 7
            if x < 128:
                break
        runs.append(v)
    raw = bytearray(N)
    pos, val = 0, 0
    for r in runs:
        if val:
            raw[pos:pos + r] = b"\x01" * r
        pos += r
        val ^= 1
    if pos != N or sum(raw) != D.BLOCKED_COUNT:
        raw = bytearray(N)  # corrupted: assume open
    return raw


RAW = _decode()


def _dilate(src, rad):
    out = bytearray(src)
    for _ in range(rad):
        cur = bytearray(out)
        for j in range(NY):
            row = j * NX
            for i in range(NX):
                k = row + i
                if cur[k]:
                    continue
                if (i > 0 and cur[k - 1]) or (i < NX - 1 and cur[k + 1]) or \
                   (j > 0 and cur[k - NX]) or (j < NY - 1 and cur[k + NX]):
                    out[k] = 1
    return out


BLK = _dilate(RAW, 1)  # planning grid: mech radius 2.5 m


def cell(x, y):
    i = int((x - X0) // C)
    j = int((y - Y0) // C)
    if i < 0:
        i = 0
    elif i >= NX:
        i = NX - 1
    if j < 0:
        j = 0
    elif j >= NY:
        j = NY - 1
    return j * NX + i


def center(k):
    j, i = divmod(k, NX)
    return (X0 + (i + 0.5) * C, Y0 + (j + 0.5) * C)


def blocked_at(x, y, grid=BLK):
    i = int((x - X0) // C)
    j = int((y - Y0) // C)
    if i < 0 or j < 0 or i >= NX or j >= NY:
        return True
    return bool(grid[j * NX + i])


def nearest_free(x, y, maxr=20, grid=BLK):
    k = cell(x, y)
    if not grid[k]:
        return k
    j0, i0 = divmod(k, NX)
    for r in range(1, maxr):
        best = None
        for dj in range(-r, r + 1):
            for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                i, j = i0 + di, j0 + dj
                if 0 <= i < NX and 0 <= j < NY and not grid[j * NX + i]:
                    d = di * di + dj * dj
                    if best is None or d < best[0]:
                        best = (d, j * NX + i)
        if best:
            return best[1]
    return k


def los(ax, ay, bx, by, grid=BLK, step=1.0):
    dx, dy = bx - ax, by - ay
    d = math.hypot(dx, dy)
    n = int(d / step) + 1
    for s in range(1, n + 1):
        f = s / n
        if blocked_at(ax + dx * f, ay + dy * f, grid):
            return False
    return True


def cover_score(k, rad=3):
    j0, i0 = divmod(k, NX)
    c = 0
    for dj in range(-rad, rad + 1):
        j = j0 + dj
        if j < 0 or j >= NY:
            continue
        for di in range(-rad, rad + 1):
            i = i0 + di
            if 0 <= i < NX and RAW[j * NX + i]:
                c += 1
    return c


_NB = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
       (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2)]


class AStar:
    """Incremental A*: call step(budget) every tick until done."""

    def __init__(self, sx, sy, gx, gy, penalty=None):
        self.s = nearest_free(sx, sy)
        self.g = nearest_free(gx, gy)
        self.goal_xy = (gx, gy)
        self.penalty = penalty  # optional fn(k)->extra cost
        gj, gi = divmod(self.g, NX)
        self.gi, self.gj = gi, gj
        self.dist = {self.s: 0.0}
        self.came = {}
        self.heap = [(self._h(self.s), 0.0, self.s)]
        self.closed = set()
        self.done = False
        self.path = None
        self.best = (self._h(self.s), self.s)

    def _h(self, k):
        j, i = divmod(k, NX)
        dx, dy = abs(i - self.gi), abs(j - self.gj)
        return (dx + dy) + (SQ2 - 2) * min(dx, dy)

    def step(self, budget=3000):
        if self.done:
            return True
        heap, dist, came, closed = self.heap, self.dist, self.came, self.closed
        blk = BLK
        pen = self.penalty
        n = 0
        while heap and n < budget:
            f, gk, k = heapq.heappop(heap)
            if k in closed:
                continue
            closed.add(k)
            n += 1
            h = f - gk
            if h < self.best[0]:
                self.best = (h, k)
            if k == self.g:
                self._finish(k)
                return True
            j, i = divmod(k, NX)
            for di, dj, w in _NB:
                ii, jj = i + di, j + dj
                if ii < 0 or jj < 0 or ii >= NX or jj >= NY:
                    continue
                nk = jj * NX + ii
                if blk[nk] or nk in closed:
                    continue
                if di and dj and (blk[j * NX + ii] or blk[jj * NX + i]):
                    continue
                ng = gk + w
                if pen is not None:
                    ng += pen(nk)
                if ng < dist.get(nk, 1e18):
                    dist[nk] = ng
                    came[nk] = k
                    heapq.heappush(heap, (ng + self._h(nk), ng, nk))
            if len(closed) > 60000:
                break
        if not heap or len(closed) > 60000:
            self._finish(self.best[1])  # partial path to closest point
            return True
        return False

    def _finish(self, k):
        cells = [k]
        while k in self.came:
            k = self.came[k]
            cells.append(k)
        cells.reverse()
        pts = [center(c) for c in cells]
        self.path = smooth(pts)
        self.done = True


def smooth(pts):
    if len(pts) <= 2:
        return pts
    out = [pts[0]]
    i = 0
    while i < len(pts) - 1:
        j = min(len(pts) - 1, i + 40)
        while j > i + 1 and not los(pts[i][0], pts[i][1], pts[j][0], pts[j][1]):
            j -= 1
        out.append(pts[j])
        i = j
    return out
