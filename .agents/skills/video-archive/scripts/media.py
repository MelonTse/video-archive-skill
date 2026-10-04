"""Media operations extracted from the original archival compression workflow."""
from __future__ import annotations
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

LOG = None
PRESET = 'medium'
ENCODER = 'libx265'
THREADS = 0
COLOR_FIELDS = ('color_range', 'color_space', 'color_transfer', 'color_primaries', 'chroma_location')
COLOR_OPTIONS = ('-color_range', '-colorspace', '-color_trc', '-color_primaries', '-chroma_sample_location')

def num(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


def fps(stream):
    for field in ('avg_frame_rate', 'r_frame_rate'):
        try:
            value = float(Fraction(stream.get(field, '0/0')))
            if value > 0:
                return value
        except (ValueError, ZeroDivisionError, TypeError):
            pass
    return 0.0


def videos(probe):
    return [s for s in probe.get('streams', []) if s.get('codec_type') == 'video'
            and not s.get('disposition', {}).get('attached_pic')]


def audios(probe):
    return [s for s in probe.get('streams', []) if s.get('codec_type') == 'audio']


def duration(probe):
    return num(videos(probe)[0].get('duration')) or num(probe.get('format', {}).get('duration'))


def stamp(path):
    st = path.stat()
    return {'size_bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}


def log_event(kind, **data):
    if LOG is None:
        return
    with LOG.open('a', encoding='utf-8') as file:
        file.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(), 'event': kind, **data}, ensure_ascii=False) + '\n')


def probe_file(path, tools):
    args = [tools['ffprobe'], '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]
    log_event('command', argv=args)
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    log_event('ffprobe_result', returncode=result.returncode, stderr=result.stderr)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    data = json.loads(result.stdout)
    if not videos(data):
        raise ValueError('没有有效视频流')
    return data


def keyframe_start(path, target, video_index, tools):
    if target <= 0:
        return 0.0
    args = [tools['ffprobe'], '-v', 'error', '-select_streams', str(video_index),
            '-read_intervals', f'{target:.6f}%+2', '-show_packets',
            '-show_entries', 'packet=pts_time,flags', '-of', 'json', str(path)]
    log_event('command', argv=args)
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    log_event('keyframe_probe_result', returncode=result.returncode, stderr=result.stderr)
    if result.returncode:
        raise RuntimeError('无法读取关键帧时间：' + result.stderr.strip())
    keys = [num(p.get('pts_time'), -1) for p in json.loads(result.stdout).get('packets', []) if 'K' in p.get('flags', '')]
    eligible = [t for t in keys if 0 <= t <= target + 0.05]
    if not eligible:
        raise ValueError('无法确认目标附近的关键帧，换候选；不重编码 reference')
    result = min(eligible)
    if target - result > 10:
        raise ValueError('关键帧离目标超过 10 秒，换候选')
    return result


def leading_discard_packets(path, tools):
    """Read packet flags, not decoded frames, to account for MP4 preroll."""
    args = [tools['ffprobe'], '-v', 'error', '-select_streams', 'v:0',
            # An explicit seek to zero can skip negative-PTS preroll packets.
            '-read_intervals', '%+#64', '-show_packets', '-show_entries', 'packet=flags',
            '-of', 'json', str(path)]
    log_event('command', argv=args)
    result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=True)
    count = sum('D' in packet.get('flags', '') for packet in json.loads(result.stdout).get('packets', []))
    log_event('preroll_packets', path=str(path), discard_packet_count=count)
    return count


