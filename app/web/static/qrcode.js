/**
 * Minimal QR Code encoder: byte mode, error-correction level M, versions 1-10.
 *
 * The console previously drew three finder patterns and then filled the module
 * grid from an xorshift PRNG seeded by a hash of the address. It looked like a
 * QR code, was labelled "入网码", and no scanner on earth could read it.
 *
 * This is a real encoder. It is vendored rather than loaded from a CDN because
 * the whole point of the join page is a classroom LAN with no internet.
 *
 * Implements ISO/IEC 18004: data encoding, Reed-Solomon error correction,
 * block interleaving, function-pattern placement, all eight data masks with
 * penalty scoring, and format/version information.
 */
(function (global) {
  "use strict";

  // Total data codewords available at ECC level M, versions 1-10.
  const DATA_CODEWORDS_M = [null, 16, 28, 44, 64, 86, 108, 124, 154, 182, 216];

  // [ecCodewordsPerBlock, [blockCount, dataCodewords], ...] at ECC level M.
  const BLOCK_LAYOUT_M = [
    null,
    [10, [1, 16]],
    [16, [1, 28]],
    [26, [1, 44]],
    [18, [2, 32]],
    [24, [2, 43]],
    [16, [4, 27]],
    [18, [4, 31]],
    [22, [2, 38], [2, 39]],
    [22, [3, 36], [2, 37]],
    [26, [4, 43], [1, 44]],
  ];

  const ALIGNMENT_POSITIONS = [
    null, [], [6, 18], [6, 22], [6, 26], [6, 30],
    [6, 34], [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50],
  ];

  // Pre-computed BCH format strings for ECC level M, masks 0-7.
  const FORMAT_INFO_M = [
    0x5412, 0x5125, 0x5e7c, 0x5b4b, 0x45f9, 0x40ce, 0x4f97, 0x4aa0,
  ];

  // Version information blocks, only present from version 7 upwards.
  const VERSION_INFO = {
    7: 0x07c94, 8: 0x085bc, 9: 0x09a99, 10: 0x0a4d3,
  };

  // ---------------------------------------------------------------- GF(256)

  const EXP = new Uint8Array(512);
  const LOG = new Uint8Array(256);
  (function buildTables() {
    let value = 1;
    for (let i = 0; i < 255; i += 1) {
      EXP[i] = value;
      LOG[value] = i;
      value <<= 1;
      if (value & 0x100) value ^= 0x11d; // x^8 + x^4 + x^3 + x^2 + 1
    }
    for (let i = 255; i < 512; i += 1) EXP[i] = EXP[i - 255];
  })();

  function gfMultiply(a, b) {
    if (a === 0 || b === 0) return 0;
    return EXP[LOG[a] + LOG[b]];
  }

  function generatorPolynomial(degree) {
    let poly = [1];
    for (let i = 0; i < degree; i += 1) {
      const next = new Array(poly.length + 1).fill(0);
      for (let j = 0; j < poly.length; j += 1) {
        next[j] ^= poly[j];
        next[j + 1] ^= gfMultiply(poly[j], EXP[i]);
      }
      poly = next;
    }
    return poly;
  }

  function reedSolomon(data, ecLength) {
    const generator = generatorPolynomial(ecLength);
    const remainder = new Array(ecLength).fill(0);
    for (const byte of data) {
      const factor = byte ^ remainder[0];
      remainder.shift();
      remainder.push(0);
      if (factor !== 0) {
        for (let i = 0; i < generator.length - 1; i += 1) {
          remainder[i] ^= gfMultiply(generator[i + 1], factor);
        }
      }
    }
    return remainder;
  }

  // ------------------------------------------------------------- encoding

  function toUtf8Bytes(text) {
    return Array.from(new TextEncoder().encode(text));
  }

  function chooseVersion(byteLength) {
    for (let version = 1; version <= 10; version += 1) {
      // 2 bytes of overhead for mode + character count (4 + 8 or 4 + 16 bits)
      const countBits = version >= 10 ? 16 : 8;
      const needed = Math.ceil((4 + countBits + byteLength * 8) / 8);
      if (needed <= DATA_CODEWORDS_M[version]) return version;
    }
    return null;
  }

  function buildDataCodewords(bytes, version) {
    const capacity = DATA_CODEWORDS_M[version];
    const countBits = version >= 10 ? 16 : 8;
    const bits = [];
    const push = (value, length) => {
      for (let i = length - 1; i >= 0; i -= 1) bits.push((value >> i) & 1);
    };

    push(0b0100, 4); // byte mode
    push(bytes.length, countBits);
    for (const byte of bytes) push(byte, 8);

    // terminator, then pad to a byte boundary
    const capacityBits = capacity * 8;
    for (let i = 0; i < 4 && bits.length < capacityBits; i += 1) bits.push(0);
    while (bits.length % 8 !== 0) bits.push(0);

    const codewords = [];
    for (let i = 0; i < bits.length; i += 8) {
      let byte = 0;
      for (let j = 0; j < 8; j += 1) byte = (byte << 1) | bits[i + j];
      codewords.push(byte);
    }
    const PAD = [0xec, 0x11];
    let padIndex = 0;
    while (codewords.length < capacity) {
      codewords.push(PAD[padIndex % 2]);
      padIndex += 1;
    }
    return codewords;
  }

  function interleave(codewords, version) {
    const layout = BLOCK_LAYOUT_M[version];
    const ecLength = layout[0];
    const groups = layout.slice(1);

    const blocks = [];
    let offset = 0;
    for (const [count, dataLength] of groups) {
      for (let i = 0; i < count; i += 1) {
        const data = codewords.slice(offset, offset + dataLength);
        offset += dataLength;
        blocks.push({ data, ec: reedSolomon(data, ecLength) });
      }
    }

    const result = [];
    const maxData = Math.max(...blocks.map((block) => block.data.length));
    for (let i = 0; i < maxData; i += 1) {
      for (const block of blocks) {
        if (i < block.data.length) result.push(block.data[i]);
      }
    }
    for (let i = 0; i < ecLength; i += 1) {
      for (const block of blocks) result.push(block.ec[i]);
    }
    return result;
  }

  // ------------------------------------------------------------- placement

  function createMatrix(version) {
    const size = version * 4 + 17;
    const modules = Array.from({ length: size }, () => new Array(size).fill(null));
    const reserved = Array.from({ length: size }, () => new Array(size).fill(false));

    function placeFinder(row, col) {
      for (let r = -1; r <= 7; r += 1) {
        for (let c = -1; c <= 7; c += 1) {
          const rr = row + r;
          const cc = col + c;
          if (rr < 0 || rr >= size || cc < 0 || cc >= size) continue;
          const onRing =
            (r >= 0 && r <= 6 && (c === 0 || c === 6)) ||
            (c >= 0 && c <= 6 && (r === 0 || r === 6));
          const inCore = r >= 2 && r <= 4 && c >= 2 && c <= 4;
          modules[rr][cc] = onRing || inCore ? 1 : 0;
          reserved[rr][cc] = true;
        }
      }
    }

    placeFinder(0, 0);
    placeFinder(0, size - 7);
    placeFinder(size - 7, 0);

    // timing patterns
    for (let i = 8; i < size - 8; i += 1) {
      const bit = i % 2 === 0 ? 1 : 0;
      modules[6][i] = bit;
      modules[i][6] = bit;
      reserved[6][i] = true;
      reserved[i][6] = true;
    }

    // alignment patterns
    const positions = ALIGNMENT_POSITIONS[version];
    for (const row of positions) {
      for (const col of positions) {
        const nearFinder =
          (row <= 8 && col <= 8) ||
          (row <= 8 && col >= size - 9) ||
          (row >= size - 9 && col <= 8);
        if (nearFinder) continue;
        for (let r = -2; r <= 2; r += 1) {
          for (let c = -2; c <= 2; c += 1) {
            const ring = Math.max(Math.abs(r), Math.abs(c));
            modules[row + r][col + c] = ring === 1 ? 0 : 1;
            reserved[row + r][col + c] = true;
          }
        }
      }
    }

    // dark module and reserved format areas
    modules[size - 8][8] = 1;
    reserved[size - 8][8] = true;
    for (let i = 0; i < 9; i += 1) {
      if (!reserved[8][i]) { modules[8][i] = 0; reserved[8][i] = true; }
      if (!reserved[i][8]) { modules[i][8] = 0; reserved[i][8] = true; }
    }
    for (let i = 0; i < 8; i += 1) {
      reserved[8][size - 1 - i] = true;
      modules[8][size - 1 - i] = modules[8][size - 1 - i] ?? 0;
      reserved[size - 1 - i][8] = true;
      modules[size - 1 - i][8] = modules[size - 1 - i][8] ?? 0;
    }

    if (version >= 7) {
      const info = VERSION_INFO[version];
      for (let i = 0; i < 18; i += 1) {
        const bit = (info >> i) & 1;
        const row = Math.floor(i / 3);
        const col = size - 11 + (i % 3);
        modules[row][col] = bit;
        reserved[row][col] = true;
        modules[col][row] = bit;
        reserved[col][row] = true;
      }
    }

    return { size, modules, reserved };
  }

  function placeData(matrix, codewords) {
    const { size, modules, reserved } = matrix;
    const bits = [];
    for (const byte of codewords) {
      for (let i = 7; i >= 0; i -= 1) bits.push((byte >> i) & 1);
    }

    let index = 0;
    let upward = true;
    for (let right = size - 1; right > 0; right -= 2) {
      if (right === 6) right -= 1; // skip the vertical timing column
      for (let step = 0; step < size; step += 1) {
        const row = upward ? size - 1 - step : step;
        for (const col of [right, right - 1]) {
          if (reserved[row][col]) continue;
          modules[row][col] = index < bits.length ? bits[index] : 0;
          index += 1;
        }
      }
      upward = !upward;
    }
  }

  function maskBit(mask, row, col) {
    switch (mask) {
      case 0: return (row + col) % 2 === 0;
      case 1: return row % 2 === 0;
      case 2: return col % 3 === 0;
      case 3: return (row + col) % 3 === 0;
      case 4: return (Math.floor(row / 2) + Math.floor(col / 3)) % 2 === 0;
      case 5: return ((row * col) % 2) + ((row * col) % 3) === 0;
      case 6: return (((row * col) % 2) + ((row * col) % 3)) % 2 === 0;
      default: return (((row + col) % 2) + ((row * col) % 3)) % 2 === 0;
    }
  }

  function penalty(modules, size) {
    let score = 0;

    // rule 1: runs of five or more same-coloured modules
    for (let i = 0; i < size; i += 1) {
      for (const horizontal of [true, false]) {
        let run = 1;
        for (let j = 1; j < size; j += 1) {
          const current = horizontal ? modules[i][j] : modules[j][i];
          const previous = horizontal ? modules[i][j - 1] : modules[j - 1][i];
          if (current === previous) {
            run += 1;
          } else {
            if (run >= 5) score += run - 2;
            run = 1;
          }
        }
        if (run >= 5) score += run - 2;
      }
    }

    // rule 2: 2x2 blocks of one colour
    for (let r = 0; r < size - 1; r += 1) {
      for (let c = 0; c < size - 1; c += 1) {
        const v = modules[r][c];
        if (v === modules[r][c + 1] && v === modules[r + 1][c] && v === modules[r + 1][c + 1]) {
          score += 3;
        }
      }
    }

    // rule 3: finder-like 1:1:3:1:1 patterns
    const A = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0];
    const B = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1];
    const matches = (get, start) =>
      A.every((_, k) => get(start + k) === A[k]) || B.every((_, k) => get(start + k) === B[k]);
    for (let i = 0; i < size; i += 1) {
      for (let j = 0; j <= size - 11; j += 1) {
        if (matches((k) => modules[i][k], j)) score += 40;
        if (matches((k) => modules[k][i], j)) score += 40;
      }
    }

    // rule 4: deviation from a 50% dark ratio
    let dark = 0;
    for (let r = 0; r < size; r += 1) {
      for (let c = 0; c < size; c += 1) dark += modules[r][c];
    }
    const percent = (dark * 100) / (size * size);
    score += Math.floor(Math.abs(percent - 50) / 5) * 10;
    return score;
  }

  function applyFormatInfo(modules, size, mask) {
    // The 15 format bits are written twice, and the two copies run in opposite
    // directions. Column 6 and row 6 are timing patterns and are stepped over,
    // which is what the index shifts below are for.
    const info = FORMAT_INFO_M[mask];
    for (let i = 0; i < 15; i += 1) {
      const bit = (info >> i) & 1;

      // copy one: down the left edge of the top-left finder
      if (i < 6) modules[i][8] = bit;
      else if (i < 8) modules[i + 1][8] = bit;
      else modules[size - 15 + i][8] = bit;

      // copy two: along row 8, right to left
      if (i < 8) modules[8][size - 1 - i] = bit;
      else if (i === 8) modules[8][7] = bit;
      else modules[8][14 - i] = bit;
    }
    modules[size - 8][8] = 1; // always-dark module
  }

  /**
   * Encode text as a QR matrix.
   * @returns {{size:number, modules:number[][], version:number}|null}
   *          null when the text is too long for version 10 at ECC level M.
   */
  function encode(text) {
    const bytes = toUtf8Bytes(String(text == null ? "" : text));
    const version = chooseVersion(bytes.length);
    if (!version) return null;

    const codewords = interleave(buildDataCodewords(bytes, version), version);
    const base = createMatrix(version);
    placeData(base, codewords);

    let best = null;
    for (let mask = 0; mask < 8; mask += 1) {
      const modules = base.modules.map((row) => row.slice());
      for (let r = 0; r < base.size; r += 1) {
        for (let c = 0; c < base.size; c += 1) {
          if (!base.reserved[r][c] && maskBit(mask, r, c)) modules[r][c] ^= 1;
        }
      }
      applyFormatInfo(modules, base.size, mask);
      const score = penalty(modules, base.size);
      if (!best || score < best.score) best = { score, modules };
    }
    return { size: base.size, modules: best.modules, version };
  }

  /** Draw an encoded matrix onto a canvas, sizing modules to fit. */
  function draw(canvas, text, options) {
    const settings = Object.assign({ quiet: 4, dark: "#101917", light: "#ffffff" }, options);
    const context = canvas.getContext("2d");
    context.fillStyle = settings.light;
    context.fillRect(0, 0, canvas.width, canvas.height);

    const result = encode(text);
    if (!result) return false;

    const total = result.size + settings.quiet * 2;
    const scale = Math.max(Math.floor(Math.min(canvas.width, canvas.height) / total), 1);
    const drawn = total * scale;
    const originX = Math.floor((canvas.width - drawn) / 2);
    const originY = Math.floor((canvas.height - drawn) / 2);

    context.fillStyle = settings.light;
    context.fillRect(originX, originY, drawn, drawn);
    context.fillStyle = settings.dark;
    for (let r = 0; r < result.size; r += 1) {
      for (let c = 0; c < result.size; c += 1) {
        if (!result.modules[r][c]) continue;
        context.fillRect(
          originX + (c + settings.quiet) * scale,
          originY + (r + settings.quiet) * scale,
          scale,
          scale,
        );
      }
    }
    return true;
  }

  global.QRCode = { encode, draw };
})(window);
