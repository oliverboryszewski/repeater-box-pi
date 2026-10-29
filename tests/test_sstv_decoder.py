"""Robot 36 image assembly, live preview and bounded private PNG storage."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zlib

from test_data_decoder import module, Factory, samples

AudioHub, SstvDecoder = module.AudioHub, module.SstvDecoder


def event(kind, **fields):
    return b'SSTV1 ' + json.dumps({'event': kind, **fields}).encode() + b'\n'


def decode_png(raw):
    """Independently inspect PNG chunks/CRCs and recover unfiltered RGB rows."""
    assert raw[:8] == b'\x89PNG\r\n\x1a\n'
    offset, compressed, header = 8, bytearray(), None
    while offset < len(raw):
        length = struct.unpack('>I', raw[offset:offset+4])[0]
        kind, data = raw[offset+4:offset+8], raw[offset+8:offset+8+length]
        crc = struct.unpack('>I', raw[offset+8+length:offset+12+length])[0]
        assert crc == zlib.crc32(kind + data) & 0xffffffff
        if kind == b'IHDR': header = struct.unpack('>IIBBBBB', data)
        elif kind == b'IDAT': compressed.extend(data)
        elif kind == b'IEND': assert length == 0
        else: raise AssertionError('Unexpected PNG chunk')
        offset += length + 12
    assert offset == len(raw) and header == (320, 240, 8, 2, 0, 0, 0)
    scanlines = zlib.decompress(compressed)
    assert len(scanlines) == 240 * 961
    rows = []
    for row in range(240):
        start = row * 961
        assert scanlines[start] == 0
        rows.append(scanlines[start+1:start+961])
    return rows


class SstvDecoderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.now = 100.0
        self.hub = AudioHub(self.root/'recordings', {'input_enabled': True})
        self.factory = Factory()
        self.decoder = self.make_decoder()

    def make_decoder(self, **options):
        return SstvDecoder(self.root, self.hub, enabled=True, process_factory=self.factory,
            decoder_path='/fake/sstv', sox_path='/fake/sox', monotonic=lambda:self.now,
            wall_time=lambda:1800000000+self.now, **options)

    def tearDown(self):
        self.decoder.close(); self.hub.close(); self.tmp.cleanup()

    def accept(self, kind, **fields):
        return self.decoder._accept_output(event(kind, **fields), self.decoder._epoch)

    def begin(self):
        self.assertTrue(self.accept('begin', mode='Robot36', width=320, height=240))
        return self.decoder.status()['current']

    def row(self, number, color=(255,0,0)):
        self.assertTrue(self.accept('row', row=number, rgb=(bytes(color)*320).hex()))

    def picture(self, lines=240):
        self.now += 1; self.begin()
        for row in range(lines): self.row(row, (row,255-row,37))
        self.assertTrue(self.accept('end', complete=lines == 240))
        return self.decoder.list_images()[0]

    def test_live_scanlines_png_pixels_progress_and_revision_history(self):
        initial = self.begin(); initial_png, _ = self.decoder.image(current_id=initial['id'], revision=initial['revision'])
        self.assertEqual(decode_png(initial_png)[0], bytes(960))
        self.row(0); self.now += 0.5; self.row(1, (0,255,0))
        value = self.decoder.status(); current=value['current']
        self.assertEqual(current['lines_received'],2); self.assertEqual(value['progress'],0.8)
        png, revision = self.decoder.image(current_id=current['id'],revision=current['revision'])
        rows=decode_png(png)
        self.assertEqual(rows[0],b'\xff\0\0'*320); self.assertEqual(rows[1],b'\0\xff\0'*320)
        self.assertEqual(rows[2],bytes(960)); self.assertEqual(revision,current['revision'])
        self.assertEqual(self.decoder.image(current_id=initial['id'],revision=initial['revision'])[0],initial_png)
        self.assertEqual(self.decoder.list_images(),[])

    def test_complete_picture_is_exact_valid_png_and_persists_privately(self):
        saved=self.picture()
        self.assertTrue(saved['complete']); self.assertEqual(saved['lines_received'],240)
        self.assertEqual(self.decoder.status()['phase'],'complete'); self.assertEqual(self.decoder.status()['progress'],100)
        png, revision=self.decoder.image(saved['id']); self.assertEqual(revision,1)
        rows=decode_png(png)
        self.assertEqual(rows[0],bytes((0,255,37))*320); self.assertEqual(rows[239],bytes((239,16,37))*320)
        self.assertEqual(os.stat(self.decoder.directory).st_mode&0o777,0o700)
        for path in self.decoder.directory.iterdir(): self.assertEqual(path.stat().st_mode&0o777,0o600)
        reloaded=self.make_decoder()
        try:self.assertEqual(reloaded.list_images(),[saved]);self.assertEqual(reloaded.image(saved['id'])[0],png)
        finally:reloaded.close()

    def test_partial_picture_is_retained_and_labelled_when_audio_is_interrupted(self):
        self.begin(); self.row(0); self.row(1)
        self.decoder._end_pipeline()
        state=self.decoder.status(); self.assertEqual(state['phase'],'interrupted')
        saved=self.decoder.list_images()[0]
        self.assertFalse(saved['complete']); self.assertEqual(saved['lines_received'],2)
        self.assertEqual(decode_png(self.decoder.image(saved['id'])[0])[0],b'\xff\0\0'*320)
        self.decoder._end_pipeline(); self.assertEqual(len(self.decoder.list_images()),1)

    def test_no_empty_picture_is_added_and_all_rows_finish_even_if_end_is_lost(self):
        self.begin(); self.decoder._end_pipeline(); self.assertEqual(self.decoder.list_images(),[])
        self.begin()
        for row in range(240):self.row(row)
        self.decoder._end_pipeline()
        self.assertTrue(self.decoder.list_images()[0]['complete'])

    def test_preview_revision_cache_is_bounded_and_new_id_never_serves_old_picture(self):
        first=self.begin()
        for row in range(8):self.now+=0.5;self.row(row)
        self.assertLessEqual(len(self.decoder._previews),4)
        with self.assertRaises(ValueError):self.decoder.image(current_id=first['id'],revision=first['revision'])
        latest=self.decoder.status()['current'];self.begin()
        with self.assertRaises(ValueError):self.decoder.image(current_id=latest['id'],revision=latest['revision'])

    def test_only_sequential_exact_rgb_rows_and_fixed_robot36_dimensions_are_accepted(self):
        for fields in ({'mode':'Other','width':320,'height':240},{'mode':'Robot36','width':640,'height':480},
                       {'mode':'Robot36','width':True,'height':240}):
            self.assertFalse(self.accept('begin',**fields))
        self.assertIsNone(self.decoder.status()['current']); self.begin()
        for fields in ({'row':1,'rgb':'00'*960},{'row':False,'rgb':'00'*960},{'row':0,'rgb':'zz'*960},
                       {'row':0,'rgb':'00'*959},{'row':0,'rgb':'00'*960,'path':'/tmp/file'}):
            self.assertFalse(self.accept('row',**fields))
        self.row(0); self.assertFalse(self.accept('end',complete=True));self.assertTrue(self.accept('end',complete=False))

    def test_malformed_duplicate_and_oversized_output_is_not_an_image_or_command(self):
        for raw in (b'APRS: ignored\n',b'SSTV1 {}\n',b'SSTV1 {"event":"begin","event":"row"}\n',
                    event('begin',mode='Robot36',width=320,height=240)[:-1],b'x'*4097):
            self.assertFalse(self.decoder._accept_output(raw,self.decoder._epoch))
        self.assertEqual(self.decoder.list_images(),[]); self.assertIsNone(self.decoder.status()['current'])

    def test_feed_off_disabled_closed_and_old_worker_cannot_publish_images(self):
        old=self.decoder._epoch; self.decoder.set_enabled(False)
        self.assertFalse(self.accept('begin',mode='Robot36',width=320,height=240))
        self.decoder.set_enabled(True)
        self.assertFalse(self.decoder._accept_output(event('begin',mode='Robot36',width=320,height=240),old))
        self.hub.update_settings({'input_enabled':False})
        self.assertFalse(self.accept('begin',mode='Robot36',width=320,height=240))
        self.hub.update_settings({'input_enabled':True});self.decoder.close()
        self.assertFalse(self.accept('begin',mode='Robot36',width=320,height=240))

    def test_gallery_rotates_only_owned_images_and_respects_count_and_byte_limits(self):
        self.decoder.MAX_IMAGES=2
        unrelated=self.decoder.directory/'keep.txt';unrelated.write_text('unrelated')
        saved=[self.picture(2) for _ in range(3)]
        self.assertEqual([v['id'] for v in self.decoder.list_images()],[saved[2]['id'],saved[1]['id']])
        self.assertTrue(unrelated.exists())
        with self.assertRaises(FileNotFoundError):self.decoder.image(saved[0]['id'])
        self.decoder.MAX_BYTES=6000
        for _ in range(4):self.picture(3)
        self.assertLessEqual(self.decoder.status()['stored_bytes'],6000)

    def test_low_space_rotates_own_gallery_then_pauses_without_touching_other_files(self):
        self.picture(2)
        low=Mock(free=0)
        with patch.object(module.shutil,'disk_usage',return_value=low):
            self.begin();self.row(0);self.assertTrue(self.accept('end',complete=False))
        self.assertFalse(self.decoder.status()['available']);self.assertIsNotNone(self.decoder.status()['last_error'])
        self.assertEqual(self.decoder.list_images(),[])

    def test_store_failure_stops_further_disk_writes_and_image_tampering_is_refused(self):
        saved=self.picture(2);path=self.decoder.directory/(saved['id']+'.png')
        original=path.read_bytes();path.write_bytes(original[:-1]+b'X')
        with self.assertRaises(OSError):self.decoder.image(saved['id'])
        path.write_bytes(original)
        self.begin();self.row(0)
        with patch.object(self.decoder,'_atomic_image_file',side_effect=OSError('disk fault')):
            self.assertTrue(self.accept('end',complete=False))
        self.assertFalse(self.decoder.status()['available'])
        self.assertFalse(self.accept('begin',mode='Robot36',width=320,height=240))

    def test_new_header_preserves_a_previous_image_save_failure(self):
        self.begin();self.row(0)
        identifier=self.decoder.status()['current']['id']
        with patch.object(self.decoder,'_atomic_image_file',side_effect=OSError('disk fault')):
            self.assertFalse(self.accept('begin',mode='Robot36',width=320,height=240))
        self.assertFalse(self.decoder.status()['available'])
        self.assertIsNotNone(self.decoder.status()['last_error'])
        self.assertEqual(self.decoder.status()['current']['id'],identifier)

    def test_symlink_images_and_orphans_are_never_offered_and_targets_are_preserved(self):
        identifier='f'*32;outside=self.root/'outside.png';outside.write_bytes(b'keep')
        path=self.decoder.directory/(identifier+'.png');path.symlink_to(outside)
        reloaded=self.make_decoder()
        try:self.assertFalse(reloaded.status()['available']);self.assertEqual(outside.read_bytes(),b'keep')
        finally:reloaded.close()
        path.unlink();path.write_bytes(b'orphan generated image');path.chmod(0o600)
        reloaded=self.make_decoder()
        try:self.assertFalse(path.exists());self.assertEqual(reloaded.list_images(),[])
        finally:reloaded.close()

    def test_native_pipeline_uses_only_48k_stdin_and_two_decoders_share_one_raw_feed(self):
        packet_decoder=module.DataDecoder(self.root,self.hub,enabled=True,process_factory=self.factory,
            decoder_path='/fake/packet',sox_path='/fake/sox')
        try:
            packet_decoder.start();self.decoder.start()
            self.assertEqual(len(self.hub._raw_consumers),2)
            self.assertEqual(self.decoder._resampler_command().count('48000'),2)
            self.assertEqual(self.decoder._decoder_command(),['/fake/sstv'])
            self.assertNotIn('22050',self.decoder._resampler_command())
        finally:packet_decoder.close()

    def test_data_settings_saves_are_idempotent_and_late_install_can_be_discovered(self):
        self.begin();self.row(0);current=self.decoder.status()['current']
        self.decoder.set_enabled(True)
        self.assertEqual(self.decoder.status()['current'],current)
        self.decoder._tools_ready=False;self.decoder._tools_check_at=self.now+5
        with patch.object(self.decoder,'_validate_tools',return_value=True) as check:
            self.decoder.status();check.assert_not_called();self.now+=6
            self.assertTrue(self.decoder.status()['available']);check.assert_called_once()


if __name__=='__main__':unittest.main()
