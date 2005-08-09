# -*- test-case-name: vertex.test.test_q2q -*-
# Copyright 2005 Divmod, Inc.  See LICENSE file for details

# stdlib
import itertools
import md5
import struct

from zope.interface import implements

# twisted
from twisted.internet import reactor, defer, interfaces, protocol
from twisted.internet.main import CONNECTION_DONE
from twisted.python import log
from twisted.python.failure import Failure
from twisted.application import service

# atop
from axiom.extime import Time
from axiom.slotmachine import _structlike

# vertex
from vertex import sslverify, juice, subproducer, ptcp
from vertex import endpoint
from vertex.conncache import ConnectionCache

MESSAGE_PROTOCOL = 'q2q-message'
port = 8788

class ConnectionError(Exception):
    pass

class AttemptsFailed(ConnectionError):
    pass

class NoAttemptsMade(ConnectionError):
    pass

class BadCertificateRequest(sslverify.VerifyError):
    pass

def _endeferify(funcsAndArgs, failureChain=None):
    """ Take a list of function/arg/kw tuples and call them each in turn, returning
    a Deferred that fires with the first successful attempt.  This is used for
    attempting each connection mechanism in turn and then finally succeeding.

    @param funcsAndArgs: an iterable of (func, args, kw) tuples.
    @param lastFailure: a Failure instance (or None)
    """
    stuff = iter(funcsAndArgs)
    if failureChain is None:
        failureChain = []
        if len(funcsAndArgs) == 0:
            return defer.fail(NoAttemptsMade(
                    "there was no available connection path"))
    try:
        function, arguments, keywords = stuff.next()
    except StopIteration:
        return defer.fail(AttemptsFailed(failureChain))
    else:
        D = defer.maybeDeferred(function, *arguments, **keywords)
        def _fcrecurse(f):
            failureChain.append(f)
            return _endeferify(stuff, failureChain)
        D.addErrback(_fcrecurse)
        return D

class IgnoreConnectionFailed(protocol.ClientFactory):
    def __init__(self, realFactory):
        self.realFactory = realFactory

    def clientConnectionLost(self, connector, reason):
        self.realFactory.clientConnectionLost(connector, reason)

    def clientConnectionFailed(self, connector, reason):
        pass

    def buildProtocol(self, addr):
        return self.realFactory.buildProtocol(addr)

class Q2QAddress(object):
    def __init__(self, domain, resource=None):
        self.resource = resource
        self.domain = domain

    def domainAddress(self):
        """ Return an Address object which is the same as this one with ONLY the
        'domain' attribute set, not 'resource'.

        May return 'self' if 'resource' is already None.
        """
        if self.resource is None:
            return self
        else:
            return Q2QAddress(self.domain)

    def claimedAsIssuerOf(self, cert):
        """
        Check if the information in a provided certificate *CLAIMS* to be issued by
        this address.

        PLEASE NOTE THAT THIS METHOD IS IN NO WAY AUTHORITATIVE.  It does not
        perform any cryptographic checks.

        Currently this check is if L{Q2QAddress.__str__}C{(self)} is equivalent
        to the commonName on the certificate's issuer.
        """
        return cert.getIssuer().commonName == str(self)

    def claimedAsSubjectOf(self, cert):
        """
        Check if the information in a provided certificate *CLAIMS* to be
        provided for use by this address.

        PLEASE NOTE THAT THIS METHOD IS IN NO WAY AUTHORITATIVE.  It does not
        perform any cryptographic checks.

        Currently this check is if L{Q2QAddress.__str__}C{(self)} is equivalent
        to the commonName on the certificate's subject.
        """
        return cert.getSubject().commonName == str(self)

    def __cmp__(self, other):
        if not isinstance(other, Q2QAddress):
            return cmp(self.__class__, other.__class__)
        return cmp((self.domain, self.resource), (other.domain, other.resource))

    def __iter__(self):
        return iter((self.resource, self.domain))

    def __str__(self):
        """
        Return a string of the normalized form of this address.  e.g.:

            glyph@divmod.com    # for a user
            divmod.com          # for a domain
        """
        if self.resource:
            resource = self.resource + '@'
        else:
            resource = ''
        return resource + self.domain

    def __repr__(self):
        return '<Q2Q at %s>' % self.__str__()

    def __hash__(self):
        return hash(str(self))

    def fromString(cls, string):
        args = string.split("@",1)
        args.reverse()
        return cls(*args)
    fromString = classmethod(fromString)


class VirtualTransportAddress:
    def __init__(self, underlying):
        self.underlying = underlying

    def __repr__(self):
        return 'VirtualTransportAddress(%r)' % (self.underlying,)

class Q2QTransportAddress:
    """
    The return value of getPeer() and getHost() for Q2Q-enabled transports.
    Passed to buildProtocol of factories passed to listenQ2Q.

    @ivar underlying: The return value of the underlying transport's getPeer()
    or getHost(); an address which indicates the path which the bytes carrying
    Q2Q traffic are travelling over.  It is tempting to think of this as a
    'physical' layer but that it not necessarily accurate; there are
    potentially multiple layers of wrapping on any Q2Q transport, as an SSL
    transport may be tunnelled over a UDP NAT-traversal layer.  Implements
    C{IAddress} from Twisted, for all the good that will do you.

    @ivar logical: a L{Q2QAddress}, The logical peer; the user ostensibly
    listening to data on the other end of this transport.

    @ivar protocol: a L{str}, the name of the protocol that is connected.
    """

    def __init__(self, underlying, logical, protocol):
        self.underlying = underlying
        self.logical = logical
        self.protocol = protocol

    def __repr__(self):
        return 'Q2QTransportAddress(%r, %r, %r)' % (
            self.underlying,
            self.logical,
            self.protocol)

class Q2QAddressArgument(juice.Argument):
    fromString = Q2QAddress.fromString
    toString = Q2QAddress.__str__

class HostPort(juice.Argument):
    def toString(self, inObj):
        return "%s:%d" % tuple(inObj)

    def fromString(self, inStr):
        host, sPort = inStr.split(":")
        return (host, int(sPort))



class _Base64Wrapped(juice.Base64Binary):
    def toString(self, arg):
        assert isinstance(arg, self.loader), "%r not %r" % (arg, self.loader)
        return juice.Base64Binary.toString(self, arg.dump())

    def fromString(self, arg):
        return self.loader.load(juice.Base64Binary.fromString(self, arg))

class CertReq(_Base64Wrapped):
    loader = sslverify.CertificateRequest

class Cert(_Base64Wrapped):
    loader = sslverify.Certificate

class SimpleStringList(juice.Argument):
    separator = ', '
    def toString(self, inObj):
        for inSeg in inObj:
            assert self.separator not in inSeg, \
                "%r not allowed to contain elements containing %r" % (inObj, self.separator)
        return self.separator.join(inObj)

    def fromString(self, inString):
        if inString == '':
            return []
        return inString.split(self.separator)

class VirtualMethod:
    def __init__(self, virt=None):
        pass

    relayable = False

    def toString(self):
        return 'virtual'

    def __repr__(self):
        return '<%s>' % (self.toString(),)

    def attemptConnect(self, q2qproto, connectionID, From, to,
                       protocolName, protocolFactory, localudp):
        """
        Returns a deferred which fires the protocol that results from
        establishing a virtual connection.
        """

        innerTransport = VirtualTransport(
            q2qproto, From, to,
            protocolName, connectionID,
            protocolFactory,
            isClient=True)

        def startit(result):
            proto = innerTransport.startProtocol()
            return proto

        return Virtual(Id=connectionID).do(q2qproto).addCallback(
            startit)

from twisted.internet import protocol

class TCPMethod:
    def __init__(self, hostport):
        self.host, port = hostport.split(':')
        self.port = int(port)

    relayable = True
    ptype = 'tcp'

    def toString(self):
        return '%s@%s:%d' % (self.ptype, self.host, self.port)

    def __repr__(self):
        return '<%s>'%self.toString()

    def doHostPortConnect(self, q2qproto, host, port, f):
        reactor.connectTCP(host, port, f)

    def attemptConnect(self, q2qproto, connectionID, From, to,
                       protocolName, protocolFactory, localudp):
        cidcf = Q2QTCPConnector(q2qproto.service, connectionID, From, to,
                                protocolFactory)
        self.doHostPortConnect(q2qproto, self.host, self.port, cidcf)
        return cidcf.deferred


class PTCPMethod(TCPMethod):
    """Pseudo-TCP method.
    """
    ptype = 'ptcp'

    def doHostPortConnect(self, q2qproto, host, port, f):
        q2qproto.service.dispatcher.connectPTCP(host, port, f)


