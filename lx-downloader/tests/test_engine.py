# -*- coding: utf-8 -*-
"""落雪下载引擎单元测试。

运行：cd lx-downloader && python -m unittest discover -s tests -v
覆盖纯逻辑部分（不联网、不依赖音源脚本）：
  - 音质档位与降级序列
  - 文件名归一化 / 模糊匹配
  - 时长解析与校验
  - 版本词拦截、歌手标签匹配
  - 配置自愈（乱码路径）
  - 音频格式魔数探测
"""
import os
import sys
import json
import re
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lx_music_downloader as eng  # noqa: E402

# MPEG1 Layer3 帧头第三个字节的码率索引（高 4 位）
_BITRATE_IDX = {320: 0xE0, 192: 0xA0, 160: 0x80, 128: 0x90, 64: 0x50}


def make_mp3(path, seconds=4, bitrate_kbps=320):
    """写一个带有效帧头序列的 MP3，让 mutagen 能解析出时长与码率。

    帧头 0xFF 0xFB <br_idx> 0x00 = MPEG1 Layer3 / <br>kbps / 44100Hz，
    帧长 144*bitrate/采样率，逐帧重复即可被 mutagen 正确识别。
    """
    sample_rate = 44100
    frame_len = int(144 * bitrate_kbps * 1000 / sample_rate)
    n_frames = max(1, int(seconds * sample_rate / 1152))
    header = bytes([0xFF, 0xFB, _BITRATE_IDX[bitrate_kbps], 0x00])
    with open(path, 'wb') as f:
        for _ in range(n_frames):
            f.write(header + b'\x00' * max(0, frame_len - len(header)))


class QualityTierTest(unittest.TestCase):
    """音质档位：请求什么档位就按什么门槛验收，且绝不因“更好”而拒收。"""

    def test_min_bitrate_mapping(self):
        self.assertEqual(eng.quality_min_bitrate('128k'), 128000)
        self.assertEqual(eng.quality_min_bitrate('192k'), 192000)
        self.assertEqual(eng.quality_min_bitrate('320k'), 320000)
        self.assertIsNone(eng.quality_min_bitrate('flac'))
        self.assertIsNone(eng.quality_min_bitrate('flac24bit'))

    def test_unknown_quality_defaults_to_320k(self):
        # 拼写错误不能把门槛放低
        self.assertEqual(eng.quality_min_bitrate('hires'), 320000)
        self.assertEqual(eng.quality_min_bitrate(''), 320000)
        self.assertEqual(eng.quality_min_bitrate(None), 320000)

    def test_lower_quality_options_from_lossless(self):
        self.assertEqual(
            eng.lower_quality_options('flac', '192k'),
            ['flac', '320k', '192k'],
        )

    def test_lower_quality_options_default_floor_is_320k(self):
        # 默认底线 320k：从 flac 降级不允许掉到 192k/128k
        self.assertEqual(eng.lower_quality_options('flac'), ['flac', '320k'])

    def test_lower_quality_options_128k(self):
        self.assertEqual(eng.lower_quality_options('128k', '128k'), ['128k'])
        self.assertEqual(eng.lower_quality_options('192k', '128k'), ['192k', '128k'])

    def test_lower_quality_options_hi_res(self):
        self.assertEqual(
            eng.lower_quality_options('flac24bit', '320k'),
            ['flac24bit', 'flac', '320k'],
        )

    def test_lower_quality_options_unknown_quality(self):
        self.assertEqual(eng.lower_quality_options('weird'), ['weird'])


class NameMatchTest(unittest.TestCase):
    def test_norm_song_key_strips_brackets_and_punct(self):
        self.assertEqual(eng.norm_song_key('Hello, World! (Live)'), 'helloworld')
        # 全角括号连同内容一并剥离（版本后缀场景依赖此行为）
        self.assertEqual(eng.norm_song_key('モニタリング（高阶）'), 'モニタリング')

    def test_norm_song_key_keeps_japanese(self):
        # 回归：早期版本用 [^\w\u4e00-\u9fa5] 过滤，会把日文假名整段删掉
        self.assertEqual(eng.norm_song_key('モニタリング'), 'モニタリング')

    def test_norm_song_key_match_exact(self):
        self.assertTrue(eng.norm_song_key_match('晴天', '晴天'))

    def test_norm_song_key_match_version_suffix(self):
        self.assertTrue(eng.norm_song_key_match('萌妹进行曲', '萌妹进行曲（高阶萌妹成长指南）'))

    def test_norm_song_key_match_rejects_different(self):
        self.assertFalse(eng.norm_song_key_match('晴天', '七里香'))
        self.assertFalse(eng.norm_song_key_match('Cherry Pop', 'GHOST'))

    def test_norm_song_key_match_empty_is_permissive(self):
        # 空值不做判断（避免误杀无标题的候选）
        self.assertTrue(eng.norm_song_key_match('', '晴天'))


class DurationTest(unittest.TestCase):
    def test_interval_to_seconds(self):
        self.assertAlmostEqual(eng.interval_to_seconds('03:45'), 225.0)
        self.assertAlmostEqual(eng.interval_to_seconds('02:56.5'), 176.5)
        self.assertAlmostEqual(eng.interval_to_seconds('01:02:03'), 3723.0)
        self.assertIsNone(eng.interval_to_seconds(''))
        self.assertIsNone(eng.interval_to_seconds(None))
        self.assertIsNone(eng.interval_to_seconds('abc'))

    def test_verify_duration_no_expectation(self):
        self.assertTrue(eng.verify_duration('nonexistent.mp3', None))

    def test_verify_duration_unparsable_expected(self):
        # 预期值无法解析时放行，不误杀
        self.assertTrue(eng.verify_duration('nonexistent.mp3', 'xx:yy'))


class FilenameTest(unittest.TestCase):
    def test_sanitize_strips_illegal_chars(self):
        self.assertEqual(eng.sanitize_filename('a/b:c*d?e"f<g>h|i'), 'a_b_c_d_e_f_g_h_i')

    def test_sanitize_strips_newlines(self):
        self.assertEqual(eng.sanitize_filename('line1\nline2'), 'line1_line2')

    def test_sanitize_keeps_chinese(self):
        self.assertEqual(eng.sanitize_filename('周杰伦 - 晴天'), '周杰伦 - 晴天')

    def test_sanitize_empty_falls_back(self):
        self.assertEqual(eng.sanitize_filename('   '), 'untitled')
        self.assertEqual(eng.sanitize_filename(''), 'untitled')

    def test_path_with_ext_keeps_dots_in_name(self):
        # 歌名本身含点时不能被当成扩展名切掉
        base = Path('D:/Music/Mr.Chu')
        self.assertEqual(eng._path_with_ext(base, '.flac').name, 'Mr.Chu.flac')

    # ===== 路径穿越防护（歌名/歌手来自不可信音源脚本）=====

    def test_sanitize_never_yields_dot_dot(self):
        for raw in ('..', '.', '...', '  ..  ', ' . . '):
            got = eng.sanitize_filename(raw)
            self.assertNotIn(got, ('.', '..'), f'{raw!r} 派生出目录语义 {got!r}')
            self.assertTrue(got)

    def test_sanitize_collapses_dot_runs(self):
        # 连续点折叠成一个，避免 "..." 被某些系统当成 ".." 处理
        self.assertNotIn('..', eng.sanitize_filename('a..b'))

    def test_sanitize_truncates_long_names(self):
        self.assertLessEqual(len(eng.sanitize_filename('x' * 500)), 120)

    def test_safe_join_rejects_escape(self):
        with tempfile.TemporaryDirectory() as td:
            for bad in ('../../evil.mp3', '..\\..\\evil.mp3', 'sub/../../evil.mp3'):
                with self.assertRaises(RuntimeError):
                    eng.safe_join(td, bad)

    def test_safe_join_accepts_normal(self):
        with tempfile.TemporaryDirectory() as td:
            p = eng.safe_join(td, '周杰伦 - 晴天.mp3')
            self.assertTrue(str(p).startswith(str(Path(td).resolve())))


