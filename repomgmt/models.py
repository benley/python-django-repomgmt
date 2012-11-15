#
#   Copyright 2012 Cisco Systems, Inc.
#
#   Author: Soren Hansen <sorhanse@cisco.com>
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
import glob
import logging
import os
import os.path
import random
import paramiko
import select
import shutil
import StringIO
import socket
import sys
import tempfile
import termios
import textwrap
import time
import tty

from django.conf import settings
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.db import models
from django.template.loader import render_to_string
from django.utils import timezone

if settings.TESTING:
    import mock
    client = mock.Mock()
else:
    from novaclient.v1_1 import client


from repomgmt import utils
from repomgmt.exceptions import CommandFailed

logger = logging.getLogger(__name__)


class Repository(models.Model):
    name = models.CharField(max_length=200, primary_key=True)
    signing_key_id = models.CharField(max_length=200)
    uploaders = models.ManyToManyField(User)
    contact = models.EmailField()

    class Meta:
        verbose_name_plural = "repositories"

    def __unicode__(self):
        return self.name

    def _reprepro(self, *args):
        arg_list = list(args)
        cmd = ['reprepro', '-b', self.reprepro_dir] + arg_list
        return utils.run_cmd(cmd)

    @property
    def signing_key(self):
        return GPGKey(self.signing_key_id)

    @property
    def reprepro_dir(self):
        return '%s/%s' % (settings.BASE_REPO_DIR, self.name)

    @property
    def reprepro_outdir(self):
        return '%s/%s' % (settings.BASE_PUBLIC_REPO_DIR, self.name)

    @property
    def reprepro_incomingdir(self):
        return '%s/%s' % (settings.BASE_INCOMING_DIR, self.name)

    def process_incoming(self):
        self._reprepro('processincoming', 'incoming')

    def not_closed_series(self):
        return self.series_set.exclude(state=Series.CLOSED)

    def build_nodes(self):
        return BuildNode.objects.filter(buildrecord__series__repository=self)

    def write_configuration(self):
        logger.debug('Writing out config for %s' % (self.name,))

        confdir = '%s/conf' % (self.reprepro_dir,)

        settings_module_name = os.environ['DJANGO_SETTINGS_MODULE']
        settings_module = __import__(settings_module_name)
        settings_module_dir = os.path.dirname(settings_module.__file__)
        basedir = os.path.normpath(os.path.join(settings_module_dir,
                                                os.pardir))

        for d in [settings.BASE_PUBLIC_REPO_DIR,
                  confdir, self.reprepro_incomingdir]:
            if not os.path.exists(d):
                os.makedirs(d)

        for f in ['distributions', 'incoming', 'options', 'pulls',
                  'uploaders', 'create-build-records.sh', 'dput.cf',
                  'process-changes.sh']:
            s = render_to_string('reprepro/%s.tmpl' % (f,),
                                 {'repository': self,
                                  'architectures': Architecture.objects.all(),
                                  'settings': settings,
                                  'basedir': basedir,
                                  'outdir': self.reprepro_outdir})
            path = '%s/%s' % (confdir, f)

            with open(path, 'w') as fp:
                fp.write(s)

            if path.endswith('.sh'):
                os.chmod(path, 0755)

        if self.series_set.count() > 0:
            self._reprepro('export')

    def save(self, *args, **kwargs):
        self.write_configuration()
        return super(Repository, self).save(*args, **kwargs)


class UploaderKey(models.Model):
    key_id = models.CharField(max_length=200, primary_key=True)
    uploader = models.ForeignKey(User)

    def save(self):
        utils.run_cmd(['gpg', '--recv-keys', self.key_id])
        super(UploaderKey, self).save()

    def __unicode__(self):
        return '%s (%s)' % (self.key_id, self.uploader)


class GPGKey(object):
    def __init__(self, key_id):
        self.key_id = key_id

    def _gpg_export(self, private=False):
        if private:
            arg = '--export-secret-key'
        else:
            arg = '--export'

        out = utils.run_cmd(['gpg', '-a', '--export-options',
                             'export-clean', arg, self.key_id])

        if 'nothing exported' in out:
            raise Exception('Key with ID %s not found' % self.key_id)

        return out

    @property
    def private_key(self):
        return self._gpg_export(True)

    @property
    def public_key(self):
        return self._gpg_export(False)


