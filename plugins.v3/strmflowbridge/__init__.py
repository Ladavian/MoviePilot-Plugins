"""MoviePilot V3 companion: confirmed media facts and durable upload notifications."""
import json
from copy import deepcopy
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from apscheduler.triggers.cron import CronTrigger
from app.sdk.plugin import _PluginBase
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.media import MetaInfo, MetaInfoPath, MediaInfo
from app.sdk import scheduler as scheduler_sdk
from app.schemas.types import EventType, ChainEventType, MediaType
from app.db.oper.subscribe import SubscribeOper
from .bridge import field, write, value, identity, episode_scope, remaining_context, transfer_paths


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the dedicated integration token to a redirected destination."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class StrmFlowBridge(_PluginBase):
    """Sync subscriptions and acquired movie/episode facts through a scoped protocol."""
    plugin_name = 'StrmFlow 联动'
    plugin_desc = '硬链接完成上传通知、订阅镜像与电影/逐集双向去重（MoviePilot V3）'
    plugin_icon = 'https://raw.githubusercontent.com/Ladavian/MoviePilot-Plugins/main/icons/strmflow.png'
    plugin_version = '1.0.3'
    plugin_author = 'Ladavian'
    author_url = 'https://github.com/Ladavian'
    plugin_config_prefix = 'strmflowbridge_'
    plugin_order = 50
    auth_level = 1
    _enabled = False
    FEATURES = ('notify_upload','sync_subscriptions','mp_to_strmflow','strmflow_to_mp','complete_subscriptions')

    def init_plugin(self, config=None):
        """Load configuration and durable pending events; no credentials are logged."""
        self._lock = threading.RLock()
        self._exchange_lock = threading.Lock()
        self._config = dict(config or {})
        if config and not all(key in config for key in self.FEATURES):
            # Old versions had implicit coupling: pause until the user opts into each feature.
            self._config.update({'enabled':False,**{key:False for key in self.FEATURES}})
            self.update_config(self._config)
        self._enabled = bool(self._config.get('enabled'))
        self._features = {key:bool(self._config.get(key,False)) for key in self.FEATURES}
        self._url = str(self._config.get('url') or '').rstrip('/')
        self._token = str(self._config.get('token') or '')
        self._notify_upload = self._features['notify_upload']
        self._state = self.get_data('bridge_state') or {'outbox':{},'confirmed':[]}
        self._available = {}
        self._last_ok = 0
        self._last_error = ''
        if self._enabled and any(self._features.values()):
            scheduler_sdk.add_plugin_once_job(self.__class__.__name__, 'refresh', self.refresh, 'StrmFlow 联动初始化', delay_seconds=3)

    def _feature(self,key):
        """The master switch gates every independent, opt-in feature."""
        return self._enabled and self._features.get(key,False)

    def get_state(self):
        """Report plugin enablement to MoviePilot."""
        return self._enabled

    @staticmethod
    def get_command():
        """This plugin has no chat commands."""
        return []

    def get_api(self):
        """Polling avoids opening an additional callback endpoint in MoviePilot."""
        return []

    def get_service(self):
        """Use the host scheduler and serialize all exchange requests."""
        return [{'id':f'{self.__class__.__name__}.Refresh','name':'StrmFlow 订阅与入库对账','trigger':CronTrigger(second='*/15'),'func':self.refresh,'kwargs':{}}] if self._enabled and any(self._features.values()) else []

    def stop_service(self):
        """Stop accepting work and cancel instance-owned delayed jobs."""
        self._enabled = False
        scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, 'refresh')

    def _subscriptions(self):
        """Read host subscriptions through Oper, preserving per-season boundaries."""
        result = []
        for sub in SubscribeOper().list():
            key = identity(sub)
            if not key or field(sub, 'episode_group') or field(sub, 'best_version'):
                continue
            result.append({'id':str(sub.id),'tmdb_id':key[0],'media_type':key[1],'title':sub.name or '', 'year':str(field(sub,'year') or ''),'season':int(field(sub,'season') or 0),'start':int(field(sub,'start_episode') or 0),'end':int(field(sub,'total_episode') or 0),'paused':field(sub,'state')=='P'})
        return result

    def _request(self, payload, timeout=10):
        """Send one bounded POST without automatic replay or credential redirects."""
        parsed = urllib.parse.urlsplit(self._url)
        if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('StrmFlow 地址格式不正确')
        if not self._token:
            raise ValueError('请填写 StrmFlow 专用联动密钥')
        data = json.dumps(payload, ensure_ascii=False).encode()
        if len(data) > 4*1024*1024:
            raise ValueError('联动数据超过 4 MiB，请分批处理')
        req = urllib.request.Request(self._url+'/api/integrations/moviepilot/exchange',data=data,headers={'Content-Type':'application/json','X-StrmFlow-Token':self._token},method='POST')
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as response:
            raw = response.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:
            raise ValueError('联动响应过大')
        result = json.loads(raw)
        if result.get('protocol') != 1 or not result.get('accepted'):
            raise ValueError('联动协议不匹配')
        return result

    def refresh(self):
        """Send only enabled features; never consume disabled features' queued data."""
        if not self._enabled or not any(self._features.values()) or not self._exchange_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                pending=deepcopy(self._state['outbox'])
            def eligible(event):
                return (self._feature('notify_upload') and event.get('paths')) or (self._feature('mp_to_strmflow') and event.get('completed')) or (self._feature('sync_subscriptions') and event.get('removed'))
            selected=dict(list((key,event) for key,event in pending.items() if eligible(event))[:100])
            if not selected and not self._feature('sync_subscriptions') and not self._feature('strmflow_to_mp'):
                return
            rows=self._subscriptions() if self._feature('sync_subscriptions') or self._feature('strmflow_to_mp') else []
            payload={}
            if self._feature('sync_subscriptions'):
                payload['subscriptions']=rows
                payload['removed']=[event['removed'] for event in selected.values() if event.get('removed')]
            if self._feature('strmflow_to_mp'):
                payload['lookup']=[{'tmdb_id':row['tmdb_id'],'media_type':row['media_type']} for row in rows]
            if self._feature('mp_to_strmflow'):
                payload['completed']=[event['completed'] for event in selected.values() if event.get('completed')]
            if self._feature('notify_upload'):
                payload['paths']=sorted({path for event in selected.values() for path in event.get('paths',[])})[:1000]
            result=self._request(payload)
            with self._lock:
                if self._feature('strmflow_to_mp'):
                    self._available={(int(row['tmdb_id']),row['media_type']):row for row in result['available']}
                    self._last_ok=time.monotonic()
                for key,event in selected.items():
                    current=self._state['outbox'].get(key,{})
                    for name,flag in (('completed','mp_to_strmflow'),('removed','sync_subscriptions'),('paths','notify_upload')):
                        if not self._feature(flag) or name not in event or current.get(name)!=event[name]:
                            continue
                        if name=='paths' and not result.get('upload_accepted',True):
                            continue
                        if name=='completed' and event[name] not in self._state['confirmed']:
                            self._state['confirmed'].append(event[name])
                        current.pop(name,None)
                    if not any(current.values()):
                        self._state['outbox'].pop(key,None)
                self.save_data('bridge_state',self._state)
            self._apply_progress()
            reason='上传通知待接收，请检查 StrmFlow 绑定的上传任务和目录映射' if result.get('upload_error') else ''
            if reason and reason!=self._last_error:
                logger.warning(reason)
            self._last_error=reason
        except Exception as error:
            reason=f'联动暂未完成（{type(error).__name__}），通知已保留等待重试'
            if reason!=self._last_error:
                logger.warning(reason)
                self._last_error=reason
        finally:
            self._exchange_lock.release()

    @eventmanager.register(EventType.TransferComplete)
    def transfer_complete(self, event: Event):
        """Only successful target media files create acquired-resource facts."""
        if not self._feature('notify_upload') and not self._feature('mp_to_strmflow'):
            return
        data = event.event_data
        paths = transfer_paths(field(data,'transferinfo'))
        key = identity(field(data,'mediainfo'))
        if not paths:
            return
        if field(field(data,'mediainfo'),'episode_group') or field(field(data,'meta'),'episode_group'):
            key=None
        event_id = str(field(data,'idempotency_key') or field(data,'transfer_history_id') or '')
        if not event_id:
            import hashlib
            event_id = hashlib.sha256(json.dumps(paths).encode()).hexdigest()
        if not key or not self._feature('mp_to_strmflow'):
            facts=[]
        elif key[1]=='movie':
            facts=[{'tmdb_id':key[0],'media_type':'movie','season':0,'episodes':[],'event_id':event_id}]
        else:
            grouped={}
            for path in paths:
                season,episodes=episode_scope(MetaInfoPath(path))
                if season is not None:
                    grouped.setdefault(season,set()).update(episodes)
            # Ambiguous file names are never guessed from the whole season/pack event.
            facts=[{'tmdb_id':key[0],'media_type':'tv','season':season,'episodes':sorted(eps),'event_id':event_id} for season,eps in grouped.items() if eps]
        with self._lock:
            if facts:
                for index,fact in enumerate(facts):
                    self._state['outbox'][f'{event_id}:{index}']={'completed':fact,'paths':paths if index==0 and self._feature('notify_upload') else []}
            elif self._feature('notify_upload'):
                self._state['outbox'][event_id]={'paths':paths}
            self.save_data('bridge_state',self._state)
        scheduler_sdk.add_plugin_once_job(self.__class__.__name__,'refresh',self.refresh,'StrmFlow 硬链接完成通知',delay_seconds=1)

    @eventmanager.register(EventType.SubscribeAdded)
    @eventmanager.register(EventType.SubscribeModified)
    def subscription_changed(self, event: Event):
        """Coalesce subscription edits without performing network work on the event thread."""
        if self._feature('sync_subscriptions'):
            scheduler_sdk.add_plugin_once_job(self.__class__.__name__,'refresh',self.refresh,'StrmFlow 订阅同步',delay_seconds=1)

    def _refresh_available(self):
        """Read inventory without creating subscriptions when only deduplication is enabled."""
        if not self._feature('strmflow_to_mp') or time.monotonic()-self._last_ok<3 or not self._exchange_lock.acquire(blocking=False):
            return
        try:
            rows=self._subscriptions()
            result=self._request({'lookup':[{'tmdb_id':row['tmdb_id'],'media_type':row['media_type']} for row in rows]},timeout=3)
            with self._lock:
                self._available={(int(row['tmdb_id']),row['media_type']):row for row in result['available']}
                self._last_ok=time.monotonic()
        except Exception:
            pass
        finally:
            self._exchange_lock.release()

    @eventmanager.register(EventType.SubscribeDeleted)
    def subscription_deleted(self, event: Event):
        """Mirror explicit removals; automatic completion still waits for acquired facts."""
        remote_id=field(event.event_data,'subscribe_id')
        if not self._feature('sync_subscriptions') or remote_id is None:
            return
        with self._lock:
            self._state['outbox'][f'removed:{remote_id}']={'removed':str(remote_id)}
            self.save_data('bridge_state',self._state)
        scheduler_sdk.add_plugin_once_job(self.__class__.__name__,'refresh',self.refresh,'StrmFlow 取消订阅同步',delay_seconds=1)

    @eventmanager.register(ChainEventType.ResourceSelection)
    def filter_resources(self, event: Event):
        """Exclude confirmed movies and constrain TV packs to remaining episodes."""
        if not self._feature('strmflow_to_mp'):
            return
        self._refresh_available()
        if time.monotonic()-self._last_ok>60:
            return
        data=event.event_data
        contexts=field(data,'updated_contexts') if field(data,'updated') else field(data,'contexts',[])
        if contexts is None:
            return
        with self._lock:
            available=dict(self._available)
        changed=False;remaining=[]
        for context in contexts:
            item=remaining_context(context,available)
            changed=changed or item is not context
            if item is not None:
                remaining.append(item)
        if changed:
            write(data,'updated',True);write(data,'updated_contexts',remaining);write(data,'source','StrmFlow 已入库去重')

    @eventmanager.register(ChainEventType.ResourceDownload)
    def prevent_duplicate_download(self,event: Event):
        """Final guard for direct movie/single-episode download paths."""
        if not self._feature('strmflow_to_mp'):
            return
        self._refresh_available()
        if time.monotonic()-self._last_ok>60:
            return
        data=event.event_data
        if field(data,'cancel'):
            return
        if field(data,'context') is None:
            return
        with self._lock:
            item=remaining_context(field(data,'context'),self._available)
        if item is None:
            write(data,'cancel',True);write(data,'source','StrmFlow');write(data,'reason','对应资源已经成功入库')

    def _apply_progress(self):
        """Preserve MP notes; complete via the public host Chain after exact confirmation."""
        if not self._feature('strmflow_to_mp') or not self._feature('complete_subscriptions'):
            return
        from app.chain.subscribe import SubscribeChain
        for sub in SubscribeOper().list():
            key=identity(sub)
            if not key or field(sub,'episode_group') or field(sub,'best_version') or field(sub,'state')=='P':
                continue
            own=self._available.get(key,{})
            acquired=set(own.get('episodes',{}).get(str(field(sub,'season') or 0),[]))
            for fact in self._state['confirmed']:
                if (fact['tmdb_id'],fact['media_type'])==key and fact['season']==(field(sub,'season') or 0):
                    acquired.update(fact['episodes'])
            complete=bool(own.get('movie')) if key[1]=='movie' else False
            if key[1]=='tv' and acquired:
                start=max(int(field(sub,'start_episode') or 1),1);end=int(field(sub,'total_episode') or 0)
                relevant={e for e in acquired if e>=start and (end<=0 or e<=end)}
                note=sorted(set(field(sub,'note') or [])|relevant)
                if note!=(field(sub,'note') or []):
                    sub=SubscribeOper().update(sub.id,{'note':note}) or sub
                complete=end>=start and set(range(start,end+1))<=acquired
            if complete:
                media=MediaInfo(title=sub.name,year=field(sub,'year'),type=MediaType.MOVIE if key[1]=='movie' else MediaType.TV,media_source='tmdb',media_id=str(key[0]),tmdb_id=key[0])
                meta=MetaInfo(sub.name);meta.type=MediaType.MOVIE if key[1]=='movie' else MediaType.TV
                # The public Chain writes completion history and dispatches normal completion effects.
                SubscribeChain().finish_subscribe_or_not(subscribe=sub,meta=meta,mediainfo=media,force=True)

    def get_form(self):
        """Use explicit rows/cards, a master switch, and opt-in switches for every feature."""
        def column(component,props,md=12):
            return {'component':'VCol','props':{'cols':12,'md':md},'content':[{'component':component,'props':props}]}
        def switch(model,label,hint):
            return column('VSwitch',{'model':model,'label':label,'hint':hint,'persistent-hint':True,'inset':True,'color':'primary'})
        def section(title,subtitle,children):
            return {'component':'VCol','props':{'cols':12},'content':[{'component':'VCard','props':{'title':title,'subtitle':subtitle,'variant':'outlined','rounded':'lg'},'content':[{'component':'VCardText','content':[{'component':'VRow','content':children}]}]}]}
        content=[
            section('联动总开关','关闭时不执行任何通知、同步、去重或订阅写入。',[switch('enabled','启用插件总开关','启用总开关后，还需单独打开下面需要的功能。')]),
            section('连接设置','地址与密钥可以先填写，功能开关默认全部关闭。',[
                column('VTextField',{'model':'url','label':'StrmFlow 地址','placeholder':'http://NAS_IP:18766','variant':'outlined'},6),
                column('VTextField',{'model':'token','label':'专用联动密钥','type':'password','autocomplete':'new-password','variant':'outlined'},6)]),
            section('上传通知','仅通知主程序中已经配置好的上传任务。',[switch('notify_upload','硬链接完成后通知上传','主程序需先绑定上传任务并配置目录映射。')]),
            section('订阅同步','只同步需要交给 StrmFlow 搜索的订阅。',[switch('sync_subscriptions','将 MP 订阅同步到 StrmFlow','新增、修改和主动取消订阅时同步；关闭不会删除媒体。')]),
            section('双向去重与完成','两个方向可以分别开启，自动完成订阅另行授权。',[
                switch('mp_to_strmflow','MP 入库后通知 StrmFlow 跳过','只发送成功入库的电影或准确季集。'),
                switch('strmflow_to_mp','采用 StrmFlow 入库结果进行 MP 去重','仅过滤后续下载；不启用订阅同步也可以使用。'),
                switch('complete_subscriptions','自动更新 MP 进度并完成订阅','需同时开启上一项；关闭时不修改 MP 订阅进度或完成状态。')])]
        defaults={'enabled':False,'url':'','token':'',**{key:False for key in self.FEATURES}}
        return [{'component':'VForm','content':[{'component':'VRow','content':content}]}],defaults

    def get_page(self):
        """Show active feature selection and durable pending work without secrets."""
        labels={'notify_upload':'上传通知','sync_subscriptions':'订阅同步','mp_to_strmflow':'MP → StrmFlow','strmflow_to_mp':'StrmFlow → MP','complete_subscriptions':'订阅自动完成'}
        active='、'.join(label for key,label in labels.items() if self._feature(key))
        text=('已启用：'+active if active else '当前未启用联动功能')+f"。待发通知 {len(self._state.get('outbox',{}))} 条。"
        return [{'component':'VAlert','props':{'type':'info','variant':'tonal','text':text}}]
