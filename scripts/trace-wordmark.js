const fs = require("fs");
const zlib = require("zlib");

// ---------- 1. decode the PNG (8-bit RGB / RGBA, no interlace) ----------
function decodePNG(file) {
  const buf = fs.readFileSync(file);
  let pos = 8, w = 0, h = 0, bitDepth = 0, colorType = 0;
  const idat = [];
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos);
    const type = buf.toString("ascii", pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === "IHDR") {
      w = data.readUInt32BE(0); h = data.readUInt32BE(4);
      bitDepth = data[8]; colorType = data[9];
      if (data[12] !== 0) throw new Error("interlaced PNG unsupported");
    } else if (type === "IDAT") idat.push(data);
    else if (type === "IEND") break;
    pos += 12 + len;
  }
  if (bitDepth !== 8) throw new Error("bitDepth " + bitDepth + " unsupported");
  const channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[colorType];
  if (!channels) throw new Error("colorType " + colorType + " unsupported");

  const raw = zlib.inflateSync(Buffer.concat(idat));
  const bpp = channels;              // bytes per pixel at 8-bit
  const stride = w * bpp;
  const out = Buffer.alloc(h * stride);
  let rp = 0;
  for (let y = 0; y < h; y++) {
    const filter = raw[rp++];
    const line = raw.subarray(rp, rp + stride); rp += stride;
    const cur = out.subarray(y * stride, (y + 1) * stride);
    const prev = y > 0 ? out.subarray((y - 1) * stride, y * stride) : null;
    for (let i = 0; i < stride; i++) {
      const A = i >= bpp ? cur[i - bpp] : 0;
      const B = prev ? prev[i] : 0;
      const C = prev && i >= bpp ? prev[i - bpp] : 0;
      let v = line[i];
      switch (filter) {
        case 0: break;
        case 1: v += A; break;
        case 2: v += B; break;
        case 3: v += (A + B) >> 1; break;
        case 4: {
          const p = A + B - C;
          const pa = Math.abs(p - A), pb = Math.abs(p - B), pc = Math.abs(p - C);
          v += (pa <= pb && pa <= pc) ? A : (pb <= pc ? B : C);
          break;
        }
        default: throw new Error("bad filter " + filter);
      }
      cur[i] = v & 0xff;
    }
  }
  return { w, h, channels, data: out };
}

// ---------- 2. binary ink mask ----------
const INK_LUMA = 190;                       // letters are dark green -> black on white
function inkMask({ w, h, channels, data }) {
  const m = new Uint8Array(w * h);
  for (let i = 0, p = 0; i < w * h; i++, p += channels) {
    let r, g, b, a = 255;
    if (channels >= 3) { r = data[p]; g = data[p+1]; b = data[p+2]; if (channels === 4) a = data[p+3]; }
    else { r = g = b = data[p]; if (channels === 2) a = data[p+1]; }
    if (a < 128) { m[i] = 0; continue; }
    const luma = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    m[i] = luma < INK_LUMA ? 1 : 0;
  }
  return m;
}

// ---------- 3. crack-following contour extraction ----------
// For every ink pixel, emit the directed boundary edges facing background.
// Chaining them yields closed loops: outer boundaries and holes, consistently wound.
function contours(mask, w, h) {
  const at = (x, y) => (x < 0 || y < 0 || x >= w || y >= h) ? 0 : mask[y * w + x];
  const key = (x, y) => x + y * (w + 1);
  const next = new Map();                   // start point -> [end points]
  const push = (x1, y1, x2, y2) => {
    const k = key(x1, y1);
    if (!next.has(k)) next.set(k, []);
    next.get(k).push([x2, y2]);
  };
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (!at(x, y)) continue;
      if (!at(x, y - 1)) push(x, y, x + 1, y);           // top    ->
      if (!at(x + 1, y)) push(x + 1, y, x + 1, y + 1);   // right  v
      if (!at(x, y + 1)) push(x + 1, y + 1, x, y + 1);   // bottom <-
      if (!at(x - 1, y)) push(x, y + 1, x, y);           // left   ^
    }
  }
  const loops = [];
  for (const startKey of [...next.keys()]) {
    while (next.has(startKey) && next.get(startKey).length) {
      const start = [startKey % (w + 1), Math.floor(startKey / (w + 1))];
      const loop = [start];
      let cur = start;
      for (let guard = 0; guard < 4_000_000; guard++) {
        const k = key(cur[0], cur[1]);
        const outs = next.get(k);
        if (!outs || !outs.length) break;
        const nxt = outs.pop();
        if (!outs.length) next.delete(k);
        cur = nxt;
        if (cur[0] === start[0] && cur[1] === start[1]) break;
        loop.push(cur);
      }
      if (loop.length > 12) loops.push(loop);
    }
  }
  return loops;
}

