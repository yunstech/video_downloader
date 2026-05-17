"""End-to-end test: create real fMP4 HLS, parse, merge, verify playable."""
import struct
import tempfile
import os
import sys
import shutil
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.video_downloader import parse_m3u8_playlist, _merge_with_ffmpeg


def main():
    tmp = tempfile.mkdtemp(prefix="test_real_fmp4_")
    print(f"Working dir: {tmp}\n")

    try:
        # Step 1: Generate a tiny test video
        print("Step 1: Creating test video...")
        rc = subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-t", "3", os.path.join(tmp, "test.mp4")
        ], capture_output=True).returncode
        assert rc == 0, "Failed to create test video"
        print("  ✅ Test video created")

        # Step 2: Convert to fMP4 HLS
        print("\nStep 2: Converting to fMP4 HLS...")
        rc = subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", os.path.join(tmp, "test.mp4"),
            "-c", "copy", "-f", "hls",
            "-hls_segment_type", "fmp4",
            "-hls_time", "1",
            "-hls_list_size", "0",
            "-hls_fmp4_init_filename", "init.mp4",
            os.path.join(tmp, "stream.m3u8")
        ], capture_output=True).returncode
        assert rc == 0, "Failed to create fMP4 HLS"
        
        files = sorted(os.listdir(tmp))
        for f in files:
            size = os.path.getsize(os.path.join(tmp, f))
            print(f"  {f}: {size} bytes")

        # Step 3: Parse the m3u8
        print("\nStep 3: Parsing m3u8 playlist...")
        m3u8_path = os.path.join(tmp, "stream.m3u8")
        with open(m3u8_path) as f:
            content = f.read()
        print(f"  m3u8:\n{content}")

        base_url = f"file:///{tmp.replace(os.sep, '/')}/"
        segments, is_master, init_url = parse_m3u8_playlist(content, base_url)
        print(f"  Segments: {len(segments)}")
        print(f"  Init URL: {init_url}")
        assert init_url is not None, "EXT-X-MAP not parsed!"
        print("  ✅ EXT-X-MAP parsed correctly")

        # Step 4: Set up segment files (simulate downloaded segments)
        print("\nStep 4: Merging with ffmpeg...")
        init_path = os.path.join(tmp, "init.mp4")
        assert os.path.exists(init_path), f"Init segment not found: {init_path}"

        seg_files = sorted([
            os.path.join(tmp, f) for f in os.listdir(tmp)
            if f.endswith(".m4s")
        ])
        print(f"  Init: {os.path.getsize(init_path)} bytes")
        print(f"  Segments: {len(seg_files)}")

        # Step 5: Merge
        output = os.path.join(tmp, "final_output.mp4")
        result = _merge_with_ffmpeg(seg_files, output, tmp, init_path)
        print(f"  Merge result: {result}")
        assert result, "Merge failed!"

        # Step 6: Verify the output is playable
        print("\nStep 6: Verifying output...")
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_streams", "-select_streams", "v:0",
            "-print_format", "json",
            output
        ], capture_output=True, text=True)
        
        import json
        info = json.loads(probe.stdout)
        stream = info["streams"][0]
        codec = stream["codec_name"]
        width = stream["width"]
        height = stream["height"]
        
        print(f"  Codec: {codec}")
        print(f"  Resolution: {width}x{height}")
        print(f"  File size: {os.path.getsize(output)} bytes")
        
        assert codec == "h264", f"Expected h264, got {codec}"
        assert width == 320 and height == 240, f"Expected 320x240, got {width}x{height}"
        print("\n  ✅ Output is a VALID, PLAYABLE MP4 (h264 320x240)")
        print("\n" + "=" * 60)
        print("END-TO-END TEST PASSED ✅")
        print("=" * 60)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
