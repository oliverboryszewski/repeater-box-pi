"""Spoken menus have audible spacing, valid PCM and a bounded RF duration."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import wave

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prompt_builder", ROOT / "scripts/build-dtmf-prompts.py")
prompts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prompts)


def pcm_wave(path, audio, rate=16000, channels=1, width=2):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(audio)


class PromptAudioTest(unittest.TestCase):
    def test_menu_pause_and_lead_in_preserve_speech_samples_and_pcm_format(self):
        first = b"\x01\x00" * 1600
        second = b"\x02\x00" * 3200
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "menu.wav"
            duration = prompts.write_prompt(path, [first, second])
            expected = prompts.silence(prompts.LEAD_SECONDS) + first
            expected += prompts.silence(prompts.MENU_GAP_SECONDS) + second
            expected += prompts.silence(prompts.TAIL_SECONDS)
            self.assertEqual(prompts.read_pcm(path), expected)
            self.assertAlmostEqual(duration, len(expected) / 32000)

    def test_renderer_uses_slower_speech_and_structured_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.wav"
            output = Path(temporary) / "output.wav"
            audio = b"\x10\x00" * 1600
            def run(arguments, **kwargs):
                self.assertTrue(kwargs["check"])
                self.assertIsInstance(arguments, list)
                if arguments[0] == "/usr/bin/sox":
                    pcm_wave(Path(arguments[-1]), audio)
            with mock.patch.object(prompts.subprocess, "run", side_effect=run) as called:
                self.assertEqual(prompts.render_phrase("Control menu.", source, output), audio)
            speech = called.call_args_list[0].args[0]
            self.assertEqual(speech[speech.index("-s") + 1], "120")
            self.assertIn("-z", speech)  # Spacing is supplied explicitly once.
            self.assertEqual(speech[-1], "Control menu.")

    def test_empty_wrong_format_and_truncated_audio_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.wav"
            for rate, channels, width, audio in ((8000, 1, 2, b"\x00" * 32),
                    (16000, 2, 2, b"\x00" * 32), (16000, 1, 1, b"\x00" * 32),
                    (16000, 1, 2, b"")):
                pcm_wave(path, audio, rate, channels, width)
                with self.assertRaises(ValueError):
                    prompts.read_pcm(path)
            pcm_wave(path, b"\x01\x00" * 1600)
            path.write_bytes(path.read_bytes()[:-100])
            with self.assertRaises(ValueError):
                prompts.read_pcm(path)

    def test_duration_checks_include_combined_current_settings(self):
        durations = dict.fromkeys(prompts.PROMPTS, 1.0)
        prompts.validate_durations(durations)
        durations["authenticated"] = 12.01
        with self.assertRaises(ValueError):
            prompts.validate_durations(durations)
        durations = dict.fromkeys(prompts.PROMPTS, 1.0)
        durations["identification_on"] = 5
        durations["status_on"] = 5
        with self.assertRaisesRegex(ValueError, "current-settings"):
            prompts.validate_durations(durations)

    def test_extended_menu_and_all_decoder_settings_have_a_separate_twenty_second_bound(self):
        durations = dict.fromkeys(prompts.PROMPTS, 1.0)
        durations["authenticated_data"] = 20
        prompts.validate_durations(durations)
        durations["authenticated_data"] = 20.01
        with self.assertRaises(ValueError):
            prompts.validate_durations(durations)
        durations = dict.fromkeys(prompts.PROMPTS, 1.0)
        durations["aprs_on"] = durations["sstv_off"] = 8
        with self.assertRaisesRegex(ValueError, "extended current-settings"):
            prompts.validate_durations(durations)

    def test_full_legacy_summary_replaces_acknowledgment_at_the_exact_budget(self):
        durations = dict.fromkeys(prompts.PROMPTS, 1.0)
        # Status + pair + recording + identification + announcements = 12 s.
        # The separate recording acknowledgment must not be counted twice.
        durations["recording_on"] = 8.0
        prompts.validate_durations(durations)
        durations["recording_on"] += 1 / prompts.SAMPLE_RATE
        with self.assertRaisesRegex(ValueError, "current-settings"):
            prompts.validate_durations(durations)

    def test_extended_summary_and_its_separate_ack_share_the_twenty_second_cap(self):
        durations = dict.fromkeys(prompts.PROMPTS, 1.0)
        # Five legacy summary clips + APRS + SSTV = 20 s. The APRS
        # acknowledgment is standalone when announcements are off.
        durations["aprs_on"] = 14.0
        prompts.validate_durations(durations)
        durations["aprs_on"] += 1 / prompts.SAMPLE_RATE
        with self.assertRaisesRegex(ValueError, "extended current-settings"):
            prompts.validate_durations(durations)

    def test_standalone_clips_keep_their_original_limits(self):
        durations = dict.fromkeys(prompts.PROMPTS, 1.0)
        durations["cancelled"] = prompts.MAX_RESPONSE_SECONDS
        durations["choose_aprs"] = prompts.MAX_DATA_RESPONSE_SECONDS
        prompts.validate_durations(durations)
        for name, limit in (("cancelled", prompts.MAX_RESPONSE_SECONDS),
                            ("choose_aprs", prompts.MAX_DATA_RESPONSE_SECONDS)):
            changed = dict(durations)
            changed[name] = limit + 1 / prompts.SAMPLE_RATE
            with self.assertRaisesRegex(ValueError, "spoken prompt"):
                prompts.validate_durations(changed)

    def test_build_rejects_an_overlong_menu_before_replacing_installed_clips(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            original = directory / "authenticated.wav"
            original.write_bytes(b"existing installed clip")
            with mock.patch.object(prompts, "render_phrase", return_value=b"\x01\x00" * 16000 * 3):
                with self.assertRaisesRegex(ValueError, "transmit budget"):
                    prompts.build(directory)
            self.assertEqual(original.read_bytes(), b"existing installed clip")
            self.assertEqual(list(directory.iterdir()), [original])

    def test_complete_build_has_all_fixed_clips_and_separate_menu_phrases(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with mock.patch.object(prompts, "render_phrase", return_value=b"\x01\x00" * 1600) as render:
                durations = prompts.build(directory)
            self.assertEqual(set(durations), set(prompts.PROMPTS))
            self.assertEqual({path.stem for path in directory.iterdir()}, set(prompts.PROMPTS))
            for part in prompts.MENU_PARTS:
                self.assertEqual(sum(call.args[0] == part for call in render.call_args_list), 1)
            for path in directory.iterdir():
                self.assertTrue(prompts.read_pcm(path))
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
