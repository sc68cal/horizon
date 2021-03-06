# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 Nebula, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Exceptions raised by the Horizon code and the machinery for handling them.
"""

import logging
import os
import sys

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest
from django.utils import termcolors
from django.utils.translation import ugettext as _
from django.views.debug import SafeExceptionReporterFilter, CLEANSED_SUBSTITUTE

from cloudfiles import errors as swiftclient
from glanceclient.common import exceptions as glanceclient
from keystoneclient import exceptions as keystoneclient
from novaclient import exceptions as novaclient


LOG = logging.getLogger(__name__)
PALETTE = termcolors.PALETTES[termcolors.DEFAULT_PALETTE]


class HorizonReporterFilter(SafeExceptionReporterFilter):
    """ Error report filter that's always active, even in DEBUG mode. """
    def is_active(self, request):
        return True

    # TODO(gabriel): When Django bug #18379 is fixed, this whole method
    # can be removed: https://code.djangoproject.com/ticket/18379
    def get_traceback_frame_variables(self, request, tb_frame):
        """
        Replaces the values of variables marked as sensitive with
        stars (*********).
        """
        func_name = tb_frame.f_code.co_name
        func = tb_frame.f_globals.get(func_name)
        # Methods won't be in the global namespace, func could be None here...
        if func is None and "self" in tb_frame.f_locals:
            func = getattr(tb_frame.f_locals.get('self'), func_name, None)
        sensitive_variables = getattr(func, 'sensitive_variables', [])
        cleansed = []
        if self.is_active(request) and sensitive_variables:
            if sensitive_variables == '__ALL__':
                # Cleanse all variables
                for name, value in tb_frame.f_locals.items():
                    cleansed.append((name, CLEANSED_SUBSTITUTE))
                return cleansed
            else:
                # Cleanse specified variables
                for name, value in tb_frame.f_locals.items():
                    if name in sensitive_variables:
                        value = CLEANSED_SUBSTITUTE
                    elif isinstance(value, HttpRequest):
                        # Cleanse the request's POST parameters.
                        value = self.get_request_repr(value)
                    cleansed.append((name, value))
                return cleansed
        else:
            # Cleanse only the request if it's one of the frame variables.
            for name, value in tb_frame.f_locals.items():
                if isinstance(value, HttpRequest):
                    # Cleanse the request's POST parameters.
                    value = self.get_request_repr(value)
                cleansed.append((name, value))
            return cleansed


class HorizonException(Exception):
    """ Base exception class for distinguishing our own exception classes. """
    pass


class Http302(HorizonException):
    """
    Error class which can be raised from within a handler to cause an
    early bailout and redirect at the middleware level.
    """
    status_code = 302

    def __init__(self, location, message=None):
        self.location = location
        self.message = message


class NotAuthorized(HorizonException):
    """
    Raised whenever a user attempts to access a resource which they do not
    have role-based access to (such as when failing the
    :func:`~horizon.decorators.require_roles` decorator).

    The included :class:`~horizon.middleware.HorizonMiddleware` catches
    ``NotAuthorized`` and handles it gracefully by displaying an error
    message and redirecting the user to a login page.
    """
    status_code = 401


class NotAuthenticated(HorizonException):
    """
    Raised when a user is trying to make requests and they are not logged in.

    The included :class:`~horizon.middleware.HorizonMiddleware` catches
    ``NotAuthenticated`` and handles it gracefully by displaying an error
    message and redirecting the user to a login page.
    """
    status_code = 403


class NotFound(HorizonException):
    """ Generic error to replace all "Not Found"-type API errors. """
    status_code = 404


class RecoverableError(HorizonException):
    """ Generic error to replace any "Recoverable"-type API errors. """
    status_code = 100  # HTTP status code "Continue"


class ServiceCatalogException(HorizonException):
    """
    Raised when a requested service is not available in the ``ServiceCatalog``
    returned by Keystone.
    """
    def __init__(self, service_name):
        message = 'Invalid service catalog service: %s' % service_name
        super(ServiceCatalogException, self).__init__(message)


class AlreadyExists(HorizonException):
    """
    Exception to be raised when trying to create an API resource which
    already exists.
    """
    def __init__(self, name, resource_type):
        self.attrs = {"name": name, "resource": resource_type}
        self.msg = 'A %(resource)s with the name "%(name)s" already exists.'

    def __repr__(self):
        return self.msg % self.attrs

    def __str__(self):
        return self.msg % self.attrs

    def __unicode__(self):
        return _(self.msg) % self.attrs


class WorkflowError(HorizonException):
    """ Exception to be raised when something goes wrong in a workflow. """
    pass


class WorkflowValidationError(HorizonException):
    """
    Exception raised during workflow validation if required data is missing,
    or existing data is not valid.
    """
    pass


class HandledException(HorizonException):
    """
    Used internally to track exceptions that have gone through
    :func:`horizon.exceptions.handle` more than once.
    """
    def __init__(self, wrapped):
        self.wrapped = wrapped


