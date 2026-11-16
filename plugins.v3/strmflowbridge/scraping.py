"""通过已挂载的 STRM 目录调用 MP 自身刮削，状态回执独立于订阅和上传。"""
import json
import os
from pathlib import Path, PurePosixPath
import threading
import time
import urllib.request
import urllib.parse


def map_scrape_path(source, source_root, target_root, item_type):
    """仅允许用户配置根目录内的目标，拒绝目录穿越、越界链接和整库误刮。"""
    if not all(isinstance(v, str) and v.strip() for v in (source, source_root, target_root)):
        raise ValueError('请配置 StrmFlow 与 MP 的 STRM 目录映射')
    raw, root = PurePosixPath(source), PurePosixPath(source_root)
    if not raw.is_absolute() or not root.is_absolute() or root == PurePosixPath('/') or '..' in raw.parts or '\\' in source:
        raise ValueError('StrmFlow 路径必须是媒体根目录内的绝对路径')
    relative = raw.relative_to(root)
    if not relative.parts:
        raise ValueError('不允许把整个媒体根目录作为单个刮削任务')
    target_base = Path(target_root).resolve(strict=True)
    if target_base == Path('/') or not target_base.is_dir():
        raise ValueError('MP STRM 根目录无效')
    target = target_base.joinpath(*relative.parts).resolve(strict=True)
    target.relative_to(target_base)
    if target == target_base:
        raise ValueError('刮削目标不能是整个媒体根目录')
    if item_type == 'file':
        if not target.is_file() or target.suffix.lower() != '.strm':
            raise ValueError('刮削目标不是 STRM 文件')
    elif item_type == 'dir':
        if not target.is_dir():
            raise ValueError('刮削目录不存在')
        count, found = 0, False
        for directory, dirs, files in os.walk(target, followlinks=False):
            for name in dirs + files:
                child = Path(directory) / name
                if child.is_symlink():
                    raise ValueError('刮削目录包含符号链接，请先核对目录')
                count += 1
                if count > 20000:
                    raise ValueError('单个片目目录过大，请核对映射范围')
                if child.suffix.lower() == '.strm':
                    found = True
        if not found:
            raise ValueError('目标目录中没有 STRM 文件')
    else:
        raise ValueError('刮削目标类型无效')
    writable = target if target.is_dir() else target.parent
    if not os.access(writable, os.W_OK):
        raise PermissionError('MP 对 STRM 目录没有写权限')
    return target


def scrape_with_moviepilot(item, source_root, target_root):
    """传入明确的媒体身份；仅执行 MP 刮削链，不进入下载或文件整理链。"""
    if item.get('media_type') not in ('movie', 'tv') or isinstance(item.get('tmdb_id'), bool) or int(item.get('tmdb_id') or 0) <= 0:
        raise ValueError('缺少有效的 TMDB 媒体身份')
    target = map_scrape_path(item['path'], source_root, target_root, item.get('type'))
    from app.chain.media import MediaChain
    from app.chain.scraping import ScrapingChain
    from app.sdk.media import MetaInfoPath
    from app.schemas.file import FileItem
    from app.schemas.types import MediaSource, MediaType
    meta = MetaInfoPath(target)
    media = MediaChain().recognize_media(meta=meta, mtype=MediaType.TV if item['media_type'] == 'tv' else MediaType.MOVIE,
                                        media_source=MediaSource.TMDB, media_id=str(item['tmdb_id']))
    if not media:
        raise ValueError('MP 无法读取指定 TMDB 媒体信息')
    fileitem = FileItem(storage='local', path=target.as_posix(), type=item['type'], name=target.name)
    ok, message = ScrapingChain().scrape_metadata(fileitem=fileitem, meta=meta, mediainfo=media, overwrite=bool(item.get('force')), init_folder=item['type'] == 'dir')
    return bool(ok), str(message or ('刮削完成' if ok else 'MP 刮削未完成'))[:300]


class _ScrapeNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MoviePilotScrapeMixin:
    def _scrape_http(self, route, payload=None):
        parsed = urllib.parse.urlsplit(self._url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or not self._token:
            raise ValueError('StrmFlow 地址或联动密钥无效')
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(self._url + '/api/integrations/moviepilot/scrape' + route, data=body,
                                     headers={'X-StrmFlow-Token': self._token, 'Content-Type': 'application/json'})
        with urllib.request.build_opener(_ScrapeNoRedirect).open(req, timeout=15) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('刮削响应过大')
        return json.loads(raw)

    def _schedule_scrape(self, delay=1):
        if not self._feature('scrape_metadata'):
            return
        from app.sdk import scheduler as scheduler_sdk
        with self._lock:
            if getattr(self, '_scrape_scheduled', False):
                return
            self._scrape_scheduled = True
        try:
            accepted = scheduler_sdk.add_plugin_once_job(self.__class__.__name__, 'scrape', self._scrape_tick, 'StrmFlow STRM 刮削', delay_seconds=delay)
        except Exception:
            accepted = False
        if not accepted:
            self._scrape_scheduled = False
            self._record_communication('刮削调度', False, 'MP 调度器未接受任务，等待下一次通信重试')

    def _scrape_tick(self):
        with self._lock:
            self._scrape_scheduled = False
        if not self._feature('scrape_metadata') or not self._scrape_lock.acquire(blocking=False):
            return
        processed = False
        retry = False
        try:
            result = self._scrape_http('')
            tasks = result.get('tasks', [])
            self._scrape_network_retries = 0
            if result.get('protocol') != 1 or not isinstance(tasks, list) or len(tasks) > 1600:
                raise ValueError('刮削任务协议无效')
            for task in tasks:
                if not self._feature('scrape_metadata'):
                    break
                item = task.get('item', {})
                key, job_id = item.get('id'), task.get('job_id')
                if not isinstance(key, str) or len(key) != 32 or not isinstance(job_id, int):
                    raise ValueError('刮削任务身份无效')
                with self._lock:
                    saved = self._state.setdefault('scrape_results', {}).get(key)
                if saved:
                    receipt = dict(saved['receipt'])
                else:
                    # 先让主程序确认任务仍有效，再执行任何写入。
                    self._scrape_http('/result', {'job_id': job_id, 'id': key, 'state': 'running'})
                    try:
                        ok, message = scrape_with_moviepilot(item, self._config.get('sf_scrape_root', ''), self._config.get('mp_scrape_root', ''))
                    except Exception as error:
                        ok, message = False, str(error)[:300]
                    receipt = {'job_id': job_id, 'id': key, 'state': 'success' if ok else 'failed', 'message': message}
                    with self._lock:
                        records = self._state.setdefault('scrape_results', {})
                        records[key] = {'at': time.time(), 'receipt': receipt}
                        cutoff = time.time() - 7 * 86400
                        for old in list(records):
                            if records[old].get('at', 0) < cutoff:
                                records.pop(old, None)
                        while len(records) > 1000:
                            records.pop(next(iter(records)))
                        self.save_data('bridge_state', self._state)
                    self._record_communication('影视刮削', ok, f"{item.get('title', '媒体')} · {message}")
                self._scrape_http('/result', receipt)
                processed = True
        except Exception as error:
            self._scrape_network_retries = getattr(self, '_scrape_network_retries', 0) + 1
            permanent = hasattr(error, 'code') and error.code in (401, 403, 404, 409)
            retry = not permanent and self._scrape_network_retries <= 3
            self._record_communication('刮削通信', False, self._error_message(error))
        finally:
            self._scrape_lock.release()
            if self._feature('scrape_metadata') and (processed or retry):
                self._schedule_scrape(15 if retry else 3)
