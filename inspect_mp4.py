"""Inspect ISO BMFF box structure of the fMP4 file."""
import struct
import sys

filename = sys.argv[1] if len(sys.argv) > 1 else "f3ff6be2_index-f2-v1-a1_20260502_130859.mp4"

with open(filename, "rb") as f:
    offset = 0
    count = 0
    while count < 30:
        f.seek(offset)
        header = f.read(8)
        if len(header) < 8:
            break
        size = struct.unpack(">I", header[:4])[0]
        box_type = header[4:8].decode("ascii", errors="replace")
        
        if size == 0:
            print(f"  [{count}] offset={offset:>10} size=REST  type='{box_type}'")
            break
        if size == 1:
            # 64-bit extended size
            ext = f.read(8)
            if len(ext) < 8:
                break
            size = struct.unpack(">Q", ext)[0]
        if size < 8:
            print(f"  [{count}] offset={offset:>10} INVALID size={size} type='{box_type}'")
            break
            
        print(f"  [{count}] offset={offset:>10} size={size:>10} type='{box_type}'")
        offset += size
        count += 1
        
        if offset > 100_000_000:  # Safety limit
            print("  ... (stopped at 100MB)")
            break

print(f"\nTotal file size: {offset} bytes parsed")
