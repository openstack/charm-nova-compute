#!/usr/bin/env python3
#
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

import platform
import sys
import uuid
import yaml
import os


import charmhelpers.core.unitdata as unitdata

from charmhelpers.core.hookenv import (
    Hooks,
    config,
    is_relation_made,
    log,
    ERROR,
    relation_ids,
    remote_service_name,
    related_units,
    relation_get,
    relation_set,
    service_name,
    UnregisteredHookError,
    status_set,
)
from charmhelpers.core.templating import (
    render
)
from charmhelpers.core.host import (
    service_restart,
)
from charmhelpers.fetch import (
    apt_install,
    apt_purge,
    apt_update,
    filter_installed_packages,
)

from charmhelpers.contrib.openstack.utils import (
    config_value_changed,
    configure_installation_source,
    git_install_requested,
    openstack_upgrade_available,
    os_requires_version,
    is_unit_paused_set,
    pausable_restart_on_change as restart_on_change,
)

from charmhelpers.contrib.storage.linux.ceph import (
    ensure_ceph_keyring,
    CephBrokerRq,
    delete_keyring,
    send_request_if_needed,
    is_request_complete,
    is_broker_action_done,
    mark_broker_action_done,
)
from charmhelpers.payload.execd import execd_preinstall
from nova_compute_utils import (
    create_libvirt_secret,
    determine_packages,
    git_install,
    import_authorized_keys,
    import_keystone_ca_cert,
    initialize_ssh_keys,
    migration_enabled,
    do_openstack_upgrade,
    public_ssh_key,
    restart_map,
    services,
    register_configs,
    NOVA_CONF,
    ceph_config_file, CEPH_SECRET,
    CEPH_BACKEND_SECRET,
    enable_shell, disable_shell,
    configure_lxd,
    fix_path_ownership,
    get_topics,
    assert_charm_supports_ipv6,
    install_hugepages,
    get_hugepage_number,
    assess_status,
    set_ppc64_cpu_smt_state,
    destroy_libvirt_network,
    network_manager,
    libvirt_daemon,
    LIBVIRT_TYPES,
)

from charmhelpers.contrib.network.ip import (
    get_relation_ip,
)

from charmhelpers.core.unitdata import kv

from nova_compute_context import (
    nova_metadata_requirement,
    CEPH_SECRET_UUID,
    assert_libvirt_rbd_imagebackend_allowed,
    NovaAPIAppArmorContext,
    NovaComputeAppArmorContext,
    NovaNetworkAppArmorContext,
)
from charmhelpers.contrib.charmsupport import nrpe
from charmhelpers.core.sysctl import create as create_sysctl
from charmhelpers.contrib.hardening.harden import harden

from socket import gethostname

hooks = Hooks()
CONFIGS = register_configs()
MIGRATION_AUTH_TYPES = ["ssh"]


@hooks.hook('install.real')
@harden()
def install():
    status_set('maintenance', 'Executing pre-install')
    execd_preinstall()
    configure_installation_source(config('openstack-origin'))

    status_set('maintenance', 'Installing apt packages')
    apt_update()
    apt_install(determine_packages(), fatal=True)

    status_set('maintenance', 'Git install')
    git_install(config('openstack-origin-git'))