class RPTCPMethod(TCPMethod):
    """ Certain NATs respond very poorly to seed traffic: e.g. if they receive
    unsolicited traffic to a particular port, they will make that outbound port
    unavailable for outbound traffic originated internally.  The
    Reverse-Pseudo-TCP method is a way to have the *sender* send the first UDP
    packet, so they will bind it.

    This is a worst-case scenario: if both ends of the connection have NATs
    which behave this way, there is no way to establish a connection.
    """

    ptype = 'rptcp'

    def attemptConnect(self, q2qproto, connectionID, From, to, protocolName,
                       protocolFactory, localudp):
        realLocalUDP = q2qproto.service.dispatcher.seedNAT((self.host, self.port))
        # self.host and self.port are remote host and port
        # realLocalUDP is a local port

        # The arguments here are given from the perspective of the recipient of
        # the command. we are asking the recipient of the connection to map a
        # NAT entry of a pre-existing listening UDP socket on their end of the
        # connection by sending us some traffic.  therefore the src is their
        # endpoint, the dst is our endpoint, the user we are asking them to
        # send TO is us, the user we are asking them to accept this FROM is us.

        # we include protocol as an arg because this is helpful for relaying.

        cidcf = Q2QTCPConnector(q2qproto.service, connectionID, From, to,
                                protocolFactory)
        return BindUDP(
            q2qsrc=to,
            q2qdst=From,
            protocol=protocolName,
            udpsrc=(self.host, self.port),
            udpdst=(q2qproto._determinePublicIP(), realLocalUDP)
            ).do(q2qproto).addCallback(lambda bound:
                                       q2qproto.service.dispatcher.connectPTCP(
                self.host, self.port, cidcf))



class UnknownMethod:

    relayable = True

    def __init__(self, S):
        self.string = S

    def attemptConnect(self, q2qproto, connectionID, From, to,
                       protocolName, protocolFactory, localudp):
        return defer.fail(Failure(ConnectionError(
                    "unknown connection method: %s" % (self.string,))))


_methodFactories = {'virtual': VirtualMethod,
                    'tcp': TCPMethod,
                    'ptcp': PTCPMethod,
                    'rptcp': RPTCPMethod}

class MethodsList(SimpleStringList):
    def toString(self, inObj):
        return super(MethodsList, self).toString([x.toString() for x in inObj])

    def fromString(self, inString):
        strings = super(MethodsList, self).fromString(inString)
        accumulator = []
        accumulate = accumulator.append
        for string in strings:
            f = string.split("@",1)
            factoryName = f[0]
            if len(f)>1:
                factoryData = f[1]
            else:
                factoryData = ''
            methodFactory = _methodFactories.get(factoryName, None)
            if methodFactory is None:
                factory = UnknownMethod(string)
            else:
                factory = methodFactory(factoryData)
            accumulate(factory)
        return accumulator


class Secure(juice.Command):

    commandName = "secure"
    arguments = [
        ('From', Q2QAddressArgument(optional=True)),
        ('to', Q2QAddressArgument()),
        ('authorize', juice.Boolean())
        ]

    def makeResponse(cls, objects, proto):
        return juice.TLSBox(*objects)
    makeResponse = classmethod(makeResponse)


class Listen(juice.Command):
    """
    A simple command for registering interest with an active Q2Q connection to
    hear from a server when others come calling.

        C: -Command: Listen
        C: -Ask: 1
        C: From: glyph@divmod.com
        C: Protocols: q2q-example, q2q-example2
        C: Description: some simple protocols
        C:
        S: -Answer: 1
        S:

    This puts some state on the server side that will affect any Connect
    commands with q2q-example or q2q-example2 in the Protocol: header.
    """

    commandName = 'listen'
    arguments = [
        ('From', Q2QAddressArgument()),
        ('protocols', SimpleStringList()),
        ('description', juice.Unicode())]

    result = []

class ConnectionStartBox(juice.Box):
    def __init__(self, __transport):
        super(ConnectionStartBox, self).__init__()
        self.virtualTransport = __transport

    def sendTo(self, proto):
        super(ConnectionStartBox, self).sendTo(proto)
        self.virtualTransport.startProtocol()

class Virtual(juice.Command):
    commandName = 'virtual'
    result = []

    arguments = [('id', juice.String())]

    def makeResponse(cls, objects, proto):
        tpt = objects.pop('__transport__')
        return juice.objectsToStrings(objects, cls.response,
                                      ConnectionStartBox(tpt),
                                      proto)

    makeResponse = classmethod(makeResponse)

class Identify(juice.Command):
    """
    Respond to an IDENTIFY command with a self-signed certificate for the
    domain requested, assuming we are an authority for said domain.

        C: -Command: Identify
        C: -Ask: 1
        C: Domain: divmod.com
        C:
        S: -Answer: 1
        S: Certificate: <<<base64-encoded self-signed certificate of divmod.com>>>
        S:

    """

    commandName = 'identify'

    arguments = [('subject', Q2QAddressArgument())]

    response = [('certificate', Cert())]

class BindUDP(juice.Command):
    """
    See UDPXMethod
    """

    commandName = 'bind-udp'

    arguments = [
        ('protocol', juice.String()),
        ('q2qsrc', Q2QAddressArgument()),
        ('q2qdst', Q2QAddressArgument()),
        ('udpsrc', HostPort()),
        ('udpdst', HostPort()),
        ]

    response = []

class SourceIP(juice.Command):
    """
    Ask a server on the public internet what my public IP probably is.

        C: -Command: Source-IP
        C: -Ask: 1
        C:
        S: -Answer: 1
        S: IP: 4.3.2.1
        S:

    """

    commandName = 'source-ip'

    arguments = []

    response = [('ip', juice.String())]

class Inbound(juice.Command):
    """
    Request information about where to connect to a particular resource.

    Generally speaking this is an "I want to connect to you" request.

    The format of this request is transport neutral except for the optional
    'Udp_Source' header, which specifies an IP/Port pair for all receiving peers to
    send an almost-empty (suggested value of '\\r\\n') UDP packet to to help
    with NAT traversal issues.

    See L{Q2QService.connectQ2Q} for details.

        C: -Command: Inbound
        C: -Ask: 1
        C: From: glyph@divmod.com
        C: Id: 681949ffa3be@twistedmatrix.com
        C: To: radix@twistedmatrix.com
        C: Protocol: q2q-example
        C: Udp_Source: 1.2.3.4:4321
        C:
        S: -Answer: 1
        S: Listeners:
        S:  Description: at lab
        S:  Methods: tcp@18.38.12.4:3827, virtual
        S:
        S:  Description: my home machine
        S:  Methods: tcp@187.48.38.3:49812, udp@187.48.38.3:49814, virtual

    Now the connection-id has been registered and either client or server can
    issue WRITE or CLOSE commands.

    Failure modes:

        "NotFound": the toResource or toDomain is invalid, or the resource does
        not speak that protocol.

        "VerifyError": Authenticity or security for the requested connection
        could not be authorized.  This is a fatal error: the connection will be
        dropped.

    The "Udp_Source" header indicates the address from which this Inbound chain
    originated.  It is to be used to establish connections where possible
    between NATs which require traffic between two host/port pairs to be
    bidirectional before a "hole" is established, such as port restricted cone
    and symmetric NATs.  (Note, this only has about a 30% probability of
    working on a symmetric NAT, but it's worth trying sometimes anyway).  Any
    UDP-based connection methods (currently only Gin, but in principle others
    such as RTP, RTCP, SIP and Quake traffic) that wish to use this connection
    must first send some garbage traffic to the host/port specified by the
    "Udp_Source" header.

    The response is a list of "listeners" - a small (unicode) textual
    description of a host, plus a list of methods describing how to connect to
    it.
    """

    commandName = 'inbound'
    arguments = [('From', Q2QAddressArgument()),
                 ('to', Q2QAddressArgument()),
                 ('protocol', juice.String()),
                 ('udp_source', HostPort(optional=True))]

    response = [('listeners', juice.JuiceList(
                [('id', juice.String()),
                 ('certificate', Cert(optional=True)),
                 ('methods', MethodsList()),
                 ('expires', juice.Time()),
                 ('description', juice.Unicode())]))]

    errors = {KeyError: "NotFound"}
    fatalErrors = {sslverify.VerifyError: "VerifyError"}

class Outbound(juice.Command):
    """Similar to Inbound, but _requires that the recipient already has the
    id parameter as an outgoing connection attempt_.
    """
    commandName = 'outbound'

    arguments = [('From', Q2QAddressArgument()),
                 ('to', Q2QAddressArgument()),
                 ('protocol', juice.String()),
                 ('id', juice.String()),
                 ('methods', MethodsList())]

    response = []

    errors = {AttemptsFailed: 'AttemptsFailed'}

class Sign(juice.Command):
    commandName = 'sign'
    arguments = [('certificate_request', CertReq()),
                 ('password', juice.Base64Binary())]

    response = [('certificate', Cert())]

    errors = {KeyError: "NoSuchUser",
              BadCertificateRequest: "BadCertificateRequest"}

def textEncode(S):
    return S.encode('base64').replace('\n', '')

def textDecode(S):
    return S.decode('base64')

def safely(f, *a, **k):
    """try/except around something, w/ twisted error handling.
    """
    try:
        f(*a,**k)
    except:
        log.err()