class SandboxEnvTest(unittest.TestCase):
    """传给 Node 音源进程的环境变量必须是最小集合。

    音源脚本可通过 vm 逃逸读到 process.env，若继承全部变量，
    用户的 API key / 许可证路径会直接暴露给第三方脚本。
    """

    def test_keeps_runtime_essentials(self):
        env = eng.build_sandbox_env()
        upper = {k.upper() for k in env}
        for need in ('SYSTEMROOT', 'APPDATA', 'PATH'):
            self.assertIn(need, upper, f'{need} 必须保留，否则 Node 无法正常启动')

    def test_drops_secrets(self):
        # 构造一个含敏感变量的环境，确认不会被带过去
        fake = {
            'APPDATA': r'C:\Users\x\AppData\Roaming',
            'SYSTEMROOT': r'C:\Windows',
            'PATH': r'C:\Windows\System32',
            'MY_API_TOKEN': 'sk-secret',
            'GITHUB_TOKEN': 'ghp_secret',
            'AWS_SECRET_ACCESS_KEY': 'secret',
            'SOME_LICENSE': r'C:\ProgramData\x\a.lic',
        }
        with mock.patch.dict(os.environ, fake, clear=True):
            env = eng.build_sandbox_env()
        upper = {k.upper() for k in env}
        self.assertIn('APPDATA', upper)
        self.assertNotIn('MY_API_TOKEN', upper)
        self.assertNotIn('GITHUB_TOKEN', upper)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', upper)
        self.assertNotIn('SOME_LICENSE', upper)

    def test_case_insensitive_matching(self):
        # Windows 环境变量名不区分大小写：小写 systemroot 也应被保留。
        # （早期按大小写敏感比较，导致 SYSTEMROOT 被过滤，Node 直接起不来。）
        #
        # 注意：Windows 上 os.environ 本身会把键名归一化成大写，
        # 所以这里用大小写无关的方式断言，不依赖键的字面形式。
        fake = {'systemroot': r'C:\Windows', 'appdata': r'C:\Users\x\AppData\Roaming'}
        with mock.patch.dict(os.environ, fake, clear=True):
            env = eng.build_sandbox_env()
        upper = {k.upper(): v for k, v in env.items()}
        self.assertEqual(len(env), 2, f'小写形式的系统变量也应保留，实际 {env}')
        self.assertEqual(upper['SYSTEMROOT'], r'C:\Windows')

    def test_result_is_plain_dict(self):
        # 返回必须是普通 dict（subprocess 的 env 参数要求），且值都是 str
        env = eng.build_sandbox_env()
        self.assertIsInstance(env, dict)
        for k, v in env.items():
            self.assertIsInstance(k, str)
            self.assertIsInstance(v, str)


class UrlSafetyTest(unittest.TestCase):
    """候选 URL 来自第三方音源脚本，必须限制协议与目标主机。"""

    def test_accepts_public_http(self):
        self.assertTrue(eng.is_safe_http_url('https://music.163.com/a.mp3'))
        self.assertTrue(eng.is_safe_http_url('http://kw-bj.kuwo.cn/x.flac'))

    def test_rejects_non_http_schemes(self):
        # file:// 会让 urlopen 直接读本地文件，等同于给音源脚本任意读文件能力
        for bad in ('file:///C:/Windows/win.ini', 'ftp://a.com/x',
                    'javascript:alert(1)', 'data:audio/mp3;base64,AAA'):
            self.assertFalse(eng.is_safe_http_url(bad), bad)

    def test_rejects_loopback_and_private(self):
        for bad in ('http://127.0.0.1:8080/x', 'http://localhost/x',
                    'http://[::1]/x', 'http://10.0.0.5/x',
                    'http://192.168.1.1/x', 'http://172.16.0.1/x',
                    'http://169.254.1.1/x', 'http://evil.local/x'):
            self.assertFalse(eng.is_safe_http_url(bad), bad)

    def test_rejects_malformed(self):
        for bad in ('', None, 'https://'):
            self.assertFalse(eng.is_safe_http_url(bad), bad)

    def test_download_file_rejects_file_scheme(self):
        # 纵深防御：即使调用方漏检，download_file 自己也要拒绝
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                eng.download_file('file:///C:/Windows/win.ini',
                                  Path(td) / 'x.mp3', retries=0)


class AudioMagicTest(unittest.TestCase):
    def _write(self, data):
        fd, path = tempfile.mkstemp(suffix='.bin')
        os.close(fd)
        with open(path, 'wb') as f:
            f.write(data)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_detect_flac(self):
        self.assertEqual(eng.detect_audio_ext(self._write(b'fLaC' + b'\x00' * 60)), '.flac')

    def test_detect_ogg(self):
        self.assertEqual(eng.detect_audio_ext(self._write(b'OggS' + b'\x00' * 60)), '.ogg')

    def test_detect_wav(self):
        self.assertEqual(eng.detect_audio_ext(self._write(b'RIFF' + b'\x00' * 4 + b'WAVE')), '.wav')

    def test_detect_m4a(self):
        self.assertEqual(eng.detect_audio_ext(self._write(b'\x00\x00\x00\x20ftypM4A ')), '.m4a')

    def test_detect_mp3_id3(self):
        self.assertEqual(eng.detect_audio_ext(self._write(b'ID3\x04\x00\x00' + b'\x00' * 58)), '.mp3')

    def test_detect_mp3_frame_sync(self):
        self.assertEqual(eng.detect_audio_ext(self._write(b'\xff\xfb' + b'\x00' * 62)), '.mp3')

    def test_detect_unknown_falls_back_to_mp3(self):
        self.assertEqual(eng.detect_audio_ext(self._write(b'\x00' * 64)), '.mp3')


class ConfigSelfHealTest(unittest.TestCase):
    """历史脏配置（乱码路径 / 失效临时目录）应被自愈，而不是凭空建出乱码目录。"""

    def test_mojibake_detected(self):
        self.assertTrue(eng._looks_mojibake(r'C:\Users\x\WorkBuddy\钀介洩闊充箰涓嬭浇\lx-downloader'))
        self.assertFalse(eng._looks_mojibake(r'C:\Users\x\WorkBuddy\落雪音乐下载\lx-downloader'))
        self.assertFalse(eng._looks_mojibake(''))
        self.assertFalse(eng._looks_mojibake(None))

    def test_unusable_dir_rejected(self):
        self.assertFalse(eng._dir_is_usable(''))
        self.assertFalse(eng._dir_is_usable(None))
        self.assertFalse(eng._dir_is_usable(r'C:\Users\x\钀介洩闊充箰涓嬭浇\dl'))

    def test_mojibake_dir_is_rejected_even_if_creatable(self):
        """回归：早期实现先 mkdir 再判乱码，乱码目录被成功创建后就被误判为“可用”。

        现在乱码检测必须先于任何文件系统操作，且不得因为“目录能被创建”而放行。
        """
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / '钀介洩闊充箰涓嬭浇' / 'downloads'
            self.assertFalse(eng._dir_is_usable(str(bad)))
            # 判定过程本身不能顺手把乱码目录建出来
            self.assertFalse(bad.exists(), '可用性检查不应创建乱码目录')

    def test_missing_dir_under_existing_parent_is_usable(self):
        # 首次下载允许自动创建（父目录存在）
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(eng._dir_is_usable(str(Path(td) / 'new-downloads')))

    def test_missing_nested_dir_is_created(self):
        with tempfile.TemporaryDirectory() as td:
            nested = Path(td) / 'a' / 'b' / 'c'
            self.assertTrue(eng._dir_is_usable(str(nested)))
            self.assertTrue(nested.is_dir())

    def test_missing_drive_is_rejected(self):
        self.assertFalse(eng._dir_is_usable(r'Q:\definitely-not-a-drive\dl'))

    def test_usable_dir_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(eng._dir_is_usable(td))

    def test_load_config_heals_mojibake_dir(self):
        original = eng.CONFIG_PATH
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / 'cfg.json'
            cfg.write_text(
                '{"dir": "C:\\\\Users\\\\x\\\\钀介洩闊充箰涓嬭浇\\\\dl", "quality": "flac"}',
                encoding='utf-8',
            )
            eng.CONFIG_PATH = cfg
            try:
                loaded = eng.load_config()
                self.assertEqual(loaded['dir'], eng.DOWNLOAD_DIR)
                self.assertEqual(loaded['quality'], 'flac')  # 其他字段保留

                # 自愈结果应写回配置文件。
                # 注意：不要断言路径里包含「落雪」——开发模式与 CI 上的
                # DOWNLOAD_DIR 是 `<repo>/lx-downloader/downloads`，并不含这两个字；
                # 真正该验证的是「写回的目录可用」且「不再是乱码路径」。
                written = json.loads(cfg.read_text(encoding='utf-8'))
                self.assertEqual(written['dir'], eng.DOWNLOAD_DIR)
                self.assertFalse(eng._looks_mojibake(written['dir']))
                self.assertTrue(eng._dir_is_usable(written['dir']))
            finally:
                eng.CONFIG_PATH = original


