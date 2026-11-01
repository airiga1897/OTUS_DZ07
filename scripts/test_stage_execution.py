"""Exercise real Terraform stage sequencing and guards with synthetic CLI results."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from stage_store import StageStore, resources

ROOT=Path(__file__).resolve().parents[1]


class StageExecutionTests(unittest.TestCase):
    def store(self,root,before,after,plan):
        api=SimpleNamespace(ROOT=root)
        store=StageStore(api,{})
        store.identities=Mock(side_effect=[before,after])
        def cli(name,operation,*args):
            if operation=='show':return json.dumps(plan)
            if operation=='output':return '{"id":"new"}'
            return ''
        store.tf=Mock(side_effect=cli)
        return store

    def test_safe_plan_is_applied_once_and_id_changes_after_apply_are_rejected(self):
        before={'existing':'old'}
        plan={'resource_changes':[{'address':'existing','change':{'actions':['no-op'],'before':{'id':'old'}}},
                                  {'address':'new','change':{'actions':['create']}}]}
        with tempfile.TemporaryDirectory(dir=ROOT/'.tools') as d:
            store=self.store(Path(d),before,{**before,'new':'new-id'},plan)
            self.assertEqual(store.apply('es1'),{'id':'new'})
            self.assertEqual([c.args[1] for c in store.tf.call_args_list],['init','plan','show','apply','output'])
            store=self.store(Path(d),before,{'existing':'replacement'},plan)
            with self.assertRaisesRegex(RuntimeError,'identity changed'):store.apply('es1')

    def test_repeat_and_extra_validation_block_changes_before_apply(self):
        plan={'resource_changes':[{'address':'new','change':{'actions':['create']}}]}
        with tempfile.TemporaryDirectory(dir=ROOT/'.tools') as d:
            store=self.store(Path(d),{}, {},plan)
            with self.assertRaises(ValueError):store.apply('es1',unchanged=True)
            self.assertNotIn('apply',[c.args[1] for c in store.tf.call_args_list])
            store=self.store(Path(d),{}, {},plan)
            guard=Mock(side_effect=ValueError('unsafe provider change'))
            with self.assertRaises(ValueError):store.apply('es1',validate_extra=guard)
            self.assertNotIn('apply',[c.args[1] for c in store.tf.call_args_list])

    def test_destroy_requires_empty_final_state_and_nested_resource_walk(self):
        plan={'resource_changes':[{'address':'one','change':{'actions':['delete'],'before':{'id':'id'}}}]}
        with tempfile.TemporaryDirectory(dir=ROOT/'.tools') as d:
            self.assertEqual(self.store(Path(d),{'one':'id'},{},plan).apply('es1',destroy=True),{})
            with self.assertRaisesRegex(RuntimeError,'not empty'):
                self.store(Path(d),{'one':'id'},{'one':'id'},plan).apply('es1',destroy=True)
        tree={'resources':[{'mode':'data'}],'child_modules':[{'resources':[{'mode':'managed','address':'owned'}]}]}
        self.assertEqual(resources(tree),[{'mode':'managed','address':'owned'}])
