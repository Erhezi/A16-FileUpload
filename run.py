"""Main entry point for the File Upload Flask application."""

from app import create_app
from app.config import setting


class PrefixMiddleware:
    """Apply URL path prefix when the upstream server does not set SCRIPT_NAME."""

    def __init__(self, app, prefix):
        self.app = app
        self.prefix = prefix.rstrip('/')

    def __call__(self, environ, start_response):
        if not self.prefix:
            return self.app(environ, start_response)

        script_name = environ.get('SCRIPT_NAME', '')
        path_info = environ.get('PATH_INFO', '')

        # Respect an upstream SCRIPT_NAME if already provided (e.g., IIS app alias).
        if script_name:
            return self.app(environ, start_response)

        # IIS matches the alias in any case, so /FileUpload reaches this app too.
        if not path_info.lower().startswith(self.prefix.lower()):
            location = f"{self.prefix}{path_info if path_info.startswith('/') else '/' + path_info}"
            query_string = environ.get('QUERY_STRING', '')
            if query_string:
                location = f"{location}?{query_string}"
            start_response('308 Permanent Redirect', [('Location', location)])
            return [b'']

        environ['SCRIPT_NAME'] = self.prefix
        environ['PATH_INFO'] = path_info[len(self.prefix):] or '/'
        return self.app(environ, start_response)


# Unset means production, so a missing setting can never turn on the development auth bypass.
ENV = setting('ENVIRONMENT', 'production').lower()
URL_PREFIX = setting('URL_PREFIX', '/fileupload')

app = create_app(ENV)

if URL_PREFIX:
    app.config['APPLICATION_ROOT'] = URL_PREFIX
    app.wsgi_app = PrefixMiddleware(app.wsgi_app, URL_PREFIX)


if __name__ == '__main__':
    print(f"Starting Flask development server (ENV={ENV}, URL_PREFIX={URL_PREFIX or '(none)'})")
    # Loopback only: in development the auth bypass treats every request as the local user.
    app.run(host='127.0.0.1', port=int(setting('PORT', '5016')), debug=(ENV != 'production'))
