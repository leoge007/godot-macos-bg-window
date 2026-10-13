#!/usr/bin/env python3
"""Decode two 8-bit RGB/RGBA PNGs with the standard library and compare size and every RGBA pixel."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib


def decode_png(path):
    data = path.read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError(f'{path}: not a PNG')
    position = 8
    header = None
    compressed = bytearray()
    transparent = None
    ended = False
    while position < len(data):
        if position + 12 > len(data):
            raise ValueError(f'{path}: truncated PNG chunk')
        length = struct.unpack_from('>I', data, position)[0]
        kind = data[position + 4:position + 8]
        end = position + 8 + length
        if end + 4 > len(data):
            raise ValueError(f'{path}: truncated PNG data')
        payload = data[position + 8:end]
        checksum = struct.unpack_from('>I', data, end)[0]
        if zlib.crc32(kind + payload) != checksum:
            raise ValueError(f'{path}: PNG CRC mismatch')
        position = end + 4
        if header is None and kind != b'IHDR':
            raise ValueError(f'{path}: missing IHDR')
        if kind == b'IHDR':
            if header is not None or length != 13:
                raise ValueError(f'{path}: invalid IHDR')
            header = struct.unpack('>IIBBBBB', payload)
        elif kind == b'IDAT':
            compressed.extend(payload)
        elif kind == b'tRNS':
            if transparent is not None or header[3] != 2 or length != 6:
                raise ValueError(f'{path}: invalid tRNS')
            transparent = struct.unpack('>HHH', payload)
        elif kind == b'IEND':
            if length or position != len(data):
                raise ValueError(f'{path}: invalid IEND')
            ended = True
            break
        elif kind[0] & 32 == 0 and kind != b'PLTE':
            raise ValueError(f'{path}: unsupported PNG chunk {kind!r}')
    if not ended or header is None or not compressed:
        raise ValueError(f'{path}: incomplete PNG')
    width, height, depth, color, compression, filtering, interlace = header
    if not width or not height or depth != 8 or color not in (2, 6) or any((compression, filtering, interlace)):
        raise ValueError(f'{path}: only non-interlaced 8-bit RGB/RGBA PNG is supported')
    channels = 3 if color == 2 else 4
    stride = width * channels
    raw = zlib.decompress(compressed)
    if len(raw) != (stride + 1) * height:
        raise ValueError(f'{path}: unexpected decompressed length')
    previous = bytearray(stride)
    pixels = bytearray()
    for y in range(height):
        offset = y * (stride + 1)
        method = raw[offset]
        if method > 4:
            raise ValueError(f'{path}: invalid PNG filter')
        row = bytearray(raw[offset + 1:offset + 1 + stride])
        for x in range(stride):
            left = row[x - channels] if x >= channels else 0
            above = previous[x]
            corner = previous[x - channels] if x >= channels else 0
            if method == 0:
                predictor = 0
            elif method == 1:
                predictor = left
            elif method == 2:
                predictor = above
            elif method == 3:
                predictor = (left + above) // 2
            else:
                estimate = left + above - corner
                distances = (abs(estimate - left), abs(estimate - above), abs(estimate - corner))
                predictor = (left, above, corner)[distances.index(min(distances))]
            row[x] = (row[x] + predictor) & 255
        if channels == 4:
            pixels.extend(row)
        else:
            for x in range(0, stride, channels):
                rgb = row[x:x + 3]
                pixels.extend(rgb)
                pixels.append(0 if tuple(rgb) == transparent else 255)
        previous = row
    return width, height, pixels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('the result file must be a new path')
    try:
        width_a, height_a, pixels_a = decode_png(args.before)
        width_b, height_b, pixels_b = decode_png(args.after)
        same_size = (width_a, height_a) == (width_b, height_b)
        different = sum(pixels_a[x:x + 4] != pixels_b[x:x + 4]
            for x in range(0, len(pixels_a), 4)) if same_size else None
        result = {'before': str(args.before.resolve()), 'after': str(args.after.resolve()),
            'before_size': [width_a, height_a], 'after_size': [width_b, height_b],
            'before_rgba_sha256': hashlib.sha256(pixels_a).hexdigest(),
            'after_rgba_sha256': hashlib.sha256(pixels_b).hexdigest(),
            'different_pixels': different, 'pass': same_size and different == 0}
    except (OSError, ValueError, zlib.error) as error:
        result = {'pass': False, 'error': str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    if not result['pass']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
