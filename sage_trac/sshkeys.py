from trac.core import Component, implements, TracError
from trac.config import Option, PathOption
from trac.web.chrome import ITemplateProvider, add_notice
from trac.util.translation import gettext
from trac.prefs import IPreferencePanelProvider
from trac.admin.api import IAdminCommandProvider
from trac.util.text import printout

from tracrpc.api import IXMLRPCHandler

import os
import subprocess

from threading import Lock
from fasteners import InterProcessLock as IPLock, locked

from .common import *

class UserDataStore(Component):
    def save_data(self, user, dictionary):
        """
        Saves user data for user.
        """
        self._create_table()
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('DELETE FROM "user_data_store" WHERE "user"=%s', (user,))

            for key, value in dictionary.iteritems():
                cursor.execute('INSERT INTO "user_data_store" VALUES (%s, %s, %s)', (user, key, value))

    def get_data(self, user):
        """
        Returns a dictionary with all data keys
        """
        self._create_table()
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute('SELECT key, value FROM "user_data_store" WHERE "user"=%s', (user,))
            return {key:value for key, value in cursor}

    def get_data_all_users(self):
        """
        Returns a dictionary with all data keys
        """
        self._create_table()
        return_value = {}
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute('SELECT "user", key, value FROM "user_data_store"')
            for user, key, value in cursor:
                if return_value.has_key(user):
                    return_value[user][key] = value
                else:
                    return_value[user] = {key: value}
        return return_value

    def _create_table(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('SELECT * FROM information_schema.tables WHERE "table_name"=%s', ('user_data_store',))
            try:
                cursor.next()
            except StopIteration:
                cursor.execute('CREATE TABLE "user_data_store" ( "user" text, key text, value text, UNIQUE ( "user", key ) )')

class SshKeysPlugin(Component):
    implements(IPreferencePanelProvider, IAdminCommandProvider,
               IXMLRPCHandler, ITemplateProvider)

    gitolite_user = Option('sage_trac', 'gitolite_user', 'git',
                           doc='the user with which to log into the gitolite '
                               'server (default: git)')

    gitolite_host = Option('sage_trac', 'gitolite_host', '',
                           doc='the hostname of the gitolite server')

    gitolite_admin = PathOption('sage_trac', 'gitolite_admin',
                                os.path.join(os.pardir, 'gitolite-admin'),
                                doc='directory for clone of the '
                                    'gitolite-admin repository, used to '
                                    'manage SSH keys (default: '
                                    'gitolite-admin, directly under the '
                                    'trac environment path')

    def __init__(self):
        Component.__init__(self)
        self._user_data_store = UserDataStore(self.compmgr)


        if not self.gitolite_host:
            raise TracError(
                'The [sage_trac]/gitolite_host option must be set in '
                'trac.ini')

        lockfilename = '.{0}.lock'.format(
                os.path.basename(self.gitolite_admin))

        lockfile = os.path.join(os.path.dirname(self.gitolite_admin),
                                lockfilename)

        self._locks = [IPLock(lockfile), Lock()]
        self._init_gitolite_admin()

    @locked(lock='_locks')
    def _init_gitolite_admin(self):
        """
        Initilizes (by cloning) or cleans up (if it already exists) the
        local clone of the gitolite-admin repository in which SSH keys
        are stored.

        This is locked between processes so when starting Trac up in a
        multiprocess environment only one process touches the git repo
        at a time.
        """

        if not os.path.exists(self.gitolite_admin):
            # Initialize new clone of the gitolite-admin repo
            clone_path = '{user}@{host}:gitolite-admin'.format(
                    user=self.gitolite_user, host=self.gitolite_host)
            ret, out = self._git('clone', clone_path, self.gitolite_admin,
                                 chdir=False)
            if ret != 0:
                raise TracError(
                    'Failed to clone gitolite-admin repository: '
                    '{0}'.format(out))

            return

        # Clean up any uncommitted files or changes; this suggests
        # the repository was left in an inconsistent state (e.g.
        # on process crash); then fetch and update from origin
        for cmds in [('clean', '-dfx'), ('fetch', 'origin'),
                     ('reset', '--hard', 'origin/master')]:
            ret, out = self._git(*cmds)
            if ret != 0:
                raise TracError(
                    'Error cleaning up / updating the gitolite-admin '
                    'repository: {0}; you may have to manually clean up '
                    'or re-clone the repository'.format(out))

    # IPreferencePanelProvider methods
    def get_preference_panels(self, req):
        yield ('sshkeys', gettext('SSH keys'))

    def render_preference_panel(self, req, panel):
        if req.method == 'POST':
            new_ssh_keys = set(key.strip() for key in req.args.get('ssh_keys').splitlines())
            if new_ssh_keys:
                self.setkeys(req, new_ssh_keys)
                add_notice(req, 'Your ssh key has been saved.')
            req.redirect(req.href.prefs(panel or None))

        return 'prefs_ssh_keys.html', self._user_data_store.get_data(req.authname)

    def get_templates_dirs(self):
        from pkg_resources import resource_filename
        return [resource_filename('sage_trac', 'templates')]

    def get_htdocs_dirs(self):
        return []

    # IAdminCommandProvider methods
    def get_admin_commands(self):
        yield ('sshkeys listusers', '',
               'Get a list of users that have a SSH key registered',
               None, self._do_listusers)
        yield ('sshkeys dumpkey', '<user>',
               "export the <user>'s SSH key to stdout",
               None, self._do_dump_key)

    # AdminCommandProvider boilerplate

    def _do_listusers(self):
         for user in self._listusers():
              printout(user)

    def _do_dump_key(self, user):
        printout(self._getkeys(user))

    def _git(self, *args, **kwargs):
        chdir = kwargs.pop('chdir', True)
        prev_dir = os.getcwd()
        if chdir:
            os.chdir(self.gitolite_admin)
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

    # Gitolite exporting
    @locked(lock='_locks')
    def _export_to_gitolite(self, user, keys):
        def _mkdir(path):
            if not os.path.isdir(path):
                _mkdir(os.path.dirname(path))
                os.mkdir(path)

        def get_keyname(idx):
            dirno = '{0:>02}'.format(hex(idx)[2:])
            keydir = os.path.join(self.gitolite_admin, 'keydir', dirno)
            return os.path.join(keydir, user + '.pub')

        added_keys = []
        deleted_keys = []

        for idx, key in enumerate(keys):
            keyname = get_keyname(idx)
            _mkdir(os.path.dirname(keyname))
            with open(keyname, 'w') as f:
                f.write(key)
            added_keys.append(keyname)

        for idx in range(len(keys), len(self._getkeys(user))):
            keyname = get_keyname(idx)
            try:
                os.unlink(keyname)
            except OSError:
                pass
            else:
                deleted_keys.append(keyname)

        cmds = [('pull', '-s', 'ours', 'origin', 'master')]

        if added_keys:
            cmds.append(('add',) + tuple(added_keys))

        if deleted_keys:
            cmds.append(('rm',) + tuple(deleted_keys))

        cmds.extend([
            ('commit', '-m', 'trac: updating user keys'),
            ('push', 'origin', 'master')
        ])

        for cmd in cmds:
            ret, out = self._git(*cmd)
            if ret != 0:
                # Error occurred; attempt rollback
                self._git('reset', '--hard', 'origin/master')
                raise TracError('A git error occurred while saving your '
                                'updated SSH keys: {0}; the attempted '
                                'command was {1}'.format(out, cmd))

    # general functionality
    def _listusers(self):
        all_data = self._user_data_store.get_data_all_users()
        for user, data in all_data.iteritems():
            if data.has_key('ssh_keys'):
                yield user

    def _getkeys(self, user):
        ret = self._user_data_store.get_data(user)
        if not ret: return []
        return ret['ssh_keys'].splitlines()

    def _setkeys(self, user, keys):
        self._export_to_gitolite(user, keys)
        self._user_data_store.save_data(user, {'ssh_keys': '\n'.join(keys)})

    # RPC boilerplate
    def listusers(self, req):
        return list(self._listusers())

    def getkeys(self, req):
        return self._getkeys(req.authname)

    def setkeys(self, req, keys):
        if req.authname == 'anonymous':
            raise TracError('cannot set ssh keys for anonymous users')
        keys = set(keys)
        if len(keys) > 0x100:
            add_warning(req, 'We only support using your first 256 ssh keys.')
        return self._setkeys(req.authname, keys)

    def addkeys(self, req, keys):
        new_keys = self.getkeys(req)
        new_keys.extend(keys)
        self.setkeys(req, new_keys)

    def addkey(self, req, key):
        self.addkeys(req, (key,))

    # IXMLRPCHandler methods
    def xmlrpc_namespace(self):
        return "sshkeys"

    def xmlrpc_methods(self):
        yield (None, ((list,),), self.listusers)
        yield (None, ((list,),), self.getkeys)
        yield (None, ((None,list),), self.setkeys)
        yield (None, ((None,list),), self.addkeys)
        yield (None, ((None,str),), self.addkey)
