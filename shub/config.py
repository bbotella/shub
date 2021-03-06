import contextlib
import netrc
import os
import warnings
from collections import namedtuple

import click
import six
import ruamel.yaml as yaml

from shub.exceptions import (BadParameterException, BadConfigException,
                             ConfigParseException, MissingAuthException,
                             NotFoundException)
from shub.utils import (closest_file, get_scrapycfg_targets, get_sources,
                        pwd_hg_version, pwd_git_version, pwd_version)


GLOBAL_SCRAPINGHUB_YML_PATH = os.path.expanduser("~/.scrapinghub.yml")
NETRC_PATH = os.path.expanduser('~/_netrc' if os.name == 'nt' else '~/.netrc')


class ShubConfig(object):

    DEFAULT_ENDPOINT = 'https://dash.scrapinghub.com/api/'

    def __init__(self):
        self.projects = {}
        self.endpoints = {
            'default': self.DEFAULT_ENDPOINT,
        }
        self.apikeys = {}
        self.version = 'AUTO'
        self.stacks = {}
        self.requirements_file = None

    def load(self, stream):
        """Load Scrapinghub configuration from stream."""
        try:
            yaml_cfg = yaml.safe_load(stream)
            if not yaml_cfg:
                return
            for option in ('projects', 'endpoints', 'apikeys', 'stacks'):
                getattr(self, option).update(yaml_cfg.get(option, {}))
            self.version = yaml_cfg.get('version', self.version)
            if 'requirements_file' in yaml_cfg:
                self.requirements_file = yaml_cfg['requirements_file']
        except (yaml.YAMLError, AttributeError):
            # AttributeError: stream is valid YAML but not dictionary-like
            raise ConfigParseException

    def load_file(self, filename):
        """Load Scrapinghub configuration from YAML file. """
        try:
            with open(filename, 'r') as f:
                self.load(f)
        except ConfigParseException:
            raise ConfigParseException(
                "Unable to parse configuration file %s. Maybe a missing "
                "colon?" % filename
            )

    def _load_scrapycfg_target(self, tname, t):
        default_endpoint = ('url' not in t or
                            t['url'] == self.endpoints['default'])
        default_user = ('username' not in t or
                        t['username'] == self.apikeys.get('default'))
        if 'project' in t:
            if tname == 'default' or (default_endpoint and default_user):
                self.projects[tname] = t['project']
            elif default_endpoint and not default_user:
                self.projects[tname] = {
                    'id': t['project'], 'apikey': tname,
                }
            else:
                self.projects[tname] = tname + '/' + t['project']
        if not default_endpoint:
            self.endpoints[tname] = t['url']
        if not default_user or (not default_endpoint and 'username' in t):
            self.apikeys[tname] = t['username']
        if 'version' in t:
            self.version = t['version']

    def load_scrapycfg(self, sources):
        """Load configuration from a list of scrapy.cfg-like sources."""
        targets = get_scrapycfg_targets(sources)
        self._load_scrapycfg_target('default', targets['default'])
        del targets['default']
        for tname, t in six.iteritems(targets):
            self._load_scrapycfg_target(tname, t)

    def save(self, path=None):
        with update_yaml_dict(path) as yml:
            yml['projects'] = self.projects
            # Write "123" instead of "'123'"
            for target, project in yml['projects'].iteritems():
                try:
                    if isinstance(project, dict):
                        project['id'] = int(project['id'])
                    else:
                        yml['projects'][target] = int(project)
                except Exception:
                    pass
            yml['endpoints'] = self.endpoints
            yml['apikeys'] = self.apikeys
            yml['version'] = self.version
            yml['stacks'] = self.stacks
            if self.requirements_file:
                yml['requirements_file'] = self.requirements_file
            # Don't write defaults
            if self.endpoints['default'] == ShubConfig.DEFAULT_ENDPOINT:
                del yml['endpoints']['default']
            if self.version == 'AUTO':
                del yml['version']

    @property
    def normalized_projects(self):
        """
        Return a copy of ``self.projects`` where all values are dictionaries
        that have at least the keys ``id``, ``endpoint``, and ``apikey``.
        """
        projects = self.projects.copy()
        for target, proj in projects.items():
            if not isinstance(proj, dict):
                proj = {'id': proj}
                projects[target] = proj
            elif 'id' not in proj:
                raise BadConfigException("Please define an ID for project "
                                         "\"%s\"" % target)
            try:
                proj['endpoint'], proj['id'] = proj['id'].split('/')
            except (ValueError, AttributeError):
                proj.setdefault('endpoint', 'default')
            proj.setdefault('apikey', proj['endpoint'])
            try:
                proj['id'] = int(proj['id'])
            except ValueError:
                raise BadConfigException(
                    "\"%s\" is not a valid Scrapinghub project ID. Please "
                    "check your scrapinghub.yml" % proj['id']
                )
        return projects

    def get_project(self, project):
        """
        Given a project alias or a canonical project ID, return the
        corresponding normalized configuration dictionary from
        ``self.projects``.
        """
        if project in self.projects:
            return self.normalized_projects[project]
        try:
            endpoint, proj_id = project.split('/')
        except (ValueError, AttributeError):
            endpoint, proj_id = 'default', project
        try:
            proj_id = int(proj_id)
        except ValueError:
            if project == 'default':
                msg = ("Please specify target or configure a default target "
                       "in scrapinghub.yml.")
            else:
                msg = ("Could not find target \"%s\". Please define it in "
                       "your scrapinghub.yml or supply a numerical project ID."
                       "" % project)
            raise BadParameterException(msg, param_hint='target')
        for proj in self.normalized_projects.values():
            if proj['id'] == proj_id and proj['endpoint'] == endpoint:
                return proj
        else:
            return {'id': proj_id, 'endpoint': endpoint, 'apikey': endpoint}

    def get_version(self):
        if not self.version or self.version == 'AUTO':
            return pwd_version()
        elif self.version == 'GIT':
            return pwd_git_version()
        elif self.version == 'HG':
            return pwd_hg_version()
        elif self.version:
            return str(self.version)

    def get_target_conf(self, target, auth_required=True):
        proj = self.get_project(target)
        if proj['endpoint'] not in self.endpoints:
            raise NotFoundException("Could not find endpoint %s. Please "
                                    "define it in your scrapinghub.yml."
                                    "" % proj['endpoint'])
        try:
            apikey = str(self.apikeys[proj['apikey']])
        except KeyError:
            if auth_required:
                msg = None
                if proj['endpoint'] != 'default':
                    msg = ("Could not find API key for endpoint %s."
                           "" % proj['endpoint'])
                raise MissingAuthException(msg)
            apikey = None
        return Target(
            project_id=proj['id'],
            endpoint=self.endpoints[proj['endpoint']],
            apikey=apikey,
            stack=(self.stacks.get(proj['stack'], proj['stack'])
                   if 'stack' in proj else None),
            requirements_file=self.requirements_file,
            version=self.get_version(),
        )

    def get_target(self, target, auth_required=True):
        """Return (project_id, endpoint, apikey) for given target."""
        warnings.warn("get_target is deprecated, use get_target_conf instead")
        targetconf = self.get_target_conf(target, auth_required=auth_required)
        return (
            targetconf.project_id,
            targetconf.endpoint,
            targetconf.apikey
        )

    def get_project_id(self, target):
        return self.get_target_conf(target, auth_required=False).project_id

    def get_endpoint(self, target):
        return self.get_target_conf(target, auth_required=False).endpoint

    def get_apikey(self, target, required=True):
        return self.get_target_conf(target, auth_required=required).apikey


