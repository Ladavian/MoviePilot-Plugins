import datetime as dt
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('retention',Path(__file__).parents[1]/'retain-builds.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)

class RetentionTests(unittest.TestCase):
    def version(self,index,suffix=''):
        sha=f'{index:040x}';return {'id':index*10+len(suffix),'name':str(index)+suffix,'created_at':f'2026-01-{index:02d}T00:00:00Z','metadata':{'container':{'tags':['sha-'+sha+suffix]}}}
    def test_retains_five_complete_architecture_groups(self):
        versions=[self.version(i,s) for i in range(1,8) for s in ('','-amd64','-arm64')]
        roots=mod.retained_roots(versions,5);self.assertEqual({r['id'] for r in roots},{30,40,50,60,70})
        removed=mod.image_candidates(versions,roots,set(),dt.datetime(2026,2,1,tzinfo=dt.timezone.utc))
        self.assertEqual(len(removed),6);self.assertTrue(all(v['id']<30 for v in removed))
    def test_latest_shared_children_and_unknown_tags_are_safe(self):
        versions=[self.version(i) for i in range(1,8)];versions[0]['metadata']['container']['tags'].append('latest')
        manual=self.version(1,'-amd64');manual['metadata']['container']['tags']=['manual-pinned'];versions.append(manual)
        roots=mod.retained_roots(versions,5)
        removed=mod.image_candidates(versions,roots,{'2'},dt.datetime(2026,2,1,tzinfo=dt.timezone.utc))
        self.assertEqual(removed,[])
    def test_keeps_three_runs_and_never_deletes_active(self):
        runs=[{'id':i,'created_at':f'2026-01-{i:02d}','status':'completed'} for i in range(1,7)]
        runs[0]['status']='in_progress'
        self.assertEqual([r['id'] for r in mod.old_runs(runs,3,'2')],[3])

if __name__=='__main__':unittest.main()