class StopFileTest(unittest.TestCase):
    """GUI 优雅停止：信号文件出现即置位停止事件。"""

    def test_watch_stop_file_sets_event(self):
        import threading
        import time

        eng.DOWNLOAD_STOP_EVENT.clear()
        with tempfile.TemporaryDirectory() as td:
            flag = Path(td) / 'stop.flag'
            eng.watch_stop_file(flag, interval=0.05)
            self.assertFalse(eng.DOWNLOAD_STOP_EVENT.is_set())
            flag.write_text('stop\n', encoding='utf-8')
            for _ in range(60):
                if eng.DOWNLOAD_STOP_EVENT.is_set():
                    break
                time.sleep(0.05)
            self.assertTrue(eng.DOWNLOAD_STOP_EVENT.is_set())
            # 信号文件被消费掉，避免下一次启动误判
            self.assertFalse(flag.exists())
        eng.DOWNLOAD_STOP_EVENT.clear()
        _ = threading  # 保持导入显式


class SongFileTest(unittest.TestCase):
    """GUI 单曲下载：--song-file 的解析。"""

    def _write(self, payload):
        import json
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_single_song_object(self):
        p = self._write({'name': '晴天', 'singer': '周杰伦', 'source': 'kw', 'id': '123'})
        songs = eng.load_song_file(p)
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]['name'], '晴天')
        self.assertEqual(songs[0]['singer'], '周杰伦')
        self.assertEqual(songs[0]['source'], 'kw')
        meta = json.loads(songs[0]['meta'])
        self.assertEqual(meta['id'], '123')
        self.assertEqual(meta['songId'], '123')

    def test_songs_array(self):
        p = self._write({'songs': [
            {'name': 'A', 'singer': 'X', 'source': 'wy', 'id': '1'},
            {'name': 'B', 'singer': 'Y', 'source': 'tx', 'id': '2'},
        ]})
        songs = eng.load_song_file(p)
        self.assertEqual([s['name'] for s in songs], ['A', 'B'])

    def test_bare_list(self):
        p = self._write([{'name': 'A', 'singer': 'X', 'source': 'kw', 'id': '1'}])
        self.assertEqual(len(eng.load_song_file(p)), 1)

    def test_skips_entries_without_name(self):
        p = self._write({'songs': [
            {'name': '', 'singer': 'X'},
            {'name': 'B', 'singer': 'Y', 'source': 'kw', 'id': '2'},
        ]})
        songs = eng.load_song_file(p)
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]['name'], 'B')

    def test_tx_gets_songmid(self):
        p = self._write({'name': 'A', 'singer': 'X', 'source': 'tx', 'id': 'abc'})
        meta = json.loads(eng.load_song_file(p)[0]['meta'])
        self.assertEqual(meta['songmid'], 'abc')

    def test_interval_preserved(self):
        p = self._write({'name': 'A', 'singer': 'X', 'source': 'kw', 'id': '1', 'interval': 269})
        self.assertEqual(eng.load_song_file(p)[0]['interval'], 269)

    def test_invalid_json_raises(self):
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('not json')
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with self.assertRaises(Exception):
            eng.load_song_file(path)


class ProbeStatusLogicTest(unittest.TestCase):
    """音源体检的状态判定逻辑。

    这段逻辑在 lx_url_server.js 里（JS），Python 测试无法直接调用，
    所以这里用等价实现做「规则回归」：确保规则本身没被改错。

    历史 bug：原判定用 `okCount === samples.length`，但 samples 只装成功样本，
    该条件恒为真，会把「3 个样本只成功 1 个」误判成 ok。
    """

    @staticmethod
    def judge(ok_count, total):
        """与 lx_url_server.js 中 probeSources 的判定保持一致。"""
        if ok_count > 0 and ok_count == total:
            return 'ok'
        if ok_count > 0:
            return 'flaky'
        return 'dead'

    def test_all_success_is_ok(self):
        self.assertEqual(self.judge(3, 3), 'ok')

    def test_partial_success_is_flaky(self):
        # 这是历史 bug 的核心场景：部分成功必须判 flaky，不能判 ok
        for ok_n, total in ((1, 3), (2, 3), (1, 2), (2, 5)):
            self.assertEqual(self.judge(ok_n, total), 'flaky', f'{ok_n}/{total} 应为 flaky')

    def test_none_success_is_dead(self):
        for total in (1, 2, 3):
            self.assertEqual(self.judge(0, total), 'dead', f'0/{total} 应为 dead')

    def test_never_ok_unless_all_passed(self):
        # 不变量：只有 100% 成功才是 ok
        for total in range(1, 6):
            for ok_n in range(total):
                self.assertNotEqual(self.judge(ok_n, total), 'ok',
                                    f'{ok_n}/{total} 不应判为 ok')


class SearchApiTest(unittest.TestCase):
    """搜索/列歌相关的纯逻辑（不联网）。"""

    def test_url_server_has_search_songs(self):
        self.assertTrue(hasattr(eng.UrlServer, 'search_songs'))

    def test_list_songs_flag_requires_playlist(self):
        # 仅确认参数存在，实际执行需要数据库
        import inspect
        src = inspect.getsource(eng.run)
        self.assertIn('--list-songs', src)
        self.assertIn('--search', src)
        self.assertIn('--song-file', src)

    def test_probe_and_update_flags_exist(self):
        """新增的 --probe-sources / --check-update 必须注册（GUI 依赖它们）。"""
        import inspect
        src = inspect.getsource(eng.run)
        self.assertIn('--probe-sources', src)
        self.assertIn('--check-update', src)

    def test_probe_samples_are_complete(self):
        """探测样本必须字段齐全，否则体检会把好音源误判为失活。"""
        self.assertGreaterEqual(len(eng._PROBE_SAMPLES), 2)
        for sample in eng._PROBE_SAMPLES:
            self.assertEqual(len(sample), 4, f'样本字段不完整: {sample}')
            src, name, singer, song_id = sample
            self.assertIn(src, ('kw', 'wy', 'tx', 'kg', 'mg'))
            self.assertTrue(name and singer and song_id)

    def test_probe_samples_cover_multiple_platforms(self):
        """样本要跨平台，否则「只支持某平台」的音源会被误判为失活。"""
        platforms = {s[0] for s in eng._PROBE_SAMPLES}
        self.assertGreaterEqual(len(platforms), 2, f'样本平台过于单一: {platforms}')


class StrictMatchTest(unittest.TestCase):
    """严格匹配：歌名必须完全相同，绝不把「出山」和「出山 (DJ版)」当同一首。"""

    def test_identical_names_match(self):
        self.assertTrue(eng.strict_song_key_match('晴天', '晴天'))
        self.assertTrue(eng.strict_song_key_match('出山', '出山'))

    def test_version_suffix_does_not_match(self):
        # 核心回归：真实库 773 首里有 35 组这样的同键冲突
        self.assertFalse(eng.strict_song_key_match('出山', '出山 (DJ版)'))
        self.assertFalse(
            eng.strict_song_key_match('高阶萌妹成长指南', '高阶萌妹成长指南 (消音伴奏)')
        )
        self.assertFalse(
            eng.strict_song_key_match('萌妹进行曲', '萌妹进行曲（高阶萌妹成长指南）')
        )

    def test_whitespace_and_case_normalized(self):
        self.assertTrue(eng.strict_song_key_match('  晴天  ', '晴天'))
        self.assertTrue(eng.strict_song_key_match('Hello  World', 'hello world'))
        self.assertTrue(eng.strict_song_key_match('ＳＵＮ', 'SUN'))  # 全角 → 半角

    def test_brackets_content_is_significant(self):
        self.assertFalse(eng.strict_song_key_match('歌 (Live)', '歌 (Remix)'))
        self.assertTrue(eng.strict_song_key_match('歌 (Live)', '歌 （Live）'))

    def test_empty_never_matches(self):
        self.assertFalse(eng.strict_song_key_match('', '晴天'))
        self.assertFalse(eng.strict_song_key_match('晴天', ''))
        self.assertFalse(eng.strict_song_key_match('', ''))

    def test_loose_key_still_strips_brackets(self):
        # 宽松版本保留给「已下载文件扫描」，行为不变
        self.assertEqual(eng.norm_song_key('歌 (Live)'), '歌')
        self.assertEqual(eng.norm_song_key('歌 （Live）'), '歌')

    def test_strict_key_handles_none(self):
        self.assertEqual(eng.strict_song_key(None), '')