HORIZON_CONFIG = getattr(settings, "HORIZON_CONFIG", {})
EXCEPTION_CONFIG = HORIZON_CONFIG.get("exceptions", {})


UNAUTHORIZED = (keystoneclient.Unauthorized,
                keystoneclient.Forbidden,
                novaclient.Unauthorized,
                novaclient.Forbidden,
                glanceclient.AuthorizationFailure,
                glanceclient.Unauthorized,
                swiftclient.AuthenticationFailed,
                swiftclient.AuthenticationError)
UNAUTHORIZED += tuple(EXCEPTION_CONFIG.get('unauthorized', []))

NOT_FOUND = (keystoneclient.NotFound,
             novaclient.NotFound,
             glanceclient.NotFound,
             swiftclient.NoSuchContainer,
             swiftclient.NoSuchObject)
NOT_FOUND += tuple(EXCEPTION_CONFIG.get('not_found', []))


# NOTE(gabriel): This is very broad, and may need to be dialed in.
RECOVERABLE = (keystoneclient.ClientException,
               # AuthorizationFailure is raised when Keystone is "unavailable".
               keystoneclient.AuthorizationFailure,
               novaclient.ClientException,
               glanceclient.ClientException,
               swiftclient.Error,
               AlreadyExists)
RECOVERABLE += tuple(EXCEPTION_CONFIG.get('recoverable', []))


def _error_color(msg):
    return termcolors.colorize(msg, **PALETTE['ERROR'])


def handle(request, message=None, redirect=None, ignore=False,
           escalate=False, log_level=None, force_log=None):
    """ Centralized error handling for Horizon.

    Because Horizon consumes so many different APIs with completely
    different ``Exception`` types, it's necessary to have a centralized
    place for handling exceptions which may be raised.

    Exceptions are roughly divided into 3 types:

    #. ``UNAUTHORIZED``: Errors resulting from authentication or authorization
       problems. These result in being logged out and sent to the login screen.
    #. ``NOT_FOUND``: Errors resulting from objects which could not be
       located via the API. These generally result in a user-facing error
       message, but are otherwise returned to the normal code flow. Optionally
       a redirect value may be passed to the error handler so users are
       returned to a different view than the one requested in addition to the
       error message.
    #. RECOVERABLE: Generic API errors which generate a user-facing message
       but drop directly back to the regular code flow.

    All other exceptions bubble the stack as normal unless the ``ignore``
    argument is passed in as ``True``, in which case only unrecognized
    errors are bubbled.

    If the exception is not re-raised, an appropriate wrapper exception
    class indicating the type of exception that was encountered will be
    returned.
    """
    exc_type, exc_value, exc_traceback = sys.exc_info()
    log_method = getattr(LOG, log_level or "exception")
    force_log = force_log or os.environ.get("HORIZON_TEST_RUN", False)
    force_silence = getattr(exc_value, "silence_logging", False)

    # Because the same exception may travel through this method more than
    # once (if it's re-raised) we may want to treat it differently
    # the second time (e.g. no user messages/logging).
    handled = issubclass(exc_type, HandledException)
    wrap = False

    # Restore our original exception information, but re-wrap it at the end
    if handled:
        exc_type, exc_value, exc_traceback = exc_value.wrapped
        wrap = True

    # We trust messages from our own exceptions
    if issubclass(exc_type, HorizonException):
        message = exc_value
    # If the message has a placeholder for the exception, fill it in
    elif message and "%(exc)s" in message:
        message = message % {"exc": exc_value}

    if issubclass(exc_type, UNAUTHORIZED):
        if ignore:
            return NotAuthorized
        request.user_logout()
        if not force_silence and not handled:
            log_method(_error_color("Unauthorized: %s" % exc_value))
        if not handled:
            # We get some pretty useless error messages back from
            # some clients, so let's define our own fallback.
            fallback = _("Unauthorized. Please try logging in again.")
            messages.error(request, message or fallback, extra_tags="login")
        raise NotAuthorized  # Redirect handled in middleware

    if issubclass(exc_type, NOT_FOUND):
        wrap = True
        if not force_silence and not handled and (not ignore or force_log):
            log_method(_error_color("Not Found: %s" % exc_value))
        if not ignore and not handled:
            messages.error(request, message or exc_value)
        if redirect:
            raise Http302(redirect)
        if not escalate:
            return NotFound  # return to normal code flow

    if issubclass(exc_type, RECOVERABLE):
        wrap = True
        if not force_silence and not handled and (not ignore or force_log):
            log_method(_error_color("Recoverable error: %s" % exc_value))
        if not ignore and not handled:
            messages.error(request, message or exc_value)
        if redirect:
            raise Http302(redirect)
        if not escalate:
            return RecoverableError  # return to normal code flow

    # If we've gotten here, time to wrap and/or raise our exception.
    if wrap:
        raise HandledException([exc_type, exc_value, exc_traceback])
    raise exc_type, exc_value, exc_traceback
