# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for L{twisted.application.strports}.
"""

from twisted.trial.unittest import TestCase
from twisted.application import strports
from twisted.application import internet
from twisted.internet.protocol import Factory
from twisted.internet.endpoints import TCP4ServerEndpoint



class ServiceTestCase(TestCase):
    """
    Tests for L{strports.service}.
    """

    def test_service(self):
        """
        L{strports.service} returns a L{StreamServerEndpointService}
        constructed with an endpoint produced from
        L{endpoint.serverFromString}, using the same syntax.
        """
        reactor = object() # the cake is a lie
        aFactory = Factory()
        aGoodPort = 1337
        svc = strports.service(
            'tcp:'+str(aGoodPort), aFactory, reactor=reactor)
        self.assertIsInstance(svc, internet.StreamServerEndpointService)

        # See twisted.application.test.test_internet.TestEndpointService.
        # test_synchronousRaiseRaisesSynchronously
        self.assertEqual(svc._raiseSynchronously, True)
        self.assertIsInstance(svc.endpoint, TCP4ServerEndpoint)
        # Maybe we should implement equality for endpoints.
        self.assertEqual(svc.endpoint._port, aGoodPort)
        self.assertIdentical(svc.factory, aFactory)
        self.assertIdentical(svc.endpoint._reactor, reactor)


    def test_serviceDefaultReactor(self):
        """
        L{strports.service} will use the default reactor when none is provided
        as an argument.
        """
        from twisted.internet import reactor as globalReactor
        aService = strports.service("tcp:80", None)
        self.assertIdentical(aService.endpoint._reactor, globalReactor)