class FailedListTest(unittest.TestCase):
    """failed.txt 的 v2 格式与 v1 向后兼容。

    重点：旧格式是「歌手 - 歌名: 原因」，本身有歧义（歌名/原因里都可能有冒号），
    只要求「尽力解析」，绝不要求 100% 还原；新格式必须完全可逆。
    """

    def test_parse_v2_jsonl(self):
        text = '\n'.join([
            eng.FAILED_HEADER_V2,
            '# 2026-10-18 15:30',
            json.dumps({'name': '晴天', 'singer': '周杰伦', 'source': 'kw',
                        'interval': '04:29', 'error': '音质不达标'}, ensure_ascii=False),
            json.dumps({'name': '出山', 'singer': '花粥', 'source': 'wy',
                        'interval': '03:20', 'error': '按名搜索无候选'}, ensure_ascii=False),
        ])
        songs = eng.parse_failed_txt(text)
        self.assertEqual(len(songs), 2)
        self.assertEqual(songs[0]['name'], '晴天')
        self.assertEqual(songs[0]['singer'], '周杰伦')
        self.assertEqual(songs[0]['source'], 'kw')
        self.assertEqual(songs[0]['interval'], '04:29')
        self.assertEqual(songs[1]['name'], '出山')

    # ===== v1 切分边界（真实错误串里几乎每行都含多个 ": "）=====

    def test_v1_splits_on_error_prefix_not_first_colon(self):
        # 引擎的错误来自 f'{source}: {异常}'，若按第一个 ": " 切，
        # 会把 "kw: flac: ..." 整段错误吃进歌名，重试时按名搜索必然失败。
        r = eng._parse_v1_line('周杰伦 - 晴天: kw: flac: 时长校验失败(预期04:29)')
        self.assertEqual(r['singer'], '周杰伦')
        self.assertEqual(r['name'], '晴天')
        self.assertIn('时长校验失败', r['error'])

    def test_v1_handles_semicolon_joined_errors(self):
        r = eng._parse_v1_line(
            '郑智化 - 水手: kw: flac: socket hang up; 320k: 音质不达标(低于 flac)')
        self.assertEqual(r['singer'], '郑智化')
        self.assertEqual(r['name'], '水手')

    def test_v1_handles_search_error_prefix(self):
        r = eng._parse_v1_line('周杰伦 - 晴天: 按名搜索: 按名搜索失败: 周杰伦 - 晴天')
        self.assertEqual(r['singer'], '周杰伦')
        self.assertEqual(r['name'], '晴天')

    def test_v1_name_without_singer(self):
        r = eng._parse_v1_line('只有歌名')
        self.assertEqual(r['singer'], '')
        self.assertEqual(r['name'], '只有歌名')

    def test_v1_no_error_part(self):
        r = eng._parse_v1_line('Unknown - Song')
        self.assertEqual(r['singer'], 'Unknown')
        self.assertEqual(r['name'], 'Song')
        self.assertEqual(r['error'], '')

    def test_v1_never_puts_error_text_into_name(self):
        # 核心不变量：歌名里绝不能出现错误文案
        bad_tokens = ('socket hang up', '音质不达标', '时长校验失败', '按名搜索')
        for line in (
            '周杰伦 - 晴天: kw: flac: 时长校验失败(预期04:29)',
            'A - B: 音质不达标(低于 flac)',
            'A - B: 按名搜索: 按名搜索失败: A - B',
        ):
            r = eng._parse_v1_line(line)
            for tok in bad_tokens:
                self.assertNotIn(tok, r['name'], f'{line} -> name={r["name"]!r}')

    def test_parse_v1_legacy(self):
        text = '\n'.join([
            '',
            '--- 2026-10-18 15:30 ---',
            '周杰伦 - 晴天: 音质不达标(低于 320k)',
            '花粥 - 出山: 按名搜索无候选',
        ])
        songs = eng.parse_failed_txt(text)
        self.assertEqual([s['name'] for s in songs], ['晴天', '出山'])
        self.assertEqual([s['singer'] for s in songs], ['周杰伦', '花粥'])
        self.assertEqual(songs[0]['error'], '音质不达标(低于 320k)')

    def test_v1_name_with_colon_is_best_effort(self):
        # v1 有歧义，只要求「尽量拿到正确的歌手 + 一个非空歌名」，不要求完美还原。
        # 这里取第一个 ': ' 切开：错误原因里几乎必有 ': '，歌名里有则很少。
        songs = eng.parse_failed_txt('A - 歌: 副标题: 失败原因: 细节')
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]['singer'], 'A')
        self.assertEqual(songs[0]['name'], '歌')
        self.assertEqual(songs[0]['error'], '副标题: 失败原因: 细节')

    def test_v1_real_engine_error_text(self):
        # 引擎真实写出的错误串形如 "kw: 音源未返回任何链接; 按名搜索: 无候选"，
        # 必须整体留在 error 里，不能污染歌名（否则重试按名搜索必然失败）
        songs = eng.parse_failed_txt('周杰伦 - 晴天: kw: 音源未返回任何链接; 按名搜索: 无候选')
        self.assertEqual(songs[0]['name'], '晴天')
        self.assertEqual(songs[0]['singer'], '周杰伦')
        self.assertEqual(songs[0]['error'], 'kw: 音源未返回任何链接; 按名搜索: 无候选')

    def test_mixed_v1_and_v2(self):
        text = '\n'.join([
            '--- 2026-10-18 15:30 ---',
            '周杰伦 - 晴天: 音质不达标',
            eng.FAILED_HEADER_V2,
            json.dumps({'name': '出山', 'singer': '花粥', 'source': 'wy'}, ensure_ascii=False),
        ])
        songs = eng.parse_failed_txt(text)
        self.assertEqual([s['name'] for s in songs], ['晴天', '出山'])
        # v1 没有 source/interval，应给空串而不是缺字段
        self.assertEqual(songs[0]['source'], '')
        self.assertEqual(songs[0]['interval'], '')

    def test_duplicate_songs_are_merged(self):
        # 同一首歌失败多次（不同批次）只应出现一次，否则会重复下载
        text = '\n'.join([
            json.dumps({'name': '晴天', 'singer': '周杰伦'}, ensure_ascii=False),
            json.dumps({'name': '晴天 ', 'singer': '周杰伦'}, ensure_ascii=False),
        ])
        self.assertEqual(len(eng.parse_failed_txt(text)), 1)

    def test_garbage_lines_are_skipped(self):
        text = '\n'.join([
            '这不是任何已知格式',
            json.dumps({'singer': '没有歌名'}, ensure_ascii=False),
            json.dumps({'name': '有效', 'singer': 'X'}, ensure_ascii=False),
        ])
        songs = eng.parse_failed_txt(text)
        # 「这不是任何已知格式」会被 v1 兜底成无歌手的歌名，这是可接受的尽力解析；
        # 但没有 name 字段的 JSON 必须被丢弃
        self.assertNotIn('没有歌名', [s['name'] for s in songs])
        self.assertIn('有效', [s['name'] for s in songs])

    def test_empty_input(self):
        self.assertEqual(eng.parse_failed_txt(''), [])
        self.assertEqual(eng.parse_failed_txt(None), [])

    def test_write_and_reload_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            items = [{
                'name': '晴天', 'singer': '周杰伦', 'source': 'kw',
                'interval': '04:29', 'error': '音质不达标(低于 320k)',
            }]
            eng.write_failed_file(td, items)
            path = Path(td) / eng.FAILED_FILE_NAME
            self.assertTrue(path.is_file())
            raw = path.read_text(encoding='utf-8')
            self.assertIn(eng.FAILED_HEADER_V2, raw)
            self.assertEqual(eng.load_failed_songs(td), items)

    def test_write_empty_removes_file(self):
        with tempfile.TemporaryDirectory() as td:
            eng.write_failed_file(td, [{'name': 'A', 'singer': 'B'}])
            path = Path(td) / eng.FAILED_FILE_NAME
            self.assertTrue(path.is_file())
            # 全部成功时必须删掉文件，否则 GUI 会一直显示「重试」按钮
            eng.write_failed_file(td, [])
            self.assertFalse(path.exists())

    def test_load_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(eng.load_failed_songs(td), [])

    def test_failed_to_song_shape(self):
        song = eng.failed_to_song({
            'name': '晴天', 'singer': '周杰伦', 'source': 'kw',
            'interval': '04:29', 'error': 'x',
        })
        # 必须与 run_download 期望的字段完全一致，否则 process_song 会 KeyError
        for key in ('id', 'name', 'singer', 'source', 'interval', 'meta'):
            self.assertIn(key, song)
        self.assertEqual(song['name'], '晴天')
        self.assertEqual(json.loads(song['meta']), {})

    def test_failed_to_song_carries_id_and_meta(self):
        """新版清单会存 id/meta，重试时可直接命中原平台记录。"""
        song = eng.failed_to_song({
            'name': '晴天', 'singer': '周杰伦', 'source': 'kw', 'interval': '04:29',
            'error': '加密格式', 'id': 'kw_186016', 'meta': '{"songId":"186016"}',
        })
        self.assertEqual(song['id'], 'kw_186016')
        self.assertEqual(json.loads(song['meta']), {'songId': '186016'})

    def test_write_reload_keeps_id_and_meta(self):
        with tempfile.TemporaryDirectory() as td:
            eng.write_failed_file(td, [{
                'name': '晴天', 'singer': '周杰伦', 'source': 'kw', 'interval': '04:29',
                'error': '加密格式', 'id': 'kw_186016', 'meta': '{"songId":"186016"}',
            }])
            items = eng.load_failed_songs(td)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], 'kw_186016')
        self.assertEqual(json.loads(items[0]['meta']), {'songId': '186016'})

    def test_old_list_without_id_still_works(self):
        """老清单没有 id/meta，必须保持兼容（退化为按名搜索）。"""
        with tempfile.TemporaryDirectory() as td:
            Path(td, eng.FAILED_FILE_NAME).write_text(
                eng.FAILED_HEADER_V2 + '\n'
                + '{"name":"晴天","singer":"周杰伦","source":"kw","interval":"04:29","error":"x"}\n',
                encoding='utf-8')
            items = eng.load_failed_songs(td)
        self.assertEqual(len(items), 1)
        self.assertNotIn('id', items[0])
        song = eng.failed_to_song(items[0])
        self.assertEqual(song['id'], '')
        self.assertEqual(json.loads(song['meta']), {})


