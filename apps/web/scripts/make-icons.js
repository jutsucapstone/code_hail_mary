/**
 * Generates the app icons AND the served logo from assets/jutsu-logo-source.png.
 *
 * The full-resolution artwork lives in assets/ and is not served. public/ gets
 * a 256px copy: the mark never renders above 32px, so shipping the 1254px
 * original was ~900KB of pure deploy weight.
 *
 * Pure Node: decodes the source PNG, box-filters it down (proper area
 * averaging, not nearest-neighbour, so the glossy gradients stay smooth), and
 * re-encodes. Avoids adding sharp as a dependency for three build-time files.
 *
 *   node scripts/make-icons.js
 */
const fs = require("fs");
const zlib = require("zlib");

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
  const ch = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[colorType];
  if (!ch) throw new Error("colorType " + colorType + " unsupported");
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = w * ch;
  const out = Buffer.alloc(h * stride);
  let rp = 0;
  for (let y = 0; y < h; y++) {
    const f = raw[rp++];
    const line = raw.subarray(rp, rp + stride); rp += stride;
    const cur = out.subarray(y * stride, (y + 1) * stride);
    const prev = y > 0 ? out.subarray((y - 1) * stride, y * stride) : null;
    for (let i = 0; i < stride; i++) {
      const A = i >= ch ? cur[i - ch] : 0;
      const B = prev ? prev[i] : 0;
      const C = prev && i >= ch ? prev[i - ch] : 0;
      let v = line[i];
      if (f === 1) v += A;
      else if (f === 2) v += B;
      else if (f === 3) v += (A + B) >> 1;
      else if (f === 4) {
        const p = A + B - C;
        const pa = Math.abs(p - A), pb = Math.abs(p - B), pc = Math.abs(p - C);
        v += (pa <= pb && pa <= pc) ? A : (pb <= pc ? B : C);
      } else if (f !== 0) throw new Error("bad filter " + f);
      cur[i] = v & 0xff;
    }
  }
  // normalise to RGBA
  const rgba = Buffer.alloc(w * h * 4);
  for (let i = 0; i < w * h; i++) {
    const s = i * ch, d = i * 4;
    if (ch >= 3) {
      rgba[d] = out[s]; rgba[d+1] = out[s+1]; rgba[d+2] = out[s+2];
      rgba[d+3] = ch === 4 ? out[s+3] : 255;
    } else {
      rgba[d] = rgba[d+1] = rgba[d+2] = out[s];
      rgba[d+3] = ch === 2 ? out[s+1] : 255;
    }
  }
  return { w, h, data: rgba };
}

/** Area-average downsample, premultiplying alpha so edges don't fringe. */
function resize(src, size, pad, bg) {
  const dst = Buffer.alloc(size * size * 4);
  const inner = size - pad * 2;
  const sx = src.w / inner, sy = src.h / inner;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const d = (y * size + x) * 4;
      const ix = x - pad, iy = y - pad;
      let r = 0, g = 0, b = 0, a = 0;
      if (ix >= 0 && iy >= 0 && ix < inner && iy < inner) {
        const x0 = Math.floor(ix * sx), x1 = Math.max(x0 + 1, Math.floor((ix + 1) * sx));
        const y0 = Math.floor(iy * sy), y1 = Math.max(y0 + 1, Math.floor((iy + 1) * sy));
        let n = 0;
        for (let yy = y0; yy < y1 && yy < src.h; yy++) {
          for (let xx = x0; xx < x1 && xx < src.w; xx++) {
            const s = (yy * src.w + xx) * 4;
            const sa = src.data[s + 3] / 255;
            r += src.data[s] * sa; g += src.data[s+1] * sa; b += src.data[s+2] * sa;
            a += src.data[s + 3];
            n++;
          }
        }
        if (n) { r /= n; g /= n; b /= n; a /= n; }
      }
      const alpha = a / 255;
      if (bg) {
        // composite over an opaque plate (iOS ignores icon transparency)
        dst[d]   = Math.round(r + bg[0] * (1 - alpha));
        dst[d+1] = Math.round(g + bg[1] * (1 - alpha));
        dst[d+2] = Math.round(b + bg[2] * (1 - alpha));
        dst[d+3] = 255;
      } else {
        // un-premultiply back to straight alpha
        dst[d]   = alpha ? Math.min(255, Math.round(r / alpha)) : 0;
        dst[d+1] = alpha ? Math.min(255, Math.round(g / alpha)) : 0;
        dst[d+2] = alpha ? Math.min(255, Math.round(b / alpha)) : 0;
        dst[d+3] = Math.round(a);
      }
    }
  }
  return { w: size, h: size, data: dst };
}

function encodePNG({ w, h, data }) {
  const raw = Buffer.alloc(h * (w * 4 + 1));
  for (let y = 0; y < h; y++) {
    raw[y * (w * 4 + 1)] = 0; // filter: none
    data.copy(raw, y * (w * 4 + 1) + 1, y * w * 4, (y + 1) * w * 4);
  }
  const chunk = (type, body) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(body.length);
    const td = Buffer.concat([Buffer.from(type, "ascii"), body]);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td) >>> 0);
    return Buffer.concat([len, td, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; ihdr[9] = 6; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

let TBL = null;
function crc32(buf) {
  if (!TBL) {
    TBL = new Int32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      TBL[n] = c;
    }
  }
  let c = -1;
  for (let i = 0; i < buf.length; i++) c = TBL[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return c ^ -1;
}

const src = decodePNG("assets/jutsu-logo-source.png");
const targets = [
  { out: "public/jutsu-logo.png", size: 256, pad: 0, bg: null },
  { out: "app/icon.png", size: 192, pad: 4, bg: null },
  // iOS composites away transparency; the logo's black lobe would vanish on a
  // dark plate, so this one ships on the light brand ground.
  { out: "app/apple-icon.png", size: 180, pad: 16, bg: [249, 250, 251] },
];
for (const t of targets) {
  fs.writeFileSync(t.out, encodePNG(resize(src, t.size, t.pad, t.bg)));
  console.log(t.out, fs.statSync(t.out).size, "bytes", `(${t.size}x${t.size})`);
}
