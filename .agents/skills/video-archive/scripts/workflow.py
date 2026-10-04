#!/usr/bin/env python3
"""Local helpers for the video-archive Skill. Python standard library only."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import unicodedata
import uuid

import media

EXTENSIONS = {'.mp4', '.mov', '.mkv', '.m4v', '.avi'}
PRESETS = {'ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_path(path):
    """Do not follow symlinks or accept ambiguous parent traversal."""
    path = Path(path).expanduser().absolute()
    if '..' in path.parts:
        raise ValueError(f'Parent traversal is not allowed: {path}')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f'Symlink is not allowed: {part}')
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError(f'Hard-linked file is not allowed: {path}')
    return path


def read_json(path):
    return json.loads(safe_path(path).read_text(encoding='utf-8'))


def write_json(path, data):
    path = safe_path(path)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        safe_path(path)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def locked(job):
    if os.name != 'posix':
        raise ValueError('This release requires macOS/Linux POSIX file locks; native Windows is not supported.')
    import fcntl
    with safe_path(job / 'job.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('This job already has an active operation. Use status or pause.')
        yield


def active(job):
    if not (job / 'job.lock').exists():
        return False
    import fcntl
    with safe_path(job / 'job.lock').open('r') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def doctor():
    report = {'platform': platform.system(), 'python': sys.version.split()[0],
              'python_ok': sys.version_info >= (3, 9), 'posix_execution': os.name == 'posix',
              'tools': {}, 'encoders': {}, 'missing': []}
    for name in ('ffmpeg', 'ffprobe'):
        path = shutil.which(name)
        if not path:
            report['missing'].append(name)
            continue
        try:
            version = subprocess.run([path, '-version'], text=True, capture_output=True, timeout=15, check=True)
            report['tools'][name] = {'path': path, 'version': version.stdout.splitlines()[0]}
        except (OSError, subprocess.SubprocessError, IndexError) as error:
            report['missing'].append(name)
            report['tools'][name] = {'error': str(error)}
    if 'ffmpeg' not in report['missing']:
        path = report['tools']['ffmpeg']['path']
        result = subprocess.run([path, '-hide_banner', '-encoders'], text=True, capture_output=True, timeout=15, check=True)
        names = {line.split()[1] for line in result.stdout.splitlines() if len(line.split()) > 1}
        report['encoders'] = {name: name in names for name in ('libx265', 'libx264')}
        help_text = subprocess.run([path, '-hide_banner', '-h', 'full'], text=True, capture_output=True, timeout=15, check=True).stdout
        report['fps_mode_supported'] = '-fps_mode' in help_text
    report['ready_to_scan'] = report['python_ok'] and 'ffprobe' not in report['missing'] and report['posix_execution']
    report['ready_to_sample'] = (report['ready_to_scan'] and not report['missing']
                                 and any(report['encoders'].values()) and report.get('fps_mode_supported', False))
    return report


def tools_for(encoder=None):
    report = doctor()
    if not report['ready_to_scan'] or (encoder and not report['ready_to_sample']):
        raise ValueError('Environment is incomplete. Run doctor and follow references/environment.md.')
    if encoder and not report['encoders'].get(encoder):
        raise ValueError(f'Missing encoder {encoder}; do not silently substitute another encoder.')
    return {name: value['path'] for name, value in report['tools'].items() if 'path' in value}


def init_job(job, source, output):
    job, source, output = map(safe_path, (job, source, output))
    if not source.is_dir():
        raise ValueError('Source directory does not exist.')
    if job.exists():
        raise ValueError('Job directory already exists; use a new directory or resume the existing job.')
    if job.is_relative_to(source) or source.is_relative_to(job):
        raise ValueError('Keep the job directory separate from the source directory.')
    if source.is_relative_to(output) or output.is_relative_to(job) or job.is_relative_to(output):
        raise ValueError('Output must not contain the source/job or be inside the job.')
    if output.exists() and not output.is_dir():
        raise ValueError('Output must be a directory.')
    job.mkdir(parents=True)
    config = {'schema_version': 1, 'source_root': str(source), 'output_root': str(output),
              'archive_mode': 'keep', 'archive_folder': 'Originals',
              'exclude_directories': ['已压缩', '压缩后的文件夹'], 'skip_crf_names': True}
    write_json(job / 'config.json', config)
    return config


def load_config(job):
    config = read_json(job / 'config.json')
    source, output = (safe_path(config[key]) for key in ('source_root', 'output_root'))
    if not source.is_dir() or source.is_relative_to(output):
        raise ValueError('Invalid or unavailable source/output directory.')
    if job.is_relative_to(source) or source.is_relative_to(job) or output.is_relative_to(job) or job.is_relative_to(output):
        raise ValueError('Job/source/output directories overlap unsafely.')
    folder = config['archive_folder']
    if not isinstance(folder, str) or folder in ('', '.', '..') or '/' in folder or '\\' in folder:
        raise ValueError('archive_folder must be a single directory name.')
    if config['archive_mode'] not in ('keep', 'move'):
        raise ValueError('archive_mode must be keep or move.')
    if not isinstance(config['exclude_directories'], list) or not all(isinstance(x, str) for x in config['exclude_directories']):
        raise ValueError('exclude_directories must be a list of directory names.')
    return config


def profile(probe):
    v = media.videos(probe)[0]
    fields = {key: v.get(key) for key in ('codec_name', 'width', 'height', 'pix_fmt', *media.COLOR_FIELDS)}
    fields['fps'] = round(media.fps(v), 2)
    return digest(fields)[:12], fields


def eligibility(probe):
    videos = media.videos(probe)
    if len(videos) != 1:
        return 'REVIEW', 'Expected one main video stream.'
    v = videos[0]
    if v.get('codec_name') not in ('hevc', 'h264') or v.get('pix_fmt') not in ('yuv420p', 'yuv420p10le'):
        return 'REVIEW', 'Unsupported codec/pixel format; no automatic conversion.'
    if v.get('color_transfer') in ('arib-std-b67', 'smpte2084') or any(
        any(word in str(s.get('side_data_type', '')).lower() for word in ('mastering display', 'content light', 'hdr', 'dovi', 'dolby'))
        for s in v.get('side_data_list', [])):
        return 'REVIEW', 'HDR requires a separate, tested preservation workflow.'
    if any(v.get(key) != 'bt709' for key in ('color_space', 'color_transfer', 'color_primaries')):
        return 'REVIEW', 'Color metadata is not fully BT.709; do not infer the shooting mode.'
    if any(s.get('codec_type') == 'subtitle' for s in probe.get('streams', [])):
        return 'REVIEW', 'Subtitle preservation is not implemented.'
    if media.duration(probe) <= 0 or media.fps(v) <= 0 or not v.get('width') or not v.get('height'):
        return 'REVIEW', 'Missing duration/frame rate/dimensions.'
    return 'ELIGIBLE', 'Preserve main video parameters and all audio; auxiliary tracks are omitted.'


def scan(job):
    config = load_config(job)
    if (job / 'manifest.json').exists():
        raise ValueError('This job already has a scan snapshot. Create a new job for new/changed source material.')
    tools = tools_for()
    root, output = Path(config['source_root']), Path(config['output_root'])
    excluded = set(config['exclude_directories']) | {config['archive_folder']}
    items, errors = [], []
    def onerror(error):
        errors.append({'path': error.filename, 'error': str(error)})
    for parent, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in excluded
                         and not (Path(parent) / d).is_symlink() and not (Path(parent) / d).is_relative_to(output))
        for name in sorted(names):
            path = Path(parent) / name
            if name.startswith('.') or path.suffix.lower() not in EXTENSIONS:
                continue
            item = {'relative_path': str(path.relative_to(root)), 'status': 'REVIEW'}
            try:
                safe_path(path)
                item['signature'] = media.stamp(path)
                if config['skip_crf_names'] and re.search(r'crf\s*\d+', name, re.I):
                    item.update(status='SKIP', reason='Filename indicates an existing CRF version.')
                else:
                    data = media.probe_file(path, tools)
                    if media.stamp(path) != item['signature']:
                        raise ValueError('Source changed while scanning.')
                    item['probe'] = data
                    item['profile_id'], item['profile'] = profile(data)
                    item['status'], item['reason'] = eligibility(data)
                    item['duration_seconds'] = media.duration(data)
                items.append(item)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                item.update(reason=str(error))
                items.append(item)
    manifest = {'created_at': now(), 'scan_config': config, 'items': items, 'directory_errors': errors}
    write_json(job / 'manifest.json', manifest)
    groups = {}
    for item in items:
        if 'profile_id' in item:
            group = groups.setdefault(item['profile_id'], {'attributes': item['profile'], 'count': 0, 'bytes': 0, 'seconds': 0})
            group['count'] += 1
            group['bytes'] += item['signature']['size_bytes']
            group['seconds'] += item['duration_seconds']
    result = {'counts': dict(Counter(i['status'] for i in items)), 'profiles': groups, 'directory_errors': errors}
    write_json(job / 'analysis.json', result)
    return result


def load_plan(job):
    config = load_config(job)
    manifest, plan = read_json(job / 'manifest.json'), read_json(job / 'plan.json')
    for key in ('source_root', 'output_root', 'archive_folder', 'exclude_directories', 'skip_crf_names'):
        if config[key] != manifest['scan_config'][key]:
            raise ValueError(f'{key} changed since scanning. Create a new job.')
    if not str(plan.get('goal', '')).strip() or not str(plan.get('rationale', '')).strip():
        raise ValueError('Record the user goal and recommendation rationale before sampling.')
    encoder = plan['encoder']
    if encoder not in ('libx265', 'libx264') or plan['preset'] not in PRESETS:
        raise ValueError('Unsupported encoder/preset.')
    crfs = plan['crfs']
    if not isinstance(crfs, list) or not 1 <= len(crfs) <= 4 or len(set(crfs)) != len(crfs) or not all(type(x) is int and 0 <= x <= 51 for x in crfs):
        raise ValueError('Choose 1–4 distinct integer CRFs between 0 and 51.')
    if type(plan.get('threads', 0)) is not int or not 0 <= plan.get('threads', 0) <= 256:
        raise ValueError('threads must be an integer from 0 (automatic) to 256.')
    if type(plan.get('timeout_seconds', 0)) is not int or plan.get('timeout_seconds', 0) < 0:
        raise ValueError('timeout_seconds must be a nonnegative integer; 0 means no automatic time limit.')
    selected = plan['profile_ids']
    if not isinstance(selected, list) or not selected or len(set(selected)) != len(selected):
        raise ValueError('Select at least one distinct profile_id.')
    candidates = [i for i in manifest['items'] if i.get('profile_id') in selected and i['status'] == 'ELIGIBLE']
    if set(selected) != {i['profile_id'] for i in candidates}:
        raise ValueError('Plan includes unknown or unsupported profiles.')
    if encoder == 'libx264' and any(i['profile']['pix_fmt'] != 'yuv420p' for i in candidates):
        raise ValueError('H.264 compatibility mode only supports 8-bit here. Do not reduce 10-bit sources.')
    samples = plan['samples']
    if not isinstance(samples, list) or not 1 <= len(samples) <= 12:
        raise ValueError('Select 1–12 representative sample windows.')
    by_path = {i['relative_path']: i for i in candidates}
    covered = set()
    for sample in samples:
        item = by_path.get(sample['relative_path'])
        if item is None:
            raise ValueError('Sample must be from a selected eligible profile.')
        start, seconds = sample['start_seconds'], sample['seconds']
        if not (isinstance(start, (int, float)) and isinstance(seconds, (int, float)) and 0 <= start < item['duration_seconds'] and 0 < seconds <= 60 and start + seconds <= item['duration_seconds'] + .01):
            raise ValueError('Sample window must be within the source, positive and no longer than 60 seconds.')
        covered.add(item['profile_id'])
    if covered != set(selected):
        raise ValueError('Include a sample from every selected profile; narrow the batch scope if necessary.')
    media.PRESET, media.ENCODER, media.THREADS = plan['preset'], encoder, plan.get('threads', 0)
    return config, manifest, plan, candidates


def fingerprint(config, manifest, plan):
    return digest({'config': config, 'manifest': manifest, 'plan': plan})


def check_source(path, item, tools):
    safe_path(path)
    if media.stamp(path) != item['signature']:
        raise ValueError('Source size/mtime differs from the scan snapshot.')
    data = media.probe_file(path, tools)
    if eligibility(data)[0] != 'ELIGIBLE' or profile(data)[0] != item['profile_id']:
        raise ValueError('Source media parameters differ from the approved profile.')
    data['_leading_discard_packets'] = media.leading_discard_packets(path, tools)
    return data


def sample_job(job):
    config, manifest, plan, candidates = load_plan(job)
    tools = tools_for(plan['encoder'])
    if (job / 'state.json').exists():
        raise ValueError('Batch has started; use a new job to change the experiment.')
    folder = safe_path(job / ('samples-' + uuid.uuid4().hex[:12]))
    folder.mkdir()
    result = {'fingerprint': fingerprint(config, manifest, plan), 'created_at': now(), 'results': [], 'status': 'RUNNING'}
    write_json(job / 'samples.json', result)
    by_path = {i['relative_path']: i for i in candidates}
    try:
        for index, sample in enumerate(plan['samples'], 1):
            item = by_path[sample['relative_path']]
            source = Path(config['source_root']) / item['relative_path']
            original = check_source(source, item, tools)
            dest = folder / f'sample-{index:02d}'
            dest.mkdir()
            reference = dest / 'reference.mp4'
            start = media.keyframe_start(source, sample['start_seconds'], media.videos(original)[0]['index'], tools)
            media.media_command(media.reference_command(source, reference, start, sample['seconds'], original, tools), 'reference')
            baseline, issues = media.validate_file(reference, original, False, tools, sample['seconds'])
            if issues:
                raise ValueError('Reference validation failed: ' + '; '.join(issues))
            for crf in plan['crfs']:
                output = dest / f'CRF{crf}.mp4'
                elapsed = media.media_command(media.encode_command(reference, output, crf, baseline, tools), f'Sample {index} CRF{crf}')
                _, issues = media.validate_file(output, baseline, True, tools)
                if media.stamp(source) != item['signature']:
                    issues.append('Source changed while sampling.')
                result['results'].append({'sample_index': index, 'profile_id': item['profile_id'], 'source': item['relative_path'],
                                          'crf': crf, 'reference': str(reference), 'output': str(output),
                                          'reference_signature': media.stamp(reference), 'output_signature': media.stamp(output),
                                          'elapsed_seconds': elapsed, 'reference_seconds': media.duration(baseline),
                                          'status': 'FAIL' if issues else 'PASS', 'issues': issues,
                                          'reduction_percent': round(100 * (1 - output.stat().st_size / reference.stat().st_size), 2)})
                write_json(job / 'samples.json', result)
        result['status'] = 'PASS' if all(r['status'] == 'PASS' for r in result['results']) else 'FAILED'
    except BaseException:
        result['status'] = 'INTERRUPTED_OR_FAILED'
        raise
    finally:
        write_json(job / 'samples.json', result)
        lines = ['# Sample comparison', '', 'Watch the same interval in reference and each candidate. PASS checks media structure, not visual quality.', '',
                 '| Sample | Profile | CRF | Reference MB | Output MB | Reduction % | Encode seconds | Check |',
                 '|---|---|---:|---:|---:|---:|---:|---|']
        for r in result['results']:
            lines.append(f"| {r['sample_index']} | {r['profile_id']} | {r['crf']} | {r['reference_signature']['size_bytes']/1e6:.3f} | {r['output_signature']['size_bytes']/1e6:.3f} | {r['reduction_percent']} | {r['elapsed_seconds']:.1f} | {r['status']} |")
        lines += ['', '## Files', '']
        for r in result['results']:
            lines += [f"- Sample {r['sample_index']} CRF{r['crf']}: [Reference](<{r['reference']}>) · [Candidate](<{r['output']}>)"]
        safe_path(job / 'sample-report.md').write_text('\n'.join(lines), encoding='utf-8')
    return result


def sample_evidence(job, current_fingerprint, crf, plan):
    samples = read_json(job / 'samples.json')
    if samples['fingerprint'] != current_fingerprint or samples['status'] != 'PASS':
        raise ValueError('Samples are incomplete or belong to a different configuration. Repeat sampling.')
    rows = [r for r in samples['results'] if r['crf'] == crf and r['status'] == 'PASS']
    if len(rows) != len(plan['samples']) or {r['sample_index'] for r in rows} != set(range(1, len(plan['samples']) + 1)):
        raise ValueError('Chosen CRF was not successfully tested on every sample.')
    for row in rows:
        for key in ('reference', 'output'):
            path = safe_path(row[key])
            if not path.is_relative_to(job) or media.stamp(path) != row[key + '_signature']:
                raise ValueError('Sample files changed or are missing; review fresh samples.')
    return samples


def approve(job, crf, confirmation):
    config, manifest, plan, _ = load_plan(job)
    fp = fingerprint(config, manifest, plan)
    samples = sample_evidence(job, fp, crf, plan)
    if not confirmation.strip():
        raise ValueError('Record the actual user confirmation, including the selected CRF and archival policy.')
    approval = {'fingerprint': fp, 'sample_digest': digest(samples), 'crf': crf,
                'archive_mode': config['archive_mode'], 'user_confirmation': confirmation, 'approved_at': now()}
    if (job / 'state.json').exists():
        raise ValueError('A batch already exists. Resume it unchanged, or create a new job.')
    write_json(job / 'approval.json', approval)
    return approval


def approved_context(job):
    config, manifest, plan, candidates = load_plan(job)
    approval = read_json(job / 'approval.json')
    fp = fingerprint(config, manifest, plan)
    samples = sample_evidence(job, fp, approval['crf'], plan)
    if approval['fingerprint'] != fp or approval['sample_digest'] != digest(samples) or not approval.get('user_confirmation', '').strip():
        raise ValueError('Approval no longer matches the plan/sample results. Obtain approval for the revised plan.')
    return config, plan, candidates, approval


def destination(config, relative, crf):
    root = Path(config['source_root'])
    source = safe_path(root / relative)
    if not source.is_relative_to(root) or config['archive_folder'] in source.relative_to(root).parts:
        raise ValueError('Source path is outside the selected scope.')
    path = Path(relative)
    output = safe_path(Path(config['output_root']) / path.with_name(path.stem + f'_CRF{crf}.mp4'))
    if not output.is_relative_to(Path(config['output_root'])):
        raise ValueError('Output path escapes its root.')
    return output


def source_location(config, item, record):
    original = safe_path(Path(config['source_root']) / item['relative_path'])
    archived = safe_path(original.parent / config['archive_folder'] / original.name)
    if record.get('archive_path'):
        if record['archive_path'] != str(archived):
            raise ValueError('Unexpected archive path in recovery record.')
        if record.get('archive_status') == 'PENDING' and original.exists() and not archived.exists():
            return original
        if original.exists() or not archived.is_file() or media.stamp(archived) != item['signature']:
            raise ValueError('Source/archive locations conflict or archived source changed.')
        return archived
    return original


def archive_one(job, config, item, record, state, tools):
    if config['archive_mode'] == 'keep':
        record['archive_status'] = 'KEPT_IN_PLACE'
        return
    source = source_location(config, item, record)
    original = Path(config['source_root']) / item['relative_path']
    archive = safe_path(original.parent / config['archive_folder'] / original.name)
    output = safe_path(record['output_path'])
    if record['status'] != 'DONE' or media.stamp(output) != record['output_signature']:
        raise ValueError('Only a validated output may authorize an archival move.')
    baseline = check_source(source, item, tools)
    _, issues = media.validate_file(output, baseline, True, tools)
    if issues:
        raise ValueError('Archival validation failed: ' + '; '.join(issues))
    if source != archive:
        if archive.exists():
            raise ValueError('Archive destination exists; originals will not be overwritten.')
        archive.parent.mkdir(exist_ok=True)
        record.update(archive_status='PENDING', archive_path=str(archive))
        write_json(job / 'state.json', state)
        media.move_without_overwrite(source, safe_path(archive))
    if media.stamp(archive) != item['signature']:
        raise ValueError('Archived source size/mtime changed.')
    record.update(archive_status='DONE', archived_at=now())


def batch_plan(config, candidates, approval):
    seen, rows = set(), []
    for item in candidates:
        output = destination(config, item['relative_path'], approval['crf'])
        key = unicodedata.normalize('NFC', str(output)).casefold()
        if key in seen:
            raise ValueError(f'Output filename collision: {output}')
        seen.add(key)
        rows.append({'source': item['relative_path'], 'output': str(output), 'source_bytes': item['signature']['size_bytes']})
    return rows


def report_batch(job, state):
    rows = state['files']
    good = [r for r in rows.values() if r.get('status') == 'DONE']
    source_bytes = sum(r.get('source_signature', {}).get('size_bytes', 0) for r in good)
    output_bytes = sum(r.get('output_signature', {}).get('size_bytes', 0) for r in good)
    summary = {'status': state['status'], 'counts': dict(Counter(r['status'] for r in rows.values())),
               'completed': len(good), 'source_bytes': source_bytes, 'output_bytes': output_bytes,
               'copy_reduction_percent': round(100 * (1 - output_bytes / source_bytes), 2) if source_bytes else None,
               'originals_retained': True, 'disk_space_freed_by_this_job': 0,
               'archive_review_count': sum(bool(r.get('archive_error')) for r in rows.values())}
    write_json(job / 'summary.json', summary)
    with safe_path(job / 'results.csv').open('w', newline='', encoding='utf-8-sig') as file:
        fields = ['source', 'status', 'output_path', 'archive_status', 'archive_path', 'error', 'archive_error']
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows({'source': key, **value} for key, value in rows.items())
    return summary


def pause_requested(job):
    path = job / 'control.json'
    if not path.exists():
        return False
    mode = read_json(path)['mode']
    if mode not in ('run', 'pause'):
        raise ValueError('Invalid control file.')
    return mode == 'pause'


def run_batch(job):
    config, plan, candidates, approval = approved_context(job)
    planned = batch_plan(config, candidates, approval)
    tools = tools_for(plan['encoder'])
    state = read_json(job / 'state.json') if (job / 'state.json').exists() else {'files': {}, 'approval_digest': digest(approval)}
    if state['approval_digest'] != digest(approval):
        raise ValueError('Batch approval changed; create a new job.')
    state.update(status='RUNNING', controller_pid=os.getpid())
    write_json(job / 'batch-plan.json', planned)
    write_json(job / 'state.json', state)
    try:
        for item, planned_item in zip(candidates, planned):
            if pause_requested(job):
                state['status'] = 'PAUSED'
                break
            key, output = item['relative_path'], safe_path(planned_item['output'])
            record = state['files'].setdefault(key, {'status': 'PENDING', 'source_signature': item['signature'], 'output_path': str(output)})
            if record['source_signature'] != item['signature'] or record['output_path'] != str(output):
                raise ValueError('Batch record differs from the approved plan.')
            state['current_file'] = key
            write_json(job / 'state.json', state)
            partial, awake = None, None
            try:
                source = source_location(config, item, record)
                baseline = check_source(source, item, tools)
                if output.exists():
                    # A validated output signature is checkpointed BEFORE publication.
                    # A later state-write or transient probe error must not lose that provenance.
                    if media.stamp(output) != record.get('output_signature'):
                        raise ValueError('Existing output has no matching completed/publishing record; it will not be overwritten.')
                    _, issues = media.validate_file(output, baseline, True, tools)
                    if issues:
                        raise ValueError('; '.join(issues))
                    record['status'] = 'DONE'
                else:
                    if record.get('archive_path'):
                        raise ValueError('Archived source has a missing output; review before re-encoding.')
                    old_part = record.get('partial_path')
                    if old_part:
                        old = safe_path(old_part)
                        if old.parent != output.parent or not old.name.startswith('.' + output.name + '.') or not old.name.endswith('.partial.mp4'):
                            raise ValueError('Invalid partial-file recovery record.')
                        if old.exists():
                            old.unlink()
                    output.parent.mkdir(parents=True, exist_ok=True)
                    safe_path(output)
                    if shutil.disk_usage(output.parent).free < item['signature']['size_bytes'] * 2 + 100_000_000:
                        raise ValueError('Insufficient free space for the conservative per-file reserve.')
                    partial = output.with_name('.' + output.name + '.' + uuid.uuid4().hex + '.partial.mp4')
                    record.update(status='ENCODING', partial_path=str(partial))
                    write_json(job / 'state.json', state)
                    if platform.system() == 'Darwin' and Path('/usr/bin/caffeinate').exists():
                        awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())], stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    elapsed = media.media_command(media.encode_command(source, partial, approval['crf'], baseline, tools), key,
                                                  plan.get('timeout_seconds', 0) or None)
                    _, issues = media.validate_file(partial, baseline, True, tools)
                    if media.stamp(source) != item['signature']:
                        issues.append('Source changed during encoding.')
                    if issues:
                        raise ValueError('; '.join(issues))
                    record.update(status='PUBLISHING', output_signature=media.stamp(partial), elapsed_seconds=elapsed)
                    write_json(job / 'state.json', state)
                    media.move_without_overwrite(safe_path(partial), safe_path(output))
                    record.update(status='DONE')
                    record.pop('partial_path', None)
                    write_json(job / 'state.json', state)
                record.pop('error', None)
                try:
                    archive_one(job, config, item, record, state, tools)
                    record.pop('archive_error', None)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    record['archive_error'] = str(error)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, TimeoutError) as error:
                record.update(status='FAILED', error=str(error))
            finally:
                if partial and partial.exists() and record['status'] != 'PUBLISHING':
                    safe_path(partial).unlink()
                    record.pop('partial_path', None)
                if awake is not None:
                    awake.terminate()
                    awake.wait(timeout=10)
                state.pop('current_file', None)
                write_json(job / 'state.json', state)
                report_batch(job, state)
        else:
            state['status'] = 'COMPLETED_WITH_REVIEW' if any(r['status'] != 'DONE' or r.get('archive_error') for r in state['files'].values()) else 'COMPLETED'
    except BaseException:
        state['status'] = 'INTERRUPTED'
        raise
    finally:
        state['updated_at'] = now()
        write_json(job / 'state.json', state)
        summary = report_batch(job, state)
    return summary


def status(job):
    state = read_json(job / 'state.json') if (job / 'state.json').exists() else {}
    return {'controller_active': active(job), 'status': state.get('status', 'NOT_STARTED'),
            'current_file': state.get('current_file'), 'pause_requested': pause_requested(job),
            'counts': dict(Counter(r['status'] for r in state.get('files', {}).values()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    for name in ('init', 'scan', 'sample', 'approve', 'plan', 'run', 'start', 'pause', 'resume', 'status'):
        command = sub.add_parser(name)
        command.add_argument('--job', required=True, type=Path)
        if name == 'init':
            command.add_argument('--source', required=True, type=Path)
            command.add_argument('--output', required=True, type=Path)
        if name == 'approve':
            command.add_argument('--crf', required=True, type=int)
            command.add_argument('--user-confirmation', required=True)
        if name == 'plan':
            command.add_argument('--crf', required=True, type=int)
    args = parser.parse_args()
    try:
        if args.command == 'doctor':
            result = doctor()
        elif args.command == 'init':
            result = init_job(args.job, args.source, args.output)
        else:
            job = safe_path(args.job)
            if not job.is_dir():
                raise ValueError('Job directory does not exist.')
            media.LOG = safe_path(job / 'commands.log')
            if args.command == 'status':
                result = status(job)
            elif args.command in ('pause', 'resume'):
                write_json(job / 'control.json', {'mode': 'pause' if args.command == 'pause' else 'run', 'requested_at': now()})
                result = {'message': 'Pause finishes the current file. Resume clears the request; use start/run if controller has exited.', **status(job)}
            elif args.command == 'start':
                approved_context(job)
                if active(job):
                    raise ValueError('Controller is already active.')
                with safe_path(job / 'controller.log').open('ab') as log:
                    child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'run', '--job', str(job)],
                                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                result = {'launched_pid': child.pid, 'message': 'Run status to verify progress; inspect controller.log on failure.'}
            else:
                with locked(job):
                    if args.command == 'scan':
                        result = scan(job)
                    elif args.command == 'sample':
                        result = sample_job(job)
                    elif args.command == 'approve':
                        result = approve(job, args.crf, args.user_confirmation)
                    elif args.command == 'plan':
                        config, _, plan, candidates = load_plan(job)
                        if args.crf not in plan['crfs']:
                            raise ValueError('Preview CRF must be one of the proposed candidates.')
                        result = batch_plan(config, candidates, {'crf': args.crf})
                    else:
                        result = run_batch(job)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if isinstance(result, dict) and (result.get('status') in ('FAILED', 'COMPLETED_WITH_REVIEW') or (args.command == 'doctor' and not result['ready_to_sample'])):
            return 1
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
