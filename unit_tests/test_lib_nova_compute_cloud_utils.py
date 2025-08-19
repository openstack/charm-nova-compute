# Copyright 2020 Canonical Ltd
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
import configparser

from unittest import TestCase
from unittest.mock import MagicMock, patch

import nova_compute.cloud_utils as cloud_utils


class NovaServiceMock:

    def __init__(self, id, host, binary):
        self.id = id
        self.host = host
        self.binary = binary


class NovaHypervisorMock:

    def __init__(self, hostname, running_vms=0):
        self.hypervisor_hostname = hostname
        self.running_vms = running_vms


class TestCloudUtilsNovaCfg(TestCase):
    def test_nova_cfg_allows_duplicate_keys(self):
        _conf = """
        [pci]
        alias = first
        alias = second
        """

        def fake_read(self, filename, encoding=None):
            self.read_string(_conf)
            return ['dummy']

        with patch.object(configparser.ConfigParser, "read", new=fake_read):
            cfg = cloud_utils._nova_cfg()
            self.assertEqual(cfg["pci"]["alias"], "second")


class TestCloudUtils(TestCase):

    def __init__(self, methodName='runTest'):
        super(TestCloudUtils, self).__init__(methodName=methodName)

        self.nova_client = MagicMock()
        self.nova_cfg = {}

        nova_services = MagicMock()
        nova_services.list.return_value = []
        self.nova_client.services = nova_services

        nova_hypervisors = MagicMock()
        nova_hypervisors.list.return_value = []
        self.nova_client.hypervisors = nova_hypervisors

        self.neutron_client = MagicMock()

        self.unit_hostname = 'nova-compute-0'

    def setUp(self):
        to_patch = [
            'loading',
            'log',
            'nova_client_',
            '_nova_cfg',
        ]
        for object_ in to_patch:
            mock_ = patch.object(cloud_utils, object_, MagicMock())
            mock_.start()
            self.addCleanup(mock_.stop)

        cloud_utils._nova_cfg.return_value = self.nova_cfg
        cloud_utils.nova_client.return_value = self.nova_client

    def tearDown(self):
        # Cleanup any changes made to the self.nova_cfg
        self.nova_cfg = {}

    def test_os_credentials_content(self):
        """Test that function '_os_credentials' returns credentials
        in expected format"""
        credentials = cloud_utils._os_credentials()
        expected_keys = [
            'username',
            'password',
            'auth_url',
            'project_name',
            'project_domain_name',
            'user_domain_name',
        ]

        for key in expected_keys:
            self.assertIn(key, credentials.keys())

    def test_nova_service_id(self):
        """Test that `nova_service_id` returns expected nova service ID."""
        expected_id = 0
        other_host_id = 1
        other_host_name = "other-nova-compute-1"

        self.nova_client.services.list.return_value = [
            NovaServiceMock(expected_id, self.unit_hostname, 'nova-compute'),
            NovaServiceMock(other_host_id, other_host_name, 'nova-compute'),
        ]

        with patch.object(cloud_utils, "service_hostname",
                          return_value=self.unit_hostname):
            nova_id = cloud_utils.nova_service_id(self.nova_client)

        self.assertEqual(nova_id, expected_id)

    def test_nova_service_id_not_present(self):
        """Test that function 'nova_service_id' raises expected exception if
        current unit is not registered in 'nova-cloud-controller'"""
        nova_client = MagicMock()
        nova_services = MagicMock()
        nova_services.list.return_value = []
        nova_client.services = nova_services
        cloud_utils.nova_client.return_value = nova_client

        with patch.object(cloud_utils, "service_hostname",
                          return_value=self.unit_hostname):
            self.assertRaises(RuntimeError, cloud_utils.nova_service_id,
                              nova_client)

    def test_nova_service_id_multiple_services(self):
        """Test that function 'nova_service_id' will log warning and return
        first ID in the event that multiple nova-compute services are present
        on the same host"""
        first_id = 0
        second_id = 1
        warning_msg = 'Host "{}" has more than 1 nova-compute service ' \
                      'registered. Selecting one ID ' \
                      'randomly.'.format(self.unit_hostname)

        self.nova_client.services.list.return_value = [
            NovaServiceMock(first_id, self.unit_hostname, 'nova-compute'),
            NovaServiceMock(second_id, self.unit_hostname, 'nova-compute'),
        ]

        with patch.object(cloud_utils, "service_hostname",
                          return_value=self.unit_hostname):
            service_id = cloud_utils.nova_service_id(self.nova_client)

        self.assertEqual(service_id, first_id)
        cloud_utils.log.assert_called_with(warning_msg, cloud_utils.WARNING)

    def test_service_hostname_from_config(self):
        """Test that `service_hostname` func prioritizes hostname in config.

        In case that nova config contains "host" key in "DEFAULT" section,
        it should be used instead of calling `socket.gethostname()`.
        """
        expected_hostname = "nova-compute-0"
        self.nova_cfg["DEFAULT"] = {"host": expected_hostname}

        with patch.object(cloud_utils.socket, "gethostname",
                          return_value="foo"):
            self.assertEqual(cloud_utils.service_hostname(), expected_hostname)

    def test_service_hostname_from_gethostname(self):
        """Test that `service_hostname` falls back to socket.gethostname.

        In case that nova config does not contain "host" key in the "DEFAULT"
        section, this function should fall back to calling
        `socket.gethostname()`
        """
        expected_hostname = "nova-compute-0"
        self.nova_cfg["DEFAULT"] = {}

        with patch.object(cloud_utils.socket, "gethostname",
                          return_value=expected_hostname):
            self.assertEqual(cloud_utils.service_hostname(), expected_hostname)

    def test_running_vms(self):
        """Test that `running_vms` returns correct number of VMs."""
        expected_vms = 3
        expected_hostname = self.unit_hostname
        hostname_info = {"host_fqdn": expected_hostname}
        self.nova_client.hypervisors.list.return_value = [
            NovaHypervisorMock(expected_hostname, expected_vms),
            NovaHypervisorMock("other-nova-compute-0", 0)
        ]

        with patch.object(cloud_utils.HostInfoContext, '__call__',
                          return_value=hostname_info):
            vm_count = cloud_utils.running_vms(self.nova_client)

        self.assertEqual(vm_count, expected_vms)

    def test_running_vms_not_found(self):
        """Test error raised if the hypervisor is not find in the nova list."""
        hostname_info = {"host_fqdn": self.unit_hostname}
        expected_error = ("Nova compute node '{}' not found in the list of "
                          "hypervisors. Is the unit already removed from the"
                          " cloud?").format(self.unit_hostname)

        with patch.object(cloud_utils.HostInfoContext, '__call__',
                          return_value=hostname_info):
            with self.assertRaises(RuntimeError) as exc:
                cloud_utils.running_vms(self.nova_client)

        self.assertEqual(str(exc.exception), expected_error)