Target = namedtuple('Target', ['project_id', 'endpoint', 'apikey', 'stack',
                               'requirements_file', 'version'])


MIGRATION_BANNER = """
-------------------------------------------------------------------------------
Welcome to shub version 2!

This release contains major updates to how shub is configured, as well as
updates to the commands and shub's look & feel.

Run 'shub' to get an overview over all available commands, and
'shub command --help' to get detailed help on a command. Definitely try the
new 'shub items -f [JOBID]' to see items live as they are being scraped!

From now on, shub configuration should be done in a file called
'scrapinghub.yml', living next to the previously used 'scrapy.cfg' in your
Scrapy project directory. Global configuration, for example API keys, should be
done in a file called '.scrapinghub.yml' in your home directory.

But no worries, shub has automatically migrated your global settings to
~/.scrapinghub.yml, and will also automatically migrate your project settings
when you run a command within a Scrapy project.

Visit http://doc.scrapinghub.com/shub.html for more information on the new
configuration format and its benefits.

Happy scraping!
-------------------------------------------------------------------------------
"""


def _migrate_to_global_scrapinghub_yml():
    conf = ShubConfig()
    conf.load_scrapycfg(get_sources(use_closest=False))
    try:
        info = netrc.netrc(NETRC_PATH)
        netrc_key, _, _ = info.authenticators("scrapinghub.com")
    except (IOError, TypeError):
        netrc_key = None
    if netrc_key:
        conf.apikeys['default'] = netrc_key
    conf.save()
    default_conf = ShubConfig()
    migrated_data = any(getattr(conf, attr) != getattr(default_conf, attr)
                        for attr in ('projects', 'endpoints', 'apikeys',
                                     'version'))
    if migrated_data:
        click.echo(MIGRATION_BANNER, err=True)


