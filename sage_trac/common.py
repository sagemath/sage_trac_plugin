# -*- coding: utf-8 -*-

from trac.core import Component, TracError
import pygit2
import os
import urllib
import urlparse

MASTER_BRANCH = u'develop'
MASTER_BRANCHES = {u'develop', u'master'}

WWW_DATA_HOME = '/home/www-data'
GITOLITE_KEYDIR = os.path.join(WWW_DATA_HOME, 'gitolite', 'keydir')
GITOLITE_UPDATE = os.path.join(WWW_DATA_HOME, 'bin', 'gitolite-update')

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

class GitBase(Component):

    def __init__(self, *args, **kwds):
        Component.__init__(self, *args, **kwds)
        self.git_dir = self.config.get("trac","repository_dir","")
        if not self.git_dir:
            raise TracError("repository_dir is not set in the config file")
        self.cgit_host = self.config.get("trac", "cgit_host", "")
        if not self.cgit_host:
            raise TracError("cgit_host is not set in the config file")
        self.cgit_repo = self.config.get("trac", "cgit_repository", "")

    @property
    def _git(self):
        try:
            return self.__git
        except AttributeError:
            self.__git = pygit2.Repository(self.git_dir)
            return self.__git

    @property
    def master(self):
        return self._git.lookup_branch(MASTER_BRANCH).get_object()

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

    def commit_url(self, commit):
        commit = hexify(commit)
        return urlparse.urlunsplit((
            'http',
            self.cgit_host,
            os.path.join(self.cgit_repo,'commit/'),
            urllib.urlencode({'id': commit}),
            '',
            ))

    def log_url(self, base, tip=None):
        base, tip = hexify(base, tip)
        if tip is None:
            query = urllib.urlencode({'h': base})
        else:
            query = urllib.urlencode({
                'h': tip,
                'q': base+'..'+tip,
                'qt': 'range',
                })
        return urlparse.urlunsplit((
            'http',
            self.cgit_host,
            os.path.join(self.cgit_repo,'log/'),
            query,
            '',
            ))

    def diff_url(self, base, tip=None):
        base, tip = hexify(base, tip)
        if tip is None:
            query = urllib.urlencode({'id': base})
        else:
            query = urllib.urlencode({'id2': base, 'id': tip})
        return urlparse.urlunsplit((
            'http',
            self.cgit_host,
            os.path.join(self.cgit_repo,'diff/'),
            query,
            '',
            ))