// ---------- 4. Douglas-Peucker ----------
function simplify(pts, eps) {
  if (pts.length < 4) return pts;
  const keep = new Uint8Array(pts.length); keep[0] = keep[pts.length - 1] = 1;
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    const [ax, ay] = pts[a], [bx, by] = pts[b];
    const dx = bx - ax, dy = by - ay;
    const len = Math.hypot(dx, dy) || 1;
    let far = -1, fd = eps;
    for (let i = a + 1; i < b; i++) {
      const d = Math.abs((pts[i][0] - ax) * dy - (pts[i][1] - ay) * dx) / len;
      if (d > fd) { fd = d; far = i; }
    }
    if (far > 0) { keep[far] = 1; stack.push([a, far], [far, b]); }
  }
  return pts.filter((_, i) => keep[i]);
}

// ---------- 5. smooth closed polygon -> quadratic path ----------
// Vertices become control points, midpoints become on-curve points. Rounds the
// stair-steps of a pixel trace into the brush curves of the original artwork.
function toPath(pts, sx, sy, ox, oy, prec = 2) {
  const n = pts.length;
  const P = (i) => { const p = pts[(i + n) % n]; return [(p[0] - ox) * sx, (p[1] - oy) * sy]; };
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const f = (v) => Number(v.toFixed(prec));
  let m0 = mid(P(0), P(1));
  let d = `M${f(m0[0])} ${f(m0[1])}`;
  for (let i = 1; i <= n; i++) {
    const c = P(i), m = mid(P(i), P(i + 1));
    d += `Q${f(c[0])} ${f(c[1])} ${f(m[0])} ${f(m[1])}`;
  }
  return d + "Z";
}

// ---------- run ----------
const SRC = process.argv[2];
const png = decodePNG(SRC);
const mask = inkMask(png);
const { w, h } = png;

// ink bounding box, so the viewBox is tight instead of mostly margin
let minX = w, minY = h, maxX = -1, maxY = -1;
for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) if (mask[y * w + x]) {
  if (x < minX) minX = x; if (x > maxX) maxX = x;
  if (y < minY) minY = y; if (y > maxY) maxY = y;
}
const bw = maxX - minX + 1, bh = maxY - minY + 1;

const loops = contours(mask, w, h)
  .map((l) => simplify(l, 1.6))
  .filter((l) => l.length > 8);

// normalise to a 1000-wide viewBox
const VB_W = 1000;
const scale = VB_W / bw;
const VB_H = Number((bh * scale).toFixed(2));
const paths = loops
  .map((l) => ({ d: toPath(l, scale, scale, minX, minY), n: l.length }))
  .sort((a, b) => b.n - a.n);

console.log(JSON.stringify({
  source: SRC, imageSize: [w, h], inkBBox: [minX, minY, bw, bh],
  viewBox: `0 0 ${VB_W} ${VB_H}`, loops: paths.length,
  totalPathBytes: paths.reduce((s, p) => s + p.d.length, 0),
  pointCounts: paths.map((p) => p.n),
}, null, 2));
fs.writeFileSync(process.argv[3], JSON.stringify({ viewBox: `0 0 ${VB_W} ${VB_H}`, paths: paths.map(p => p.d) }));
