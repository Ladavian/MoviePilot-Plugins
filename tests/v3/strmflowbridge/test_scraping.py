"""共享目录边界、原生刮削调用和回执重试验证；不连接真实 MP 或修改真实媒体。"""
import importlib.util
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

path = Path(__file__).parents[3] / 'plugins.v3/strmflowbridge/scraping.py'
spec = importlib.util.spec_from_file_location('scraping_under_test', path)
scraping = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scraping)


class ScrapingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.title = self.root / '示例 (2026)'
        self.title.mkdir()
        (self.title / '示例.S01E01.strm').write_text('http://example.invalid/d/abc')

    def test_mapping_and_boundaries(self):
        mapped = scraping.map_scrape_path('/source/示例 (2026)', '/source', str(self.root), 'dir')
        self.assertEqual(mapped, self.title)
        for source in ['/source2/示例 (2026)', '/source/../escape', '/source']:
            with self.assertRaises((ValueError, FileNotFoundError)):
                scraping.map_scrape_path(source, '/source', str(self.root), 'dir')
        outside = self.root.parent / (self.root.name + '-outside')
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        (self.root / 'escape').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            scraping.map_scrape_path('/source/escape', '/source', str(self.root), 'dir')

    def test_native_chain_receives_explicit_identity_and_preservation_mode(self):
        calls = {}
        class Media:
            def recognize_media(self, **kwargs):
                calls['identity'] = kwargs
                return types.SimpleNamespace(tmdb_id=42)
        class Scraper:
            def scrape_metadata(self, **kwargs):
                calls['scrape'] = kwargs
                return True, '完成'
        modules = {
            'app.chain.media': types.SimpleNamespace(MediaChain=Media),
            'app.chain.scraping': types.SimpleNamespace(ScrapingChain=Scraper),
            'app.sdk.media': types.SimpleNamespace(MetaInfoPath=lambda p: p),
            'app.schemas.file': types.SimpleNamespace(FileItem=lambda **kw: types.SimpleNamespace(**kw)),
            'app.schemas.types': types.SimpleNamespace(MediaSource=types.SimpleNamespace(TMDB='tmdb'), MediaType=types.SimpleNamespace(TV='电视剧', MOVIE='电影')),
        }
        item = {'id': 'a'*32, 'path': '/source/示例 (2026)', 'type': 'dir', 'tmdb_id': 42, 'media_type': 'tv', 'force': False}
        with patch.dict('sys.modules', modules):
            self.assertTrue(scraping.scrape_with_moviepilot(item, '/source', str(self.root))[0])
        self.assertEqual(calls['identity']['media_id'], '42')
        self.assertEqual(calls['scrape']['fileitem'].path, self.title.as_posix())
        self.assertFalse(calls['scrape']['overwrite'])
        self.assertTrue(calls['scrape']['init_folder'])
        item['type'] = 'file'; item['path'] += '/示例.S01E01.strm'
        with patch.dict('sys.modules', modules):
            scraping.scrape_with_moviepilot(item, '/source', str(self.root))
        self.assertFalse(calls['scrape']['init_folder'])

    def test_lost_receipt_is_retried_without_scraping_again(self):
        class Runner(scraping.MoviePilotScrapeMixin):
            def _feature(self, key): return True
            def save_data(self, *args): pass
            def _record_communication(self, *args): pass
            def _schedule_scrape(self, *args): pass
            def _error_message(self, error): return type(error).__name__
        runner = Runner()
        runner._lock = threading.RLock(); runner._scrape_lock = threading.Lock()
        runner._state = {}; runner._config = {'sf_scrape_root': '/source', 'mp_scrape_root': str(self.root)}
        item = {'id': 'a'*32, 'path': '/source/示例 (2026)', 'type': 'dir', 'tmdb_id': 42, 'media_type': 'tv'}
        receipts = []
        def http(route, payload=None):
            if not route: return {'protocol': 1, 'tasks': [{'job_id': 1, 'item': item}]}
            receipts.append(payload)
            if payload['state'] == 'success' and len(receipts) == 2: raise OSError('temporary')
            return {'accepted': True}
        runner._scrape_http = http
        with patch.object(scraping, 'scrape_with_moviepilot', return_value=(True, '完成')) as call:
            runner._scrape_tick(); runner._scrape_tick()
            self.assertEqual(call.call_count, 1)
        self.assertEqual([r['state'] for r in receipts], ['running', 'success', 'success'])
        self.assertEqual(runner._state['scrape_results']['a'*32]['receipt']['state'], 'success')
