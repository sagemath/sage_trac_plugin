# -*- coding: utf-8 -*-

from trac.core import Component, TracError, implements
from trac.config import Option, PathOption
from trac.db.api import DatabaseManager
from trac.env import IEnvironmentSetupParticipant

import pygit2
import re
import os
import subprocess
import urllib
import urlparse


# Simple regexp for "Name <email>" signatures
_signature_re = re.compile(r'\s*(.*\S)\s*<(.+@.+)>\s*$')


def hexify(*args):
    res = []
    for arg in args:
        try:
            res.append(arg.hex)
        except AttributeError:
            res.append(arg)
    if len(res) == 1:
        return res[0]
    return res


def run_git(*args, **kwargs):
    """
    Run the ``git`` command with the given arguments.

    If given, change directory to the one specified by ``chdir``; otherwise run
    ``git`` in the current directory.
    """

    chdir = kwargs.pop('chdir', None)

    prev_dir = os.getcwd()
    if chdir:
        if os.path.isdir(chdir):
            os.chdir(chdir)
        else:
            return (-1, 'Cannot change directories to %s; '
                        'it does not exist yet or is not a directory.')
    try:
        out = subprocess.check_output(('git',) + args,
                                      stderr=subprocess.STDOUT)
        code = 0
    except subprocess.CalledProcessError as exc:
        out, code = exc.output, exc.returncode
    finally:
        if chdir:
            os.chdir(prev_dir)

    return code, out.decode('latin1')


class GitBase(Component):
    master_branch = Option('sage_trac', 'master_branch', 'develop',
                           doc='the mainline development branch of the '
                               'repository (by default "develop" for sage '
                               'for historical reasons)')

    git_dir = PathOption('sage_trac', 'repository_dir', '',
                         doc='path to bare git repositories')


    cgit_protocol = Option('sage_trac', 'cgit_protocol', 'https',
                           doc='protocol to use when linking to the cgit '
                               'server (default: https)')

    cgit_host = Option('sage_trac', 'cgit_host', '',
                       doc='hostname of the cgit server to link to for '
                           'repository viewing')

    cgit_url = Option('sage_trac', 'cgit_url', '',
                      doc='full URL including protocol, hostname, and '
                          'optional server path of the cgit server to '
                          'link to for repository viewing; this option '
                          'supersedes cgit_protocol and cgit_host')

    cgit_repo = Option('sage_trac', 'cgit_repository', '',
                       doc="name of the project's repository under cgit")

    abstract = True

    def __init__(self, *args, **kwds):
        Component.__init__(self, *args, **kwds)
        if not self.git_dir or not os.path.exists(self.git_dir):
            raise TracError("repository_dir is not set in the config file or "
                            "does not exist")

        if self.cgit_url and self.cgit_host:
            raise TracError('both cgit_url and cgit_host are defined in '
                            'trac.ini and may conflict; define only one or '
                            'the other')
        elif not (self.cgit_url or self.cgit_host):
            raise TracError('one of cgit_url or cgit_host must be set in '
                            'trac.ini')
        elif self.cgit_host:
            self._cgit_host = (self.cgit_protocol, self.cgit_host)
            self._cgit_path = ''
        else:
            url_split = urlparse.urlsplit(self.cgit_url)
            self._cgit_host = url_split[:2]
            self._cgit_path = url_split[2].rstrip('/')

    @property
    def _git(self):
        try:
            return self.__git
        except AttributeError:
            self.__git = pygit2.Repository(self.git_dir)
            return self.__git

    @property
    def master(self):
        return self._git.lookup_branch(self.master_branch).get_object()

    def generic_lookup(self, ref_or_sha):
        for s in ('refs/heads/', 'refs/tags/'):
            # check for branches then tags
            try:
                return (False,
                        self._git.lookup_reference(s+ref_or_sha).get_object())
            except KeyError:
                pass
        # try raw sha1 hexes if all else fails
        return (True, self._git[ref_or_sha])

    def _cgit_url(self, path='', query={}, fragment=''):
        if not isinstance(path, str):
            path = '/'.join(path)

        return urlparse.urlunsplit(self._cgit_host +
                ('/'.join((self._cgit_path, path)),) +
                (urllib.urlencode(query), fragment))

    def commit_url(self, commit):
        commit = hexify(commit)
        return self._cgit_url((self.cgit_repo, 'commit'), {'id': commit})

    def log_url(self, base, tip=None):
        base, tip = hexify(base, tip)
        if tip is None:
            query = {'h': base}
        else:
            query = {
                'h': tip,
                'q': base+'..'+tip,
                'qt': 'range',
            }

        return self._cgit_url((self.cgit_repo, 'log'), query)

    def diff_url(self, base, tip=None):
        base, tip = hexify(base, tip)
        if tip is None:
            query = {'id': base}
        else:
            query = {'id2': base, 'id': tip}

        return self._cgit_url((self.cgit_repo, 'diff'), query)


class GenericTableProvider(Component):
    """
    Mixin class for Components that provide a new table for the Trac
    database.

    Each Component can have its own schema + schema version independent
    of other Components included in this plug-in, so that each can be
    enabled independently without requiring database tables for the other
    Components.
    """

    implements(IEnvironmentSetupParticipant)
    abstract = True

    _schema = []
    _schema_version = None

    def __init__(self):
        if not (self._schema and self._schema_version is not None):
            raise TracError(
                "As a subclass of GenericTableProvider, %s must provide "
                "valid _schema and _schema_version attributes." %
                self.__class__.__name__)

        super(GenericTableProvider, self).__init__()

    @property
    def _name(self):
        """
        The name of this component as used to store its schema version.
        """

        cls = self.__class__

        return ('%s.%s' % (cls.__module__, cls.__name__)).lower()

    def _upgrade_schema(self, db, prev_version):
        """
        Override this method to provide Component-specific schema upgrade
        instructions.  This is optional, in case there are no specific
        instructions.
        """

        raise NotImplementedError

    # IEnvironmentSetupParticipant methods
    def environment_created(self):
        dbm = DatabaseManager(self.env)
        dbm.create_tables(self._schema)
        dbm.set_database_version(self._schema_version, self._name)

    def environment_needs_upgrade(self):
        dbm = DatabaseManager(self.env)
        return dbm.needs_upgrade(self._schema_version, self._name)

    def upgrade_environment(self):
        dbm = DatabaseManager(self.env)

        prev_version = dbm.get_database_version(self._name)
        to_create = []

        if prev_version is False:
            # The schema for this Component has never been created; presumably
            # all tables have not been created, but for earlier versions of
            # this plugin they may have been, so only create tables that don't
            # already exist (this is a bit fragile but good enough for the
            # limited use case of this plugin)
            table_names = dbm.get_table_names()
            to_create = [table for table in self._schema
                         if table.name not in table_names]

        # Use a common transaction for both create_tables and upgrade_schema
        # (create_tables creates its own transaction context manager, but
        # these can be safely nested)
        with self.env.db_transaction as db:
            if to_create:
                dbm.create_tables(to_create)

            # This calls _upgrade_schema even if prev_version was False, to
            # support older versions of the plugin that did not track their
            # schema version, and need to be able to "update" even when tables
            # for this Component already exist
            try:
                self._upgrade_schema(db, prev_version)
            except NotImplementedError:
                pass

        # Only if no exceptions occurred above
        dbm.set_database_version(self._schema_version, self._name)