class Series(models.Model):
    ACTIVE = 1
    MAINTAINED = 2
    FROZEN = 3
    CLOSED = 4
    SERIES_STATES = (
        (ACTIVE, 'Active development'),
        (MAINTAINED, 'Maintenance mode'),
        (FROZEN, 'Frozen for testing'),
        (CLOSED, 'No longer maintained')
    )

    name = models.CharField(max_length=200)
    repository = models.ForeignKey(Repository)
    base_ubuntu_series = models.ForeignKey('UbuntuSeries')
    numerical_version = models.CharField(max_length=200)
    state = models.SmallIntegerField(default=ACTIVE,
                                     choices=SERIES_STATES)

    class Meta:
        verbose_name_plural = "series"
        unique_together = ('name', 'repository')

    def __unicode__(self):
        return '%s-%s' % (self.repository, self.name)

    def get_absolute_url(self):
        kwargs = {'series_name': self.name,
                  'repository_name': self.repository.name}
        return reverse('packages_list', kwargs=kwargs)

    def accept_uploads_into(self):
        # If frozen, stuff uploads into -queued. If active (or
        # maintained) put them in -proposed.
        if self.state in (Series.ACTIVE, Series.MAINTAINED):
            return '%s-proposed' % (self.name,)
        elif self.state == Series.FROZEN:
            return '%s-queued' % (self.name,)

    def save(self, *args, **kwargs):
        self.repository.write_configuration()
        if self.pk:
            old = Series.objects.get(pk=self.pk)
            if old.state != self.state:
                if (old.state == Series.FROZEN and
                         self.state == Series.ACTIVE):
                    self.flush_queue()
        return super(Series, self).save(*args, **kwargs)

    def freeze(self):
        self.state = Series.FROZEN
        self.save()

    def unfreeze(self):
        self.state = Series.ACTIVE
        self.save()

    def flush_queue(self):
        logger.info('Flushing queue for %s' % (self,))
        self.repository._reprepro('pull', '%s-proposed' % (self.name, ))

    def get_source_packages(self):
        pkgs = {}

        def get_pkglist(distribution):
            pkglist = self.repository._reprepro('-A', 'source', 'list', distribution)
            for l in pkglist.split('\n'):
                if l.strip() == '':
                    continue
                repo_info = pkglist.split(':')[0]
                _distribution, _section, arch = repo_info.split('|')
                pkg_name, pkg_version = l.split(' ')[1:]
                yield (pkg_name, pkg_version)

        for distribution_fmt, key in [('%s', 'stable'),
                                      ('%s-proposed', 'proposed'),
                                      ('%s-queued', 'queued')]:
            distribution = distribution_fmt % (self.name,)
            pkgs[key] = {}
            for pkg_name, pkg_version in get_pkglist(distribution):
                pkgs[key][pkg_name] = pkg_version

        return pkgs

    def promote(self):
        self.repository._reprepro('pull', self.name)


class Package(object):
    def __init__(self, name, version):
        self.name = name
        self.version = version

    def __unicode__(self):
        return '%s-%s' % (self.name,
                          self.version)

    def __repr__(self):
        return '<Package name=%r version=%r>' % (self.name,
                                                 self.version)


class Architecture(models.Model):
    name = models.CharField(max_length=200, primary_key=True)
    builds_arch_all = models.BooleanField(default=False)

    def __unicode__(self):
        return self.name


class UbuntuSeries(models.Model):
    name = models.CharField(max_length=200, primary_key=True)

    def __unicode__(self):
        return 'Ubuntu %s' % (self.name.capitalize())


