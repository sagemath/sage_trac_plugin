# -*- coding: utf-8 -*-

from trac.core import implements
from trac.ticket.api import ITicketManipulator
from trac.ticket.model import Ticket
from trac.web.api import ITemplateStreamFilter
from tracrpc.api import IXMLRPCHandler

from .common import GitBase, hexify

from . import git_merger

from genshi.builder import tag
from genshi.filters import Transformer

from twisted.cred import credentials
from twisted.internet import reactor
from twisted.spread import pb

import multiprocessing
import re
import urlparse

GIT_DIFF_REGEX = re.compile(r'^diff --git a/(.*) b/(.*)$', re.MULTILINE)

FILTER = Transformer('//table[@class="properties"]')

RESULTS = ("Success", "Warnings", "Failure", "Skipped", "Exception", "Retry")

class BuildbotHook(git_merger.GitMerger):
    implements(ITicketManipulator)
    implements(IXMLRPCHandler)
    implements(ITemplateStreamFilter)

    def __init__(self, *args, **kwds):
        git_merger.GitMerger.__init__(self, *args, **kwds)

        for attr in ("host", "username", "password", "repository"):
            setattr(self, attr, self.config.get("buildbot", attr, ''))

        if self.host == '':
            raise TracError("Must set the buildmast host in trac.ini")
        else:
            if ':' in self.host:
                self.host, self.port = self.host.split(':')[:2]
                self.port = int(self.port)
            else:
                self.port = 9989

        self.port = int(self.config.get("buildbot", "port", self.port))
        self.prefix = self.config.get("buildbot", "prefix", "")
        if self.prefix and self.prefix[-1] != "/":
            self.prefix += "/"

    def get_changed_files(self, ancestor, descendant):
        if ancestor.oid == descendant.oid:
            return None
        matches = GIT_DIFF_REGEX.finditer(
                self._git.diff(ancestor, descendant).patch)
        return {file for match in matches for file in match.groups()}

    def _get_cache(self, commitortracid):
        BuildbotHook._create_table(self)
        commit = hexify(commitortracid)
        if isinstance(commit, basestring):
            statement = ('SELECT base, builder, number, status FROM "build_store" WHERE target=%s', (commit,))
        else:
            statement = ('SELECT base, builder, number, status FROM "build_store" WHERE tracid=%s', (tracid,))
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute(*statement)
            try:
                base, builder, number, status = cursor.next()
            except StopIteration:
                if self.master.hex != commit:
                    self._real_get_build(self.master_branch)
                return None
        if base != self.master.hex:
            BuildbotHook._drop_table(self)
            self._real_get_build(self.master_branch)
            return None
        return builder, number, status

    def _set_cache(self, commit, builder=None, number=None, status=None, tracid=None):
        BuildbotHook._create_table(self)
        commit = hexify(commit)
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('DELETE FROM "build_store" WHERE target=%s', (commit,))
            cursor.execute('INSERT INTO "build_store" VALUES (%s, %s, %s, %s, %s, %s)', (self.master.hex, commit, builder, number, status, tracid))

    def _create_table(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('SELECT * FROM information_schema.tables WHERE "table_name"=%s', ('build_store',))
            try:
                cursor.next()
            except StopIteration:
                cursor.execute('CREATE TABLE "build_store" ( base text, target text, builder text, number int, status int, tracid int, PRIMARY KEY ( target ), UNIQUE ( target ) )')

    def _drop_table(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('SELECT * FROM information_schema.tables WHERE "table_name"=%s', ('build_store',))
            try:
                cursor.next()
                cursor.execute('DROP TABLE "build_store"')
            except StopIteration:
                pass

    def _real_get_build(self, branch, author='', tracid=None):
        try:
            _, commit = self.generic_lookup(branch.strip())
        except (KeyError, ValueError):
            return None
        except AttributeError:
            commit = branch

        res = BuildbotHook._get_cache(self, commit)
        if res is not None:
            return res

        if commit.oid == self.master.oid:
            merge = commit
        else:
            merge = self.get_merge(commit)

            if merge in (git_merger.GIT_UPTODATE, git_merger.GIT_FAILED_MERGE):
                # don't bother trying to build failed merges or old branches
                return None
            elif merge == git_merger.GIT_FASTFORWARD:
                merge = commit

        change = {
                'repository': self.repository,
                'who': author,
                'files': self.get_changed_files(self.master, merge),
                'comments': u'',
                'branch': branch,
                'revision': hexify(merge),
                'revlink': self.commit_url(merge), # Maybe link to the tree instead?
                'src': 'git',
                }

        if tracid is not None:
            change['comments'] = \
                    u'From Trac #{tracid} ({base}/{tracid})'.format(
                            base=self.env.base_url, tracid=unicode(tracid))
            change['properties']['trac_ticket'] = tracid

        for key in change:
            if isinstance(change[key], str):
                change[key] = unicode(change[key])

        queue = multiprocessing.Queue()

        def call_addChange(remote):
            self.log.debug('sucessfully connected to {}'.format(self.host))
            deferred = remote.callRemote('addChange', change)
            deferred.addCallbacks(
                    lambda res: queue.put(True),
                    lambda res: queue.put(False))
            deferred.addBoth(lambda res: remote.broker.transport.loseConnection())
            return deferred

        def connectFailed(error):
            self.log.error('connecting to {} failed'.format(self.host))
            queue.put(False)
            return error

        def cleanup(res):
            reactor.stop()
            return res

        def run_reactor():
            factory = pb.PBClientFactory()
            deferred = factory.login(
                    credentials.UsernamePassword(self.username, self.password))

            reactor.connectTCP(self.host, self.port, factory)

            deferred.addCallbacks(call_addChange, connectFailed)
            deferred.addBoth(cleanup)

            reactor.run()

        # since twisted's reactor can only be run once,
        # do it in another process
        multiprocessing.Process(target=run_reactor).start()

        if queue.get():
            BuildbotHook._set_cache(self, commit, tracid=tracid)
        else:
            return None
        return self._real_get_build(branch, author, tracid)

    def _get_build(self, req, ticket, extra_checks):
        def get_build():
            if req.args.get('preview') is not None:
                return

            if extra_checks:
                if req.args.get('id') is None:
                    return
                if ticket['status'] == 'needs_work':
                    return self._get_cache(req.args.get('id'))
                if ticket['status'] not in ('needs_review', 'positive_review'):
                    return

            branch = ticket['branch']
            if not branch:
                return

            return self._real_get_build(branch, req.authname, req.args.get('id'))
        build = get_build()
        if build is None:
            return ()
        return build

    def get_build(self, req, ticketnum):
        ticket = Ticket(self.env, ticketnum)
        req.perm(ticket.resource).require('TICKET_VIEW')
        return list(self._get_build(req, ticket, False))

    def set_build(self, req, sha, builder=None, number=None, status=None, tracid=None):
        if req.authname != 'git':
            raise TracError("only buildbot has permissions to set builds")
        BuildbotHook._set_cache(self, sha, builder, number, status, tracid)

    # ITemplateStreamFilter methods
    def filter_stream(self, req, method, filename, stream, data):
        ticket = data.get('ticket')
        if filename != 'ticket.html' or ticket is None:
            return stream

        build = self._get_build(req, ticket, True)

        if not build:
            return stream

        builder, number, rc = build

        def buildbot_status(status):
            content = tag.h2("Buildbot: ")
            content.append(status)
            return FILTER.append(tag.div(content, class_="buildbot"))

        if rc is None:
            return stream | buildbot_status("Queued")

        if rc == -1:
            result = 'In progress'
            color_class = 'needs_review'
        else:
            result = RESULTS[rc]
            if rc:
                color_class = 'needs_work'
            else:
                color_class = 'positive_review'

        return stream | buildbot_status(
                tag.a(
                    result,
                    class_=color_class,
                    href=urlparse.urlunsplit((
                        'http',
                        self.host,
                        '{prefix}builders/{builder}/builds/{number}'.format(
                            prefix=self.prefix,
                            builder=builder,
                            number=number,
                            ),
                        '',
                        '',
                        ))
                ))

    # ITicketManipulator methods
    def validate_ticket(self, req, ticket):
        self._get_build(req, ticket, True)
        return []

    # doesn't actually do anything, according to the api
    def prepare_ticket(self, req, ticket, fields, actions): pass

    # IXMLRPCHandler methods
    def xmlrpc_namespace(self):
        return 'buildbot'

    def xmlrpc_methods(self):
        yield (None, ((None,str,str,int,int,int),), self.set_build)
        yield (None, ((list,int),), self.get_build)