class Q2Q(juice.Juice, subproducer.SuperProducer):
    """ Quotient to Quotient protocol.

    At a low level, this uses a protocol called 'Juice' (JUice Is Concurrent
    Events), which is a simple rfc2822-inspired (although not -compliant)
    protocol for request/response pair hookup.

    At a higher level, it provides a mechanism for SSL certificate exchange,
    looking up physical locations of users' data, and switching into other
    protocols after an initial handshake.

    @ivar publicIP: The IP that the other end of the connection claims to know
    us by.  This will be used when responding to L{Inbound} commands if the Q2Q
    service I am attached to does not specify a public IP to use.

    @ivar authorized: A boolean indicating whether SSL verification has taken
    place to ensure that this connection's peer has claimed an accurate identity.
    """

    protocolName = 'q2q'
    service = None
    publicIP = None
    authorized = False

    def __init__(self, *a, **kw):
        """ Q2Q instances should only be created by Q2QService.  See
        L{Q2QService.connectQ2Q} and L{Q2QService.listenQ2Q}.
        """
        subproducer.SuperProducer.__init__(self)
        juice.Juice.__init__(self, *a, **kw)

    def connectionMade(self):
        ""
        self.producingTransports = {}
        self.connections = {}
        self.listeningClient = []
        self.connectionObservers = []
        if self.service.publicIP is None:
            self.service.publicIP = self.transport.getHost().host
            self.service._publicIPIsReallyPrivate = True
            def rememberPublicIP(pubip):
                ip = pubip['ip']
                log.msg('remembering public ip as %r' % ip)
                self.publicIP = ip
                self.service.publicIP = ip
                self.service._publicIPIsReallyPrivate = False
            SourceIP().do(self).addCallback(rememberPublicIP)

    def connectionLost(self, reason):
        ""
        juice.Juice.connectionLost(self, reason)
        self.producingTransports = {}
        for key, value in self.listeningClient:
            self.service.listeningClients[key].remove(value)
        self.listeningClient = []
        for xport in self.connections.values():
            safely(xport.connectionLost, reason)
        for observer in self.connectionObservers:
            safely(observer)

    def notifyOnConnectionLost(self, observer):
        ""
        self.connectionObservers.append(observer)

    def command_BIND_UDP(self, q2qsrc, q2qdst, udpsrc, udpdst, protocol):

        # we are representing the src, because they are the ones being told to
        # originate a UDP packet.

        self.verifyCertificateAllowed(q2qsrc, q2qdst)

        # if I've got a local factory for this 3-tuple, do the bind if I own
        # this IP...
        srchost, srcport = udpsrc

        if (self.service.getLocalFactories(q2qdst, q2qsrc, protocol)
            and srchost == self._determinePublicIP()):
            self.service.dispatcher.seedNAT((udpdst, srcport))
            return dict()
        else:
            for (listener, listenCert, desc
                 ) in self.service.listeningClients.get(
                (q2qsrc, protocol), ()):
                if listener.transport.getPeer().host == srchost:
                    return BindUDP(q2qsrc=q2qsrc,
                                   q2qdst=q2qdst,
                                   udpsrc=udpsrc,
                                   udpdst=udpdst,
                                   protocol=protocol).do(listener)
        raise ConnectionError("unable to find appropriate UDP binder")

    command_BIND_UDP.command = BindUDP

    def command_IDENTIFY(self, subject):
        """
        Implementation of L{Identify}.
        """
        ourCA = self.service.certificateStorage.getPrivateCertificate(str(subject))
        return dict(Certificate=ourCA)

    command_IDENTIFY.command = Identify

    def verifyCertificateAllowed(self,
                                 ourAddress,
                                 theirAddress):
        """
        Check that the certificate currently in use by this transport is valid to
        claim that the connection offers authorization for this host speaking
        for C{ourAddress}, to a host speaking for C{theirAddress}.  The remote
        host (the one claiming to use theirAddress) may have a certificate
        which is issued for the domain for theirAddress or the full address
        given in theirAddress.

        This method runs _after_ cryptographic verification of the validity of
        certificates, although it does not perform any cryptographic checks
        itself.  It depends on SSL connection handshaking - *and* the
        particular certificate lookup logic which prevents spoofed Issuer
        fields, to work properly.  However, all it checks is the X509 names
        present in the certificates matching with the application-level
        security claims being made by our peer.

        Example:

        This is valid because both parties have properly signed certificates
        for their usage from the domain they have been issued:

            our current certficate:
                issuer: divmod.com
                subject: glyph@divmod.com
            their current certificate:
                issuer: twistedmatrix.com
                subject: exarkun@twistedmatrix.com
            Arguments to verifyCertificateAllowed:
                ourAddress: glyph@divmod.com
                theirAddress: exarkun@twistedmatrix.com
            Result of verifyCertificateAllowed: None

        This is invalid because domain certificates are always *self*-signed in
        Q2Q; verisign is not a trusted certificate authority for the entire
        internet as with some other TLS implementations:

            our current certificate:
                issuer: divmod.com
                subject: divmod.com
            their current certificate:
                issuer: verisign.com
                subject: twistedmatrix.com
            Arguments to verifyCertificateAllowed:
                ourAddress: divmod.com
                theirAddress: twistedmatrix.com
            Result of verifyCertificateAllowed: exception VerifyError raised

        This case is OK rather than invalid because our current certificate, we
        assume is under the control of this side of the connection, so *any*
        claimed subject is considered acceptable.

            our current certificate:
                issuer: divmod.com
                subject: divmod.com
            their current certificate:
                issuer: divmod.com
                subject: glyph@twistedmatrix.com
            Arguments to verifyCertificateAllowed:
                ourAddress: divmod.com
                theirAddress: glyph@twistedmatrix.com
            Result of verifyCertificateAllowed: None

        This case is OK because the user is claiming to be anonymous; there is
        also a somewhat looser cryptographic check applied to signatures for
        anonymous connections.

        Accept anonymous connections with caution:

            our current certificate:
                issuer: divmod.com
                subject: divmod.com
            their current certificate:
                issuer: @
                subject: @
            arguments to verifyCertificateAllowed:
                ourAddress: divmod.com
                theirAddress: @
            Result of verifyCertificateAllowed: None


        @param ourAddress: a L{Q2QAddress} representing the address that we are
        supposed to have authority for, requested by our peer.

        @param theirAddress: a L{Q2QAddress} representing the address that our
        network peer claims to be communicating on behalf of.  For example, if
        our peer is foobar.com they may claim to be operating on behalf of any
        user @foobar.com.

        @raise: L{sslverify.VerifyError} if the certificates do not match the
        claimed addresses.
        """

        # XXX TODO: Somehow, it's got to be possible for a single cluster to
        # internally claim to be agents of any other host when issuing a
        # CONNECT; in other words, we always implicitly trust ourselves.  Also,
        # we might want to issue anonymous CONNECTs over unencrypted
        # connections.

        # IOW: *we* can sign a certificate to be whoever, but the *peer* can
        # only sign the certificate to be the peer.

        # The easiest way to make this work is to issue ourselves a wildcard
        # certificate.

        if not self.authorized:
            if theirAddress.domain == '':
                # XXX TODO: document this rule, anonymous connections are
                # allowed to not be authorized because they are not making any
                # claims about who they are

                # XXX also TODO: make it so that anonymous connections are
                # disabled by default for most protocols
                return True
            raise sslverify.VerifyError("No official negotiation has taken place.")

        peerCert = sslverify.Certificate.peerFromTransport(self.transport)
        ourCert = self.hostCertificate

        ourClaimedDomain = ourAddress.domainAddress()
        theirClaimedDomain = theirAddress.domainAddress()

        # Sanity check #1: did we pick the right certificate on our end?
        if not ourClaimedDomain.claimedAsIssuerOf(ourCert):
            raise sslverify.VerifyError(
                "Something has gone horribly wrong: local domain mismatch "
                "claim: %s actual: %s" % (ourClaimedDomain,
                                          ourCert.getIssuer()))
        if theirClaimedDomain.claimedAsIssuerOf(peerCert):
            # Their domain issued their certificate.
            if theirAddress.claimedAsSubjectOf(peerCert) or theirClaimedDomain.claimedAsSubjectOf(peerCert):
                return
        elif ourClaimedDomain.claimedAsIssuerOf(peerCert):
            # *our* domain can spoof *anything*
            return
        elif ourAddress.claimedAsIssuerOf(peerCert):
            # Neither our domain nor their domain signed this.  Did *we*?
            # (Useful in peer-to-peer persistent transactions where we don't
            # want the server involved: exarkun@twistedmatrix.com can sign
            # glyph@divmod.com's certificate).
            return

        raise sslverify.VerifyError(
            "Us: %s Them: %s "
            "TheyClaimWeAre: %s TheyClaimTheyAre: %s" %
            (ourCert, peerCert,
             ourAddress, theirAddress))

    def command_LISTEN(self, protocols, From, description):
        """
        Implementation of L{Listen}.
        """
        # The peer is coming from a client-side representation of the user
        # described by 'From', and talking *to* a server-side representation of
        # the user described by 'From'.
        self.verifyCertificateAllowed(From, From)
        theirCert = sslverify.Certificate.peerFromTransport(self.transport)
        for protocolName in protocols:
            if protocolName.startswith('.'):
                raise sslverify.VerifyError(
                    "Internal protocols are for server-server use _only_: %r" %
                    protocolName)

            key = (From, protocolName)
            value = (self, theirCert, description)
            log.msg("%r listening for %r" % key)
            self.listeningClient.append((key, value))
            self.service.listeningClients.setdefault(key, []).append(value)
        return {}

    command_LISTEN.command = Listen

    def command_INBOUND(self, From, to, protocol, udp_source=None):
        """
        Implementation of L{Inbound}.
        """
        # Verify stuff!

        self.verifyCertificateAllowed(to, From)

        # 2-tuples of factory, description
        srvfacts = self.service.getLocalFactories(From, to, protocol)

        result = []             # list of listener dicts

        if srvfacts:
            localMethods = []
            publicIP = self._determinePublicIP()
            privateIP = self._determinePrivateIP()
            if self.service.inboundTCPPort is not None:
                tcpPort = self.service.inboundTCPPort.getHost().port
                localMethods.append(TCPMethod(
                        '%s:%d' %
                        (publicIP, tcpPort)))
                if publicIP != privateIP:
                    localMethods.append(TCPMethod(
                            '%s:%d' %
                            (privateIP, tcpPort)))

            if udp_source is None:
                log.msg("udp_source was none on inbound")
            else:
                if self.service.dispatcher is None:
                    log.msg("udp_source %s:%d, but dispatcher not running" %
                            udp_source)
                else:
                    remoteUDPHost, remoteUDPPort = udp_source
                    udpPort = self.service.dispatcher.seedNAT(udp_source)
                    if remoteUDPHost == publicIP and publicIP != privateIP:
                        log.msg(
                            "Remote IP matches local, public IP %r;"
                            " preferring internal IP %r" % (publicIP, privateIP))
                        localMethods.append(
                            PTCPMethod("%s:%d" % (privateIP, udpPort)))
                    localMethods.append(
                        PTCPMethod("%s:%d" % (publicIP, udpPort)))
                    udpxPort = self.service.dispatcher.bindNewPort()
                    localMethods.append(
                        RPTCPMethod("%s:%d" % (publicIP, udpxPort)))

            localMethods.append(VirtualMethod())
            log.msg('sending local methods to peer: %r' % (localMethods,))

            for serverFactory, description in srvfacts:
                expiryTime, listenID = self.service.mapListener(
                    to, From, protocol, serverFactory)
                result.append(dict(id=listenID,
                                   expires=expiryTime,
                                   methods=localMethods,
                                   description=description))

        # We've looked for our local factory.  Let's see if we have any
        # listening protocols elsewhere.

        key = (to, protocol)
        if key in self.service.listeningClients:
            args = dict(From=From,
                        To=to,
                        Protocol=protocol,
                        UDP_Source=udp_source)
            DL = []
            for listener, listenCert, desc in self.service.listeningClients[key]:
                DL.append(Inbound(**args).do(listener).addCallback(
                    self._massageClientInboundResponse, listener, result))

            def allListenerResponses(x):
                return dict(listeners=result)
            return defer.DeferredList(DL).addCallback(allListenerResponses)
        else:
            return dict(listeners=result)

    command_INBOUND.command = Inbound

    def _massageClientInboundResponse(self, inboundResponse, listener, result):
        irl = inboundResponse['listeners']
        for listenerInfo in irl:
            # inboundResponse['description'] = ??? trust client version for
            # now... maybe the server doesn't even need to know about
            # descriptions...?
            listenerInfo['methods'] = [
                meth for meth in listenerInfo['methods'] if meth.relayable]
            # make sure that the certificate that we're relaying matches the
            # certificate that they gave us!
            if listenerInfo['methods']:
                allowedCertificate = sslverify.Certificate.peerFromTransport(
                    listener.transport)
                listenerInfo['certificate'] = allowedCertificate
                result.append(listenerInfo)

    def _determinePublicIP(self):
        reservePublicIP = None
        if self.service.publicIP is not None:
            if self.service._publicIPIsReallyPrivate:
                reservePublicIP = self.service.publicIP
            else:
                return self.service.publicIP
        if self.publicIP is not None:
            return self.publicIP
        if reservePublicIP is not None:
            return reservePublicIP
        return self._determinePrivateIP()

    def _determinePrivateIP(self):
        return self.transport.getHost().host

    def command_SOURCE_IP(self):
        result = {'ip': self.transport.getPeer().host}
        return result

    command_SOURCE_IP.command = SourceIP

    def juice_WRITE(self, box):
        """

        Respond to a WRITE command, sending some data over a virtual channel
        created by VIRTUAL.  The answer is simply an acknowledgement, as it is
        simply meant to note that the write went through without errors.

            C: -Command: Write
            C: -Ask: 1
            C: -Length: 13
            C: Id: glyph@divmod.com->radix@twistedmatrix.com:q2q-example:0
            C:
            C: HELLO WORLD
            C:
            S: -Answer: 1
            S:

        """
        connection = self.connections[box['id']]
        data = box[juice.BODY]
        connection.dataReceived(data)
        return juice.Box()

    def juice_CLOSE(self, box):
        """

        Respond to a CLOSE command, dumping some data onto the stream.  As with
        WRITE, this returns an empty acknowledgement.

            C: -Command: Close
            C: -Ask: 1
            C: Id: glyph@divmod.com->radix@twistedmatrix.com:q2q-example:0
            C:
            S: -Answer: 1
            S:

        """
        from twisted.internet.main import CONNECTION_DONE
        self.connections[box['id']].connectionLost(CONNECTION_DONE)
        return juice.Box()

    def command_SIGN(self, certificate_request, password):
        """
        Respond to a request to sign a CSR for a user or agent located within
        our domain.
        """
        subj = certificate_request.getSubject()

        if subj.keys() != ['CN']:
            raise BadCertificateRequest(
                "Certificate requested with bad subject: %s" % (subj.keys(),))

        username, domain = subj.commonName.split("@")

        CS = self.service.certificateStorage
        ourCert = CS.getPrivateCertificate(domain)

        D = self.service.checkPrivateSecret(username, domain, password)

        def _(ignored):
            newCert = ourCert.signRequestObject(certificate_request,
                                                CS.genSerial(domain))
            log.msg('signing certificate for user %s@%s: %s' % (
                    username, domain, newCert.digest()))
            return dict(certificate=newCert)
        return D.addCallback(_)


    command_SIGN.command = Sign


    def command_SECURE(self, to, From, authorize):
        """
        Response to a SECURE command, starting TLS when necessary, and using a
        certificate identified by the To: header.

            C: -Command: Secure
            C: -Ask: 1
            C: To: divmod.com
            C: From: twistedmatrix.com
            C: Authorize: True
            C:
        --- Client Starts TLS here with twistedmatrix.com certificate ---
            S: -Answer: 1
            S:
        --- Server Starts TLS here with divmod.com certificate ---

        """
        if self.hostCertificate is not None:
            raise RuntimeError("Re-encrypting already encrypted connection")
        CS = self.service.certificateStorage
        ourCert = CS.getPrivateCertificate(str(to.domainAddress()))
        if authorize:
            D = CS.getSelfSignedCertificate(str(From.domainAddress()))
        else:
            self.authorized = False
            return [ourCert]

        def hadCert(peerSigned):
            self.authorized = True
            tcpeer = self.transport.getPeer()
            # XXX 'port' is insane here, but we lack a better number to hash
            # against.  perhaps the SECURE request should give a reciprocal
            # connection identifier...?
            self.service.secureConnectionCache.cacheUnrequested(
                endpoint.TCPEndpoint(tcpeer.host, port),
                (From, to.domain, authorize), self)
            return [ourCert, peerSigned]

        def didNotHaveCert(err):
            err.trap(KeyError)
            return self._retrieveRemoteCertificate(From, port)

        D.addErrback(didNotHaveCert)
        D.addCallback(hadCert)

        return D

    command_SECURE.command = Secure


    def _retrieveRemoteCertificate(self, From, port=port):
        """
        The entire conversation, starting with TCP handshake and ending at
        disconnect, to retrieve a foreign domain's certificate for the first
        time.
        """
        CS = self.service.certificateStorage
        host = str(From.domainAddress())
        p = juice.Juice(False)
        p.wrapper = self.wrapper
        f = protocol.ClientCreator(reactor, lambda: p)
        connD = f.connectTCP(host, port)

        def connected(proto):
            dhost = From.domainAddress()
            iddom = Identify(subject=dhost).do(proto)
            def gotCert(identifyBox):
                theirCert = identifyBox['certificate']
                theirIssuer = theirCert.getIssuer().commonName
                theirName = theirCert.getSubject().commonName
                if (theirName != str(dhost)):
                    raise sslverify.VerifyError(
                        "%r claimed it was %r in IDENTIFY response"
                        % (theirName, dhost))
                if (theirIssuer != str(dhost)):
                    raise sslverify.VerifyError(
                        "self-signed %r claimed it was issued by "
                        "%r in IDENTIFY response" % (dhost, theirIssuer))
                def storedCert(ignored):
                    return theirCert
                return CS.storeSelfSignedCertificate(
                    str(dhost), theirCert).addCallback(storedCert)
            def nothingify(x):
                proto.transport.loseConnection()
                return x
            return iddom.addCallback(gotCert).addBoth(nothingify)
        connD.addCallback(connected)
        return connD


    def secure(self, fromAddress, toAddress,
               fromCertificate, foreignCertificateAuthority=None,
               authorize=True):
        """Return a Deferred which fires True when this connection has been secured as
        a channel between fromAddress (locally) and toAddress (remotely).
        Raises an error if this is not possible.
        """
        if self.hostCertificate is not None:
            raise RuntimeError("Re-securing already secured connection.")

        def _cbSecure(response):
            if foreignCertificateAuthority is None:
                # *Don't* verify the certificate in this case.
                self.startTLS(fromCertificate)
                self.authorized = False
            else:
                self.startTLS(fromCertificate, foreignCertificateAuthority)
                self.authorized = True
            return True
        return Secure(From=fromAddress,
                      To=toAddress,
                      Authorize=authorize).do(self).addCallback(_cbSecure)

    def command_VIRTUAL(self, id):
        cwait, call = self.service.inboundConnections.pop(id)
        call.cancel()
        # We are double-deferring here so that we only start writing data to
        # our client _after_ they have processed our ACK.
        tpt = VirtualTransport(self, cwait.to, cwait.From, cwait.protocolName,
                               id, cwait.protocolFactory,
                               False)

        return dict(__transport__=tpt)

    command_VIRTUAL.command = Virtual


    # Client/Support methods.

    def _attemptSingleConnect(self, method, *args, **kw):
        # return method.attemptConnect(self, *args, **kw)
        log.msg('Attempting connection method %s ...' % (method,))
        def _done(x, message):
            log.msg("Connection attempt to %s %s: %s" % (method, message, x))
            return x
        return method.attemptConnect(self, *args, **kw).addCallback(
            _done, 'succeeded').addErrback(
            _done, 'failed')

    def attemptConnectionMethods(self, methods, connectionID, From, to,
                                 protocolName, protocolFactory,
                                 localudp):
        wrapperFactory = IgnoreConnectionFailed(protocolFactory)
        return _endeferify([(self._attemptSingleConnect,
                             (method, connectionID,  From, to,
                              protocolName, wrapperFactory,
                              localudp), {})
                            for method in methods])

    def listen(self, fromAddress, protocols, serverDescription):
        return Listen(From=fromAddress,
                      Protocols=protocols,
                      Description=serverDescription).do(self)

    def connect(self, From, to,
                protocolName, clientFactory,
                chooser):
        """
        Issue an INBOUND command, creating a virtual connection to the peer,
        given identifying information about the endpoint to connect to, and a
        protocol factory.

        @param clientFactory: a *Client* ProtocolFactory instance which will
        generate a protocol upon connect.

        @return: a Deferred which fires with the protocol instance that was
        connected, or fails with AttemptsFailed if the connection was not
        possible.
        """

        publicIP = self._determinePublicIP()

        A = dict(From=From,
                 To=to,
                 Protocol=protocolName)

        if self.service.dispatcher is not None:
            mypeer = self.transport.getPeer()
            localudp = self.service.dispatcher.seedNAT(
                (mypeer.host, mypeer.port+17)) # don't run anything there...
            A['udp_source'] = (publicIP, localudp)
        else:
            log.msg("dispatcher unavailable when connecting")
            localudp = None

        D = Inbound(**A).do(self)

        def _connected(answer):
            listenersD = defer.maybeDeferred(chooser, answer['listeners'])
            def gotListeners(listeners):
                allConnectionAttempts = []
                for listener in listeners:
                    d = self.attemptConnectionMethods(
                        listener['methods'],
                        listener['id'],
                        From, to,
                        protocolName, clientFactory,
                        localudp
                        )
                    allConnectionAttempts.append(d)
                return defer.DeferredList(allConnectionAttempts)
            listenersD.addCallback(gotListeners)
            def finishedAllAttempts(results):
                succeededAny = False
                failures = []
                if not results:
                    return Failure(NoAttemptsMade(
                            "there was no available path for connections"))
                for succeeded, result in results:
                    if succeeded:
                        succeededAny = True
                        randomConnection = result
                        break
                    else:
                        failures.append(result)
                if not succeededAny:
                    return Failure(AttemptsFailed(
                            [failure.getBriefTraceback() for failure in failures]))

                # XXX TODO: this connection is really random; connectQ2Q should
                # not return one of the connections it's made, put it into your
                # protocol's connectionMade handler

                return randomConnection

            return listenersD.addCallback(finishedAllAttempts)
        return D.addCallback(_connected)