@hooks.hook('config-changed')
@restart_on_change(restart_map())
@harden()
def config_changed():
    if config('prefer-ipv6'):
        status_set('maintenance', 'configuring ipv6')
        assert_charm_supports_ipv6()

    if (migration_enabled() and
            config('migration-auth-type') not in MIGRATION_AUTH_TYPES):
        message = ("Invalid migration-auth-type")
        status_set('blocked', message)
        raise Exception(message)
    global CONFIGS
    send_remote_restart = False
    if git_install_requested():
        if config_value_changed('openstack-origin-git'):
            status_set('maintenance', 'Running Git install')
            git_install(config('openstack-origin-git'))
    elif not config('action-managed-upgrade'):
        if openstack_upgrade_available('nova-common'):
            status_set('maintenance', 'Running openstack upgrade')
            do_openstack_upgrade(CONFIGS)
            send_remote_restart = True

    sysctl_settings = config('sysctl')
    if sysctl_settings:
        sysctl_dict = yaml.safe_load(sysctl_settings)
        sysctl_dict['vm.swappiness'] = sysctl_dict.get('vm.swappiness', 1)
        create_sysctl(yaml.dump(sysctl_dict),
                      '/etc/sysctl.d/50-nova-compute.conf')

    destroy_libvirt_network('default')

    if migration_enabled() and config('migration-auth-type') == 'ssh':
        # Check-in with nova-c-c and register new ssh key, if it has just been
        # generated.
        status_set('maintenance', 'SSH key exchange')
        initialize_ssh_keys()
        import_authorized_keys()

    if config('enable-resize') is True:
        enable_shell(user='nova')
        status_set('maintenance', 'SSH key exchange')
        initialize_ssh_keys(user='nova')
        import_authorized_keys(user='nova', prefix='nova')
    else:
        disable_shell(user='nova')

    if config('instances-path') is not None:
        fp = config('instances-path')
        fix_path_ownership(fp, user='nova')

    [compute_joined(rid) for rid in relation_ids('cloud-compute')]
    for rid in relation_ids('zeromq-configuration'):
        zeromq_configuration_relation_joined(rid)

    for rid in relation_ids('neutron-plugin'):
        neutron_plugin_joined(rid, remote_restart=send_remote_restart)

    if is_relation_made("nrpe-external-master"):
        update_nrpe_config()

    if config('hugepages'):
        install_hugepages()

    # Disable smt for ppc64, required for nova/libvirt/kvm
    arch = platform.machine()
    log('CPU architecture: {}'.format(arch))
    if arch in ['ppc64el', 'ppc64le']:
        set_ppc64_cpu_smt_state('off')

    # NOTE(jamespage): trigger any configuration related changes
    #                  for cephx permissions restrictions and
    #                  keys on disk for ceph-access backends
    for rid in relation_ids('ceph'):
        for unit in related_units(rid):
            ceph_changed(rid=rid, unit=unit)
    for rid in relation_ids('ceph-access'):
        for unit in related_units(rid):
            ceph_access(rid=rid, unit=unit)

    CONFIGS.write_all()

    NovaComputeAppArmorContext().setup_aa_profile()
    if (network_manager() in ['flatmanager', 'flatdhcpmanager'] and
            config('multi-host').lower() == 'yes'):
        NovaAPIAppArmorContext().setup_aa_profile()
        NovaNetworkAppArmorContext().setup_aa_profile()


@hooks.hook('amqp-relation-joined')
def amqp_joined(relation_id=None):
    relation_set(relation_id=relation_id,
                 username=config('rabbit-user'),
                 vhost=config('rabbit-vhost'))


@hooks.hook('amqp-relation-changed')
@hooks.hook('amqp-relation-departed')
@restart_on_change(restart_map())
def amqp_changed():
    if 'amqp' not in CONFIGS.complete_contexts():
        log('amqp relation incomplete. Peer not ready?')
        return
    CONFIGS.write(NOVA_CONF)


@hooks.hook('shared-db-relation-joined')
def db_joined(rid=None):
    if is_relation_made('pgsql-db'):
        # error, postgresql is used
        e = ('Attempting to associate a mysql database when there is already '
             'associated a postgresql one')
        log(e, level=ERROR)
        raise Exception(e)

    relation_set(relation_id=rid,
                 nova_database=config('database'),
                 nova_username=config('database-user'),
                 nova_hostname=get_relation_ip('shared-db'))


@hooks.hook('pgsql-db-relation-joined')
def pgsql_db_joined():
    if is_relation_made('shared-db'):
        # raise error
        e = ('Attempting to associate a postgresql database when'
             ' there is already associated a mysql one')
        log(e, level=ERROR)
        raise Exception(e)

    relation_set(**{'database': config('database'),
                    'private-address': get_relation_ip('pgsql-db')})


@hooks.hook('shared-db-relation-changed')
@restart_on_change(restart_map())
def db_changed():
    if 'shared-db' not in CONFIGS.complete_contexts():
        log('shared-db relation incomplete. Peer not ready?')
        return
    CONFIGS.write(NOVA_CONF)


@hooks.hook('pgsql-db-relation-changed')
@restart_on_change(restart_map())
def postgresql_db_changed():
    if 'pgsql-db' not in CONFIGS.complete_contexts():
        log('pgsql-db relation incomplete. Peer not ready?')
        return
    CONFIGS.write(NOVA_CONF)


@hooks.hook('image-service-relation-changed')
@restart_on_change(restart_map())
def image_service_changed():
    if 'image-service' not in CONFIGS.complete_contexts():
        log('image-service relation incomplete. Peer not ready?')
        return
    CONFIGS.write(NOVA_CONF)


@hooks.hook('ephemeral-backend-relation-changed',
            'ephemeral-backend-relation-broken')
@restart_on_change(restart_map())
def ephemeral_backend_hook():
    if 'ephemeral-backend' not in CONFIGS.complete_contexts():
        log('ephemeral-backend relation incomplete. Peer not ready?')
        return
    CONFIGS.write(NOVA_CONF)


