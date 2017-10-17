from trac.core import Component, implements, TracError
from trac.config import Option, PathOption
from trac.db.schema import Table, Column, Index
from trac.web.chrome import ITemplateProvider, add_notice, add_warning
from trac.util.translation import gettext
from trac.prefs import IPreferencePanelProvider
from trac.admin.api import IAdminCommandProvider
from trac.util.text import printout
from trac.util.html import escape

from tracrpc.api import IXMLRPCHandler

from genshi import Markup

import os
import shutil
import socket
import sys

from threading import Lock, current_thread
from fasteners import InterProcessLock as IPLock, locked as locked_
from sshpubkeys import SSHKey, InvalidKeyException

from .common import GenericTableProvider, run_git


def _my_id():
    return 'pid:%s, tid:%s' % (os.getpid(), current_thread().name)


def locked(method):
    """
    Wrapper around fasteners.locked which provides a decorator
    for methods that allows passing the Trac logger to the
    ``locked`` argument of fastners.locked.

    Also adds logging when a locked method is being entered
    and exits.
    """

    my_id = _my_id()

    def inner_method(self, *args, **kwargs):
        # Wrapper around the original method to log when the
        # lock is acquired/released--this goes inside the wrapper
        # provided by `fasteners.locked` so entering/leaving this
        # method means acquiring/releasing the lock
        self.log.debug(
            '[%s] Acquired lock for and entered method %s' %
            (my_id, method.__name__))
        try:
            return method(self, *args, **kwargs)
        finally:
            self.log.debug(
                '[%s] Releasing lock for and leaving method %s' %
                (my_id, method.__name__))

    def wrapper(self, *args, **kwargs):
        # Use fasteners.locked to make the normal wrapper around
        # the given method
        inner_decorator = locked_(lock='_locks', logger=self.log)
        inner_wrapper = inner_decorator(inner_method)
        self.log.debug(
            '[%s] About to enter locked method %s; may have to wait on '
            'lock' % (my_id, method.__name__))
        ret = inner_wrapper(self, *args, **kwargs)
        self.log.debug(
            '[%s] Left locked method %s; it should be unlocked now' %
            (my_id, method.__name__))
        return ret

    return wrapper


class UserDataStore(Component):
    _schema = [
        Table('user_data_store', key=('user', 'key'))[
            Column('user'),
            Column('key'),
            Column('value')
        ]
    ]

    _schema_version = 1

    def __init__(self):
        self.log.warning('The UserDataStore plugin is deprecated and may be '
                         'disabled (it no longer does anything).')
        super(UserDataStore, self).__init__()


    def save_data(self, user, dictionary):
        """
        Saves user data for user.
        """

        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('DELETE FROM "user_data_store" WHERE "user"=%s', (user,))

            for key, value in dictionary.iteritems():
                cursor.execute('INSERT INTO "user_data_store" VALUES (%s, %s, %s)', (user, key, value))

    def get_data(self, user):
        """
        Returns a dictionary with all data keys
        """

        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute('SELECT key, value FROM "user_data_store" WHERE "user"=%s', (user,))
            return {key:value for key, value in cursor}

    def get_data_all_users(self):
        """
        Returns a dictionary with all data keys
        """

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