class Q2QLayeringMixin:
    subProtocol = None
    q2qhost = None
    q2qpeer = None
    protocolName = 'unknown'

    # ITransport
    disconnecting = property(lambda self: self.transport.disconnecting)

    # IQ2QTransport

    def getQ2QHost(self):
        return self.q2qhost

    def getQ2QPeer(self):
        return self.q2qpeer

    def connectionMade(self):
        self.service.tcpConnections.append(self)


    def getPeer(self):
        return Q2QTransportAddress(self.getQ2QPeer(),
                                   self.transport.getPeer(),
                                   self.protocolName)

    def getHost(self):
        return Q2QTransportAddress(self.getQ2QHost(),
                                   self.transport.getHost(),
                                   self.protocolName)

    def dataReceived(self, data):
        self.subProtocol.dataReceived(data)

    def write(self, data):
        self.transport.write(data)

    def writeSequence(self, data):
        self.transport.writeSequence(data)

    def registerProducer(self, producer, streaming):
        self.transport.registerProducer(producer, streaming)

    def unregisterProducer(self):
        self.transport.unregisterProducer()

    def loseConnection(self):
        self.transport.loseConnection()

    def connectionLost(self, reason):
        self.service.tcpConnections.remove(self)
        if self.subProtocol is not None:
            return self.subProtocol.connectionLost(reason)