@hooks.hook('cloud-compute-relation-joined')
def compute_joined(rid=None):
    # NOTE(james-page) in MAAS environments the actual hostname is a CNAME
    # record so won't get scanned based on private-address which is an IP
    # add the hostname configured locally to the relation.
    settings = {
        'hostname': gethostname(),
        'private-address': get_relation_ip(
            'cloud-compute', cidr_network=config('os-internal-network')),
    }

    if migration_enabled():
        auth_type = config('migration-auth-type')
        settings['migration_auth_type'] = auth_type
        if auth_type == 'ssh':
            settings['ssh_public_key'] = public_ssh_key()
        relation_set(relation_id=rid, **settings)
    if config('enable-resize'):
        settings['nova_ssh_public_key'] = public_ssh_key(user='nova')
        relation_set(relation_id=rid, **settings)


@hooks.hook('cloud-compute-relation-changed')
@restart_on_change(restart_map())
def compute_changed():
    # rewriting all configs to pick up possible net or vol manager
    # config advertised from controller.
    CONFIGS.write_all()
    import_authorized_keys()
    import_authorized_keys(user='nova', prefix='nova')
    import_keystone_ca_cert()


@hooks.hook('ceph-access-relation-joined')
@hooks.hook('ceph-relation-joined')
@restart_on_change(restart_map())
def ceph_joined():
    pkgs = filter_installed_packages(['ceph-common'])
    if pkgs:
        status_set('maintenance', 'Installing ceph-common package')
        apt_install(pkgs, fatal=True)
        # Bug 1427660
        if not is_unit_paused_set() and config('virt-type') in LIBVIRT_TYPES:
            service_restart(libvirt_daemon())


def get_ceph_request():
    rq = CephBrokerRq()
    if (config('libvirt-image-backend') == 'rbd' and
            assert_libvirt_rbd_imagebackend_allowed()):
        name = config('rbd-pool')
        replicas = config('ceph-osd-replication-count')
        weight = config('ceph-pool-weight')
        rq.add_op_create_pool(name=name, replica_count=replicas, weight=weight,
                              group='vms')
    if config('restrict-ceph-pools'):
        rq.add_op_request_access_to_group(name="volumes",
                                          permission='rwx')
        rq.add_op_request_access_to_group(name="images",
                                          permission='rwx')
        rq.add_op_request_access_to_group(name="vms",
                                          permission='rwx')
    return rq


@hooks.hook('ceph-relation-changed')
@restart_on_change(restart_map())
def ceph_changed(rid=None, unit=None):
    if 'ceph' not in CONFIGS.complete_contexts():
        log('ceph relation incomplete. Peer not ready?')
        return

    if not ensure_ceph_keyring(service=service_name(), user='nova',
                               group='nova'):
        log('Could not create ceph keyring: peer not ready?')
        return

    CONFIGS.write(ceph_config_file())
    CONFIGS.write(CEPH_SECRET)
    CONFIGS.write(NOVA_CONF)

    # With some refactoring, this can move into NovaComputeCephContext
    # and allow easily extended to support other compute flavors.
    key = relation_get(attribute='key', rid=rid, unit=unit)
    if config('virt-type') in ['kvm', 'qemu', 'lxc'] and key:
        create_libvirt_secret(secret_file=CEPH_SECRET,
                              secret_uuid=CEPH_SECRET_UUID, key=key)

    if is_request_complete(get_ceph_request()):
        log('Request complete')
        # Ensure that nova-compute is restarted since only now can we
        # guarantee that ceph resources are ready, but only if not paused.
        if (not is_unit_paused_set() and
                not is_broker_action_done('nova_compute_restart', rid,
                                          unit)):
            service_restart('nova-compute')
            mark_broker_action_done('nova_compute_restart', rid, unit)
    else:
        send_request_if_needed(get_ceph_request())


@hooks.hook('ceph-relation-broken')
def ceph_broken():
    service = service_name()
    delete_keyring(service=service)
    CONFIGS.write_all()


@hooks.hook('amqp-relation-broken',
            'image-service-relation-broken',
            'shared-db-relation-broken',
            'pgsql-db-relation-broken')
@restart_on_change(restart_map())
def relation_broken():
    CONFIGS.write_all()


@hooks.hook('upgrade-charm')
@harden()
def upgrade_charm():
    # NOTE: ensure psutil install for hugepages configuration
    status_set('maintenance', 'Installing apt packages')
    apt_install(filter_installed_packages(['python-psutil']))
    for r_id in relation_ids('amqp'):
        amqp_joined(relation_id=r_id)

    if is_relation_made('nrpe-external-master'):
        update_nrpe_config()


