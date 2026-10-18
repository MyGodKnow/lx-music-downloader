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


if __name__ == '__main__':
    unittest.main(verbosity=2)
