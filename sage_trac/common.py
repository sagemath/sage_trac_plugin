# -*- coding: utf-8 -*-

from trac.core import Component, TracError
from trac.config import Option, PathOption

import pygit2
import re
import os
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

class GitBase(Component):
    master_branch = Option('trac', 'master_branch', 'develop',
                           doc='the mainline development branch of the '
                               'repository (by default "develop" for sage ',
                               'for historical reasons)')

    git_dir = PathOption('trac', 'repository_dir', '',
                         doc='path to bare git repositories')


    cgit_protocol = Option('trac', 'cgit_protocol', 'https',
                           doc='protocol to use when linking to the cgit '
                               'server (default: https)')

    cgit_host = Option('trac', 'cgit_host', '',
                       doc='hostname of the cgit server to link to for '
                           'repository viewing')

    cgit_url = Option('trac', 'cgit_url', '',
                      doc='full URL including protocol, hostname, and '
                          'optional server path of the cgit server to '
                          'link to for repository viewing; this option '
                          'supersedes cgit_protocol and cgit_host')

    cgit_repo = Option('trac', 'cgit_repository', '',
                       doc="name of the project's repository under cgit")

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
            raise TracError('one of cgit_url or cgit_hot must be set in '
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
