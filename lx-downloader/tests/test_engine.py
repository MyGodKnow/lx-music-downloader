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
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lx_music_downloader as eng  # noqa: E402


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
