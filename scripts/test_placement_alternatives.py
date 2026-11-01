"""Allowed AMD and disk alternatives must remain candidates, with strict dimensions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pool_selection as pools
import run_settings


class PlacementAlternativesTests(unittest.TestCase):
    def flavors(self):
        return [{'id':'intel','name':'SL1.4-8192','vcpus':4,'ram':8192,'disk':0,'availability_zones':['ru-3a','ru-3b']},
                {'id':'amd','name':'SL2.4-8192-AMD','vcpus':4,'ram':8192,'disk':0,'availability_zones':['ru-3b']}]

    def config(self):
        return {'pools':['ru-3'],'profiles':{'app':{'vcpus':4,'ram_mb':8192}},'image_name':'Ubuntu','image_checksums':['a'*32],
                'boot_disk_gb':20,'disk_type_patterns':['universal.*','basicssd.*'],
                'flavor_name_patterns':['SL1.{vcpus}-{ram_mb}','SL2.{vcpus}-{ram_mb}-AMD']}

    def test_exact_resources_explicit_families_network_disk_and_no_duplicates(self):
        flavors=self.flavors()
        for overrides in ({'id':'small','ram':4096},{'id':'local','disk':64},
                          {'id':'other','name':'CPU1.4-8192'},{'id':'private','os-flavor-access:is_public':False}):
            flavors.append({**flavors[0],**overrides})
        flavors.append(flavors[0].copy())
        self.assertEqual([f['id'] for f in pools.flavor_choices(flavors,self.config())['app']],['intel','amd'])
        with self.assertRaises(pools.CandidateUnavailable):pools.flavor_choices([],self.config())

    def test_every_allowed_flavor_disk_combination_obeys_zone_bindings(self):
        catalog=[{'type':k,'endpoints':[{'interface':'public','region':'ru-3','url':'https://synthetic.invalid/'+k}]} for k in pools.SERVICES]
        def get(url,**kwargs):
            if 'images?' in url:return {'images':[{'id':'image','name':'Ubuntu','visibility':'public','status':'active','os_distro':'ubuntu','checksum':'a'*32}]},{}
            if url.endswith('/flavors/detail'):return {'flavors':self.flavors()},{}
            if url.endswith('/types'):return {'volume_types':[{'name':name+'.ru-3b','extra_specs':{'RESKEY:availability_zones':'ru-3b'}} for name in ('universal','basicssd')]},{}
            if url.endswith('/v2.0/availability_zones'):return {'availability_zones':[{'name':'ru-3b','state':'available','resource':r} for r in ('network','router')]},{}
            return {'availabilityZoneInfo':[{'zoneName':'ru-3b','zoneState':{'available':True}}]},{}
        with patch.object(pools,'authenticate',return_value=('synthetic',catalog)),patch.object(pools,'request_json',side_effect=get):
            values=list(pools.candidates(self.config(),{}))
        self.assertEqual([(c['volume_type'],c['flavor_ids']['app']) for c in values],
                         [('universal.ru-3b','intel'),('universal.ru-3b','amd'),('basicssd.ru-3b','intel'),('basicssd.ru-3b','amd')])

    def test_defaults_exclude_requested_pools_without_excluding_s3_or_weakening_hosts(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root / '.tools') as folder:
            isolated=Path(folder)
            (isolated / '.env.example').write_bytes((root / '.env.example').read_bytes())
            policy=run_settings.load(isolated,environ={})
        self.assertTrue(policy['require_distinct_hosts'])
        self.assertEqual(policy['excluded_pools'],['ru-7','ru-4','ru-5','ru-6','kz-1','ke-1','gis-1','gis-2','uz-1','uz-2'])
        self.assertIn('SL2.{vcpus}-{ram_mb}-AMD',policy['flavor_name_patterns'])
