"""Contract-helper tests, independent of an installed MoviePilot instance."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
spec=importlib.util.spec_from_file_location('strmflow_bridge_helpers',Path(__file__).parents[3]/'plugins.v3/strmflowbridge/bridge.py')
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)

class BridgeTests(unittest.TestCase):
    def context(self,season=1,first=1,last=3):
        return SimpleNamespace(mediainfo={'media_source':'tmdb','media_id':'42','type':'电视剧'},meta={'begin_season':season,'begin_episode':first,'end_episode':last},allowed_episodes=None)

    def test_partial_pack_keeps_missing_episode(self):
        context=self.context();got=bridge.remaining_context(context,{(42,'tv'):{'episodes':{'1':[2]}}})
        self.assertEqual(got.allowed_episodes,{1,3});self.assertIsNone(context.allowed_episodes)
        self.assertIsNone(bridge.remaining_context(self.context(first=2,last=2),{(42,'tv'):{'episodes':{'1':[2]}}}))

    def test_seasons_and_types_do_not_collide(self):
        context=self.context(season=2);self.assertIs(bridge.remaining_context(context,{(42,'tv'):{'episodes':{'1':[1,2,3]}}}),context)
        self.assertIsNone(bridge.identity({'media_source':'douban','media_id':'42','type':'电影'}))
        film={'mediainfo':{'media_source':'tmdb','media_id':'42','type':'电影'}}
        self.assertIs(bridge.remaining_context(film,{(42,'tv'):{'episodes':{'1':[1]}}}),film)

    def test_failed_transfer_and_source_paths_never_upload(self):
        self.assertEqual(bridge.transfer_paths({'success':False,'file_list_new':['/media/Movie.mkv']}),[])
        self.assertEqual(bridge.transfer_paths({'success':True,'file_list':['/download/source.mkv'],'fileitem':{'path':'/download/source.mkv'}}),[])
        self.assertEqual(bridge.transfer_paths({'success':True,'file_list_new':['/media/Movie.mkv','/media/Movie.nfo']}),['/media/Movie.mkv'])

    def test_existing_allowed_set_is_preserved(self):
        context=self.context();context.allowed_episodes={2,3}
        self.assertEqual(bridge.remaining_context(context,{(42,'tv'):{'episodes':{'1':[2]}}}).allowed_episodes,{3})


class CustomGroupTests(unittest.TestCase):
    def test_custom_group_context_is_not_filtered(self):
        context={'mediainfo':{'media_source':'tmdb','media_id':'42','type':'电视剧','episode_group':'group-a'},'meta':{'begin_season':1,'begin_episode':1}}
        self.assertIs(bridge.remaining_context(context,{(42,'tv'):{'episodes':{'1':[1]}}}),context)

if __name__=='__main__':unittest.main()