@hooks.hook('nova-ceilometer-relation-changed')
@restart_on_change(restart_map())
def nova_ceilometer_relation_changed():
    CONFIGS.write_all()


@hooks.hook('zeromq-configuration-relation-joined')
@os_requires_version('kilo', 'nova-common')
def zeromq_configuration_relation_joined(relid=None):
    relation_set(relation_id=relid,
                 topics=" ".join(get_topics()),
                 users="nova")


@hooks.hook('zeromq-configuration-relation-changed')
@restart_on_change(restart_map())
def zeromq_configuration_relation_changed():
    CONFIGS.write(NOVA_CONF)


@hooks.hook('nrpe-external-master-relation-joined',
            'nrpe-external-master-relation-changed')
def update_nrpe_config():
    # python-dbus is used by check_upstart_job
    apt_install('python-dbus')
    hostname = nrpe.get_nagios_hostname()
    current_unit = nrpe.get_nagios_unit_name()
    nrpe_setup = nrpe.NRPE(hostname=hostname)
    monitored_services = services()
    try:
        # qemu-kvm is a one-shot service
        monitored_services.remove('qemu-kvm')
    except ValueError:
        pass
    nrpe.add_init_service_checks(nrpe_setup, monitored_services, current_unit)
    nrpe_setup.write()


@hooks.hook('neutron-plugin-relation-joined')
def neutron_plugin_joined(relid=None, remote_restart=False):
    rel_settings = {
        'hugepage_number': get_hugepage_number(),
        'default_availability_zone': config('default-availability-zone')
    }
    if remote_restart:
        rel_settings['restart-trigger'] = str(uuid.uuid4())
    relation_set(relation_id=relid,
                 **rel_settings)


@hooks.hook('neutron-plugin-relation-changed')
@restart_on_change(restart_map())
def neutron_plugin_changed():
    enable_nova_metadata, _ = nova_metadata_requirement()
    if enable_nova_metadata:
        apt_update()
        apt_install(filter_installed_packages(['nova-api-metadata']),
                    fatal=True)
    else:
        apt_purge('nova-api-metadata', fatal=True)
    service_restart_handler(default_service='nova-compute')
    CONFIGS.write(NOVA_CONF)


# TODO(jamespage): Move this into charmhelpers for general reuse.
def service_restart_handler(relation_id=None, unit=None,
                            default_service=None):
    '''Handler for detecting requests from subordinate
    charms for restarts of services'''
    restart_nonce = relation_get(attribute='restart-nonce',
                                 unit=unit,
                                 rid=relation_id)
    db = unitdata.kv()
    nonce_key = 'restart-nonce'
    if restart_nonce != db.get(nonce_key):
        if not is_unit_paused_set():
            service = relation_get(attribute='remote-service',
                                   unit=unit,
                                   rid=relation_id) or default_service
            if service:
                service_restart(service)
        db.set(nonce_key, restart_nonce)
        db.flush()


@hooks.hook('lxd-relation-joined')
def lxd_joined(relid=None):
    relation_set(relation_id=relid,
                 user='nova')


@hooks.hook('lxd-relation-changed')
@restart_on_change(restart_map())
def lxc_changed():
    nonce = relation_get('nonce')
    db = kv()
    if nonce and db.get('lxd-nonce') != nonce:
        db.set('lxd-nonce', nonce)
        configure_lxd(user='nova')
        CONFIGS.write(NOVA_CONF)


@hooks.hook('nova-designate-relation-changed')
@restart_on_change(restart_map())
def designate_changed():
    CONFIGS.write(NOVA_CONF)


@hooks.hook('ceph-access-relation-changed')
def ceph_access(rid=None, unit=None):
    '''Setup libvirt secret for specific ceph backend access'''
    key = relation_get('key', unit, rid)
    uuid = relation_get('secret-uuid', unit, rid)
    if key and uuid:
        remote_service = remote_service_name(rid)
        if config('virt-type') in LIBVIRT_TYPES:
            secrets_filename = CEPH_BACKEND_SECRET.format(remote_service)
            render(os.path.basename(CEPH_SECRET), secrets_filename,
                   context={'ceph_secret_uuid': uuid,
                            'service_name': remote_service})
            create_libvirt_secret(secret_file=secrets_filename,
                                  secret_uuid=uuid,
                                  key=key)
        # NOTE(jamespage): LXD ceph integration via host rbd mapping, so
        #                  install keyring for rbd commands to use
        ensure_ceph_keyring(service=remote_service,
                            user='nova', group='nova',
                            key=key)


@hooks.hook('update-status')
@harden()
def update_status():
    log('Updating status.')


def main():
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
    assess_status(CONFIGS)


if __name__ == '__main__':
    main()
