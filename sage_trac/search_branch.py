"""
Search for the "Branch" custom field

Only exact matches are returned
"""

from trac.core import Component, implements
from trac.search import ISearchSource
from trac.util.datefmt import from_utimestamp
from tracrpc.api import IXMLRPCHandler


class BranchSearchModule(Component):
    """Search the "Branch" custom field"""

    implements(ISearchSource)
    implements(IXMLRPCHandler)

    # IXMLRPCHandler methods
    def xmlrpc_namespace(self):
        return 'search'

    def xmlrpc_methods(self):
        yield ('SEARCH_VIEW', ((list,str),), self.branch)

    def branch(self, req, terms):
        return self.get_search_results(req, [terms], ['branch'])

    # ISearchSource methods
    def get_search_filters(self, req):
        if 'CHANGESET_VIEW' in req.perm:
            yield ('branch', 'Branch')

    def get_search_results(self, req, terms, filters):
        # Note: output looks like this:
        # yield (12345, 'title', from_utimestamp(0), 'owner', 'search match')
        if not 'branch' in filters:
            return
        try:
            branch_name = terms[0].encode('ascii')
        except UnicodeDecodeError:
            return
        query_string = """
        SELECT t.id AS ticket, summary, t.time as time, owner, c.value AS branch
        FROM ticket t, ticket_custom c
        WHERE t.id = c.ticket AND c.name = %s AND c.value = %s
        ORDER BY t.id
        """
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute(query_string, ['branch', branch_name])
            for ticket, summary, time, owner, branch in cursor:
                yield (int(ticket), summary, from_utimestamp(time), owner, branch)


