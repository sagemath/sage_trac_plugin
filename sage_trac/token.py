"""
JSON Web Token based authentication for Trac.

Currently very basic; simply uses static (not time-based) tokens with no
revocation mechanism (short of changing the secret key, thus revoking all
tokens for all users).
"""


from pkg_resources import resource_filename

from trac.config import Option
from trac.core import Component, implements
from trac.prefs import IPreferencePanelProvider
from trac.util import hex_entropy
from trac.web.api import IAuthenticator
from trac.web.chrome import ITemplateProvider

from itsdangerous import JSONWebSignatureSerializer, BadSignature


class TokenAuthenticator(Component):
    implements(IAuthenticator, IPreferencePanelProvider, ITemplateProvider)

    secret_key = Option('sage_trac', 'secret_key',
                        doc='Secret key to use for signing tokens; ensure '
                            'that this is well protected.')

    def __init__(self):
        super(TokenAuthenticator, self).__init__()
        if self.secret_key:
            self._serializer = JSONWebSignatureSerializer(self.secret_key)
        else:
            self.log.warning('No secret key configured for token-based '
                             'authentication in {}.'.format(
                                 self.__class__.__name__))
            self._serializer = None

    # ITemplateProvider methods
    def get_htdocs_dirs(self):
        return []

    def get_templates_dirs(self):
        return [resource_filename(__name__, 'templates')]

    # IPreferencePanelProvider methods
    def get_preference_panels(self, req):
        if req.authname and req.authname != 'anonymous':
            yield 'token', 'Token'

    def render_preference_panel(self, req, panel):
        token = self._serializer.dumps(req.authname)
        return 'prefs_token.html', {'token': token}

    # IAuthenticator methods
    def authenticate(self, req):
        username = self._check_token(req)
        if username:
            req.environ['REMOTE_USER'] = username
            return username

    def _check_token(self, req):
        if not self._serializer:
            return None

        header = req.get_header('Authorization')
        if not header:
            self.log.debug("No authorization header; skipping token auth")
            return None

        try:
            scheme, token = header.split(None, 1)
        except ValueError:
            # Malformed Authorization header; bail
            self.log.debug("Malformed authorization header; "
                           "skipping token auth")
            return None

        if scheme.lower() != 'bearer':
            self.log.debug("Unrecognized authorization scheme; "
                           "skipping token auth")
            return None

        try:
            username = self._serializer.loads(token)
            self.log.debug("Successful token auth for user {}".format(
                username))
        except BadSignature:
            username = None

        return username