class Q2QTCPConnector(Q2QLayeringMixin, protocol.ClientFactory, protocol.Protocol):
    """
    I am an implementor of IClientFactory that can hook up a ClientFactory to a
    listening port.
    """
    def __init__(self, service, connectionID, From, to, protocolFactory):
        self.service = service
        self.connectionID = connectionID
        self.q2qhost = From
        self.q2qpeer = to
        self.protocolFactory = protocolFactory
        self.deferred = defer.Deferred()

    addr = None

    def buildProtocol(self, addr):
        assert self.addr is None, "You shouldn't re-use these: %s" % (self.addr,)
        self.addr = addr
        return self

    def connectionMade(self):
        Q2QLayeringMixin.connectionMade(self)
        self.transport.write('Q2Q %s\r\n' % self.connectionID)
        self.subProtocol = self.protocolFactory.buildProtocol(self.addr)
        self.subProtocol.makeConnection(self)
        self.deferred.callback(self.subProtocol)

    def clientConnectionLost(self, connector, reason):
        self.protocolFactory.clientConnectionLost(connector, reason)

    def clientConnectionFailed(self, connector, reason):
        self.protocolFactory.clientConnectionFailed(connector, reason)
        self.deferred.errback(reason)


class Q2QTCPListener(Q2QLayeringMixin, protocol.Protocol):
    buf = ''

    def dataReceived(self, data):
        if self.subProtocol is None:
            self.buf += data
            bufz = self.buf.split('\r\n',1)
            if len(bufz) > 1:
                # We've got our Q2Q ID
                intro, rest = bufz
                command, id = intro.split(' ',1)
                if command != 'Q2Q':
                    self.transport.loseConnection()
                    return
                proto, self.q2qhost, self.q2qpeer = self.service.protocolAndAuthFromId(id, self)
                if proto is None:
                    self.transport.loseConnection()
                    return
                self.subProtocol = proto
                proto.makeConnection(self)
                if rest:
                    proto.dataReceived(rest)
        else:
            return Q2QLayeringMixin.dataReceived(self, data)

class Q2QTCPListenerFactory(protocol.Factory):
    def __init__(self, service):
        self.service = service

    def buildProtocol(self, addr):
        q2etc = Q2QTCPListener()
        q2etc.service = self.service
        return q2etc