def media_command(args, label, timeout_seconds=None):
    log_event('command', label=label, argv=args)
    start = time.monotonic()
    with LOG.open('a', encoding='utf-8', buffering=1) as log:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            while True:
                try:
                    code = process.wait(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - start
                    print(f'{label} 编码中，已用 {elapsed:.0f} 秒', flush=True)
                    if timeout_seconds is not None and elapsed > timeout_seconds:
                        raise TimeoutError(f'单个任务超过 {timeout_seconds:.0f} 秒，停止该任务')
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    elapsed = time.monotonic() - start
    log_event('command_result', label=label, returncode=code, elapsed_seconds=elapsed)
    if code:
        raise RuntimeError(f'{label} FFmpeg 失败（退出码 {code}），详见 {LOG}')
    return elapsed


def ffmpeg_base(tools):
    return [tools['ffmpeg'], '-hide_banner', '-nostdin', '-n', '-loglevel', 'warning',
            '-stats_period', '5', '-progress', 'pipe:1']


def reference_command(source, dest, start, seconds, source_probe, tools):
    video = videos(source_probe)[0]
    return ffmpeg_base(tools) + ['-ss', f'{start:.6f}', '-noautorotate', '-i', str(source),
            '-t', f'{seconds:.6f}', '-map', f"0:{video['index']}", '-map', '0:a?',
            '-c:v', 'copy', '-c:a', 'copy', '-map_metadata', '0', '-map_chapters', '-1',
            '-avoid_negative_ts', 'make_zero', '-movflags', '+faststart', str(dest)]


def encode_command(reference, dest, crf, reference_probe, tools):
    video = videos(reference_probe)[0]
    pixel_format = video.get('pix_fmt', 'yuv420p')
    if pixel_format not in ('yuv420p', 'yuv420p10le'):
        raise ValueError(f'不支持的原始像素格式：{pixel_format}')
    color = []
    for field, option in zip(COLOR_FIELDS, COLOR_OPTIONS):
        if video.get(field) not in (None, '', 'unknown', 'unspecified'):
            color += [option, str(video[field])]
    return ffmpeg_base(tools) + ['-noautorotate', '-i', str(reference), '-map', f"0:{video['index']}", '-map', '0:a?',
            '-c:v', ENCODER, '-preset', PRESET, '-crf', str(crf), '-pix_fmt', pixel_format,
            '-c:a', 'copy', '-fps_mode', 'passthrough', '-map_metadata', '0', '-map_chapters', '-1',
            '-tag:v', 'hvc1' if ENCODER == 'libx265' else 'avc1',
            *(['-threads', str(THREADS)] if THREADS else []),
            *(['-x265-params', f'pools={THREADS}:frame-threads=1'] if THREADS and ENCODER == 'libx265' else []), *color, '-movflags', '+faststart', str(dest)]


def validation(probed, baseline, encoded, expected_duration=None):
    issues = []
    v, base = videos(probed)[0], videos(baseline)[0]
    if (v.get('width'), v.get('height')) != (base.get('width'), base.get('height')):
        issues.append('分辨率变化')
    expected_frames = None
    expected_fps = fps(base)
    if encoded and base.get('nb_frames') and '_leading_discard_packets' in baseline:
        discarded = baseline['_leading_discard_packets']
        expected_frames = int(base['nb_frames']) - discarded
        # MOV avg_frame_rate may include preroll with a different cadence.
        # Compare the average over the actual presentation interval instead.
        if discarded and duration(baseline) > 0:
            expected_fps = expected_frames / duration(baseline)
    if abs(fps(v) - expected_fps) > max(0.05, expected_fps * 0.002):
        issues.append('FPS 不一致')
    if v.get('pix_fmt') != base.get('pix_fmt') or v.get('pix_fmt') not in ('yuv420p', 'yuv420p10le'):
        issues.append('像素格式/色深与原视频不一致')
    expected_codec = ('hevc' if ENCODER == 'libx265' else 'h264') if encoded else base.get('codec_name')
    if v.get('codec_name') != expected_codec:
        issues.append('编码不符合要求')
    if len(audios(probed)) != len(audios(baseline)):
        issues.append('音轨数量变化')
    for a, b in zip(audios(probed), audios(baseline)):
        if any(a.get(key) != b.get(key) for key in ('codec_name', 'sample_rate', 'channels', 'channel_layout')):
            issues.append('音频编码或参数变化')
    for key in COLOR_FIELDS:
        if base.get(key) not in (None, '', 'unknown', 'unspecified') and v.get(key) != base[key]:
            issues.append(f'色彩标记变化：{key}')
    for key in ('sample_aspect_ratio', 'display_aspect_ratio'):
        if base.get(key) not in (None, '', 'N/A', '0:1') and v.get(key) != base[key]:
            issues.append(f'宽高比变化：{key}')
    def display_matrices(stream):
        return [s for s in stream.get('side_data_list', []) if s.get('side_data_type') == 'Display Matrix']
    if display_matrices(v) != display_matrices(base):
        issues.append('显示方向矩阵变化')
    if num(v.get('tags', {}).get('rotate')) != num(base.get('tags', {}).get('rotate')):
        issues.append('旋转标记变化')
    expected = duration(baseline) if encoded else expected_duration
    if expected is not None and abs(duration(probed) - expected) > (0.25 if encoded else 2.0):
        issues.append('视频流时长超出容差')
    # Stream-copy references may contain leading packets marked for discard.
    # nb_frames counts those packets too; comparing it strictly to a decoded
    # encode would reject otherwise identical presentation intervals.
    if expected_frames is not None and v.get('nb_frames'):
        if int(v['nb_frames']) != expected_frames:
            issues.append('扣除不播放的预滚包后，帧数不一致')
    if encoded:
        # Container duration includes audio; allow only a small muxing difference.
        delta = abs(num(probed['format'].get('duration')) - num(baseline['format'].get('duration')))
        if delta > 0.25:
            issues.append('容器时长偏差超过 0.25 秒')
    return issues


def validate_file(path, baseline, encoded, tools, expected_duration=None):
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError('输出不存在或为空')
    data = probe_file(path, tools)
    if not encoded:
        data['_leading_discard_packets'] = leading_discard_packets(path, tools)
    issues = validation(data, baseline, encoded, expected_duration)
    log_event('validation', path=str(path), issues=issues, probe_on_failure=data if issues else None)
    return data, issues


def move_without_overwrite(part, destination):
    """Caller must guard both paths; preserve content and mtime without copying."""
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    reservation = os.fstat(fd)
    os.close(fd)
    try:
        current = destination.lstat()
        if current.st_ino != reservation.st_ino or current.st_size != 0 or current.st_nlink != 1:
            raise ValueError('新建输出占位文件发生变化，拒绝覆盖')
        os.replace(part, destination)
    except BaseException:
        if destination.exists() and destination.lstat().st_ino == reservation.st_ino and destination.stat().st_size == 0:
            destination.unlink()
        raise