class ChrootTarball(models.Model):
    NOT_AVAILABLE = 1
    WAITING_TO_BUILD = 2
    CURRENTLY_BUILDING = 3
    READY = 4

    BUILD_STATES = (
        (NOT_AVAILABLE, 'Not available'),
        (WAITING_TO_BUILD, 'Build scheduled'),
        (CURRENTLY_BUILDING, 'Currently building'),
        (READY, 'Ready'))

    architecture = models.ForeignKey(Architecture)
    series = models.ForeignKey(UbuntuSeries)
    last_refresh = models.DateTimeField(null=True, blank=True)
    state = models.SmallIntegerField(default=1, choices=BUILD_STATES)

    class Meta:
        unique_together = ('architecture', 'series')

    def __unicode__(self):
        return '%s-%s' % (self.series, self.architecture)

    def download_link(self):
        return '%s%s-%s.tgz' % (settings.BASE_TARBALL_URL,
                               self.series.name,
                               self.architecture.name)

    def refresh(self, proxy=False, mirror=False):
        if self.state == self.CURRENTLY_BUILDING:
            logger.info('Already building %s. '
                        'Ignoring request to refresh.' % (self,))
            return
        logger.info('Refreshing %s tarball.' % (self,))

        self.state = self.CURRENTLY_BUILDING
        self.save()

        for k in os.environ.keys():
            if k.startswith('LC_'):
                del os.environ[k]
            elif k.startswith('LANG'):
                del os.environ[k]

        saved_cwd = os.getcwd()
        os.chdir('/')
        stdout = utils.run_cmd(['schroot', '-l'])
        expected = 'source:%s-%s' % (self.series.name,
                                        self.architecture.name)

        if expected not in stdout.split('\n'):
            logger.info('Existing schroot for %s not found. '
                        'Starting from scratch.' % (self,))

            def _run_in_chroot(cmd, input=None):
                series_name = self.series.name
                arch_name = self.architecture.name
                return utils.run_cmd(['schroot',
                                      '-c', '%s-%s-source' % (series_name,
                                                              arch_name),
                                      '-u', 'root', '--'] + cmd, input)

            mk_sbuild_extra_args = []
            if proxy:
                mk_sbuild_extra_args += ["--debootstrap-proxy=%s" % (proxy,)]

            if mirror:
                mk_sbuild_extra_args += ["--debootstrap-mirror=%s" % (mirror,)]

            cmd = ['mk-sbuild']
            cmd += ['--name=%s' % (self.series.name,)]
            cmd += ['--arch=%s' % (self.architecture.name)]
            cmd += ['--type=file']
            cmd += mk_sbuild_extra_args
            cmd += [self.series.name]

            utils.run_cmd(cmd)
            utils.run_cmd(['sudo', 'sed', '-i', '-e', 's/^#source/source/g',
                           ('/etc/schroot/chroot.d/sbuild-%s-%s' %
                                                  (self.series.name,
                                                   self.architecture.name))])

            if hasattr(settings, 'POST_MK_SBUILD_CUSTOMISATION'):
                _run_in_chroot(settings.POST_MK_SBUILD_CUSTOMISATION)

        logger.info("sbuild-update'ing %s tarball." % (self,))
        utils.run_cmd(['sbuild-update',
                       '-udcar',
                       '%s' % (self.series.name,),
                       '--arch=%s' % (self.architecture.name,)])
        os.chdir(saved_cwd)
        self.last_refresh = timezone.now()
        self.state = self.READY
        self.save()