class VirtualTransport(subproducer.SubProducer):
    implements(interfaces.IProducer, interfaces.ITransport, interfaces.IConsumer)
    disconnecting = False

    def __init__(self, q2q, hostAddress, peerAddress,
                 protocolName, connectionID, protocolFactory,
                 isClient):
        """
        @param q2q: a Q2Q Protocol instance.

        @param peerAddress: a Q2QAddress instance identifying the peer entity
        on this connection.

        @param hostAddress: a Q2QAddress instance identifying the host entity
        on this connection.

        @param protocolName: a string describing the name of the protocol being
        spoken over this connection, i.e. 'http'.

        @param connectionID: a string identifier, unique to the q2q instance
        that I am wrapping (my underlying physical connection).

        @param protocolFactory: an IProtocolFactory implementor which returns a
        protocol instance for me to use.  I'll use it to build the protocol,
        and if the 'client' flag is True, also use it to notify
        connectionLost/connectionFailed.

        @param isClient: a boolean describing whether my protocol is the
        initiating half of this connection or not.
        """
        subproducer.SubProducer.__init__(self, q2q)
        self.q2q = q2q

        self._host = hostAddress
        self._peer = peerAddress
        self.id = connectionID
        self.isClient = isClient
        self.q2q.connections[self.id] = self
        self.protocolName = protocolName
        self.protocolFactory = protocolFactory

    def startProtocol(self):
        self.protocol = self.protocolFactory.buildProtocol(self._peer)
        self.protocol.makeConnection(self)
        return self.protocol

    def writeSequence(self, iovec):
        self.write(''.join(iovec))

    def loseConnection(self):
        self.disconnecting = True
        self.q2q.sendCommand('close', id=self.id).addCallbacks(
            lambda ign: self.connectionLost(CONNECTION_DONE),
            self.connectionLost)

    def connectionLost(self, reason):
        del self.q2q.connections[self.id]
        self.protocol.connectionLost(reason)
        if self.isClient:
            self.protocolFactory.clientConnectionLost(None, reason)

    def dataReceived(self, data):
        try:
            self.protocol.dataReceived(data)
        except:
            # XXX: unconditionally logging errors from user code makes it hard
            # to write tests, and is not always the right thing to do.  we
            # should revamp Twisted to have some kind of control over this
            # behavior, and add that control back in to this code path as well
            # (although logging exceptions from dataReceived is _by default_
            # certainly the right thing to do)  --glyph+exarkun
            reason = Failure()
            log.err(reason)
            self.connectionLost(reason)

    def write(self, data):
        self.q2q.sendCommand('write', data,
                             id=self.id)

    def getHost(self):
        return Q2QTransportAddress(
            self._host,
            VirtualTransportAddress(self.q2q.transport.getHost()),
            self.protocolName)

    def getPeer(self):
        return Q2QTransportAddress(
            self._peer,
            VirtualTransportAddress(self.q2q.transport.getPeer()),
            self.protocolName)

    def getQ2QPeer(self):
        return self._peer

    def getQ2QHost(self):
        return self._host


_counter = 0
def _nextJuiceLog():
    global _counter
    try:
        return str(_counter)
    finally:
        _counter = _counter + 1

class DefaultCertificateStore:

    def __init__(self):
        self.remoteStore = {}
        self.localStore = {}
        self.users = {}

    def getSelfSignedCertificate(self, domainName):
        return defer.maybeDeferred(self.remoteStore.__getitem__, domainName)

    def addUser(self, domain, username, privateSecret):
        self.users[domain, username] = privateSecret

    def checkUser(self, domain, username, privateSecret):
        if self.users.get((domain, username)) != privateSecret:
            return defer.fail(KeyError())
        return defer.succeed(True)

    def storeSelfSignedCertificate(self, domainName, mainCert):
        """

        @return: a Deferred which will fire when the certificate has been
        stored successfully.
        """
        assert not isinstance(mainCert, str)
        return defer.maybeDeferred(self.remoteStore.__setitem__, domainName, mainCert)

    def getPrivateCertificate(self, domainName):
        """

        @return: a PrivateCertificate instance, e.g. a certificate including a
        private key, for 'domainName'.
        """
        return self.localStore[domainName]


    def genSerial(self, name):
        return abs(struct.unpack('!i', md5.md5(name).digest()[:4])[0])

    def addPrivateCertificate(self, subjectName, existingCertificate=None):
        """
        Add a PrivateCertificate object to this store for this subjectName.

        If existingCertificate is None, add a new self-signed certificate.
        """
        if existingCertificate is None:
            assert '@' not in subjectName, "Don't self-sign user certs!"
            mainDN = sslverify.DistinguishedName(commonName=subjectName)
            mainKey = sslverify.KeyPair.generate()
            mainCertReq = mainKey.certificateRequest(mainDN)
            mainCertData = mainKey.signCertificateRequest(mainDN, mainCertReq,
                                                          lambda dn: True,
                                                          self.genSerial(subjectName))
            mainCert = mainKey.newCertificate(mainCertData)
        else:
            mainCert = existingCertificate
        self.localStore[subjectName] = mainCert

import os

class _pemmap(object):
    def __init__(self, pathname, certclass):
        self.pathname = pathname
        try:
            os.makedirs(pathname)
        except (OSError, IOError):
            pass
        self.certclass = certclass

    def file(self, name, mode):
        try:
            return file(os.path.join(self.pathname, name)+'.pem', mode)
        except IOError, ioe:
            raise KeyError(name, ioe)

    def __setitem__(self, key, cert):
        kn = cert.getSubject().commonName
        assert kn == key
        self.file(kn, 'wb').write(cert.dumpPEM())

    def __getitem__(self, cn):
        return self.certclass.loadPEM(self.file(cn, 'rb').read())

    def iteritems(self):
        files = os.listdir(self.pathname)
        for file in files:
            if file.endswith('.pem'):
                key = file[:-4]
                value = self[key]
                yield key, value

    def items(self):
        return list(self.iteritems())

    def iterkeys(self):
        for k, v in self.iteritems():
            yield k

    def keys(self):
        return list(self.iterkeys())

    def itervalues(self):
        for k, v in self.iteritems():
            yield v

    def values(self):
        return list(self.itervalues())



class DirectoryCertificateStore(DefaultCertificateStore):
    def __init__(self, filepath):
        self.remoteStore = _pemmap(os.path.join(filepath, 'public'),
                                   sslverify.Certificate)
        self.localStore = _pemmap(os.path.join(filepath, 'private'),
                                  sslverify.PrivateCertificate)

theMessageFactory = juice.JuiceClientFactory()

class _MessageChannel(object):
    """Conceptual curry over source and destination addresses, as well as a namespace.

    Acts as a transport for delivering Q2Q commands between two particular endpoints.
    """

    def __init__(self, q2qsvc,
                 fromAddress, toAddress,
                 namespace):
        self.q2qsvc = q2qsvc
        self.fromAddress = fromAddress
        self.toAddress = toAddress
        self.namespace = namespace

    def __call__(self, command):
        return self.q2qsvc.sendMessage(
            self.fromAddress,
            self.toAddress,
            self.namespace, command)

class _ConnectionWaiter(_structlike):
    __names__ = ['From',
                 'to',
                 'protocolName',
                 'protocolFactory',
                 'isClient']

    def createProtocolWithTransport(self, transport):
        prot = self.protocolFactory.buildProtocol(transport.getPeer())
        return prot

class Q2QClientFactory(protocol.ClientFactory):

    def __init__(self, service):
        self.service = service

    def buildProtocol(self, addr):
        p = Q2Q(False)
        p.service = self.service
        p.factory = self
        p.wrapper = self.service.wrapper
        return p


class _AddressDiscoveryProtocol(protocol.Protocol):
    def __init__(self, addrDiscDef):
        self.addrDiscDef = addrDiscDef

    def _done(self, passthrough):
        # print 'awesome', passthrough
        self.transport.loseConnection()
        return passthrough

    def connectionMade(self):
        # print 'woo conn'
        return self.transport.whoami().addBoth(
            self._done).chainDeferred(
            self.addrDiscDef)


class _AddressDiscoveryFactory(protocol.ClientFactory):
    def __init__(self, addressDiscoveredDeferred):
        self.addressDiscoveredDeferred = addressDiscoveredDeferred

    def buildProtocol(self, addr):
        # print 'sweet'
        return _AddressDiscoveryProtocol(self.addressDiscoveredDeferred)


def _noResults(*x):
    return []

class PTCPConnectionDispatcher(object):
    def __init__(self, factory):
        self.factory = factory
        self._ports = {}

    def seedNAT(self, (host, port)):
        proto = ptcp.Ptcp(self.factory)
        proto.peerAddressTuple = (host, port)
        p = reactor.listenUDP(0, proto)
        portNum = p.getHost().port
        proto.sendPacket(ptcp.PtcpPacket.create(0, 0, 0, '', destination=(host, port)))
        self._ports[portNum] = p
        return portNum

    def bindNewPort(self):
        p = reactor.listenUDP(0, ptcp.Ptcp(self.factory))
        portNum = p.getHost().port
        self._ports[portNum] = p
        return portNum

    def connectPTCP(self, host, port, factory):
        proto = ptcp.Ptcp(self.factory)
        p = reactor.listenUDP(0, proto)
        self._ports[p.getHost().port] = p
        return proto.connect(factory, host, port)

    def iterconnections(self):
        for p in self._ports.itervalues():
            for c in p.protocol._connections.itervalues():
                yield c

    def killAllConnections(self):
        dl = []
        for p in self._ports.itervalues():
            for c in p.protocol._connections.itervalues():
                c._stopRetransmitting()
            dl.append(defer.maybeDeferred(p.stopListening))
        self._ports = {}
        return defer.DeferredList(dl)


