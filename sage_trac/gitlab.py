import json
import re
import subprocess

from pprint import pformat

import requests
import pygit2

from trac.config import Option, IntOption
from trac.core import implements
from trac.ticket.api import ITicketChangeListener
from trac.ticket.model import Ticket
from trac.util.text import exception_to_unicode
from trac.web.api import IRequestHandler

from .common import GitBase, run_git
from .token import TokenAuthenticator


class GitlabWebhook(GitBase):
    """
    Component that handles webhook API requests from GitLab.

    Currently just handles merge request events.
    """

    implements(IRequestHandler, ITicketChangeListener)

    endpoint = Option('sage_trac', 'gitlab_webhook_endpoint',
        '/gitlab-hook', doc='string or regular expression to match with '
                            'URLs whose requests should be handled by '
                            'this component')

    username = Option('sage_trac', 'gitlab_webhook_username',
        'trac',  doc="the username that the webhook should authenticate "
                     "as (by passing that user's auth token) and that "
                     "will be used as the default username for ticket "
                     "reports and git branches")

    branch_prefix = Option('sage_trac', 'gitlab_webhook_branch_prefix',
            'mrs/', doc='prefix to prepend (after u/<username>/) to git '
                        'branches synced from merge requests')

    gitlab_api_token = Option('sage_trac', 'gitlab_api_token',
            doc="API token for GitLab with permissions to post to the "
                "GitLab project; used to sync back to the GitLab project "
                "to automatically provide a link to the Trac project as "
                "well as close merge requests.")

    gitlab_url = Option('sage_trac', 'gitlab_url', 'https://gitlab.com',
            doc='base URL of the GitLab server hosting the project')

    max_commits = IntOption('sage_trac', 'gitlab_webhook_max_commit_log', 10,
            doc='max number of commits to show in the commit log comment '
                'that is added to a ticket when commits are added to a '
                'merge request')

    _field_name = '_gitlab_webhook_merge_request'
    """
    The name of the hidden custom ticket field used to associate a ticket
    with a merge request.
    """

    # IRequestHandler methods

    def match_request(self, req):
        if req.method == 'POST' and re.match(self.endpoint, req.path_info):
            return True

    def process_request(self, req):
        # First check for the expected X-Gitlab-Event header
        event = req.get_header('X-Gitlab-Event')
        if not event and event.lower() == 'merge request hook':
            self.log.warn('GitLab webhook request event missing or '
                          'not handled: {}'.format(event))
            req.send_response(422)
            req.end_headers()
            return

        token = req.get_header('X-Gitlab-Token')
        if not self._verify_token(token):
            self.log.warn('GitLab webhook request security token missing '
                          'or not valid')
            req.send_response(401)
            req.end_headers()
            return

        try:
            hook_data = json.load(req)
        except Exception as exc:
            self.log.warn(
                'Gitlab webhook failed to parse the JSON request '
                'data: {}'.format(exc))
            return req.send_no_content()

        self.log.debug('GitLab webhook received event payload:\n' +
                pformat(hook_data))

        if hook_data['object_attributes']['state'] == 'closed':
            # Do not update tickets/branches for closed merged requests
            return req.send_no_content()

        try:
            synced_branch = self._sync_branch(hook_data)
        except Exception as exc:
            self.log.warn(
                'Gitlab webhook failed to sync the downstream '
                'branch: {}'.format(exception_to_unicode(exc, True)))
            synced_branch = False

        try:
            self._create_or_update_ticket(hook_data, synced_branch)
        except Exception as exc:
            self.log.warn(
                'Gitlab webhook failed to create or update the '
                'ticket for this merge request: {}'.format(
                    exception_to_unicode(exc, True)))

        req.send_no_content()


    # ITicketChangeListener methods

    def ticket_created(self, ticket):
        pass

    def ticket_changed(self, ticket, comment, author, old_values):
        # If the ticket was closed, close the associated merge request as well
        # (regardless of what the resolution was)
        if 'status' in old_values and ticket['status'] == 'closed':
            # Look up the ticket's MR, if any
            for row in self.env.db_query("""
                    SELECT value FROM ticket_custom
                    WHERE ticket=%s AND name=%s""",
                    (ticket.id, self._field_name)):
                # There should really be only one, but if for some bizarre
                # reason there is more than one, let's deal with them anyways
                proj_id, mr_id = (int(x) for x in row[0].split(':'))
                self._close_mr(ticket.id, proj_id, mr_id, ticket['resolution'])


    def ticket_deleted(self, ticket):
        pass

    def _verify_token(self, token):
        if token is None:
            return False

        auth = TokenAuthenticator(self.env)
        return auth.verify_token(token) == self.username

    def _upstream_branch(self, mr_id, branch):
        """
        Return the name of the branch for this merge request as stored in
        our repository.
        """

        return 'u/{}/{}{}/{}'.format(self.username, self.branch_prefix,
                                     mr_id, branch)

    def _sync_branch(self, hook_data):
        """
        Fetch the merge request's branch data from the downstream repository
        into a branch on our repository.  Returns True if successful.
        """

        # Here we just assume the data from GitLab is all well-formed;
        # if not the exception will be handled in the main request handler
        attrs = hook_data['object_attributes']
        source_url = attrs['source']['git_http_url']
        source_branch = attrs['source_branch']
        upstream_branch = self._upstream_branch(attrs['iid'], source_branch)

        # First check if the branch already exists and is up-to-date
        branch = self._git.lookup_branch(upstream_branch)
        if branch is not None:
            if branch.target.hex == attrs['last_commit']['id']:
                self.log.debug(
                    'Upstream branch for MR{} already up to date.'.format(
                        attrs['iid']))
                return True

        refspec = '+refs/heads/{}:refs/heads/{}'.format(
            source_branch, upstream_branch)

        # Here we just call the git executable; we would like to be able
        # to do this with pygit2, but neither it, nor libgit2 itself, appear to
        # have APIs to simply fetch from a remote repository without explicitly
        # creating a remote
        self.log.debug('GitLab hook updating branch from {} with refspec '
                       '{}'.format(source_url, refspec))
        git_args = ('--git-dir={}'.format(self.git_dir), 'fetch',
                    source_url, refspec)
        code, output = run_git(*git_args)
        if code != 0:
            self.log.error('GitLab hook failed to fetch downstream '
                           'branch {} from {}: {}'.format(
                               source_branch, source_url, output))
            return False

        self.log.info('GitLab hook updated branch {} from {}'.format(
            upstream_branch, source_url))
        return True

    def _create_or_update_ticket(self, hook_data, synced_branch=True):
        """
        Create a ticket from a new merge request.

        We use a hidden custom ticket field to store the merge request
        associated with a ticket, for now.
        """

        attrs = hook_data['object_attributes']
        mr_id = attrs['iid']
        proj_id = attrs['target']['id']
        source_branch = attrs['source_branch']
        # First check whether a ticket already exists for this MR;
        # if so we might update the ticket if its summary or description
        # changed (in the future we might also handle other fields, or
        # comments)
        tkt_id = None

        proj_mr_id = '{}:{}'.format(proj_id, mr_id)

        for row in self.env.db_query("""
                SELECT ticket FROM ticket_custom
                WHERE name=%s AND value=%s""", (self._field_name, proj_mr_id)):
            tkt_id = row[0]
            # There should only be one; if there are more we might want to
            # raise a warning...
            break

        ticket = Ticket(self.env, tkt_id=tkt_id)
        if ticket.id is None:
            # New ticket
            ticket['reporter'] = self.username
            ticket['summary'] = self._format_summary(hook_data)
            ticket['description'] = self._format_description(hook_data)

            if synced_branch:
                ticket['branch'] = self._upstream_branch(mr_id, source_branch)
                ticket['commit'] = attrs['last_commit']['id']

            # Some small hacks to force the ticket system to save the
            # hidden custom field that stores the merge request ID
            ticket.fields.append({'name': self._field_name,
                                  'custom': True})
            ticket.custom_fields.append(self._field_name)
            ticket[self._field_name] = proj_mr_id
            try:
                ticket.insert()
            except Exception as exc:
                self.log.error(
                    'Database error inserting ticket from Gitlab '
                    'webhook: {}'.format(exception_to_unicode(exc, True)))
                raise
            else:
                self._post_ticket_to_mr(ticket.id, proj_id, mr_id)
        else:
            # Maybe update existing ticket
            comment = None

            if 'changes' in hook_data:
                changes = hook_data['changes']
                if 'title' in changes:
                    ticket['summary'] = self._format_summary(
                            hook_data)
                if 'description' in changes:
                    ticket['description'] = self._format_description(
                            hook_data)
            if synced_branch:
                if 'branch' not in ticket.values:
                    ticket['branch'] = self._upstream_branch(mr_id,
                                                             source_branch)

                new_commit = attrs['last_commit']['id']
                comment = self._update_commit(ticket, new_commit)
                ticket['commit'] = new_commit

            ticket.save_changes(author=self.username, comment=comment)

    def _update_commit(self, ticket, new_commit):
        """
        Produce a changelog comment when the branch has new commits.

        This is copied almost exactly from our post-receive git hook.
        """

        prev_commit = ticket.values.get('commit')
        if prev_commit == new_commit:
            return

        walker = self._git.walk(self._git[new_commit].oid,
                        pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_TIME)
        ignore = set([self.master_branch, 'master'])

        if prev_commit:
            ignore.add(prev_commit)

        for br in ignore:
            c = self._git.lookup_branch(br)
            if c is None:
                c = self._git.get(br)
            else:
                c = c.get_object()
            if c is not None:
                walker.hide(c.oid)

        table = []

        for commit in walker:
            if len(table) > self.max_commits:
                break

            short_sha1 = commit.hex[:7]
            title = commit.message.splitlines()
            if title:
                title = title[0]
            else:
                title = u''

            table.append(
                u'||[{} {}]||{{{{{{{}}}}}}}||'.format(
                    self.commit_url(commit.hex), short_sha1, title))

        comment = (u'New commits added to merge request.  I updated the '
                    'commit SHA-1.')
        if not self._is_ancestor_of(prev_commit, new_commit):
            comment += u'  This was a forced push.'

        if len(table) > self.max_commits:
            comment += u'  Last {} new commits:\n'.format(self.max_commits)
            table = table[:self.max_commits]
        else:
            comment += u'  New commits:\n'

        comment += u'\n'.join(reversed(table))

        return comment

    def _is_ancestor_of(self, a, b):
        if not a or a == '0' * 40:
            return True

        a = self._git[a].oid
        b = self._git[b].oid
        return self._git.merge_base(a, b) == a

    def _format_summary(self, hook_data):
        attrs = hook_data['object_attributes']
        return 'MR{}: {}'.format(attrs['iid'], attrs['title'])

    def _format_description(self, hook_data):
        """Format the description to add to the Trac ticket."""

        attrs = hook_data['object_attributes']
        description = attrs['description']
        user = hook_data['user']
        name = user['name']
        username = user['username']
        image_url = user['avatar_url']
        user_url = '{}/{}'.format(self.gitlab_url.rstrip('/'), username)
        url = attrs['url']

        # If the description is very short or non-existent we append some extra
        # breaks so that the floated image does not float outside the height of
        # the description box; this a a bit flaky and dimension dependent, but
        # good enough
        n_breaks = max(5 - description.count('\n'), 0)
        if description.strip():
            description = u'{{{\n#!markdown\n' + description + '\n}}}'
        else:
            description = u''

        description += u'\n' + (u'[[BR]]' * n_breaks)

        return (u'[[Image({image_url}, right, margin=5)]] '
                 '{name} ([{user_url} @{username}]) opened a '
                 'merge request at {url}: [[BR]][[BR]]\n{description}'.format(
                    image_url=image_url, name=name, user_url=user_url,
                    username=username, url=url, description=description))

    def _post_ticket_to_mr(self, ticket_id, proj_id, mr_id):
        if not self.gitlab_api_token:
            self.log.warn(
                "GitLab API token not configured; GitLab webhook can't "
                "update the downstream merge request")
            return

        text = ("I created a ticket on Trac for this merge request: "
                "[Trac#{}]({})".format(
                    ticket_id, self.env.abs_href.ticket(ticket_id)))

        self._post_comment_to_mr(proj_id, mr_id, text)

    def _post_comment_to_mr(self, proj_id, mr_id, text):
        if not self.gitlab_api_token:
            self.log.warn(
                "GitLab API token not configured; GitLab webhook can't "
                "update the downstream merge request")
            return

        headers = {'Private-Token': self.gitlab_api_token}
        url = '{}/api/v4/projects/{}/merge_requests/{}/notes'.format(
                self.gitlab_url.rstrip('/'), proj_id, mr_id)

        try:
            r = requests.post(url, data={'body': text}, headers=headers,
                             timeout=10)
        except Exception as exc:
            self.log.error(
                    'Error comment to GitLab: {}'.format(
                        exception_to_unicode(exc, True)))

    def _close_mr(self, ticket_id, proj_id, mr_id, resolution):
        if not self.gitlab_api_token:
            self.log.warn(
                "GitLab API token not configured; GitLab webhook can't "
                "update the downstream merge request")
            return

        self.log.debug('Trying to close merge request {} since ticket {} '
                       'was closed.'.format(mr_id, ticket_id))

        headers = {'Private-Token': self.gitlab_api_token}
        url = '{}/api/v4/projects/{}/merge_requests/{}'.format(
                self.gitlab_url.rstrip('/'), proj_id, mr_id)
        try:
            r = requests.put(url, data={'state_event': 'close'},
                             headers=headers,
                             timeout=10)
        except Exception as exc:
            self.log.error(
                    'Error updating merge request: {}'.format(
                        exception_to_unicode(exc, True)))
            return

        text = ("Downstream ticket [Trac#{}]({}) was closed as {}, so I "
                "closed this merge request.  If you feel this was in error "
                "feel free to reopen.".format(
                    ticket_id, self.env.abs_href.ticket(ticket_id),
                    resolution))

        self._post_comment_to_mr(proj_id, mr_id, text)

        self.log.info('Successfully closed merge request {}'.format(mr_id))
