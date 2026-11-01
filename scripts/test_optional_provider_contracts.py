"""Real provider constructor supports saved optional topology and cleanup independently."""
import copy
import unittest
from unittest.mock import patch

import selectel_staged
from selectel_adapter import infrastructure_result
import test_selectel_retained_contracts as fixtures


class OptionalProviderContracts(unittest.TestCase):
    def test_cleanup_uses_saved_five_node_topology_without_new_optional_settings(self):
        with patch('privacy._values', set()):
            helper = fixtures.RetainedSelectelContracts()
            helper.setUp()
            self.addCleanup(helper.doCleanups)
            backend = helper.backend
            base = copy.deepcopy(backend.desired)
            backend.journal['topology'] = copy.deepcopy(base)
            backend.journal['topology']['nodes']['es4'] = {'host':34,'role':'es'}
            backend.journal['optional_es4'] = {'base_topology':base,'requested_node':'es4'}
            backend.save()
            (helper.root / 'config/optional-elasticsearch.json').unlink()
            cleanup = selectel_staged.Backend(helper.api, cleanup=True)
            self.assertIn('es4', cleanup.desired['nodes'])
            self.assertEqual(cleanup.journal['deployment_id'], backend.journal['deployment_id'])

    def test_provider_contract_retains_explicit_roles_instead_of_inferring_from_names(self):
        output = {'server_ids':{'app':'app-id','storage':'data-id'},
            'node_roles':{'app':'app','storage':'es'}, 'etcd_members':[],
            'private_ips':{'app':'10.77.0.10','storage':'10.77.0.34'},
            'volume_ids':{'app':'app-disk','storage':'data-disk'},
            'port_ids':{'app':'app-port','storage':'data-port'},
            'app_public_ip':'8.8.8.8','subnet_cidr':'10.77.0.0/24'}
        result = infrastructure_result(output, {'project_id':'synthetic','region':'ru-9'}, 'otus-dz07', 'ru-9a')
        self.assertEqual(result.nodes['storage']['roles'], ['es'])