PROJECT_MIGRATION_OK_BANNER = """
INFO: Your deploy configuration has been migrated to scrapinghub.yml.
shub will no longer read from scrapy.cfg (but Scrapy will, so don't delete it).
Visit http://doc.scrapinghub.com/shub.html for more information.
"""


PROJECT_MIGRATION_FAILED_BANNER = """
WARNING: shub failed to convert your scrapy.cfg to scrapinghub.yml. Please
visit http://doc.scrapinghub.com/shub.html for help on how to use the new
configuration format. We would be grateful if you could also file a bug report
at https://github.com/scrapinghub/shub/issues

For now, shub fell back to reading from scrapy.cfg, everything should work as
expected.
"""


def _migrate_and_load_scrapy_cfg(conf):
    # Load from closest scrapy.cfg
    closest_scrapycfg = closest_file('scrapy.cfg')
    if not closest_scrapycfg:
        return
    targets = get_scrapycfg_targets([closest_scrapycfg])
    if targets == get_scrapycfg_targets():
        # No deploy configuration in scrapy.cfg
        return
    conf.load_scrapycfg([closest_scrapycfg])
    # Migrate to scrapinghub.yml
    closest_sh_yml = os.path.join(os.path.dirname(closest_scrapycfg),
                                  'scrapinghub.yml')
    temp_conf = ShubConfig()
    temp_conf.load_scrapycfg([closest_scrapycfg])
    try:
        temp_conf.save(closest_sh_yml)
    except Exception:
        click.echo(PROJECT_MIGRATION_FAILED_BANNER, err=True)
    else:
        click.echo(PROJECT_MIGRATION_OK_BANNER, err=True)


def load_shub_config(load_global=True, load_local=True, load_env=True):
    """
    Return a ShubConfig instance with ~/.scrapinghub.yml and the closest
    scrapinghub.yml already loaded
    """
    conf = ShubConfig()
    if load_global:
        if not os.path.exists(GLOBAL_SCRAPINGHUB_YML_PATH):
            _migrate_to_global_scrapinghub_yml()
        conf.load_file(GLOBAL_SCRAPINGHUB_YML_PATH)
    if load_env and 'SHUB_APIKEY' in os.environ:
        conf.apikeys['default'] = os.environ['SHUB_APIKEY']
    if load_local:
        closest_sh_yml = closest_file('scrapinghub.yml')
        if closest_sh_yml:
            conf.load_file(closest_sh_yml)
        else:
            _migrate_and_load_scrapy_cfg(conf)
    return conf


@contextlib.contextmanager
def update_yaml_dict(conf_path=None):
    """
    Context manager for updating a YAML file while preserving key ordering and
    comments.
    """
    conf_path = conf_path or GLOBAL_SCRAPINGHUB_YML_PATH
    dumper = yaml.RoundTripDumper
    try:
        with open(conf_path, 'r') as f:
            conf = yaml.load(f, yaml.RoundTripLoader) or {}
    except IOError as e:
        if e.errno != 2:
            raise
        conf = {}
        # Use alphabetic order when creating files
        dumper = yaml.Dumper
    # Code inside context manager is executed after this yield
    yield conf
    # Avoid writing "key: {}"
    for key in conf.keys():
        if conf[key] == {}:
            del conf[key]
    with open(conf_path, 'w') as f:
        # Avoid writing "{}"
        if conf:
            yaml.dump(conf, f, default_flow_style=False, Dumper=dumper)


def get_target(target, auth_required=True):
    """Load shub configuration and return target."""
    conf = load_shub_config()
    return conf.get_target(target, auth_required=auth_required)


def get_target_conf(target, auth_required=True):
    """Load shub configuration and return target."""
    conf = load_shub_config()
    return conf.get_target_conf(target, auth_required=auth_required)


def get_version():
    """Load shub configuratoin and return version."""
    conf = load_shub_config()
    return conf.get_version()