class EncryptedContainerTest(unittest.TestCase):
    """平台加密容器（酷我 .mgg/.mflac、QQ .qmc*）必须被识别出来。

    这类文件头是自定义加密容器，mutagen 解析不出时长与码率，
    四重校验必然失败。此前不识别会让每首歌白下载几十 MB 后
    才以「时长校验失败 / 音质不达标」这种误导性原因失败。
    """

    def test_url_suffix_detected(self):
        cases = [
            'http://car-er.kuwo.cn/x/resource/30106/trackmedia/Q0M3004Loxf.mgg',
            'http://a.b/c/abc.mflac',
            'http://a.b/c/abc.mflac0',
            'http://a.b/c/abc.mgg1',
            'http://a.b/c/abc.mgg?token=abc',
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertTrue(eng.is_encrypted_audio_url(url))

    def test_query_string_suffix_not_flagged(self):
        """只按 URL **路径**判断：查询参数里出现 .mgg 不代表资源本身加密。"""
        self.assertFalse(eng.is_encrypted_audio_url('https://a.b/x.flac?fname=y.mgg'))

    def test_plain_audio_url_not_flagged(self):
        cases = [
            'http://a.b/c/abc.mp3',
            'http://a.b/c/F000001ziKgJ3o5Ipp.flac',
            'https://music.163.com/song/media/outer/url?id=1.mp3',
            'http://a.b/c/abc.m4a',
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertFalse(eng.is_encrypted_audio_url(url))

    def test_empty_url_not_flagged(self):
        self.assertFalse(eng.is_encrypted_audio_url(''))
        self.assertFalse(eng.is_encrypted_audio_url(None))

    def test_file_magic_detected(self):
        fd, path = tempfile.mkstemp(suffix='.part')
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        # 实测酷我 .mgg 容器头：0x29 0x01 + "Uap1P4q75f72"
        with open(path, 'wb') as f:
            f.write(b')\x01Uap1P4q75f72' + b'\x00' * 20)
        self.assertTrue(eng.is_encrypted_audio_file(path))

    def test_normal_audio_file_not_flagged(self):
        for head in (b'ID3\x04\x00\x00', b'fLaC\x00\x00\x00\x22',
                     b'OggS', b'\xff\xfb\x90\x00', b'RIFF\x00\x00\x00\x00WAVE'):
            fd, path = tempfile.mkstemp(suffix='.part')
            os.close(fd)
            self.addCleanup(lambda p=path: os.path.exists(p) and os.unlink(p))
            with open(path, 'wb') as f:
                f.write(head + b'\x00' * 40)
            with self.subTest(head=head):
                self.assertFalse(eng.is_encrypted_audio_file(path))

    def test_encrypted_candidate_is_skipped_before_download(self):
        """下载前的后缀判断要生效：加密候选不该触发任何网络请求。"""
        with mock.patch.object(eng, 'download_file') as dl:
            with self.assertRaises(RuntimeError) as ctx:
                eng.download_with_candidates(
                    [{'url': 'http://a.b/c/abc.mgg', 'quality': '320k'}],
                    Path(tempfile.gettempdir()) / '_never_written',
                    requested_quality='320k')
        dl.assert_not_called()
        self.assertIn('加密格式', str(ctx.exception))


class AlternativeSourceTest(unittest.TestCase):
    """替代源查询：换平台 + 同名，两个条件必须能同时成立。

    曾经写成 `WHERE source != ? AND name != ?`，而 Python 侧又要求
    name 完全等于当前歌名——两个条件互斥，替代源永远返回空。
    """

    def _make_db(self):
        import sqlite3
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE my_list_music_info (
            id TEXT, name TEXT, singer TEXT, source TEXT, interval TEXT, meta TEXT)""")
        conn.executemany(
            "INSERT INTO my_list_music_info VALUES (?,?,?,?,?,?)",
            [
                ('kw_6739985', '私の言葉', '久保ユリカ', 'kw', '03:38', '{"songId":"6739985"}'),
                ('wy_30070747', '私の言葉', '久保ユリカ', 'wy', '03:38', '{"songId":30070747}'),
                ('kw_1', '晴天', '周杰伦', 'kw', '04:29', '{}'),
                ('wy_2', '晴天 (Live)', '周杰伦', 'wy', '04:30', '{}'),
            ])
        conn.commit()
        conn.close()
        return path

    def test_returns_same_name_from_other_platform(self):
        db = self._make_db()
        rows = eng.get_alternative_sources(db, {
            'name': '私の言葉', 'singer': '久保ユリカ', 'source': 'kw', 'id': 'kw_6739985'})
        self.assertEqual([r['source'] for r in rows], ['wy'])

    def test_does_not_return_current_source(self):
        db = self._make_db()
        rows = eng.get_alternative_sources(db, {
            'name': '私の言葉', 'singer': '久保ユリカ', 'source': 'kw', 'id': 'kw_6739985'})
        self.assertNotIn('kw', [r['source'] for r in rows])

    def test_version_suffix_is_not_alternative(self):
        """「晴天 (Live)」不是「晴天」的替代源——名字必须严格一致。"""
        db = self._make_db()
        rows = eng.get_alternative_sources(db, {
            'name': '晴天', 'singer': '周杰伦', 'source': 'kw', 'id': 'kw_1'})
        self.assertEqual(rows, [])

    def test_no_database_returns_empty(self):
        """单曲下载没有数据库上下文，不能崩。"""
        self.assertEqual(eng.get_alternative_sources(
            str(Path(tempfile.gettempdir()) / 'nope.db'),
            {'name': 'x', 'singer': 'y', 'source': 'kw'}), [])


class ReuseAcrossQualitiesTest(unittest.TestCase):
    """降级序列里同一个候选不该被重复下载。

    flac 档位把 320k 文件判「音质不达标」后，降到 320k 档位时如果整首重下，
    等于同一份内容下两遍。reuse 缓存让内容校验通过的文件跨档位复用。
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name)
        # 造两个真实可解析的 MP3：320k 与 128k，避免依赖网络。
        self.audio = self.dir / 'src320.mp3'
        self._make_mp3(self.audio, bitrate_kbps=320)
        self.low = self.dir / 'src128.mp3'
        self._make_mp3(self.low, bitrate_kbps=128)

    # MPEG1 Layer3 帧头第三个字节的码率索引（高 4 位）
    _BITRATE_IDX = {320: 0xE0, 192: 0xA0, 160: 0x80, 128: 0x90, 64: 0x50}

    def _make_mp3(self, path, seconds=4, bitrate_kbps=320):
        make_mp3(path, seconds=seconds, bitrate_kbps=bitrate_kbps)

    def test_quality_rejection_keeps_file_for_lower_tier(self):
        """高档位因音质被拒时，文件要留在 reuse 里供低档位复用。"""
        reuse = {}
        with mock.patch.object(eng, 'download_file') as dl:
            def fake_download(url, dest, **kw):
                import shutil
                shutil.copyfile(self.low, dest)   # 128k
            dl.side_effect = fake_download
            dest_base = self.dir / 'song'
            with self.assertRaises(RuntimeError) as ctx:
                eng.download_with_candidates(
                    [{'url': 'https://a.b/c.mp3', 'quality': '320k'}],
                    dest_base, expected_interval='00:04', expected_singer='',
                    requested_quality='320k', reuse=reuse)
            self.assertIn('音质不达标', str(ctx.exception))
        self.assertEqual(dl.call_count, 1)
        self.assertEqual(len(reuse), 1, '音质不达标时应保留文件供降级复用')
        kept = Path(next(iter(reuse.values())))
        self.assertTrue(kept.exists(), f'复用文件应真实存在: {kept}')
        # 不该把最终文件写出来（.reuse 缓存文件是预期存在的临时产物）
        self.assertFalse([p for p in self.dir.glob('song.*') if not p.name.endswith('.reuse')])

    def test_lower_tier_reuses_without_second_download(self):
        reuse = {}
        calls = []

        def fake_download(url, dest, **kw):
            import shutil
            calls.append(url)
            shutil.copyfile(self.low, dest)   # 128k：320k 档位会被拒

        with mock.patch.object(eng, 'download_file', side_effect=fake_download):
            with self.assertRaises(RuntimeError):
                eng.download_with_candidates(
                    [{'url': 'https://a.b/c.mp3', 'quality': '320k'}],
                    self.dir / 's1', expected_interval='00:04', expected_singer='',
                    requested_quality='320k', reuse=reuse)
            # 降到 128k 档位：应复用，不再下载
            quality, saved = eng.download_with_candidates(
                [{'url': 'https://a.b/c.mp3', 'quality': '320k'}],
                self.dir / 's1', expected_interval='00:04', expected_singer='',
                requested_quality='128k', reuse=reuse)
        self.assertEqual(calls, ['https://a.b/c.mp3'], '第二次不应再下载')
        # 返回的是**实测**音质（文件确实是 128k），不是候选自报的 320k
        self.assertEqual(quality, '128k')
        self.assertTrue(Path(saved).exists())
        self.assertEqual(reuse, {}, '成功后应清空复用缓存')

    def test_bad_content_drops_reuse_entry(self):
        """内容不合格（时长不符）时，不能把它留在缓存里反复失败。"""
        bad = self.dir / 'bad.mp3'
        self._make_mp3(bad, seconds=1)
        reuse = {'https://a.b/bad.mp3': str(bad)}
        with self.assertRaises(RuntimeError):
            eng.download_with_candidates(
                [{'url': 'https://a.b/bad.mp3', 'quality': '320k'}],
                self.dir / 's2', expected_interval='05:00', expected_singer='',
                requested_quality='320k', reuse=reuse)
        self.assertEqual(reuse, {}, '坏候选必须从缓存剔除')


class ActualQualityTest(unittest.TestCase):
    """日志里的音质标签必须是实测的，不能照抄音源自报值。

    实测样本：音源把 16bit/44.1kHz 的普通 FLAC 报成 flac24bit，
    直接把自报值打到日志里会误导用户以为下到了 Hi-Res。
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name)

    def test_320k_mp3(self):
        p = self.dir / 'a.mp3'
        make_mp3(p, bitrate_kbps=320)
        self.assertEqual(eng.detect_actual_quality(p), '320k')

    def test_128k_mp3(self):
        p = self.dir / 'b.mp3'
        make_mp3(p, bitrate_kbps=128)
        self.assertEqual(eng.detect_actual_quality(p), '128k')

    def test_unparsable_file_returns_empty(self):
        p = self.dir / 'c.bin'
        p.write_bytes(b'\x00' * 128)
        self.assertEqual(eng.detect_actual_quality(p), '')

    def test_missing_file_returns_empty(self):
        self.assertEqual(eng.detect_actual_quality(self.dir / 'nope.mp3'), '')


class QualityHashTest(unittest.TestCase):
    """酷狗等平台每个音质档有各自的 hash，必须按请求档位挑选。

    曾经的 bug：固定按 320k→128k→flac 取第一个 hash，请求 320k/flac
    时仍把 128k 的 hash 传给音源，音源返回 128k 后被判「音质不达标」，
    表现成「这个源什么音质都下不了」。
    """

    def _row(self, **over):
        row = {
            'name': '水手', 'singer': '郑智化', 'source': 'kg', 'interval': '04:52',
            'meta': json.dumps({
                'songId': 338293,
                # 真实数据里顶层 hash 固定是 128k 那个，必须被档位专属值覆盖
                'hash': 'TOPHASH128',
                '_qualitys': {
                    '128k': {'hash': 'HASH128'},
                    '320k': {'hash': 'HASH320'},
                    'flac': {'hash': 'HASHFLAC'},
                },
            }),
        }
        row.update(over)
        return row

    def test_picks_hash_for_requested_quality(self):
        self.assertEqual(eng.get_music_info(self._row(), '320k')['hash'], 'HASH320')
        self.assertEqual(eng.get_music_info(self._row(), 'flac')['hash'], 'HASHFLAC')
        self.assertEqual(eng.get_music_info(self._row(), '128k')['hash'], 'HASH128')

    def test_top_level_hash_is_overridden(self):
        """顶层 hash 是 128k 的，必须被请求档位的 hash 覆盖。

        这是「kg 源什么音质都下不了」的根因：之前只在 hash 缺失时才补，
        顶层那个 128k hash 一直生效，请求 flac 也返回 128k。
        """
        info = eng.get_music_info(self._row(), 'flac')
        self.assertEqual(info['hash'], 'HASHFLAC')
        self.assertNotEqual(info['hash'], 'TOPHASH128')

    def test_falls_back_to_higher_then_lower(self):
        """请求 flac 但只声明了 320k 时，用 320k 的 hash（而不是 128k）。"""
        row = self._row(meta=json.dumps({
            'songId': 1,
            '_qualitys': {'128k': {'hash': 'H128'}, '320k': {'hash': 'H320'}},
        }))
        self.assertEqual(eng.get_music_info(row, 'flac')['hash'], 'H320')
        self.assertEqual(eng.get_music_info(row, 'flac24bit')['hash'], 'H320')

    def test_without_quality_keeps_existing_hash(self):
        """不传 quality 时保持旧行为（用 meta 里的 hash），供 scan_sources 等外部脚本使用。"""
        self.assertEqual(eng.get_music_info(self._row())['hash'], 'TOPHASH128')

    def test_array_qualitys_supported(self):
        row = self._row(meta=json.dumps({
            'songId': 2,
            'qualitys': [
                {'type': '128k', 'hash': 'A128'},
                {'type': '320k', 'hash': 'A320'},
                {'type': 'flac', 'hash': 'AFLAC'},
            ],
        }))
        self.assertEqual(eng.get_music_info(row, 'flac')['hash'], 'AFLAC')
        self.assertEqual(eng.get_music_info(row, '320k')['hash'], 'A320')


class DirIndexTest(unittest.TestCase):
    """已下载判断走目录索引，不能每首歌都扫一遍目录。

    实测（500 首 / 500 文件）：逐首扫描 31.8s，建索引后 0.12s。
    这里主要保证语义与刷新时机正确，不测绝对耗时。
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name)
        eng.reset_dir_index()
        self.addCleanup(eng.reset_dir_index)

    def _touch(self, name, ext='.flac'):
        p = self.dir / f'{name}{ext}'
        p.write_bytes(b'x' * 2048)
        return p

    def test_finds_existing_song(self):
        self._touch('周杰伦 - 晴天')
        self.assertIsNotNone(eng.song_exists_in_dir(self.dir, '周杰伦', '晴天'))

    def test_missing_song_returns_none(self):
        self._touch('周杰伦 - 晴天')
        self.assertIsNone(eng.song_exists_in_dir(self.dir, '周杰伦', '七里香'))

    def test_version_suffix_is_not_a_match(self):
        """「晴天 (Live)」不能被当成「晴天」——否则用户永远拿不到原版。"""
        self._touch('周杰伦 - 晴天 (Live)')
        self.assertIsNone(eng.song_exists_in_dir(self.dir, '周杰伦', '晴天'))

    def test_any_supported_extension_matches(self):
        for ext in ('.mp3', '.m4a', '.flac', '.aac', '.wav', '.ogg', '.ape'):
            with self.subTest(ext=ext):
                eng.reset_dir_index()
                d = Path(tempfile.mkdtemp())
                self.addCleanup(lambda p=d: shutil.rmtree(p, ignore_errors=True))
                (d / f'歌手 - 歌名{ext}').write_bytes(b'x' * 2048)
                self.assertIsNotNone(eng.song_exists_in_dir(d, '歌手', '歌名'))

    def test_non_audio_file_ignored(self):
        self._touch('周杰伦 - 晴天', ext='.txt')
        self.assertIsNone(eng.song_exists_in_dir(self.dir, '周杰伦', '晴天'))

    def test_external_addition_is_detected(self):
        """索引是快照，但目录变化（用户手动放文件进来）必须能发现。"""
        self._touch('周杰伦 - 晴天')
        self.assertIsNone(eng.song_exists_in_dir(self.dir, '其他', '歌'))
        time.sleep(0.02)   # 确保目录 mtime 变化可辨
        self._touch('其他 - 歌')
        self.assertIsNotNone(eng.song_exists_in_dir(self.dir, '其他', '歌'))

    def test_remember_downloaded_is_visible_immediately(self):
        """索引已建好后，登记的文件要立刻可见（不依赖目录 mtime 变化）。"""
        self._touch('占位 - 文件')
        eng.song_exists_in_dir(self.dir, '占位', '文件')   # 建索引并缓存签名
        p = self._touch('新歌 - 标题')
        # 目录 mtime 变了，正常情况下下次查询也会重建索引；
        # 这里直接登记，验证索引路径本身可用（不依赖重建）。
        eng.remember_downloaded(self.dir, p)
        self.assertIsNotNone(eng.song_exists_in_dir(self.dir, '新歌', '标题'))

    def test_accepts_str_and_path_alike(self):
        self._touch('歌手 - 歌名')
        self.assertIsNotNone(eng.song_exists_in_dir(str(self.dir), '歌手', '歌名'))
        self.assertIsNotNone(eng.song_exists_in_dir(self.dir, '歌手', '歌名'))


