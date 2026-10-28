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
    module('app.sdk.logging',logger=types.SimpleNamespace(warning=lambda *_:None))
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
        Oper.rows=[];Chain.finished=[];self.plugin=self.plugin_class();self.plugin.init_plugin({'enabled':True,'notify_upload':False})

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

if __name__=='__main__':unittest.main()
