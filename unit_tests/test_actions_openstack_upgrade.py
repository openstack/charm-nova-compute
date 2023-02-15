# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import importlib
import sys

from unittest.mock import patch

import nova_compute_utils as utils  # noqa


from test_utils import (
    CharmTestCase
)


openstack_upgrade = None  # placeholder for module loaded in setUpModule
TO_PATCH = [
    'config_changed',
    'do_openstack_upgrade'
]


def setUpModule():
    # to make sure python loads a mocked version of the module we unload it
    # first.
    if 'openstack_upgrade' in sys.modules:
        del sys.modules['openstack_upgrade']

    with patch('nova_compute_utils.restart_map'):
        with patch('nova_compute_utils.register_configs'):
            global openstack_upgrade
            openstack_upgrade = importlib.import_module('openstack_upgrade')


def tearDownModule():
    # we unload the module since it was mocked, this prevents side effects.
    if 'openstack_upgrade' in sys.modules:
        del sys.modules['openstack_upgrade']


@patch('nova_compute_utils.register_configs')
@patch('nova_compute_utils.restart_map')
@patch('charmhelpers.core.hookenv.config')
@patch('charmhelpers.contrib.openstack.context.HostInfoContext')
class TestNovaComputeUpgradeActions(CharmTestCase):

    def setUp(self):
        super(TestNovaComputeUpgradeActions, self).setUp(openstack_upgrade,
                                                         TO_PATCH)

    @patch('openstack_upgrade.nova_vgpu_joined')
    @patch('openstack_upgrade.nova_ceilometer_joined')
    @patch('openstack_upgrade.neutron_plugin_joined')
    @patch('openstack_upgrade.relation_ids')
    @patch('charmhelpers.contrib.openstack.utils.config')
    @patch('charmhelpers.contrib.openstack.utils.action_set')
    @patch('charmhelpers.contrib.openstack.utils.openstack_upgrade_available')
    @patch('charmhelpers.contrib.openstack.utils.juju_log')
    def test_openstack_upgrade_true(self, log, upgrade_avail,
                                    action_set, config, relation_ids,
                                    neutron_plugin_joined,
                                    nova_ceilometer_joined, nova_vgpu_joined,
                                    *args):

        upgrade_avail.return_value = True
        config.return_value = True

        def fake_relation_ids(thing):
            return {'neutron-plugin': ['1'],
                    'nova-ceilometer': ['2'],
                    'nova-vgpu': ['3']}[thing]

        relation_ids.side_effect = fake_relation_ids

        openstack_upgrade.openstack_upgrade()

        self.assertTrue(self.do_openstack_upgrade.called)
        self.assertTrue(self.config_changed.called)
        neutron_plugin_joined.assert_called_once_with("1", remote_restart=True)
        nova_ceilometer_joined.assert_called_once_with(
            "2", remote_restart=True)
        nova_vgpu_joined.assert_called_once_with(
            "3", remote_restart=True)

    @patch('charmhelpers.contrib.openstack.utils.config')
    @patch('charmhelpers.contrib.openstack.utils.action_set')
    @patch('charmhelpers.contrib.openstack.utils.openstack_upgrade_available')
    @patch('charmhelpers.contrib.openstack.utils.juju_log')
    def test_openstack_upgrade_false(self, log, upgrade_avail,
                                     action_set, config, *args):

        upgrade_avail.return_value = True
        config.return_value = False

        openstack_upgrade.openstack_upgrade()

        self.assertFalse(self.do_openstack_upgrade.called)
        self.assertFalse(self.config_changed.called)