class ProbeMergeTest(unittest.TestCase):
    """总大小与测速合并成一次请求；探测读到的头部不再重复下载。

    原来分两次探测（bytes=0-0 取大小、bytes=0-256K 测速），
    每首歌多一次 DNS/TLS/首包往返（实测 139ms → 67ms）。
    探测读到的 256KB 若在正式下载时重下，等于白传。
    """

    def _serve(self, body, range_ok=True):
        """起一个本地 HTTP 服务，返回 (url, shutdown)。这里不联网。"""
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                rng = self.headers.get('Range')
                data = body
                status = 200
                if rng and range_ok:
                    m = re.match(r'bytes=(\d+)-(\d*)', rng)
                    if m:
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else len(body) - 1
                        end = min(end, len(body) - 1)
                        data = body[start:end + 1]
                        status = 206
                self.send_response(status)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Content-Length', str(len(data)))
                if status == 206:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{len(body)}')
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.shutdown)
        return f'http://127.0.0.1:{srv.server_port}/f.mp3'

    def test_probe_returns_size_speed_and_head(self):
        body = bytes(range(256)) * 4000   # ~1MB
        url = self._serve(body)
        size, speed, head = eng._probe_size_and_speed(
            url, {'User-Agent': 't'}, 10, sample_bytes=65536)
        self.assertEqual(size, len(body))
        self.assertIsNotNone(speed)
        self.assertEqual(len(head), 65536)
        self.assertEqual(head, body[:65536])

    def test_download_with_preloaded_matches_full_download(self):
        """带 preloaded 的下载结果必须与一次性完整下载字节相同。"""
        body = bytes(range(256)) * 4000
        url = self._serve(body)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'a.part'
            # 测试用本地回环地址，而 is_safe_http_url 正确地拒绝本机/内网
            # （SSRF 防护，另有测试覆盖）。这里只放行这一项校验。
            with mock.patch.object(eng, 'is_safe_http_url', return_value=True):
                eng.download_file(url, p, retries=0)
            self.assertEqual(p.read_bytes(), body)

    def test_falls_back_when_server_ignores_range(self):
        """服务器忽略 Range（回 200）时不能把 preloaded 拼进去，否则文件损坏。"""
        body = bytes(range(256)) * 4000
        url = self._serve(body, range_ok=False)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'b.part'
            with mock.patch.object(eng, 'is_safe_http_url', return_value=True):
                eng.download_file(url, p, retries=0)
            self.assertEqual(p.read_bytes(), body,
                             '忽略 Range 的服务器必须回退为完整下载')

    def test_probe_tolerates_html_error_page(self):
        """返回 HTML 错误页时不能当音频测速（speed 为 None）。"""
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                payload = b'<html>error</html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        url = f'http://127.0.0.1:{srv.server_port}/x'
        size, speed, head = eng._probe_size_and_speed(url, {'User-Agent': 't'}, 10)
        self.assertIsNone(speed)