class BuildRecord(models.Model):
    BUILDING = 1
    SUCCESFULLY_BUILT = 2
    CHROOT_PROBLEM = 3
    BUILD_FOR_SUPERSEDED_SOURCE = 4
    FAILED_TO_BUILD = 5
    DEPENDENCY_WAIT = 6
    FAILED_TO_UPLOAD = 7
    NEEDS_BUILDING = 8

    BUILD_STATES = (
        (BUILDING, 'Building'),
        (SUCCESFULLY_BUILT, 'Succesfully Built'),
        (CHROOT_PROBLEM, 'Chroot Problem'),
        (BUILD_FOR_SUPERSEDED_SOURCE, 'Build for superseded source'),
        (FAILED_TO_BUILD, 'Failed to build'),
        (DEPENDENCY_WAIT, 'Dependency wait'),
        (FAILED_TO_UPLOAD, 'Failed to upload'),
        (NEEDS_BUILDING, 'Needs building'),
    )

    source_package_name = models.CharField(max_length=200)
    version = models.CharField(max_length=200)
    architecture = models.ForeignKey(Architecture)
    state = models.SmallIntegerField(default=NEEDS_BUILDING,
                                     choices=BUILD_STATES)
    priority = models.IntegerField(default=100)
    series = models.ForeignKey(Series)
    build_node = models.ForeignKey('BuildNode', null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    def get_tarball(self):
        return self.series.base_ubuntu_series.chroottarball_set.get(architecture=self.architecture)

    class Meta:
        unique_together = ('series', 'source_package_name',
                           'version', 'architecture')

    def __unicode__(self):
        return ('Build of %s_%s_%s' %
                (self.source_package_name, self.version, self.architecture))

    def update_state(self, new_state):
        self.__class__.objects.filter(id=self.id).update(state=new_state)
        # Also update this cached object
        self.state = new_state

    @classmethod
    def pending_builds(cls):
        return cls.objects.filter(state=cls.NEEDS_BUILDING,
                                        build_node__isnull=True)

    @classmethod
    def pending_build_count(cls):
        return cls.pending_builds().count()

    @classmethod
    def perform_single_build(cls):
        if cls.pending_build_count() > 0:
            bn = BuildNode.start_new()
            br = BuildRecord.pick_build(bn)
            bn.prepare(br)
            bn.build(br)

    @classmethod
    def pick_build(cls, build_node):
        """Picks the highest priority build"""
        while True:
            builds = cls.pending_builds()
            try:
                next_build = builds.order_by('-priority')[0]
            except IndexError:
                return None
            # This ensures that assigning a build node is atomic,x
            # since the filter only matches if noone else has done
            # a similar update.
            matches = cls.objects.filter(id=next_build.id,
                                         build_node__isnull=True
                                        ).update(build_node=build_node)
            # If we didn't find a single match, someone else must have
            # grabbed the build and we start over
            if matches != 1:
                continue
            else:
                return cls.objects.get(id=next_build.id)


class Cloud(models.Model):
    name = models.CharField(max_length=200, primary_key=True)
    endpoint = models.URLField(max_length=200)
    user_name = models.CharField(max_length=200)
    tenant_name = models.CharField(max_length=200)
    password = models.CharField(max_length=200)
    region = models.CharField(max_length=200, blank=True)
    flavor_name = models.CharField(max_length=200)
    image_name = models.CharField(max_length=200)

    def __unicode__(self):
        return self.name

    @property
    def client(self):
        if not hasattr(self, '_client'):
            kwargs = {}
            if self.region:
                kwargs['region_name'] = self.region

            self._client = client.Client(self.user_name,
                                         self.password,
                                         self.tenant_name,
                                         self.endpoint,
                                         service_type="compute",
                                         no_cache=True,
                                         **kwargs)
            self._client.cloud = self

        return self._client


class KeyPair(models.Model):
    cloud = models.ForeignKey(Cloud)
    name = models.CharField(max_length=200)
    private_key = models.TextField()
    public_key = models.TextField()

    def __unicode__(self):
        return '%s@%s' % (self.name, self.cloud)

    class Meta:
        verbose_name_plural = "series"
        unique_together = ('cloud', 'name')


class BuildNode(models.Model):
    NEW = 0
    BOOTING = 1
    PREPARING = 2
    READY = 3
    BUILDING = 4
    SHUTTING_DOWN = 5

    NODE_STATES = (
        (NEW, 'Newly created'),
        (BOOTING, 'Booting (not yet available)'),
        (PREPARING, 'Preparing (Installing build infrastructure)'),
        (READY, 'Ready to build'),
        (BUILDING, 'Building'),
        (SHUTTING_DOWN, 'Shutting down'),
    )

    name = models.CharField(max_length=200, primary_key=True)
    cloud = models.ForeignKey(Cloud)
    cloud_node_id = models.CharField(max_length=200)
    state = models.SmallIntegerField(default=NEW,
                                     choices=NODE_STATES)
    signing_key_id = models.CharField(max_length=200)

    def __unicode__(self):
        return self.name

    def _run_cmd(self, cmd, *args, **kwargs):
        def log(s):
            logger.info('%-15s: %s' % (self.name, s))

        def log_whole_lines(lbuf):
            while '\n' in lbuf:
                line, lbuf = lbuf.split('\n', 1)
                log(line)
            return lbuf

        out = ''
        lbuf = ''
        for data in self.run_cmd(cmd, *args, **kwargs):
            out += data
            lbuf += data
            lbuf = log_whole_lines(lbuf)

        lbuf = log_whole_lines(lbuf)
        log(lbuf)
        return out

    def update_state(self, new_state):
        self.__class__.objects.filter(id=self.id).update(state=new_state)
        # Also update this cached object
        self.state = new_state

    def prepare(self, build_record):
        self.state = self.BOOTING
        self.save()
        try:
            while True:
                try:
                    self._run_cmd('id')
                    break
                except Exception, e:
                    print e
                time.sleep(5)
            self.state = self.PREPARING
            self.save()
            self._run_cmd('sudo apt-get update')
            self._run_cmd('sudo DEBIAN_FRONTEND=noninteractive '
                          'apt-get -y --force-yes install puppet')
            self._run_cmd('sudo wget -O puppet.pp %s/puppet/%s/' %
                                          (settings.BASE_URL, build_record.id))
            self._run_cmd('sudo -H puppet apply --verbose puppet.pp')
            self._run_cmd(textwrap.dedent('''\n
                          cat <<EOF > keygen.param
                          Key-Type: 1
                          Key-Length: 4096
                          Subkey-Type: ELG-E
                          Subkey-Length: 4096
                          Name-Real: %s signing key
                          Expire-Date: 0
                          %%commit
                          EOF''' % (self,)))
            out = self._run_cmd('''gpg --gen-key --batch keygen.param''')
            for l in out.split('\n'):
                if l.startswith('gpg: key '):
                    key_id = l.split(' ')[2]
            self.signing_key_id = key_id

            public_key_data = self._run_cmd('gpg -a --export %s' %
                                            (self.signing_key_id))
            utils.run_cmd(['gpg', '--import'], input=public_key_data)

            self.state = self.READY
            self.save()
            build_record.series.repository.write_configuration()
        except Exception, e:
            logger.info('Preparing build node %s failed' % (self.name),
                         exc_info=True)
            self.delete()

    def build(self, build_record):
        self.update_state(BuildNode.BUILDING)
        build_record.update_state(BuildRecord.BUILDING)
        try:
            series = build_record.series
            self._run_cmd('mkdir build')
            sbuild_cmd = ('cd build; sbuild -d %s ' % (series.name,) +
                          '--arch=%s ' % build_record.architecture.name +
                          '-c buildchroot ' +
                          '-n -k%s ' % self.signing_key_id)

            if build_record.architecture.builds_arch_all:
                sbuild_cmd += '-A '

            sbuild_cmd += ('%s_%s' % (build_record.source_package_name,
                                      build_record.version))
            self._run_cmd(sbuild_cmd)
            self._run_cmd('cd build; dput return *.changes')
        except Exception:
            pass

        build_record.update_state(BuildRecord.SUCCESFULLY_BUILT)
        self.update_state(BuildNode.SHUTTING_DOWN)

    @classmethod
    def get_unique_keypair_name(cls, cl):
        existing_keypair_names = [kp.name for kp in cl.keypairs.list()]
        while True:
            name = 'buildd-%d' % random.randint(1, 1000)
            if name not in existing_keypair_names:
                return name

    @classmethod
    def get_unique_buildnode_name(cls, cl):
        existing_server_names = [srv.name for srv in cl.servers.list()]
        old_build_node_names = [bn.name for bn in BuildNode.objects.all()]
        names_to_avoid = set(existing_server_names + old_build_node_names)
        while True:
            name = 'buildd-%d' % random.randint(1, 1000)
            if name not in names_to_avoid:
                return name

    @classmethod
    def start_new(cls):
        cloud = random.choice(Cloud.objects.all())
        logger.info('Picked cloud %s' % (cloud,))
        cl = cloud.client
        if cloud.keypair_set.count() < 1:
            logger.info('Cloud %s does not have a keypair yet. '
                        'Creating' % (cloud,))
            name = cls.get_unique_keypair_name(cl)
            kp = cl.keypairs.create(name=name)
            keypair = KeyPair(cloud=cloud, name=name,
                              private_key=kp.private_key,
                              public_key=kp.public_key)
            keypair.save()
            logger.info('KeyPair %s created' % (keypair,))
        else:
            keypair = cloud.keypair_set.all()[0]
        logger.debug('Using cached keypair: %s' % (keypair,))

        name = cls.get_unique_buildnode_name(cl)
        flavor = utils.get_flavor_by_name(cl, cl.cloud.flavor_name)
        image = utils.get_image_by_regex(cl, cl.cloud.image_name)

        logger.info('Creating server %s on cloud %s' % (name, cloud))
        srv = cl.servers.create(name, image, flavor, key_name=keypair.name)

        if getattr(settings, 'USE_FLOATING_IPS', False):
            logger.info('Grabbing floating ip for server %s on cloud %s' %
                        (name, cloud))
            floating_ip = cl.floating_ips.create()
            logger.info('Got floating ip %s for server %s on cloud %s' %
                        (floating_ip.ip, name, cloud))

            logger.debug('Assigning floating ip %s to server %s on cloud %s.'
                         'Timing out in 20 seconds.' % (floating_ip.ip,
                                                        name, cloud))

            timeout = time.time() + 20
            succeeded = False
            while timeout > time.time():
                try:
                    srv.add_floating_ip(floating_ip.ip)
                    succeeded = True
                except:
                    pass
                time.sleep(1)

            if succeeded:
                logger.info('Assigned floating ip %s to server %s on cloud %s.'
                            % (floating_ip.ip, name, cloud))
            else:
                logger.error('Failed to assign floating ip %s to server %s on '
                             'cloud %s' % (floating_ip.ip, name, cloud))
                logger.info('Deleting server %s on cloud %s' % (name, cloud))
                srv.delete()
                logger.info('Deleting floating ip %s on cloud %s' %
                            (floating_ip.ip, cloud))
                floating_ip.delete()
                raise Exception('Failed to spawn node')

        bn = BuildNode(name=name, cloud=cloud, cloud_node_id=srv.id)
        bn.save()
        return bn

    @property
    def cloud_server(self):
        cloud = self.cloud
        client = cloud.client
        return client.servers.get(self.cloud_node_id)

    @property
    def ip(self):
        if getattr(settings, 'USE_FLOATING_IPS', False):
            index = 1
        else:
            index = 0
        return self.cloud_server.networks.values()[0][index]

    def delete(self):
        if getattr(settings, 'USE_FLOATING_IPS', False):
            floating_ip = self.ip
            ref = self.cloud.client.floating_ips.find(ip=floating_ip)
            logger.info('Unassigning floating ip %s from server %s on '
                        'cloud %s.' % (floating_ip, self, self.cloud))
            self.cloud_server.remove_floating_ip(floating_ip)
            logger.info('Deleting floating ip %s on cloud %s.' %
                        (floating_ip, self, self.cloud))
            ref.delete()

        logger.info('Deleting server %s on cloud %s.' %
                    (self, self.cloud))
        self.cloud_server.delete()

        if self.signing_key_id:
            logger.info('Deleting signing key for build node %s: %s' %
                        (self, self.signing_key_id))
            utils.run_cmd(['gpg', '--batch', '--yes',
                           '--delete-keys', self.signing_key_id])

        logger.debug('Removing all references from BuildRecords to '
                     'BuildNode %s' % (self,))
        self.buildrecord_set.all().update(build_node=None)

        logger.info('Deleting BuildNode %s' % (self,))
        super(BuildNode, self).delete()

    @property
    def keypair(self):
        return self.cloud.keypair_set.all()[0]

    @property
    def paramiko_private_key(self):
        private_key = self.keypair.private_key
        priv_key_file = StringIO.StringIO(private_key)
        return paramiko.RSAKey.from_private_key(priv_key_file)

    def ssh_client(self):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(self.ip, username='ubuntu', pkey=self.paramiko_private_key)
        return ssh

    def run_cmd(self, cmd, input=None):
        logger.debug('Running: %s' % (cmd,))

        ssh = self.ssh_client()
        transport = ssh.get_transport()

        chan = transport.open_session()
        chan.exec_command(cmd)
        chan.set_combine_stderr(True)
        if input:
            chan.sendall(input)
            chan.shutdown_write()

        while True:
            r, _, __ = select.select([chan], [], [], 1)
            if r:
                if chan in r:
                    if chan.recv_ready():
                        s = chan.recv(4096)
                        if len(s) == 0:
                            break
                        yield s
                    else:
                        status = chan.recv_exit_status()
                        if status != 0:
                            raise Exception('Command %s failed' % cmd)
                        break

        ssh.close()

    def _posix_shell(self, chan):
        oldtty = termios.tcgetattr(sys.stdin)
        try:
            tty.setraw(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
            chan.settimeout(0.0)

            while True:
                r, w, e = select.select([chan, sys.stdin], [], [])
                if chan in r:
                    try:
                        x = chan.recv(1024)
                        if len(x) == 0:
                            print '\r\n*** EOF\r\n',
                            break
                        sys.stdout.write(x)
                        sys.stdout.flush()
                    except socket.timeout:
                        pass
                if sys.stdin in r:
                    x = sys.stdin.read(1)
                    if len(x) == 0:
                        break
                    chan.send(x)
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, oldtty)

    def interactive_ssh(self):
        ssh = self.ssh_client()
        shell = ssh.invoke_shell(os.environ.get('TERM', 'vt100'))
        self._posix_shell(shell)


class TarballCacheEntry(models.Model):
    project_name = models.CharField(max_length=200)
    project_version = models.CharField(max_length=200)
    rev_id = models.CharField(max_length=200, db_index=True)

    def project_tarball_dir(self):
        return os.path.join(settings.TARBALL_DIR, self.project_name)

    def filename(self):
        return '%s.tar.gz' % (self.rev_id,)

    def filepath(self):
        return os.path.join(self.project_tarball_dir(), self.filename())

    def store_file(self, filename):
        if not os.path.exists(self.project_tarball_dir()):
            os.makedirs(self.project_tarball_dir())

        shutil.copy(filename, self.filepath())


class PackageSource(models.Model):
    OPENSTACK = 'OpenStack'

    PACKAGING_FLAVORS = (
        (OPENSTACK, 'OpenStack'),
    )

    name = models.CharField(max_length=200)
    code_url = models.CharField(max_length=200,
                                help_text="(To specify a specific branch, add "
                                          "'#branchname' to the end of the url)")
    packaging_url = models.CharField(max_length=200,
                                     help_text="(To specify a specific branch,"
                                               " add '#branchname' to the end "
                                               "of the url)")
    last_seen_code_rev = models.CharField(max_length=200)
    last_seen_pkg_rev = models.CharField(max_length=200)
    flavor = models.CharField(max_length=200, choices=PACKAGING_FLAVORS,
                              default=OPENSTACK)

    def __unicode__(self):
        return self.name

    @classmethod
    def _guess_vcs_type(cls, url):
        if 'launchpad' in url:
            return 'bzr'
        if 'github' in url:
            return 'git'
        raise Exception('No idea what to do with %r' % url)

    @classmethod
    def lookup_revision(cls, url):
        logger.debug("Looking up current revision of %s" % (url,))
        vcstype = cls._guess_vcs_type(url)

        if vcstype == 'bzr':
            out = utils.run_cmd(['bzr', 'revision-info', '-d', url])
            return out.split('\n')[0].split(' ')[1]

        if vcstype == 'git':
            if '#' in url:
                url, branch = url.split('#')
            else:
                branch = 'master'
            out = utils.run_cmd(['git', 'ls-remote', url, branch])
            return out.split('\n')[0].split('\t')[0]

    def poll(self):
        current_code_revision = self.lookup_revision(self.code_url)
        current_pkg_revision = self.lookup_revision(self.packaging_url)

        something_changed = False
        if self.last_seen_code_rev != current_code_revision:
            something_changed = True
            try:
                cache_entry = TarballCacheEntry.objects.get(rev_id=current_code_revision)
            except TarballCacheEntry.DoesNotExist:
                tmpdir = tempfile.mkdtemp()
                codedir = os.path.join(tmpdir, 'checkout')
                PackageSource._checkout_code(self.code_url, codedir,
                                             current_code_revision)

                if self.flavor == self.OPENSTACK:
                    project_name = utils.run_cmd(['python', 'setup.py', '--name'],
                                                 cwd=codedir).strip().split('\n')[-1]
                    project_version = utils.run_cmd(['python', 'setup.py',
                                                    '--version'],
                                                    cwd=codedir).strip().split('\n')[-1]

                    cache_entry = TarballCacheEntry(project_name=project_name,
                                                    project_version=project_version,
                                                    rev_id=current_code_revision)

                    utils.run_cmd(['python', 'setup.py', 'sdist'], cwd=codedir)
                    tarballs_in_dist = glob.glob(os.path.join(codedir,
                                                              'dist',
                                                              '*.tar.gz'))
                    if len(tarballs_in_dist) != 1:
                        raise Exception('Found %d tarballs after '
                                        '"python setup.py sdist". Expected 1.')

                    cache_entry.store_file(tarballs_in_dist[0])
                    cache_entry.save()

                    shutil.rmtree(tmpdir)
        else:
            cache_entry = TarballCacheEntry.objects.get(rev_id=current_code_revision)

        if self.last_seen_pkg_rev != current_pkg_revision:
            something_changed = True

        if something_changed:
            for subscription in self.subscription_set.all():
                tmpdir = tempfile.mkdtemp()
                pkgdir = os.path.join(tmpdir, 'checkout')
                PackageSource._checkout_code(self.packaging_url, pkgdir,
                                             current_pkg_revision)
                orig_version = '%s-%s' % (cache_entry.project_version,
                                          subscription.counter)
                os.symlink(cache_entry.filepath(),
                           '%s/%s_%s.orig.tar.gz' % (tmpdir,
                                                     cache_entry.project_name,
                                                     orig_version))

                pkg_version = '%s-vendor1' % (orig_version,)

                utils.run_cmd(['dch', '-b',
                              '--force-distribution',
                              '-v', pkg_version,
                              'Automated PPA build. Code revision: %s. '
                              'Packaging revision: %s.' % (current_code_revision,
                                                           current_pkg_revision),
                              '-D', subscription.target_series.name],
                              cwd=pkgdir,
                              override_env={'DEBEMAIL': 'not-valid@example.com',
                                            'DEBFULLNAME': '%s Autobuilder' % (subscription.target_series.repository.name)})

                utils.run_cmd(['bzr', 'bd', '-S',
                               '--builder=dpkg-buildpackage -nc -k%s' % subscription.target_series.repository.signing_key_id,
                               ],
                              cwd=pkgdir)

                changes_files = glob.glob(os.path.join(tmpdir, '*.changes'))

                if len(changes_files) != 1:
                    raise Exception('Unexpected number of changes files: %d' % len(changes_files))

                utils.run_cmd(['dput', '-c', '%s/conf/dput.cf' % subscription.target_series.repository.reprepro_dir,
                               'autopush', changes_files[0]])

            self.last_seen_code_rev = current_code_revision
            self.last_seen_pkg_rev = current_pkg_revision
            self.save()
        return something_changed

    @classmethod
    def _checkout_code(cls, url, destdir, revision):
        print ("Checking out revision %s of %s" % (revision, url))
        vcstype = cls._guess_vcs_type(url)

        if vcstype == 'bzr':
            if os.path.exists(destdir):
                utils.run_cmd(['bzr', 'pull',
                               '-r', revision,
                               '-d', destdir, url])
                utils.run_cmd(['bzr', 'revert', '-r', revision], cwd=destdir)
                utils.run_cmd(['bzr', 'clean-tree',
                                '--unknown', '--detritus',
                                '--ignored', '--force'], cwd=destdir)
            else:
                utils.run_cmd(['bzr', 'checkout',
                                      '--lightweight',
                                      '-r', revision,
                                      url, destdir])
        elif vcstype == 'git':
            if not os.path.exists(settings.GIT_CACHE_DIR):
                utils.run_cmd(['git', 'init', settings.GIT_CACHE_DIR])

            try:
                # If it's already here, don't fetch.
                utils.run_cmd(['git', 'show', revision, '--'],
                              cwd=settings.GIT_CACHE_DIR)
            except CommandFailed:
                fetch_cmd = ['git', 'fetch']

                if '#' in url:
                    fetch_cmd += url.split('#')
                else:
                    fetch_cmd += [url]

                utils.run_cmd(fetch_cmd, cwd=settings.GIT_CACHE_DIR)

            if not os.path.exists(destdir):
                utils.run_cmd(['git', 'clone', '--shared',
                               settings.GIT_CACHE_DIR, destdir])

            utils.run_cmd(['git', 'reset', '--hard', revision], cwd=destdir)
            utils.run_cmd(['git', 'clean', '-dfx'], cwd=destdir)


class Subscription(models.Model):
    source = models.ForeignKey(PackageSource)
    target_series = models.ForeignKey(Series)
    counter = models.IntegerField()
