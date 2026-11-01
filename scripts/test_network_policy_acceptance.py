"""Run the real post-create network audit against synthetic Neutron/Nova replies."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from selectel_staged_audit import verify_networks


def fixture(distinct=True):
    nodes={name:{'id':name,'port_id':'p-'+name,'volume_id':'v-'+name,'region':'ru-9',
                 'zone':'ru-9a','roles':['app'] if name=='app' else ['es'],
                 'private_ip':'10.77.0.'+str(host)}
           for name,host in [('app',10),('es1',31),('es2',32),('es3',33)]}
    inputs={'name':'otus-dz07-net','cidr':'10.77.0.0/24','peer_cidrs':['10.78.0.0/24'],
            'global_gateway_host':2,'admin_cidrs':['192.0.2.1/32'],'kibana_port':5601,
            'network_zone':'ru-9a','router_zone':'ru-9a'}
    network={'network_id':'network','subnet_id':'subnet','security_group_id':'group',
             'server_group_id':'placement','cidr':inputs['cidr']}
    rules=[{'direction':'ingress','ethertype':'IPv4','remote_ip_prefix':cidr}
           for cidr in [inputs['cidr'],*inputs['peer_cidrs']]]
    rules += [{'direction':'ingress','ethertype':'IPv4','protocol':'tcp','port_range_min':port,
               'port_range_max':port,'remote_ip_prefix':cidr}
              for port,cidr in [(80,'0.0.0.0/0'),(22,'192.0.2.1/32'),(5601,'192.0.2.1/32')]]
    rules += [{'direction':'egress','ethertype':'IPv4'}]
    data={('network','/v2.0/networks/network'):{'network':{'shared':False,'router:external':False,
                'port_security_enabled':True,'availability_zone_hints':['ru-9a'],'availability_zones':['ru-9a']}},
          ('network','/v2.0/subnets/subnet'):{'subnet':{'host_routes':[{'destination':'10.78.0.0/24','nexthop':'10.77.0.2'}]}},
          ('network','/v2.0/routers'):{'routers':[{'name':inputs['name'],'availability_zone_hints':['ru-9a'],'availability_zones':['ru-9a']}]},
          ('network','/v2.0/security-groups/group'):{'security_group':{'security_group_rules':rules}},
          ('network','/v2.0/floatingips'):{'floatingips':[]},
          ('compute','/os-server-groups'):{'server_groups':[{'id':'placement','policies':['anti-affinity'],
                    'members':['es1','es2','es3'] if distinct else []}]}}
    for name,node in nodes.items():
        data[('compute','/servers/'+name)]={'server':{'hostId':'host-'+name}}
        data[('network','/v2.0/ports/p-'+name)]={'port':{'port_security_enabled':True,
            'security_groups':['group'],'network_id':'network','fixed_ips':[{'ip_address':node['private_ip']}]}}
    pages={}
    def get(service,path):
        base,_,query=path.partition('?')
        marker='2' if 'marker=2' in query else '1'
        return copy.deepcopy(pages.get((service,base,marker),data[(service,base)]))
    cloud=SimpleNamespace(get=Mock(side_effect=get),endpoints={s:'https://example.invalid' for s in ('network','compute')})
    backend=SimpleNamespace(result=Mock(return_value=SimpleNamespace(nodes=nodes)),region=Mock(return_value=cloud),
        journal={'require_distinct_hosts':distinct,'stages':{'network-ru-9':{'status':'active',
            'candidate':{'region':'ru-9'},'inputs':inputs,'output':network}}},
        store=SimpleNamespace(entries=Mock(return_value=[])),api=SimpleNamespace(PREFIX='otus-dz07'),
        credentials={'project_id':'synthetic'})
    return backend,data,pages


class NetworkPolicyAcceptance(unittest.TestCase):
    def test_complete_policy_and_diagnostic_mode_are_distinct_contracts(self):
        backend,data,_=fixture()
        result=verify_networks(backend)
        self.assertTrue(result['global_routes_confirmed']);self.assertEqual(result['es_distinct_hosts'],3)
        backend,data,_=fixture(False)
        for name in ('es1','es2','es3'):data[('compute','/servers/'+name)]['server'].pop('hostId')
        result=verify_networks(backend)
        self.assertFalse(result['physical_host_separation_verified']);self.assertIsNone(result['es_distinct_hosts'])

    def test_each_private_port_property_is_mandatory(self):
        for change in ({'port_security_enabled':False},{'security_groups':['foreign']},
                       {'network_id':'foreign'},{'fixed_ips':[]},{'fixed_ips':[{'ip_address':'10.77.0.99'}]}):
            backend,data,_=fixture();data[('network','/v2.0/ports/p-es1')]['port'].update(change)
            with self.subTest(change=change),self.assertRaisesRegex(RuntimeError,'port filtering'):verify_networks(backend)

    def test_internal_public_address_is_rejected_even_on_a_later_page(self):
        for paginated in (False,True):
            backend,data,pages=fixture()
            floating={'floatingips':[{'id':'public','port_id':'p-es1'}]}
            if paginated:
                pages[('network','/v2.0/floatingips','1')]={'floatingips':[],
                    'floatingips_links':[{'rel':'next','href':'https://example.invalid/v2.0/floatingips?marker=2'}]}
                pages[('network','/v2.0/floatingips','2')]=floating
            else:data[('network','/v2.0/floatingips')]=floating
            with self.subTest(paginated=paginated),self.assertRaisesRegex(RuntimeError,'public address'):verify_networks(backend)

    def test_missing_or_weak_placement_group_is_rejected(self):
        for groups in ([],[{'id':'placement','policies':['soft-anti-affinity']}],[{'id':'foreign'}]):
            backend,data,_=fixture();data[('compute','/os-server-groups')]['server_groups']=groups
            with self.subTest(groups=groups),self.assertRaisesRegex(RuntimeError,'placement group'):verify_networks(backend)

    def test_separation_requires_membership_host_ids_and_three_distinct_hosts(self):
        for mode in ('missing-member','missing-host','same-host'):
            backend,data,_=fixture()
            if mode=='missing-member':data[('compute','/os-server-groups')]['server_groups'][0]['members']=[]
            if mode=='missing-host':data[('compute','/servers/es1')]['server'].pop('hostId')
            if mode=='same-host':data[('compute','/servers/es2')]['server']['hostId']='host-es1'
            with self.subTest(mode=mode),self.assertRaises(RuntimeError):verify_networks(backend)
        backend,data,_=fixture(False)
        data[('compute','/os-server-groups')]['server_groups'][0]['members']=['es1']
        with self.assertRaisesRegex(RuntimeError,'unexpectedly belongs'):verify_networks(backend)

    def test_shared_external_or_unfiltered_network_is_rejected(self):
        for field in ('shared','router:external','port_security_enabled'):
            backend,data,_=fixture();data[('network','/v2.0/networks/network')]['network'][field]=field!='port_security_enabled'
            with self.subTest(field=field),self.assertRaisesRegex(RuntimeError,'isolation'):verify_networks(backend)

    def test_route_targets_domains_and_exact_ingress_policy_are_checked(self):
        for mode in ('route','network-hint','network-actual','router-hint','router-actual','extra-ingress','missing-ingress'):
            backend,data,_=fixture()
            if mode=='route':data[('network','/v2.0/subnets/subnet')]['subnet']['host_routes'][0]['nexthop']='10.77.0.9'
            if mode.startswith('network-'):
                field='availability_zone_hints' if mode.endswith('hint') else 'availability_zones'
                data[('network','/v2.0/networks/network')]['network'][field]=['ru-3a']
            if mode.startswith('router-'):
                field='availability_zone_hints' if mode.endswith('hint') else 'availability_zones'
                data[('network','/v2.0/routers')]['routers'][0][field]=[]
            rules=data[('network','/v2.0/security-groups/group')]['security_group']['security_group_rules']
            if mode=='extra-ingress':rules.append({'direction':'ingress','ethertype':'IPv4','remote_ip_prefix':'0.0.0.0/0'})
            if mode=='missing-ingress':rules.pop(0)
            with self.subTest(mode=mode),self.assertRaises(RuntimeError):verify_networks(backend)

    def test_later_router_and_placement_pages_are_not_false_absence(self):
        backend,data,pages=fixture()
        for service,path,key in [('network','/v2.0/routers','routers'),('compute','/os-server-groups','server_groups')]:
            pages[(service,path,'1')]={key:[],key+'_links':[{'rel':'next','href':'https://example.invalid'+path+'?marker=2'}]}
            pages[(service,path,'2')]=data[(service,path)]
        self.assertTrue(verify_networks(backend)['success'])