class Q2QService(service.MultiService, protocol.ServerFactory):
    # server factory stuff
    publicIP = None
    _publicIPIsReallyPrivate = False

    def buildProtocol(self, addr):
        p = Q2Q(True)
        p.service = self
        p.factory = self
        p.wrapper = self.wrapper
        return p

    def iterconnections(self):
        """
        Iterator of all connections associated with this service, whether cached or
        not.  For testing purposes only.
        """
        return itertools.chain(
            self.appConnectionCache.cachedConnections.itervalues(),
            self.secureConnectionCache.cachedConnections.itervalues(),
            iter(self.tcpConnections),
            (self.dispatcher or ()) and self.dispatcher.iterconnections())

    def __init__(self, protocolFactoryFactory=None,
                 certificateStorage=None, wrapper=None,
                 q2qPortnum=port,
                 inboundTCPPortnum=None,
                 inboundUDPPortnum=None,
                 publicIP=None):
        """

        @param protocolFactoryFactory: A callable of three arguments
        (fromAddress, toAddress, protocolName) which returns a list of 2-tuples
        of (ProtocolFactory, description) appropriate for constructing
        protocols which can serve the resource specified by the toAddress.

        @param certificateStorage: an implementor of ICertificateStore, or None
        for the default implementation.
        """
        if protocolFactoryFactory is None:
            protocolFactoryFactory = _noResults
        self.protocolFactoryFactory = protocolFactoryFactory
        if certificateStorage is None:
            certificateStorage = DefaultCertificateStore()
        self.certificateStorage = certificateStorage

        # atop thingy for protocols to wrap everything in transactions.
        self.wrapper = wrapper

        # clients which have registered for network events: maps {(q2q_id,
        # protocol_name): clientQ2QProtocol}
        self.listeningClients = {}

        self.inboundConnections = {} # map of str(Id) to _ConnectionWaiter
        self.q2qPortnum = q2qPortnum # port number for q2q

        # port number for inbound almost-raw TCP
        self.inboundTCPPortnum = inboundTCPPortnum

        # port number for inbound gin
        self.inboundUDPPortnum = inboundUDPPortnum

        # list of independent TCP connections relaying Q2Q traffic.
        self.tcpConnections = []

        # map of {(fromAddress, protocolName): [(factory, description)]}
        self.localFactoriesMapping = {}

        if publicIP is not None:
            self.publicIP = publicIP

        self.appConnectionCache = ConnectionCache()
        self.secureConnectionCache = ConnectionCache()

        service.MultiService.__init__(self)

    inboundListener = None

    def checkPrivateSecret(self, username, domain, privateSecret):
        #XXX Should really live way off in cred-land
        return self.certificateStorage.checkUser(domain, username, privateSecret)

    _publicUDPPort = None

    def _retrievePublicUDPPortNumber(self, registrationServerAddress):
        # Create a PTCP port, bounce some traffic off the indicated server,
        # wait for it to tell us what our address is
        self._publicPTCPServer = ptcp.Ptcp(self._publicUDPFactory)
        self._publicUDPPort = reactor.listenUDP(0, self._publicPTCPServer)

        # print 'HELlO'

        d = defer.Deferred()
        addressDiscoveryFactory = _AddressDiscoveryFactory(d)

        # print 'connecting to!!!!', addressDiscoveryFactory, registrationServerAddress

        self._publicPTCPServer.connect(addressDiscoveryFactory,
                                       *registrationServerAddress)
        return d


    def listenQ2Q(self, fromAddress, protocolsToFactories, serverDescription):
        """
        Right now this is really only useful in the client implementation,
        since it is transient.  protocolFactoryFactory is used for persistent
        listeners.
        """
        myDomain = fromAddress.domainAddress()
        D = self.getSecureConnection(fromAddress, myDomain)
        def _secured(proto):
            lfm = self.localFactoriesMapping
            def startup(listenResult):
                for protocol, factory in protocolsToFactories.iteritems():
                    key = (fromAddress, protocol)
                    if key not in lfm:
                        lfm[key] = []
                    lfm[key].append((factory, serverDescription))
                    factory.doStart()

                def shutdown():
                    for protocol, factory in protocolsToFactories.iteritems():
                        lfm[fromAddress, protocol].remove(
                            (factory, serverDescription))
                        factory.doStop()

                proto.notifyOnConnectionLost(shutdown)
                return listenResult

            if self.dispatcher is not None:
                gp = proto.transport.getPeer()
                udpAddress = (gp.host, gp.port)
                pubUDPDeferred = self._retrievePublicUDPPortNumber(udpAddress)
            else:
                pubUDPDeferred = defer.succeed(None)

            def _gotPubUDPPort(publicAddress):
                self._publicUDPAddress = publicAddress
                return proto.listen(fromAddress, protocolsToFactories.keys(),
                                    serverDescription).addCallback(startup)

            pubUDPDeferred.addCallback(_gotPubUDPPort)
            return pubUDPDeferred

        D.addCallback(_secured)
        return D

    def requestCertificateForAddress(self, fromAddress, sharedSecret):
        """
        Connect to the authoritative server for the domain part of the given
        address and obtain a certificate signed by the root certificate for
        that domain, then store that certificate in my local certificate
        storage.

        @param fromAddress: an address that this service is authorized to use,
        and should store a separate private certificate for.

        @param sharedSecret: a str that represents a secret shared between the
        user of this service and their account on the server running on the
        domain part of the fromAddress.

        @return: a Deferred which fires None when the certificate has been
        successfully retrieved, and errbacks if it cannot be retrieved.
        """
        kp = sslverify.KeyPair.generate()
        subject = sslverify.DN(commonName=str(fromAddress))
        reqobj = kp.requestObject(subject)
        # create worthless, self-signed certificate for the moment, it will be
        # replaced later.

        #attemptAddress = q2q.Q2QAddress(fromAddress.domain,
        #   fromAddress.resource + '+attempt')
        # fakeSubj = sslverify.DN(commonName=str(attemptAddress))
        fakereq = kp.requestObject(subject)
        ssigned = kp.signRequestObject(subject, fakereq, 1)
        certpair = sslverify.PrivateCertificate.fromCertificateAndKeyPair
        fakecert = certpair(ssigned, kp)
        apc = self.certificateStorage.addPrivateCertificate

        def _2(secured):
            D = Sign(certificate_request=reqobj,
                     password=sharedSecret).do(secured)
            def _1(dcert):
                cert = dcert['certificate']
                privcert = certpair(cert, kp)
                apc(str(fromAddress), privcert)
            return D.addCallback(_1)
        return self.getSecureConnection(
            fromAddress, fromAddress.domainAddress(), authorize=False,
            usePrivateCertificate=fakecert,
            ).addCallback(_2)

    def authorize(self, fromAddress, password):
        """To-be-deprecated synonym for requestCertificateForAddress
        """
        return self.requestCertificateForAddress(fromAddress, password)

    def protocolAndAuthFromId(self, id, tpt):
        """(internal)

        Retrieve a waiting connection by its connection identifier, passing in
        the transport to be used to connect the waiting protocol factory to.
        """
        if id in self.inboundConnections:
            # make the connection?
            cwait, call = self.inboundConnections.pop(id)
            # _ConnectionWaiter instance
            proto = cwait.createProtocolWithTransport(tpt)
            call.cancel()
            return proto, cwait.to, cwait.From

    _lastConnID = 1

    def _nextConnectionID(self, From, to):
        lcid = self._lastConnID
        self._lastConnID += 1
        fmt = '%s->%s:%s' % (
            From, to, lcid)
        return fmt

    def mapListener(self, to, From, protocolName, protocolFactory, isClient=False):
        """
        Returns 2-tuple of (expiryTime, listenerID)
        """
        listenerID = self._nextConnectionID(From, to)
        call = reactor.callLater(120,
                                 self.inboundConnections.pop,
                                 listenerID, None)
        expires = Time.fromPOSIXTimestamp(call.getTime())
        self.inboundConnections[listenerID] = (
            _ConnectionWaiter(From, to, protocolName, protocolFactory, isClient),
            call)
        return expires, listenerID


    def getLocalFactories(self, From, to, protocolName):
        """
        Returns a list of 2-tuples of (protocolFactory, description) to handle
        this from/to/protocolName
        """
        result = []
        x = self.localFactoriesMapping.get((to, protocolName), ())
        result.extend(x)
        y = self.protocolFactoryFactory(From, to, protocolName)
        result.extend(y)
        return result


    q2qPort = None
    inboundTCPPort = None
    inboundUDPPort = None
    dispatcher = None


    def startInboundListener(self, portnum=None, uportnum=None):
        assert self.inboundTCPPort is None
        if portnum is None:
            portnum = self.inboundTCPPortnum
        if portnum is not None:
            self.inboundTCPPort = reactor.listenTCP(
                portnum,
                Q2QTCPListenerFactory(self))
        if uportnum is None:
            uportnum = self.inboundUDPPortnum

        self._publicUDPFactory = Q2QTCPListenerFactory(self)
        self._q2qUDPListener = reactor.listenUDP(self.q2qPort.getHost().port, ptcp.Ptcp(self._publicUDPFactory))

        if uportnum is not None:
            self.dispatcher = PTCPConnectionDispatcher(self._publicUDPFactory)


    def startService(self):
        if self.q2qPortnum is not None:
            self.q2qPort = reactor.listenTCP(
                self.q2qPortnum, self)
        self.startInboundListener()
        return service.MultiService.startService(self)

    def stopService(self):
        dl = []
        for cwait, delayed in self.inboundConnections.itervalues():
            delayed.cancel()
        self.inboundConnections.clear()
        if self.q2qPort is not None:
            dl.append(defer.maybeDeferred(self.q2qPort.stopListening))
        if self.inboundTCPPort is not None:
            dl.append(defer.maybeDeferred(self.inboundTCPPort.stopListening))
        if self.dispatcher is not None:
            dl.append(self.dispatcher.killAllConnections())
        dl.append(self.appConnectionCache.shutdown())
        dl.append(self.secureConnectionCache.shutdown())
        dl.append(defer.maybeDeferred(service.MultiService.stopService, self))
        for conn in self.tcpConnections:
            dl.append(defer.maybeDeferred(conn.transport.loseConnection))
        return defer.DeferredList(dl)


    def sendMessage(self, fromAddress, toAddress, namespace, message):
        """
        Send a message using the Q2Q-Message protocol to a peer.  This internally
        uses a connection cache to avoid setting up and tearing down
        connections too often.

        @param fromAddress: Q2QAddress instance referring to the sender of the
        message.

        @param toAddress: Q2QAddress instance referring to the receiver of the
        message.

        @param namespace: str which indicates what juice command namespace the message is in.

        @param message: a juice.Command object.
        """


        return self.connectCachedQ2Q(
            fromAddress, toAddress, MESSAGE_PROTOCOL, theMessageFactory
            ).addCallback(message.do, namespace)


    def messageChannel(self, fromAddress, toAddress, namespace):
        """Create a one-arg callable that takes a Command and sends it to .
        """
        return _MessageChannel(self, fromAddress, toAddress, namespace)

    def connectCachedQ2Q(self, fromAddress,
                         toAddress, protocolName, protocolFactory):
        return self.appConnectionCache.connectCached(
            endpoint.Q2QEndpoint(self, fromAddress, toAddress, MESSAGE_PROTOCOL),
            theMessageFactory)


    def connectQ2Q(self, fromAddress, toAddress, protocolName, protocolFactory,
                   usePrivateCertificate=None, fakeFromDomain=None,
                   chooser=lambda x: x and [x[0]]):
        """ Connect a named protocol factory from a resource@domain to a
        resource@domain.

        This is analagous to something like connectTCP, in that it creates a
        connection-oriented transport for each connection, except instead of
        specifying your credentials with an application-level (username,
        password) and your endpoint with a framework-level (host, port), you
        specify both at once, in the form of your ID (user@my-domain), their ID
        (user@their-domain) and the desired protocol.  This provides several
        useful features:

            - All connections are automatically authenticated via SSL
              certificates, although not authorized for any particular
              activities, based on their transport interface rather than having
              to have protocol logic to authenticate.

            - User-meaningful protocol nicknames are attached to
              implementations of protocol logic, rather than arbitrary
              numbering.

            - Endpoints can specify a variety of transport mechanisms
              transparently to the application: for example, you might be
              connecting to an authorized user-agent on the user's server or to
              the user directly using a NAT-circumvention handshake.  All the
              application has to know is that it wants to establish a TCP-like
              connection.

        XXX Really, really should return an IConnector implementor for symmetry
        with other connection-oriented transport APIs, but currently does not.

        The 'resource' parameters are so named (rather than beginning with
        'user', for example) because they are sometimes used to refer to
        abstract entities or roles, such as 'payments', or groups of users
        (communities) but generally the convention is to document them as
        individual users for simplicity's sake.

        The parameters are described as if Alice <alice@divmod.com> were trying
        try connect to Bob <bob@notdivmod.com> to transfer a file over HTTP.

        @param fromAddress: The address of the connecting user: in this case,
        Q2QAddress("divmod.com", "alice")

        @param toAddress: The address of the user connected to: in this case,
        Q2QAddress("notdivmod.com", "bob")

        @param protocolName: The name of the protocol, by convention observing
        similar names to http://www.iana.org/assignments/port-numbers when
        appropriate.  In this case, 'http'.

        @param protocolFactory: An implementation of
        L{twisted.internet.interfaces.IProtocolFactory}

        @param usePrivateCertificate: Use a different private certificate for
        initiating the 'secure' call.  Mostly for testing different invalid
        certificate attacks.

        @param fakeDomainName: This domain name will be used for an argument to
        the 'connect' command, but NOT as an argument to the SECURE command.
        This is to test a particular kind of invalid cert attack.

        @param chooser: a function taking a list of connection-describing
        objects and returning another list.  Those items in the remaining list
        will be attempted as connections and buildProtocol called on the client
        factory.  May return a Deferred.
        """

        def onSecureConnection(protocol):
            if fakeFromDomain:
                connectFromAddress = Q2QAddress(fakeFromDomain, toAddress.resource)
            else:
                connectFromAddress = fromAddress

            return protocol.connect(connectFromAddress, toAddress,
                                    protocolName, protocolFactory,
                                    chooser)

        def onSecureConnectionFailure(reason):
            protocolFactory.clientConnectionFailed(None, reason)
            return reason

        return self.getSecureConnection(
            fromAddress, toAddress,
            port, usePrivateCertificate).addCallback(
            onSecureConnection).addErrback(onSecureConnectionFailure)

    def getSecureConnection(self, fromAddress, toAddress, port=port,
                            failIfNoCertificate=False,
                            usePrivateCertificate=None,
                            authorize=True):
        """
        Get a secure connection between two entities by connecting to the
        domain part of toAddress

        (This really shouldn't be _entirely_ public, because it's slightly
        misleading: you pass in fully qualified addresses but the connection
        chops off the resource half of the "to" address, giving you a
        connection to their host rather than their actual client, as this is a
        necessary step to look up where their client *is*.)
        """

        # secure connections using users as clients will have to be established
        # using the 'secure' method differently than this does: we are ONLY
        # capable of connecting to other domains (supernodes)

        toDomain = toAddress.domainAddress()
        resolveme = reactor.resolve(str(toDomain))
        def cb(toIPAddress, authorize=authorize):
            GPS = self.certificateStorage.getPrivateCertificate
            if usePrivateCertificate:
                ourCert = usePrivateCertificate
                cacheFrom = fromAddress
                log.msg('Using fakie private cert:', fromAddress, ourCert, cacheFrom)
            elif fromAddress.domain == '':
                assert fromAddress.resource == '', "No domain means anonymous, bozo: %r" % (fromAddress,)
                # we are actually anonymous, whoops!
                authorize = False
                # we need to create our own certificate
                ourCert = sslverify.KeyPair.generate().selfSignedCert(218374, CN='@')
                # feel free to cache the anonymous certificate we just made, whatever
                cacheFrom = fromAddress
                log.msg("Using anonymous cert for anonymous user.")
            else:
                try:
                    # Are we in fact a domain, operating on behalf of a user?
                    x = fromAddress.domainAddress()
                    ourCert = GPS(str(x))
                    cacheFrom = x
                    log.msg('domain on behalf of user:', fromAddress, ourCert, cacheFrom)
                except KeyError:
                    # Nope, guess not.  Are we actually that user?
                    try:
                        x = fromAddress
                        ourCert = GPS(str(x))
                        cacheFrom = x
                        log.msg( 'actual user:', fromAddress, ourCert, cacheFrom)
                    except KeyError:
                        # Hmm.  We're not that user either.  Are we trying to
                        # pretend to be a user from a *different* domain, to
                        # ourselves?  (We've got to be a domain to "make
                        # believe", since this is effectively a clustering
                        # feature...)

                        try:
                            x = toDomain
                            ourCert = GPS(str(x))
                            cacheFrom = x
                            log.msg('fakie domain cert:', fromAddress, ourCert, cacheFrom)
                        except KeyError:
                            raise sslverify.VerifyError(
                                "We tried to secure a connection "
                                "between %s and %s, "
                                "but we don't have any certificates "
                                "that could be used." % (fromAddress,
                                                         toAddress))

            def connected(proto):
                certD = self.certificateStorage.getSelfSignedCertificate(
                    str(toDomain))
                def nocert(failure):
                    failure.trap(KeyError)
                    identD = Identify(subject=toDomain).do(proto).addCallback(
                        lambda x: x['certificate'])
                    def storeit(certificate):
                        return self.certificateStorage.storeSelfSignedCertificate(
                            str(toDomain), certificate
                            ).addCallback(lambda x: certificate)
                    return identD.addCallback(storeit)
                certD.addErrback(nocert)
                def gotcert(foreignCA):
                    secdef = proto.secure(cacheFrom, toDomain,
                                          ourCert, foreignCA,
                                          authorize=authorize)
                    return secdef
                certD.addCallback(gotcert)
                return certD
            return self.secureConnectionCache.connectCached(
                endpoint.TCPEndpoint(toIPAddress, port),
                Q2QClientFactory(self),
                extraWork=connected,
                extraHash=(cacheFrom, toDomain, authorize)
                )
        return resolveme.addCallback(cb)
