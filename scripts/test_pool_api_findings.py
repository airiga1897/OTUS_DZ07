"""Synthetic regressions matching observed API shapes; no API writes."""
import unittest
from unittest.mock import patch

import pool_selection as pools
from selectel_staged_capacity import require_named_quota


class CatalogFindings(unittest.TestCase):
    def test_missing_api_reports_the_actual_service(self):
        catalog = [{'type': k, 'endpoints': [{'interface':'public','region':'ru-4','url':'https://example.invalid'}]}
                   for k in ('compute','image','network')]
        with self.assertRaisesRegex(pools.CandidateUnavailable, 'volumev3'): pools.endpoints_for(catalog, 'ru-4')

    def test_pool_network_domain_does_not_have_to_equal_compute_segment(self):
        for zone in ('ru-1b', 'ru-1c'):
            self.assertEqual(pools.network_placement({}, 'ru-1', zone, {'network':{'ru-1a'},'router':{'ru-1a'}}),
                {'network_availability_zone':'ru-1a','router_availability_zone':'ru-1a'})

    def test_multi_zone_pool_keeps_exact_router_and_shared_network(self):
        result = pools.network_placement({}, 'ru-6', 'ru-6b', {'network':{'ru-6a'},'router':{'ru-6a','ru-6b','ru-6c','ru-6b-paas'}})
        self.assertEqual(result, {'network_availability_zone':'ru-6a','router_availability_zone':'ru-6b'})

    def test_ambiguous_foreign_and_paas_domains_are_not_guessed(self):
        for names in ({'ru-1a','ru-1b'}, {'ru-3a'}, {'ru-1a-paas'}, set()):
            with self.subTest(names=names), self.assertRaises(pools.CandidateUnavailable):
                pools.network_placement({}, 'ru-1', 'ru-1c', {'network':names,'router':{'ru-1a'}})
        result = pools.network_placement({}, 'ru-1', 'ru-1c', {'network':{'ru-1a','ru-1c'},'router':{'ru-1a'}})
        self.assertEqual(result['network_availability_zone'], 'ru-1c')

    def test_unapproved_universal2_is_reported_but_not_selected(self):
        types = [{'name':'universal2.ru-6a','extra_specs':{'RESKEY:availability_zones':'ru-6a'}}]
        with self.assertRaisesRegex(pools.CandidateUnavailable, 'universal2'):
            pools.disks_for_zone(types, ['universal.*','basicssd.*'], 'ru-6a', None)

    def test_image_checksum_diagnostic_does_not_accept_another_image(self):
        config = {'image_name':'Ubuntu','image_checksums':['a'*32],'boot_disk_gb':20,'profiles':{'app':{'ram_mb':8192}}}
        images = [{'name':'Ubuntu','visibility':'public','status':'active','os_distro':'ubuntu','checksum':'b'*32}]
        self.assertEqual(pools.compatible_images(images, config), [])
        self.assertIn('checksum', pools.image_skip_reason(images, config))
        self.assertIn('name (absent)', pools.image_skip_reason([], config))
        images[0]['min_disk'] = 100; images[0]['min_ram'] = 16384; images[0]['architecture'] = 'arm64'
        self.assertEqual(set(pools.image_rejections(images[0], config)), {'checksum','min_disk','min_ram','architecture'})

    def test_quota_diagnostics_name_unknown_or_insufficient_resource(self):
        for item in (None, {'limit':0,'in_use':0}):
            with self.subTest(item=item), self.assertRaisesRegex(pools.CandidateUnavailable, 'app: compute.ram'):
                require_named_quota(item, 8192, 'app: compute.ram')
