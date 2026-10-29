"""Exercise plugin lifecycle and progress semantics with a fake V3 host contract."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest


def module(name, **values):
    obj=types.ModuleType(name)
    for key,value in values.items():setattr(obj,key,value)
    sys.modules[name]=obj
    return obj


class HostBase:
    def update_config(self,config):self.updated_config=dict(config)
    def get_data(self,key):return None
    def save_data(self,key,data):self.saved=data


class Oper:
    rows=[]
    def list(self):return self.rows
    def update(self,sid,payload):
        row=next(r for r in self.rows if r.id==sid)
        for key,value in payload.items():setattr(row,key,value)
        return row


class Chain:
    finished=[]
    def finish_subscribe_or_not(self,**args):self.finished.append(args)


def load_plugin():
    module('app');module('app.sdk');module('app.sdk.plugin',_PluginBase=HostBase)
    event_manager=types.SimpleNamespace(register=lambda *_:lambda fn:fn)
    module('app.sdk.events',Event=object,eventmanager=event_manager)
    module('app.sdk.logging',logger=types.SimpleNamespace(warning=lambda *_:None,info=lambda *_:None))
    module('app.sdk.media',MetaInfo=lambda name:types.SimpleNamespace(name=name),MetaInfoPath=lambda name:None,MediaInfo=lambda **kw:types.SimpleNamespace(**kw))
    module('app.sdk.scheduler',add_plugin_once_job=lambda *a,**kw:True,remove_plugin_once_job=lambda *a:None)
    module('app.schemas');module('app.schemas.types',EventType=types.SimpleNamespace(TransferComplete='transfer',SubscribeAdded='added',SubscribeModified='modified',SubscribeDeleted='deleted'),ChainEventType=types.SimpleNamespace(ResourceSelection='selection',ResourceDownload='download'),MediaType=types.SimpleNamespace(MOVIE='电影',TV='电视剧'))
    module('app.db');module('app.db.oper');module('app.db.oper.subscribe',SubscribeOper=Oper)
    module('app.chain');module('app.chain.subscribe',SubscribeChain=Chain)
    module('apscheduler');module('apscheduler.triggers');module('apscheduler.triggers.cron',CronTrigger=lambda **kw:kw)
    path=Path(__file__).parents[3]/'plugins.v3/strmflowbridge/__init__.py'
    spec=importlib.util.spec_from_file_location('test_strmflow_plugin',path,submodule_search_locations=[str(path.parent)])
    plugin=importlib.util.module_from_spec(spec);sys.modules[spec.name]=plugin;spec.loader.exec_module(plugin)
    return plugin.StrmFlowBridge


class PluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.plugin_class=load_plugin()
    def setUp(self):
        Oper.rows=[];Chain.finished=[];self.plugin=self.plugin_class();self.plugin.init_plugin({'enabled':True,**{key:True for key in self.plugin.FEATURES},'notify_upload':False})

    def test_outbox_survives_failure_and_acknowledges_once(self):
        fact={'tmdb_id':42,'media_type':'movie','season':0,'episodes':[],'event_id':'event1'}
        self.plugin._state['outbox']['event1']={'completed':fact}
        def failure(*a,**kw):raise OSError('temporary connection failure')
        self.plugin._request=failure;self.plugin.refresh();self.assertIn('event1',self.plugin._state['outbox'])
        self.plugin._request=lambda *a,**kw:{'available':[],'accepted':True,'protocol':1}
        self.plugin.refresh();self.plugin.refresh()
        self.assertEqual(self.plugin._state['outbox'],{});self.assertEqual(self.plugin._state['confirmed'],[fact])

    def test_partial_episode_never_completes_entire_subscription(self):
        row=types.SimpleNamespace(id=7,media_source='tmdb',media_id='42',type='电视剧',episode_group=None,best_version=False,state='R',name='Series',season=1,start_episode=1,total_episode=3,note=[1],year='2026')
        Oper.rows=[row]
        self.plugin._available={(42,'tv'):{'episodes':{'1':[2]}}}
        self.plugin._state['confirmed']=[{'tmdb_id':42,'media_type':'tv','season':1,'episodes':[1]}]
        self.plugin._apply_progress()
        self.assertEqual(row.note,[1,2]);self.assertEqual(Chain.finished,[])
        self.plugin._available={(42,'tv'):{'episodes':{'1':[2,3]}}};self.plugin._apply_progress()
        self.assertEqual(len(Chain.finished),1);self.assertTrue(Chain.finished[0]['force']);self.assertEqual(Chain.finished[0]['subscribe'].id,7)

    def test_pause_and_custom_episode_groups_are_preserved(self):
        for paused,group in [('P',None),('R','custom-group')]:
            row=types.SimpleNamespace(id=8,media_source='tmdb',media_id='42',type='电影',episode_group=group,best_version=False,state=paused,name='Movie')
            Oper.rows=[row];self.plugin._available={(42,'movie'):{'movie':True}};self.plugin._apply_progress()
        self.assertEqual(Chain.finished,[])

    def test_stop_prevents_queued_network_work(self):
        self.plugin.stop_service();self.plugin._request=lambda *a,**kw:self.fail('network work after stop');self.plugin.refresh()


class AdditionalPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.plugin_class=load_plugin()
    setUp=PluginTests.setUp
    def test_upload_failure_keeps_path_notification(self):
        self.plugin._state['outbox']['event1']={'paths':['/media/Movie.mkv'],'completed':{'tmdb_id':42,'media_type':'movie','season':0,'episodes':[]}}
        self.plugin._request=lambda *a,**kw:{'available':[],'accepted':True,'protocol':1,'upload_accepted':False,'upload_error':'mapping'}
        self.plugin.refresh();self.assertIn('event1',self.plugin._state['outbox']);self.assertEqual(len(self.plugin._state['confirmed']),1)

    def test_unknown_media_still_notifies_successful_upload(self):
        self.plugin._notify_upload=True;self.plugin._features['notify_upload']=True
        event=types.SimpleNamespace(event_data={'mediainfo':{'media_source':'douban','media_id':'42','type':'电影'},'transferinfo':{'success':True,'file_list_new':['/media/Unknown.mkv']},'idempotency_key':'unknown1'})
        self.plugin.transfer_complete(event)
        self.assertEqual(self.plugin._state['outbox']['unknown1'],{'paths':['/media/Unknown.mkv']})

if __name__=='__main__':unittest.main()

class ControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.plugin_class=load_plugin()
    def setUp(self):Oper.rows=[];Chain.finished=[]
    def configured(self,**features):
        plugin=self.plugin_class();plugin.init_plugin({'enabled':True,**{key:False for key in plugin.FEATURES},**features});return plugin
    def test_defaults_and_old_config_stop_all_features(self):
        plugin=self.plugin_class();plugin.init_plugin({'enabled':True,'notify_upload':True,'url':'http://example.test','token':'saved-token'})
        self.assertFalse(plugin.get_state());self.assertEqual(plugin.get_service(),[]);self.assertEqual(plugin._token,'saved-token');self.assertTrue(all(not value for value in plugin._features.values()))
        form,defaults=plugin.get_form();self.assertFalse(defaults['enabled']);self.assertTrue(all(not defaults[key] for key in plugin.FEATURES));self.assertEqual(form[0]['content'][0]['component'],'VRow')
        plugin._request=lambda *a,**kw:self.fail('disabled configuration sent a request');plugin.refresh()
    def test_master_on_with_no_features_does_not_connect(self):
        plugin=self.configured();plugin._request=lambda *a,**kw:self.fail('idle master switch sent a request');plugin.refresh();self.assertEqual(plugin.get_service(),[])
    def test_upload_only_never_sends_subscriptions_or_acquired_facts(self):
        plugin=self.configured(notify_upload=True);captured=[]
        plugin._request=lambda body,**kw:(captured.append(body) or {'available':[],'accepted':True,'protocol':1})
        plugin._state['outbox']['one']={'paths':['/media/Movie.mkv'],'completed':{'tmdb_id':42,'media_type':'movie'}}
        plugin.refresh();self.assertEqual(set(captured[0]),{'paths','upload_ready'});self.assertIn('completed',plugin._state['outbox']['one']);self.assertNotIn('paths',plugin._state['outbox']['one'])
    def test_dedup_only_does_not_mirror_or_modify_subscriptions(self):
        plugin=self.configured(strmflow_to_mp=True);Oper.rows=[types.SimpleNamespace(id=1,media_source='tmdb',media_id='42',type='电影',episode_group=None,best_version=False,name='Movie',state='R')];captured=[]
        plugin._request=lambda body,**kw:(captured.append(body) or {'available':[{'tmdb_id':42,'media_type':'movie','movie':True,'episodes':{}}],'accepted':True,'protocol':1})
        plugin.refresh();self.assertEqual(set(captured[0]),{'lookup'});self.assertEqual(Chain.finished,[])
    def test_disabled_notification_does_not_enqueue(self):
        plugin=self.configured(sync_subscriptions=True)
        plugin.transfer_complete(types.SimpleNamespace(event_data={'mediainfo':{'media_source':'tmdb','media_id':'42','type':'电影'},'transferinfo':{'success':True,'file_list_new':['/media/Movie.mkv']}}))
        self.assertEqual(plugin._state['outbox'],{})

class V3CommunicationTests(ControlsTests):
    def test_real_v3_subscription_fields_reach_exchange(self):
        plugin=self.configured(sync_subscriptions=True)
        Oper.rows=[types.SimpleNamespace(id=8,media_source='themoviedb',media_id='297253',type='电视剧',episode_group=None,best_version=False,name='Series',state='R',season=1,total_episode=40)]
        captured=[]
        plugin._request=lambda body,**kw:(captured.append(body) or {'available':[],'accepted':True,'protocol':1})
        plugin.refresh()
        self.assertEqual(captured[0]['subscriptions'][0]['tmdb_id'],297253)
        self.assertEqual(plugin._subscription_count,1)
        self.assertTrue(plugin._last_exchange)
        self.assertIn('最近通信成功',str(plugin.get_page()))
    def test_error_panel_explains_rejected_token_without_leaking(self):
        import urllib.error
        plugin=self.configured(sync_subscriptions=True)
        def fail(*a,**kw):raise urllib.error.HTTPError('http://private/token-secret',401,'secret',{},None)
        plugin._request=fail;plugin.refresh()
        self.assertIn('401',str(plugin.get_page()))
        self.assertNotIn('token-secret',str(plugin.get_page()))

class BusyEventTests(unittest.TestCase):
    def test_busy_events_do_not_keep_replacing_pending_job(self):
        cls=load_plugin();plugin=cls();plugin.init_plugin({'enabled':False,**{key:True for key in cls.FEATURES}})
        calls=[];runtime=sys.modules[cls.__module__];original=runtime.scheduler_sdk.add_plugin_once_job
        runtime.scheduler_sdk.add_plugin_once_job=lambda *a,**kw:(calls.append(kw) or True)
        try:
            plugin._enabled=True
            for _ in range(100):plugin.subscription_changed(types.SimpleNamespace(event_data={}))
            self.assertEqual(len(calls),1)
            self.assertEqual(calls[0]['delay_seconds'],1)
        finally:runtime.scheduler_sdk.add_plugin_once_job=original

class UploadFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.plugin_class=load_plugin()
    def plugin(self):
        plugin=self.plugin_class();plugin.init_plugin({'enabled':True,**{k:(k=='notify_upload') for k in plugin.FEATURES}});return plugin
    def test_completed_legacy_event_without_paths_wakes_only_bound_task(self):
        plugin=self.plugin();plugin.transfer_complete(types.SimpleNamespace(event_data={'mediainfo':{'media_source':'themoviedb','media_id':'42','type':'电影'}}))
        captured=[];plugin._request=lambda body,**kw:(captured.append(body) or {'protocol':1,'accepted':True,'upload_accepted':True,'available':[]})
        plugin.refresh();self.assertTrue(captured[0]['upload_ready']);self.assertEqual(captured[0]['paths'],[]);self.assertNotIn('completed',captured[0]);self.assertEqual(plugin._state['outbox'],{});self.assertTrue(plugin._last_upload_ack)
    def test_explicit_failed_event_never_wakes_upload(self):
        plugin=self.plugin();plugin.transfer_complete(types.SimpleNamespace(event_data={'transferinfo':{'success':False}}));self.assertEqual(plugin._state['outbox'],{})
    def test_failed_ack_preserves_generic_ready_notification(self):
        plugin=self.plugin();plugin._queue_upload_ready();plugin._request=lambda *a,**kw:{'protocol':1,'accepted':True,'upload_accepted':False,'upload_error':'plan','available':[]};plugin.refresh();self.assertIn('upload:scan',plugin._state['outbox'])

class CanonicalDownloadGuardTests(ControlsTests):
    def test_current_v3_final_guard_refreshes_even_recent_inventory(self):
        import time
        plugin=self.configured(strmflow_to_mp=True)
        plugin._last_ok=time.monotonic()
        plugin._subscriptions=lambda:[]
        requests=[]
        def request(body,**kwargs):
            requests.append(body)
            return {'protocol':1,'accepted':True,'available':[{'tmdb_id':42,'media_type':'movie','movie':True,'episodes':{}}]}
        plugin._request=request
        event=types.SimpleNamespace(event_data={'context':{'media_info':{'media_source':'themoviedb','media_id':'42','type':'电影'},'meta_info':{}}})
        plugin.prevent_duplicate_download(event)
        self.assertTrue(event.event_data['cancel'])
        self.assertEqual(requests[0]['lookup'],[{'tmdb_id':42,'media_type':'movie'}])
        self.assertEqual(plugin._state['communications'][-1]['action'],'拦截重复下载')
    def test_communication_history_is_bounded_and_visible(self):
        plugin=self.configured()
        for n in range(105):plugin._record_communication('测试',True,str(n))
        self.assertEqual(len(plugin._state['communications']),100)
        self.assertEqual(plugin._state['communications'][0]['detail'],'5')
        self.assertIn('104',str(plugin.get_page()))

class EventDrivenTests(ControlsTests):
    def test_upload_only_has_no_idle_event_listener(self):
        plugin=self.configured(notify_upload=True)
        jobs=plugin.get_service()
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['trigger'],{'minute':'*/30','second':0})

    def test_reverse_feature_has_event_wait_and_low_frequency_compensation(self):
        plugin=self.configured(strmflow_to_mp=True)
        jobs=plugin.get_service()
        self.assertEqual(len(jobs),2)
        self.assertTrue(any(j['trigger']=={'minute':'*/30','second':0} for j in jobs))
        self.assertTrue(any(j['func']==plugin.wait_for_changes for j in jobs))

    def test_idle_wait_does_not_exchange_subscriptions(self):
        import json
        from unittest.mock import patch
        plugin=self.configured(strmflow_to_mp=True)
        plugin._url='http://example.test';plugin._token='x'*64;plugin._watch_revision='boot-1'
        plugin.refresh=lambda:self.fail('idle wait performed exchange')
        response=types.SimpleNamespace(read=lambda *_:json.dumps({'protocol':1,'revision':'boot-1','changed':False}).encode())
        class Opened:
            def __enter__(self):return response
            def __exit__(self,*_):return False
        opener=types.SimpleNamespace(open=lambda *a,**k:Opened())
        with patch('urllib.request.build_opener',return_value=opener):plugin.wait_for_changes()
        self.assertEqual(plugin._state['communications'],[])

    def test_media_change_triggers_exchange(self):
        import json
        from unittest.mock import patch
        plugin=self.configured(strmflow_to_mp=True)
        plugin._url='http://example.test';plugin._token='x'*64;calls=[]
        plugin.refresh=lambda:calls.append('refresh')
        response=types.SimpleNamespace(read=lambda *_:json.dumps({'protocol':1,'revision':'boot-2','changed':True}).encode())
        class Opened:
            def __enter__(self):return response
            def __exit__(self,*_):return False
        with patch('urllib.request.build_opener',return_value=types.SimpleNamespace(open=lambda *a,**k:Opened())):plugin.wait_for_changes()
        self.assertEqual(calls,['refresh']);self.assertEqual(plugin._watch_revision,'boot-2')