class TruncatedDownloadTest(unittest.TestCase):
    """CDN 中途断流必须被发现，不能落一个「播放到一半就断」的残损文件。

    resp.read() 在连接提前关闭时只是返回空，循环正常结束、不报错。
    若不校验字节数，分片会变短，而合并后的文件头部（含时长信息）往往完好，
    四重校验因此照常通过——这是最隐蔽的一类数据损坏。
    """

    BODY = bytes(range(256)) * 4000   # ~1MB

    def _server(self, truncate=False):
        import http.server
        import threading

        body = self.BODY
        proto = http.server.BaseHTTPRequestHandler

        class H(proto):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):
                rng = self.headers.get('Range')
                m = re.match(r'bytes=(\d+)-(\d*)', rng) if rng else None
                if m:
                    s = int(m.group(1))
                    e = int(m.group(2)) if m.group(2) else len(body) - 1
                    e = min(e, len(body) - 1)
                    data, code = body[s:e + 1], 206
                else:
                    s, e, data, code = 0, len(body) - 1, body, 200
                self.send_response(code)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Content-Range', f'bytes {s}-{e}/{len(body)}')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data[:len(data) // 2] if truncate else data)
                    self.wfile.flush()
                except Exception:
                    pass
                if truncate:
                    self.close_connection = True

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f'http://127.0.0.1:{srv.server_port}/f.mp3'

    def test_truncated_parts_are_rejected(self):
        url = self._server(truncate=True)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'a.part'
            with mock.patch.object(eng, 'is_safe_http_url', return_value=True):
                with self.assertRaises(RuntimeError) as ctx:
                    eng.download_file(url, p, retries=0, parts=4)
            self.assertIn('分片不完整', str(ctx.exception))
            self.assertFalse(p.exists(), '失败后不能留下残损文件')
            self.assertFalse(list(Path(td).glob('*.part[0-9]')), '分片临时文件要清干净')

    def test_explicit_parts_actually_uses_multipart(self):
        """显式传 parts 时必须真的分片，不能静默退化成顺序下载。

        这里曾有一个重构引入的 bug：parts 分支里直接调用了
        _download_sequential，把 n_parts 完全忽略，
        导致「分片」名存实亡，截断检测也随之失效。
        """
        url = self._server()
        calls = []
        real = eng._multipart_download

        def spy(*a, **k):
            calls.append(a)
            return real(*a, **k)

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'b.part'
            with mock.patch.object(eng, 'is_safe_http_url', return_value=True),                     mock.patch.object(eng, '_multipart_download', side_effect=spy):
                eng.download_file(url, p, retries=0, parts=4)
            self.assertEqual(len(calls), 1, 'parts=4 必须走分片路径')
            self.assertEqual(p.read_bytes(), self.BODY)

    def test_auto_decision_matches_full_bytes(self):
        url = self._server()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'c.part'
            with mock.patch.object(eng, 'is_safe_http_url', return_value=True):
                eng.download_file(url, p, retries=0)
            self.assertEqual(p.read_bytes(), self.BODY)