class SshKeysPlugin(GenericTableProvider):
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

    gitolite_author_name = Option(
            'sage_trac', 'gitolite_author', 'trac',
             doc='author name to use when committing updates to the '
                 'gitolite-admin repository')

    gitolite_author_email = Option(
            'sage_trac', 'gitolite_email',
            'trac@{0}'.format(socket.gethostname()),
             doc='author e-mail to use when committing updates to the '
                 'gitolite-admin repository')

    _schema = [
        Table('sage_trac_ssh_keys', key=('username', 'key'))[
            Column('username'),
            Column('key'),
            Column('title'),  # currently unused, but included in anticipation
            Column('key_order', type='int'),
            Index(('username',))
        ]
    ]

    _schema_version = 1

    def __init__(self):
        super(SshKeysPlugin, self).__init__()

        if not self.gitolite_host:
            raise TracError(
                'The [sage_trac]/gitolite_host option must be set in '
                'trac.ini')

        lockfilename = '.{0}.lock'.format(
                os.path.basename(self.gitolite_admin))

        lockfile = os.path.join(os.path.dirname(self.gitolite_admin),
                                lockfilename)

        self._locks = [IPLock(lockfile), Lock()]

        # This is something of a hack for now, but necessary.  The
        # gitolite-admin clone should not be created when running trac-admin,
        # as trac-admin is typically run as root (or some other user not
        # www-data itself) so the gitolite-admin clone will use the wrong
        # public key, and may have the wrong permissions
        if sys.argv[0] != 'trac-admin':
            self._init_gitolite_admin()

    @locked
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
            return self._clone_gitolite_admin()

        # Try to cleanup the gitolite-admin repo to make sure it starts out in
        # a clean state; if this fails (which can happen for example if git
        # crashed and leaves and index.lock file around) we should remove the
        # repository and try cloning a pristine copy.  Failing that, raise an
        # error.
        try:
            self._cleanup_gitolite_admin()
        except TracError as exc:
            self.log.warn('Error cleaning up gitolite-admin repository '
                          'during initialization: {0}; re-cloning the '
                          'repository'.format(exc))
            shutil.rmtree(self.gitolite_admin)
            self._clone_gitolite_admin()
        else:
            self._update_gitolite_admin()

    def _clone_gitolite_admin(self):
        # Initialize new clone of the gitolite-admin repo

        # This method should be called with the locks in self._locks
        # held!
        if os.path.exists(self.gitolite_admin):
            return

        clone_path = '{user}@{host}:gitolite-admin'.format(
                user=self.gitolite_user, host=self.gitolite_host)
        ret, out = self._git('clone', clone_path, self.gitolite_admin,
                             chdir=False)
        if ret != 0:
            if os.path.exists(self.gitolite_admin):
                shutil.rmtree(self.gitolite_admin)
            raise TracError(
                'Failed to clone gitolite-admin repository: '
                '{0}'.format(out))
        else:
            self._configure_gitolite_admin()

    def _configure_gitolite_admin(self):
        ret, out = self._git('config', '--local', 'user.name',
                             self.gitolite_author_name)
        if not ret:
            ret, out = self._git('config', '--local', 'user.email',
                                 self.gitolite_author_email)

        if ret != 0:
            raise TracError(
                'Error configuring the gitolite-admin repository: '
                '{0}'.format(out))

    def _cleanup_gitolite_admin(self):
        # Clean up any uncommitted files or changes; this suggests
        # the repository was left in an inconsistent state (e.g.
        # on process crash
        # This method should be called with the locks in self._locks
        # held!
        ret, out = self._git('clean', '-dfx')
        if ret != 0:
            raise TracError(
                'Error cleaning up the gitolite-admin repository: '
                '{0}'.format(out))
        else:
            self._configure_gitolite_admin()

    def _update_gitolite_admin(self):
        # Fetch latest changes from the main repository and reset
        # to origin/master
        # This method should be called with the locks in self._locks
        # held!
        for cmds in [('fetch', 'origin'),
                     ('reset', '--hard', 'origin/master')]:
            ret, out = self._git(*cmds)
            if ret != 0:
                raise TracError(
                    'Error updating the gitolite-admin repository: {0}; you '
                    'may have to manually clean up or re-clone the '
                    'repository'.format(out))

    # IPreferencePanelProvider methods
    def get_preference_panels(self, req):
        yield ('sshkeys', gettext('SSH keys'))

    def render_preference_panel(self, req, panel):
        if req.method == 'POST':
            seen = set()
            ssh_keys = req.args.get('ssh_keys').strip().splitlines()
            new_ssh_keys = [k.strip() for k in ssh_keys
                            if k.strip() and not (k in seen or seen.add(k))]

            if new_ssh_keys:
                self.validatekeys(req, new_ssh_keys)

            if new_ssh_keys:
                # It's possible validatekeys could have removed all keys from
                # new_ssh_keys
                self.setkeys(req, new_ssh_keys)
                add_notice(req, 'Your ssh key has been saved.')
            req.redirect(req.href.prefs(panel or None))

        ssh_keys = '\n'.join(key[0] for key in self._getkeys(req.authname))

        return 'prefs_ssh_keys.html', {'ssh_keys': ssh_keys}

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
        printout([key[0] for key in self._getkeys(user)])

    def _git(self, *args, **kwargs):
        chdir = kwargs.get('chdir', self.gitolite_admin)
        self.log.debug('[%s] Calling `git %s` in %s' %
                       (_my_id(), ' '.join(args), chdir or os.getcwd()))

        return run_git(*args, chdir=chdir)

    # Gitolite exporting
    @locked
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

        for idx in range(len(keys), len(list(self._getkeys(user)))):
            keyname = get_keyname(idx)
            try:
                os.unlink(keyname)
            except OSError:
                pass
            else:
                deleted_keys.append(keyname)

        cmds = [('pull', '-s', 'recursive', '-Xours', 'origin', 'master')]

        if added_keys:
            cmds.append(('add',) + tuple(added_keys))

        if deleted_keys:
            cmds.append(('rm',) + tuple(deleted_keys))

        ret, out = self._git('status', '--porcelain')
        if ret != 0:
            raise TracError("An unexpected git error occurred: "
                            "{0}".format(out))

        if out:
            # If git status did *not* produce output then nothing
            # changed in the repository (i.e. no keys changed) so there
            # is nothing to commit or push
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
        for user, in self.env.db_query("""
                SELECT DISTINCT username FROM sage_trac_ssh_keys
                ORDER BY username"""):
            yield user

    def _getkeys(self, user):
        for key, title in self.env.db_query("""
                SELECT key, title from sage_trac_ssh_keys
                WHERE username=%s
                ORDER BY key_order""", (user,)):
            yield key, title

    def _setkeys(self, user, keys):
        self._export_to_gitolite(user, keys)

        with self.env.db_transaction as db:
            # Since _setkeys is passed a full list of keys right now the
            # simplest thing to do is delete all existing entries and insert
            # new ones
            db('DELETE FROM "sage_trac_ssh_keys" WHERE username=%s', (user,))
            for idx, key in enumerate(keys):
                db('INSERT INTO "sage_trac_ssh_keys" VALUES (%s, %s, %s, %s)',
                   (user, key, '', idx))

    # RPC boilerplate
    def listusers(self, req):
        return list(self._listusers())

    def getkeys(self, req):
        return [key[0] for key in self._getkeys(req.authname)]

    def validatekeys(self, req, keys):
        """
        Validate each submitted SSH key.

        Any invalid keys are removed from the ``keys`` list.  Note: The list is
        modified in-place.  Warnings are displayed to the user for each invalid
        key submitted.
        """

        def wrap_key(key):
            return '<p style="word-wrap: break-word; margin: 1em 0">{0}</p>'.format(
                    escape(key))

        for idx, key in enumerate(keys[:]):
            msg = None

            try:
                ssh = SSHKey(key)
            except NotImplementedError:
                msg = ('Unknown key type encountered in key #{0}:'
                       '{1}'
                       'Currently ssh-rsa, ssh-dss (DSA), ssh-ed25519 and '
                       'ecdsa keys with NIST curves are supported.')
            except InvalidKeyException:
                msg = ('Malformatted SSH key encountered in key #{0}:'
                       '{1}'
                       'Make sure you copy-and-pasted it correctly and that '
                       'there is no spurious whitespace in the key.')

            if msg:
                add_warning(req, Markup(msg.format(idx + 1, wrap_key(key))))
                keys.remove(key)

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

    # GenericTableProvider methods
    def _upgrade_schema(self, db, prev_version):
        if prev_version is False:
            # previous versions of this plugin used the generic version key
            # 'sage_trac' for the entire plugin, before adding different
            # schema versions for each sub-component of the plugin
            #
            # Then we migrate the contents of the old user_data_store table
            # (which was *only* used by this component) and drop that table
            db('DELETE FROM system WHERE name=%s', ('sage_trac',))
            seen = set()
            with self.env.db_query as query:
                # Use DISTINCT, as the real sage_trac environment database
                # contains some duplicate keys in user_data_store for whatever
                # reason
                for user, value in query("""
                        SELECT DISTINCT "user", value FROM user_data_store
                        WHERE key=%s
                        """, ('ssh_keys',)):
                    for idx, key in enumerate(value.splitlines()):
                        key = key.strip()
                        if not key:
                            continue

                        # Turns out even with DISTINCT there are some
                        # duplicates that occur after stripping whitespace;
                        # since it's difficult to do this in a manner portable
                        # accross DB's we'll manually keep track of what
                        # user/key pairs we've seen
                        if (user, key) in seen:
                            continue

                        db("""
                            INSERT INTO sage_trac_ssh_keys
                            VALUES (%s, %s, %s, %s)
                            """, (user, key, '', idx))

                        seen.add((user, key))

            db('DROP TABLE user_data_store');
