# Copyright 2022 Canonical Ltd
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

package_upgrade = None  # placeholder for module loaded in setUpModule
TO_PATCH = [
    'config_changed',
    'do_openstack_upgrade'
]


def setUpModule():
    # to make sure python loads a mocked version of the module we unload it
    # first.
    if 'package_upgrade' in sys.modules:
        del sys.modules['package_upgrade']

    with patch('nova_compute_hooks.register_configs'):
        global package_upgrade
        package_upgrade = importlib.import_module('package_upgrade')


def tearDownModule():
    # we unload the module since it was mocked, this prevents side effects.
    if 'package_upgrade' in sys.modules:
        del sys.modules['package_upgrade']


@patch('nova_compute_utils.register_configs')
@patch('nova_compute_utils.restart_map')
@patch('charmhelpers.core.hookenv.config')
@patch('charmhelpers.contrib.openstack.context.HostInfoContext')
class TestNovaComputeUpgradeActions(CharmTestCase):

    def setUp(self):
        super(TestNovaComputeUpgradeActions, self).setUp(package_upgrade,
                                                         TO_PATCH)

    @patch('package_upgrade.relation_ids')
    @patch('charmhelpers.contrib.openstack.utils.action_set')
    @patch('charmhelpers.contrib.openstack.utils.openstack_upgrade_available')
    @patch('charmhelpers.contrib.openstack.utils.juju_log')
    def test_package_upgrade_success(self, log, upgrade_avail,
                                     action_set, relation_ids, *args):
        upgrade_avail.return_value = False
        package_upgrade.package_upgrade()
        self.assertTrue(self.do_openstack_upgrade.called)

    @patch('package_upgrade.relation_ids')
    @patch('charmhelpers.contrib.openstack.utils.action_set')
    @patch('charmhelpers.contrib.openstack.utils.openstack_upgrade_available')
    @patch('charmhelpers.contrib.openstack.utils.juju_log')
    def test_openstack_upgrade_failure(self, log, upgrade_avail,
                                       action_set, relation_ids, *args):
        upgrade_avail.return_value = True
        package_upgrade.package_upgrade()
        self.assertFalse(self.do_openstack_upgrade.called)