class DbConnectionLeakTest(unittest.TestCase):
    """只读数据库连接必须在异常路径也关闭。

    原写法 `conn = _connect_db_ro(...)` ... `conn.close()` 一旦中间抛异常
    就泄漏连接，每个连接持有文件句柄，批量下载几百首可能耗尽句柄。
    这里用包装对象记录 close 是否被调用（sqlite3.Connection 的实例属性
    不可写，故不能直接打补丁）。
    """

    def _make_db(self):
        import sqlite3
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE my_list_music_info (
            id TEXT, name TEXT, singer TEXT, source TEXT, interval TEXT, meta TEXT)""")
        conn.execute("""CREATE TABLE my_list_music_info_order (
            listId TEXT, musicInfoId TEXT, "order" INTEGER)""")
        conn.execute("CREATE TABLE my_list (id TEXT, name TEXT, position INTEGER)")
        conn.commit()
        conn.close()
        return path

    def test_closes_on_exception(self):
        db = self._make_db()
        state = {'closed': 0}
        real = sqlite3.connect

        def fake_connect(*a, **k):
            conn = real(*a, **k)
            state['closed'] += 0

            class Wrapper:
                row_factory = None

                def cursor(self):
                    return conn.cursor()

                def execute(self, *aa):
                    return conn.execute(*aa)

                def close(self):
                    state['closed'] += 1
                    return conn.close()

            return Wrapper()

        with mock.patch.object(eng.sqlite3, 'connect', side_effect=fake_connect):
            with self.assertRaises(RuntimeError):
                with eng._db_ro(db) as conn:
                    conn.execute('SELECT 1')
                    raise RuntimeError('boom inside')
        self.assertEqual(state['closed'], 1, '异常路径也必须 close')

    def test_closes_on_success(self):
        db = self._make_db()
        state = {'closed': 0}
        real = sqlite3.connect

        def fake_connect(*a, **k):
            conn = real(*a, **k)

            class Wrapper:
                row_factory = None

                def cursor(self):
                    return conn.cursor()

                def close(self):
                    state['closed'] += 1
                    return conn.close()

            return Wrapper()

        with mock.patch.object(eng.sqlite3, 'connect', side_effect=fake_connect):
            with eng._db_ro(db) as conn:
                conn.cursor().execute('SELECT 1').fetchone()
        self.assertEqual(state['closed'], 1)

    def test_row_factory_is_set(self):
        db = self._make_db()
        with eng._db_ro(db) as conn:
            cur = conn.cursor()
            cur.execute('SELECT 1 AS v')
            self.assertEqual(cur.fetchone()['v'], 1)


class StopRecordsUnfinishedTest(unittest.TestCase):
    """用户主动停止后，未完成的歌必须留在失败清单里。

    原先「停止」的歌既不算 success 也不进 fail_list。若这首歌本来不在
    清单里（首次下载），它就彻底消失了——用户下次点「重试上次失败的歌」
    看不到它，只能自己回忆还差哪些。
    """

    def _make_args(self, td):
        args = type('A', (), {})()
        args.node = None
        args.api = None
        args.api_dir = os.path.join(os.environ.get('APPDATA', td), 'lx-downloader-sources')
        args.dir = td
        args.quality = '320k'
        args.fallback_quality = False
        args.min_quality = '320k'
        args.workers = 2
        args.exclude = ''
        args.max = 0
        return args

    def test_stopped_songs_land_in_failed_txt(self):
        eng.reset_dir_index()
        eng.DOWNLOAD_STOP_EVENT.clear()
        with tempfile.TemporaryDirectory() as td:
            args = self._make_args(td)
            songs = [
                {'id': '', 'name': '不存在的歌A', 'singer': '歌手A',
                 'source': 'kw', 'interval': '03:00', 'meta': '{}'},
                {'id': '', 'name': '不存在的歌B', 'singer': '歌手B',
                 'source': 'kw', 'interval': '03:00', 'meta': '{}'},
            ]

            # 不联网：把音源服务替换成一个「永远不返回」的桩，
            # 然后立刻置停止位，模拟用户点了停止。
            class _DeadServer:
                def get_urls(self, *a, **k):
                    eng.DOWNLOAD_STOP_EVENT.set()
                    raise InterruptedError('用户请求停止')

                def search_by_name_list(self, *a, **k):
                    eng.DOWNLOAD_STOP_EVENT.set()
                    raise InterruptedError('用户请求停止')

                def close(self):
                    pass

            with mock.patch.object(eng, 'UrlServer') as srv_cls,                     mock.patch.object(eng, 'check_sources', return_value=None),                     mock.patch.object(eng, 'find_node', return_value='node'),                     mock.patch.object(eng, 'detect_database', return_value=None):
                srv_cls.return_value.start.return_value = _DeadServer()
                try:
                    eng.run_download(args, songs, db_path=None, title=None)
                finally:
                    eng.DOWNLOAD_STOP_EVENT.clear()

            items = eng.load_failed_songs(td)
            names = {it['name'] for it in items}
            self.assertIn('不存在的歌A', names, '停止后未完成的歌必须记入失败清单')
            self.assertIn('不存在的歌B', names)
            for it in items:
                self.assertTrue(it['error'], '必须有明确原因，便于用户判断')


class IntervalSecondsTest(unittest.TestCase):
    """时长解析必须同时认「MM:SS」与「秒数」。

    GUI 搜索页的 interval 是秒数（Rust 侧 Option<f64>），单曲下载时原样
    传给引擎。旧实现只认冒号格式，遇到数字返回 None，
    而 verify_duration 在 expected 为 None 时**直接放行**——
    等于时长校验静默失效，翻唱版与错歌能混过第一关。
    """

    def test_colon_formats(self):
        self.assertEqual(eng.interval_to_seconds('04:29'), 269.0)
        self.assertEqual(eng.interval_to_seconds('3:45.5'), 225.5)
        self.assertEqual(eng.interval_to_seconds('00:03:45'), 225.0)

    def test_plain_numbers_are_seconds(self):
        for v in (269, 269.0, '269', '269.0'):
            with self.subTest(v=v):
                self.assertEqual(eng.interval_to_seconds(v), 269.0)

    def test_invalid_inputs_return_none(self):
        for v in ('', None, 'abc', 0, -5, '0', True, False):
            with self.subTest(v=v):
                self.assertIsNone(eng.interval_to_seconds(v))

    def test_verify_duration_actually_runs_for_numeric_interval(self):
        """传数字时校验必须真的执行，而不是走「无法校验就放行」。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'song.mp3'
            make_mp3(p, seconds=4, bitrate_kbps=320)
            # 期望 4 秒（数字形态）：应通过
            self.assertTrue(eng.verify_duration(p, 4))
            # 期望 60 秒：必须拒绝。旧实现这里会返回 True（放行）
            self.assertFalse(eng.verify_duration(p, 60),
                             '数字 interval 也必须参与校验')


class MultiArtistMatchTest(unittest.TestCase):
    """多歌手标签必须按分隔符拆开比对。

    很多文件把多位歌手放进同一个 artist 字段（"水瀬いのり、久保ユリカ"）。
    若只拆预期、不拆标签，两个单人都匹配不上，同一首歌会因
    「标签里歌手顺序不同」被判成翻唱版而拒收。
    """

    def _match(self, tag, expected):
        with mock.patch.object(eng, 'read_artist_tag', return_value=[tag]):
            return eng.artist_matches_downloaded('x', expected)

    def test_single_artist(self):
        self.assertTrue(self._match('久保ユリカ', '久保ユリカ'))

    def test_composite_tag_either_order(self):
        for tag in ('久保ユリカ、水瀬いのり', '水瀬いのり、久保ユリカ'):
            with self.subTest(tag=tag):
                self.assertTrue(self._match(tag, '久保ユリカ、水瀬いのり'))

    def test_different_separators(self):
        self.assertTrue(self._match('水瀬いのり/久保ユリカ', '久保ユリカ、水瀬いのり'))
        self.assertTrue(self._match('水瀬いのり, 久保ユリカ', '久保ユリカ、水瀬いのり'))

    def test_missing_expected_artist_is_rejected(self):
        self.assertFalse(self._match('久保ユリカ', '久保ユリカ、水瀬いのり'))

    def test_completely_different_is_rejected(self):
        self.assertFalse(self._match('邓紫棋', '周杰伦'))

    def test_extra_guest_is_accepted(self):
        """标签多了嘉宾但包含预期歌手 -> 应通过（否则会误杀大量正常歌曲）。"""
        self.assertTrue(self._match('周杰伦、费玉清', '周杰伦'))

    def test_no_tags_is_permissive(self):
        with mock.patch.object(eng, 'read_artist_tag', return_value=[]):
            self.assertTrue(eng.artist_matches_downloaded('x', '周杰伦'))


class ConfigPersistenceTest(unittest.TestCase):
    """GUI 与命令行共用同一份配置文件；min_quality 必须能存能读。

    之前 GUI 的设置全是易失的 useState，重启全部丢失；
    而引擎配置里连 min_quality 这个键都没有，--min-quality 的默认值
    是写死的 320k，用户改完下次启动又回到 320k。
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.cfg = Path(self.td.name) / 'cfg.json'
        self._orig = eng.CONFIG_PATH
        eng.CONFIG_PATH = self.cfg
        self.addCleanup(lambda: setattr(eng, 'CONFIG_PATH', self._orig))

    def test_defaults_include_min_quality(self):
        cfg = eng.load_config()
        self.assertIn('min_quality', cfg)
        self.assertIn(cfg['min_quality'], ('128k', '192k', '320k', 'flac', 'flac24bit'))

    def test_save_and_reload_min_quality(self):
        cfg = eng.load_config()
        cfg['min_quality'] = '192k'
        cfg['workers'] = 5
        eng.save_config(cfg)
        again = eng.load_config()
        self.assertEqual(again['min_quality'], '192k')
        self.assertEqual(again['workers'], 5)

    def test_unknown_keys_are_ignored_on_load(self):
        """配置文件里混入未知键不应影响读取（也便于向前兼容）。"""
        eng.save_config({'quality': 'flac', 'bogus': 1})
        cfg = eng.load_config()
        self.assertEqual(cfg['quality'], 'flac')
        self.assertNotIn('bogus', cfg)

    def test_corrupt_config_falls_back_to_defaults(self):
        self.cfg.write_text('{not json', encoding='utf-8')
        cfg = eng.load_config()
        self.assertIn('quality', cfg)
        self.assertIn('min_quality', cfg)


if __name__ == '__main__':
    unittest.main(verbosity=2)
