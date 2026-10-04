"""Behavior checks on disposable fixtures only; never read personal video files."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / '.agents/skills/video-archive/scripts'
sys.path.insert(0, str(SCRIPTS))
import workflow as w
import media


def probe(pixel='yuv420p', codec='h264'):
    return {'streams': [{'index': 0, 'codec_type': 'video', 'codec_name': codec,
                         'width': 128, 'height': 96, 'pix_fmt': pixel, 'duration': '2',
                         'nb_frames': '48', 'avg_frame_rate': '24/1', 'color_range': 'tv',
                         'color_space': 'bt709', 'color_transfer': 'bt709', 'color_primaries': 'bt709'}],
            'format': {'duration': '2'}}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='video-archive-tests-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / 'source'
        self.source.mkdir()
        self.output, self.job = self.base / 'compressed', self.base / 'job'
        self.config = w.init_job(self.job, self.source, self.output)
        self.video = self.source / "家庭 & (一) [2] # '片段.MP4"
        self.video.write_bytes(b'original video fixture')
        media.LOG = self.job / 'commands.log'
        media.ENCODER, media.PRESET, media.THREADS = 'libx265', 'medium', 1
        self.data = probe()
        self.profile_id = w.profile(self.data)[0]
        self.plan = {'goal': 'Test fixture goal', 'rationale': 'Test fixture rationale', 'encoder': 'libx265',
                     'preset': 'medium', 'crfs': [20, 22], 'threads': 1, 'profile_ids': [self.profile_id],
                     'samples': [{'relative_path': self.video.name, 'start_seconds': 0, 'seconds': 1}]}

    def scanned(self):
        with patch.object(w, 'tools_for', return_value={'ffprobe': 'fixture'}), patch.object(media, 'probe_file', return_value=self.data):
            result = w.scan(self.job)
        w.write_json(self.job / 'plan.json', self.plan)
        return result

    def evidence(self):
        config, manifest, plan, _ = w.load_plan(self.job)
        folder = self.job / 'samples-fixture'
        folder.mkdir()
        reference, output = folder / 'reference.mp4', folder / 'CRF22.mp4'
        reference.write_bytes(b'reference fixture')
        output.write_bytes(b'sample fixture')
        record = {'sample_index': 1, 'crf': 22, 'profile_id': self.profile_id, 'reference': str(reference),
                  'output': str(output), 'reference_signature': media.stamp(reference),
                  'output_signature': media.stamp(output), 'status': 'PASS'}
        w.write_json(self.job / 'samples.json', {'fingerprint': w.fingerprint(config, manifest, plan),
                                               'status': 'PASS', 'results': [record]})

    def approved(self, archive='keep'):
        self.config['archive_mode'] = archive
        w.write_json(self.job / 'config.json', self.config)
        self.scanned()
        self.evidence()
        w.approve(self.job, 22, 'TEST FIXTURE ONLY: select CRF22 for this fixture batch; archive=' + archive)

    def run_fixture(self, validation_issues=(), callback=None):
        def encode(args, *rest):
            Path(args[-1]).write_bytes(b'encoded fixture')
            if callback:
                callback()
            return .01
        with patch.object(w, 'tools_for', return_value={'ffprobe': 'fixture', 'ffmpeg': 'fixture'}), \
                patch.object(media, 'probe_file', return_value=self.data), \
                patch.object(media, 'leading_discard_packets', return_value=0), \
                patch.object(media, 'media_command', side_effect=encode) as called, \
                patch.object(media, 'validate_file', return_value=(self.data, list(validation_issues))):
            result = w.run_batch(self.job)
            return result, called.call_count

    def test_doctor_reports_missing_tools_without_installing(self):
        with patch.object(shutil, 'which', return_value=None), patch.object(subprocess, 'run') as run:
            result = w.doctor()
        self.assertEqual(result['missing'], ['ffmpeg', 'ffprobe'])
        self.assertFalse(result['ready_to_sample'])
        run.assert_not_called()

    def test_selected_encoder_is_required(self):
        report = {'ready_to_scan': True, 'ready_to_sample': True, 'encoders': {'libx265': False, 'libx264': True}, 'tools': {}}
        with patch.object(w, 'doctor', return_value=report), self.assertRaisesRegex(ValueError, 'Missing encoder'):
            w.tools_for('libx265')

    def test_source_job_overlap_and_symlinks_rejected(self):
        with self.assertRaises(ValueError):
            w.init_job(self.source / 'job', self.source, self.output)
        link = self.base / 'link'
        link.symlink_to(self.source)
        with self.assertRaises(ValueError):
            w.init_job(self.base / 'new-job', link, self.output)
        hard = self.source / 'hard.mp4'
        os.link(self.video, hard)
        with self.assertRaises(ValueError):
            w.safe_path(hard)

    def test_scan_profiles_and_excludes_output_and_archive(self):
        self.config['output_root'] = str(self.source / 'compressed')
        w.write_json(self.job / 'config.json', self.config)
        for directory in ('compressed', 'Originals', '.hidden', '已压缩'):
            folder = self.source / directory
            folder.mkdir()
            (folder / 'skip.mp4').write_bytes(b'ignored')
        result = self.scanned()
        self.assertEqual(result['counts'], {'ELIGIBLE': 1})
        self.assertEqual(result['profiles'][self.profile_id]['count'], 1)
        self.assertEqual(self.video.read_bytes(), b'original video fixture')

    def test_hdr_unknown_color_and_subtitles_need_review(self):
        for field, value in (('color_transfer', 'arib-std-b67'), ('color_transfer', 'smpte2084'), ('color_space', None)):
            data = probe()
            data['streams'][0][field] = value
            self.assertEqual(w.eligibility(data)[0], 'REVIEW')
        data = probe()
        data['streams'].append({'codec_type': 'subtitle'})
        self.assertEqual(w.eligibility(data)[0], 'REVIEW')
        self.assertEqual(w.eligibility(probe('yuv420p10le', 'hevc'))[0], 'ELIGIBLE')

    def test_batch_requires_sample_and_user_record(self):
        self.scanned()
        with self.assertRaises(FileNotFoundError):
            w.run_batch(self.job)
        self.assertFalse(self.output.exists())
        self.evidence()
        with self.assertRaises(ValueError):
            w.approve(self.job, 22, ' ')

    def test_untested_crf_is_rejected(self):
        self.scanned()
        self.evidence()
        with self.assertRaisesRegex(ValueError, 'not successfully tested'):
            w.approve(self.job, 20, 'Fixture decision')

    def test_configuration_change_invalidates_approval(self):
        self.approved()
        self.plan['preset'] = 'fast'
        w.write_json(self.job / 'plan.json', self.plan)
        with self.assertRaisesRegex(ValueError, 'different configuration'):
            w.approved_context(self.job)
        self.assertFalse(self.output.exists())

    def test_archive_choice_change_invalidates_approval(self):
        self.approved()
        self.config['archive_mode'] = 'move'
        w.write_json(self.job / 'config.json', self.config)
        with self.assertRaises(ValueError):
            w.approved_context(self.job)

    def test_changed_sample_invalidates_approval(self):
        self.approved()
        (self.job / 'samples-fixture/CRF22.mp4').write_bytes(b'changed sample')
        with self.assertRaisesRegex(ValueError, 'Sample files changed'):
            w.approved_context(self.job)

    def test_every_selected_profile_requires_a_sample(self):
        self.scanned()
        manifest = w.read_json(self.job / 'manifest.json')
        second = copy.deepcopy(manifest['items'][0])
        second['profile_id'], second['relative_path'] = 'another-profile', 'other.mp4'
        manifest['items'].append(second)
        w.write_json(self.job / 'manifest.json', manifest)
        self.plan['profile_ids'].append('another-profile')
        w.write_json(self.job / 'plan.json', self.plan)
        with self.assertRaisesRegex(ValueError, 'every selected profile'):
            w.load_plan(self.job)

    def test_h264_does_not_reduce_ten_bit_sources(self):
        self.data = probe('yuv420p10le', 'hevc')
        self.profile_id = w.profile(self.data)[0]
        self.plan.update(encoder='libx264', profile_ids=[self.profile_id])
        self.scanned()
        with self.assertRaisesRegex(ValueError, 'Do not reduce 10-bit'):
            w.load_plan(self.job)

    def test_media_command_preserves_depth_and_rotation_validation(self):
        data = probe('yuv420p10le', 'hevc')
        command = media.encode_command(self.video, self.output / 'out.mp4', 22, data, {'ffmpeg': 'fixture'})
        self.assertEqual(command[command.index('-pix_fmt') + 1], 'yuv420p10le')
        self.assertEqual(command[command.index('-i') + 1], str(self.video))
        self.assertIn('-n', command)
        wrong = copy.deepcopy(data)
        wrong['streams'][0]['pix_fmt'] = 'yuv420p'
        self.assertTrue(media.validation(wrong, data, True))
        data['streams'][0]['side_data_list'] = [{'side_data_type': 'Display Matrix', 'rotation': 90}]
        self.assertTrue(media.validation(wrong, data, True))

    def test_preexisting_output_never_overwritten(self):
        self.approved()
        output = w.destination(self.config, self.video.name, 22)
        output.parent.mkdir()
        output.write_bytes(b'user existing file')
        result, count = self.run_fixture()
        self.assertEqual(count, 0)
        self.assertEqual(result['status'], 'COMPLETED_WITH_REVIEW')
        self.assertEqual(output.read_bytes(), b'user existing file')
        self.assertTrue(self.video.exists())

    def test_validation_failure_does_not_publish_or_archive(self):
        self.approved('move')
        result, count = self.run_fixture(validation_issues=['duration changed'])
        self.assertEqual(count, 1)
        self.assertEqual(result['completed'], 0)
        self.assertFalse(w.destination(self.config, self.video.name, 22).exists())
        self.assertTrue(self.video.exists())

    def test_success_keeps_original_and_resume_skips_reencoding(self):
        self.approved()
        original = media.stamp(self.video)
        result, count = self.run_fixture()
        self.assertEqual((result['completed'], count), (1, 1))
        self.assertEqual(media.stamp(self.video), original)
        self.assertEqual(result['disk_space_freed_by_this_job'], 0)
        result, count = self.run_fixture()
        self.assertEqual((result['completed'], count), (1, 0))

    def test_success_moves_only_when_chosen_and_resumes(self):
        self.approved('move')
        before = media.stamp(self.video)
        self.run_fixture()
        archive = self.source / 'Originals' / self.video.name
        self.assertFalse(self.video.exists())
        self.assertEqual(media.stamp(archive), before)
        result, count = self.run_fixture()
        self.assertEqual((result['completed'], count), (1, 0))

    def test_archive_collision_preserves_both_sources(self):
        self.approved('move')
        archive = self.source / 'Originals' / self.video.name
        archive.parent.mkdir()
        archive.write_bytes(b'unrelated archive')
        result, _ = self.run_fixture()
        self.assertEqual(result['archive_review_count'], 1)
        self.assertEqual(archive.read_bytes(), b'unrelated archive')
        self.assertTrue(self.video.exists())

    def test_publish_crash_is_recovered_without_reencoding(self):
        self.approved()
        original_move = media.move_without_overwrite
        def interrupted(part, output):
            original_move(part, output)
            raise KeyboardInterrupt('simulated crash after publish')
        with patch.object(media, 'move_without_overwrite', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_fixture()
        result, count = self.run_fixture()
        self.assertEqual((result['completed'], count), (1, 0))

    def test_archive_crash_is_recovered_without_reencoding(self):
        self.approved('move')
        original_move = media.move_without_overwrite
        def interrupted(source, output):
            original_move(source, output)
            if output.parent.name == 'Originals':
                raise KeyboardInterrupt('simulated crash after archive move')
        with patch.object(media, 'move_without_overwrite', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_fixture()
        self.assertFalse(self.video.exists())
        result, count = self.run_fixture()
        self.assertEqual((result['completed'], count), (1, 0))
        self.assertEqual(w.read_json(self.job / 'state.json')['files'][self.video.name]['archive_status'], 'DONE')

    def test_published_output_recovers_after_checkpoint_write_error(self):
        self.approved()
        save = w.write_json
        failed = False
        def unreliable_save(path, data):
            nonlocal failed
            records = list(data.get('files', {}).values()) if isinstance(data, dict) else []
            if not failed and path.name == 'state.json' and records and records[0]['status'] == 'DONE':
                failed = True
                raise OSError('simulated checkpoint write error after publication')
            return save(path, data)
        with patch.object(w, 'write_json', side_effect=unreliable_save):
            result, _ = self.run_fixture()
            self.assertEqual(result['status'], 'COMPLETED_WITH_REVIEW')
        result, count = self.run_fixture()
        self.assertEqual((result['completed'], count), (1, 0))

    def test_pause_finishes_current_file_and_archive(self):
        second = self.source / 'second.mp4'
        second.write_bytes(b'another video')
        self.approved('move')
        result, count = self.run_fixture(callback=lambda: w.write_json(self.job / 'control.json', {'mode': 'pause'}))
        self.assertEqual((result['status'], result['completed'], count), ('PAUSED', 1, 1))
        record = next(iter(w.read_json(self.job / 'state.json')['files'].values()))
        self.assertEqual(record['archive_status'], 'DONE')

    def test_job_lock_rejects_second_controller(self):
        with w.locked(self.job):
            self.assertTrue(w.active(self.job))
            with self.assertRaisesRegex(ValueError, 'already has an active'):
                with w.locked(self.job):
                    pass
        self.assertFalse(w.active(self.job))

    def test_long_encode_is_not_terminated_by_default(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired('fixture', 30), 0]
        with patch.object(media.subprocess, 'Popen', return_value=process), \
                patch.object(media.time, 'monotonic', side_effect=[0, 6001, 6002]):
            self.assertEqual(media.media_command(['fixture'], 'long fixture'), 6002)
        process.terminate.assert_not_called()

    def test_explicit_encode_timeout_stops_only_its_process(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired('fixture', 30), 0]
        with patch.object(media.subprocess, 'Popen', return_value=process), \
                patch.object(media.time, 'monotonic', side_effect=[0, 61]):
            with self.assertRaises(TimeoutError):
                media.media_command(['fixture'], 'timed fixture', 60)
        process.terminate.assert_called_once()

    def test_output_filename_collision_is_detected(self):
        self.approved()
        config, _, candidates, approval = w.approved_context(self.job)
        other = copy.deepcopy(candidates[0])
        other['relative_path'] = str(Path(other['relative_path']).with_suffix('.MOV'))
        with self.assertRaisesRegex(ValueError, 'collision'):
            w.batch_plan(config, candidates + [other], approval)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe') and os.name == 'posix', 'FFmpeg/POSIX required')
class SyntheticIntegrationTests(unittest.TestCase):
    def exercise(self, pixel, encoder, archive):
        with tempfile.TemporaryDirectory(prefix='video-archive-integration-') as temporary:
            base = Path(temporary).resolve()
            source, output, job = base / 'source', base / 'output', base / 'job'
            source.mkdir()
            original = source / "short & 'sample.mp4"
            generate = [shutil.which('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi', '-i',
                        'testsrc2=size=128x96:rate=24', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                        '-t', '1', '-c:v', 'libx265' if pixel == 'yuv420p10le' else 'libx264', '-threads', '1',
                        '-filter_threads', '1', '-pix_fmt', pixel, '-preset', 'ultrafast', '-g', '24',
                        '-vf', 'setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709',
                        '-c:a', 'aac', '-color_range', 'tv', '-colorspace', 'bt709', '-color_trc', 'bt709', '-color_primaries', 'bt709']
            if pixel == 'yuv420p10le':
                generate += ['-x265-params', 'pools=1:frame-threads=1:log-level=error']
            subprocess.run(generate + [str(original)], check=True, capture_output=True, timeout=60)
            before = media.stamp(original)
            config = w.init_job(job, source, output)
            config['archive_mode'] = archive
            w.write_json(job / 'config.json', config)
            media.LOG = job / 'commands.log'
            analysis = w.scan(job)
            self.assertEqual(analysis['counts'], {'ELIGIBLE': 1}, w.read_json(job / 'manifest.json')['items'])
            plan = {'goal': 'Synthetic test', 'rationale': 'Synthetic test', 'encoder': encoder, 'preset': 'ultrafast',
                    'threads': 1, 'crfs': [20, 22], 'profile_ids': list(analysis['profiles']),
                    'samples': [{'relative_path': original.name, 'start_seconds': 0, 'seconds': 1}]}
            w.write_json(job / 'plan.json', plan)
            result = w.sample_job(job)
            self.assertEqual(result['status'], 'PASS')
            w.approve(job, 22, 'TEST FIXTURE: selected CRF22, execute this synthetic test batch; archive=' + archive)
            if encoder == 'libx264':
                launch = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'workflow.py'), 'start', '--job', str(job)],
                                        capture_output=True, text=True, check=True, timeout=10)
                self.assertGreater(json.loads(launch.stdout)['launched_pid'], 0)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    state = w.status(job)
                    if state['status'] in ('COMPLETED', 'COMPLETED_WITH_REVIEW') and not state['controller_active']:
                        break
                    time.sleep(.1)
                result = w.read_json(job / 'summary.json')
            else:
                with w.locked(job):
                    result = w.run_batch(job)
            self.assertEqual(result['status'], 'COMPLETED')
            destination = w.destination(config, original.name, 22)
            metadata = media.probe_file(destination, w.tools_for(encoder))
            self.assertEqual(media.videos(metadata)[0]['pix_fmt'], pixel)
            self.assertEqual(len(media.audios(metadata)), 1)
            actual = original if archive == 'keep' else original.parent / 'Originals' / original.name
            self.assertEqual(media.stamp(actual), before)
            signature = media.stamp(destination)
            with w.locked(job):
                self.assertEqual(w.run_batch(job)['completed'], 1)
            self.assertEqual(media.stamp(destination), signature)

    def test_real_h265_8bit_keep(self):
        self.exercise('yuv420p', 'libx265', 'keep')

    def test_real_h265_10bit_move(self):
        self.exercise('yuv420p10le', 'libx265', 'move')

    def test_real_h264_8bit_keep(self):
        self.exercise('yuv420p', 'libx264', 'keep')


if __name__ == '__main__':
    unittest.main()
