"""Ownership and absence proofs exercise the real auditor against synthetic API collections."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

import selectel_staged_audit as audit


class ProviderAuditTests(unittest.TestCase):
    def backend(self,kind='openstack_compute_instance_v2'):
        attrs={'id':'owned','name':'otus-dz07-es1','region':'ru-3','network_id':'parent','os_subnet_id':'os-subnet'}
        entry={'type':kind,'attrs':attrs}
        record={'candidate':{'region':'ru-3','availability_zone':'ru-3b'},'status':'active','lineage':'same',
                'identities':{'one':'owned'},'audit_entries':[entry]}
        backend=SimpleNamespace(owner='owner',credentials={'project_id':'project'},
            journal={'stages':{'es1':record}},desired={'nodes':{'es1':{'role':'es'}}},
            store=SimpleNamespace(entries=lambda name:[entry],state=lambda name:{'lineage':'same'},
                                  identities=lambda name:{'one':'owned'}),global_router=Mock(),region=Mock())
        item={'id':'owned','name':attrs['name'],'tenant_id':'project','project_id':'project',
              'tags':['owner'],'metadata':{'dz07_owner':'owner'},'description':'owner','members':[],
              'status':'ACTIVE','OS-EXT-AZ:availability_zone':'ru-3b','network_id':'parent','os_subnet_id':'os-subnet'}
        return backend,entry,item

    def test_paginated_collections_and_foreign_or_cyclic_links(self):
        cloud=Mock(endpoints={'compute':'https://example.invalid/compute'})
        cloud.get.side_effect=[{'servers':[{'id':'one'}],'servers_links':[{'rel':'next','href':'https://example.invalid/compute/servers?page=2'}]},
                               {'servers':[{'id':'two'}]}]
        self.assertEqual([x['id'] for x in audit.objects(cloud,'compute','/servers','servers')],['one','two'])
        for links in [[{'rel':'next','href':'https://foreign.invalid/compute/servers'}],
                      [{'rel':'next','href':'https://example.invalid/other/servers'}],
                      [{'rel':'next','href':'https://example.invalid/compute/servers?limit=200'}]]:
            cloud.get.side_effect=None;cloud.get.return_value={'servers':[],'servers_links':links}
            with self.assertRaises(ValueError):audit.objects(cloud,'compute','/servers','servers')
        for page in [{'servers':{}},{'servers':[{}]*200}, {'servers':[], 'servers_links':[
            {'rel':'next','href':'https://example.invalid/compute/a'}, {'rel':'next','href':'https://example.invalid/compute/b'}]}]:
            cloud.get.return_value=page
            with self.assertRaises(ValueError):audit.objects(cloud,'compute','/servers','servers')

    def test_partial_collection_uses_direct_get_and_403_is_not_absence(self):
        backend,entry,item=self.backend();cloud=backend.region.return_value
        cloud.get.side_effect=[{'servers':[]},{'server':item}]
        self.assertEqual(audit.inventory(backend,'es1',[entry])[entry['type']]['owned'],item)
        for code in (404,403):
            cloud.get.side_effect=[{'servers':[]},urllib.error.HTTPError('https://example.invalid',code,'synthetic',{},None)]
            if code==404:self.assertFalse(audit.inventory(backend,'es1',[entry])[entry['type']])
            else:
                with self.assertRaises(urllib.error.HTTPError):audit.inventory(backend,'es1',[entry])
        with self.assertRaises(ValueError):audit.inventory(backend,'es1',[{'type':'foreign_type','attrs':{'id':'other'}}])

    def test_ownership_guards_for_every_managed_kind(self):
        kinds=['openstack_compute_instance_v2','openstack_blockstorage_volume_v3',
               'openstack_networking_network_v2','openstack_networking_router_v2',
               'openstack_networking_secgroup_v2','openstack_compute_servergroup_v2',
               'selectel_global_router_router_v1','selectel_global_router_vpc_network_v1']
        for kind in kinds:
            backend,entry,item=self.backend(kind)
            with self.subTest(kind=kind),patch.object(audit,'inventory',return_value={kind:{'owned':item}}):
                audit.assert_stage_owned(backend,'es1',active=True)
                mutations=[('tags',[]) if kind in audit.GLOBAL or 'networking_network' in kind or 'networking_router' in kind
                           else ('members',['foreign']) if 'servergroup' in kind else ('description','foreign') if 'secgroup' in kind
                           else ('metadata',{})]
                for key,value in mutations:
                    original=copy.deepcopy(item);item[key]=value
                    with self.assertRaises(ValueError):audit.assert_stage_owned(backend,'es1')
                    item.clear();item.update(original)

    def test_lineage_identity_project_region_name_and_runtime_state_are_checked(self):
        backend,entry,item=self.backend()
        with patch.object(audit,'inventory',return_value={entry['type']:{'owned':item}}):
            for target,key,value in [(backend.journal['stages']['es1'],'lineage','other'),
                (backend.journal['stages']['es1'],'identities',{'one':'other'}),
                (entry['attrs'],'region','foreign'),(item,'tenant_id','foreign'),(item,'name','foreign')]:
                original=copy.deepcopy(target[key]);target[key]=value
                with self.assertRaises(ValueError):audit.assert_stage_owned(backend,'es1',active=True)
                target[key]=original
            for key,value in [('status','ERROR'),('OS-EXT-AZ:availability_zone','ru-3a')]:
                old=item[key];item[key]=value
                with self.assertRaises(RuntimeError):audit.assert_stage_owned(backend,'es1',active=True)
                item[key]=old

    def test_global_subnet_nullable_project_requires_owned_matching_parent(self):
        kind='selectel_global_router_vpc_subnet_v1';backend,entry,item=self.backend(kind)
        item['project_id']=None
        parent={'id':'parent','tags':['owner'],'project_id':'project'}
        backend.global_router.listing.return_value=[parent]
        with patch.object(audit,'inventory',return_value={kind:{'owned':item}}):
            audit.assert_stage_owned(backend,'es1',active=True)
            parent['project_id']='foreign'
            with self.assertRaises(ValueError):audit.assert_stage_owned(backend,'es1')
            parent['project_id']='project';item['status']='ERROR'
            with self.assertRaises(RuntimeError):audit.assert_stage_owned(backend,'es1',active=True)

    def test_missing_objects_and_deletion_audit_never_assume_success(self):
        backend,entry,item=self.backend();kind=entry['type']
        with patch.object(audit,'inventory',return_value={kind:{}}):
            with self.assertRaises(ValueError):audit.assert_stage_owned(backend,'es1')
            backend.journal['stages']['es1']['status']='deleting'
            audit.assert_stage_owned(backend,'es1')
            audit.assert_stage_absent(backend,'es1')
        with patch.object(audit,'inventory',side_effect=[{kind:{'owned':item}},{kind:{}}]),patch.object(audit.time,'sleep'):
            audit.assert_stage_absent(backend,'es1')
        with patch.object(audit,'inventory',return_value={kind:{'owned':item}}),patch.object(audit.time,'monotonic',side_effect=[0,91]):
            with self.assertRaises(RuntimeError):audit.assert_stage_absent(backend,'es1')
