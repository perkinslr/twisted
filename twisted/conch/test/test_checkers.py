# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for L{twisted.conch.checkers}.
"""

try:
    import crypt
except ImportError:
    cryptSkip = 'cannot run without crypt module'
else:
    cryptSkip = None

import os, base64
from collections import namedtuple
from cStringIO import StringIO

from zope.interface.verify import verifyObject

from twisted.python import util
from twisted.python.failure import Failure
from twisted.trial.unittest import TestCase
from twisted.python.filepath import FilePath
from twisted.cred.checkers import InMemoryUsernamePasswordDatabaseDontUse
from twisted.cred.credentials import UsernamePassword, IUsernamePassword, \
    SSHPrivateKey, ISSHPrivateKey
from twisted.cred.error import UnhandledCredentials, UnauthorizedLogin
from twisted.python.fakepwd import UserDatabase, ShadowDatabase
from twisted.test.test_process import MockOS
from twisted.conch import checkers

try:
    import Crypto.Cipher.DES3
    import pyasn1
except ImportError:
    dependencySkip = "can't run without Crypto and PyASN1"
else:
    dependencySkip = None
    from twisted.conch.ssh import keys
    from twisted.conch.error import NotEnoughAuthentication, ValidPublicKey
    from twisted.conch.test import keydata

if getattr(os, 'geteuid', None) is None:
    euidSkip = "Cannot run without effective UIDs (questionable)"
else:
    euidSkip = None


class HelperTests(TestCase):
    """
    Tests for helper functions L{verifyCryptedPassword}, L{_pwdGetByName} and
    L{_shadowGetByName}.
    """
    skip = cryptSkip or dependencySkip

    def setUp(self):
        self.mockos = MockOS()


    def test_verifyCryptedPassword(self):
        """
        L{verifyCryptedPassword} returns C{True} if the plaintext password
        passed to it matches the encrypted password passed to it.
        """
        password = 'secret string'
        salt = 'salty'
        crypted = crypt.crypt(password, salt)
        self.assertTrue(
            checkers.verifyCryptedPassword(crypted, password),
            '%r supposed to be valid encrypted password for %r' % (
                crypted, password))


    def test_verifyCryptedPasswordMD5(self):
        """
        L{verifyCryptedPassword} returns True if the provided cleartext password
        matches the provided MD5 password hash.
        """
        password = 'password'
        salt = '$1$salt'
        crypted = crypt.crypt(password, salt)
        self.assertTrue(
            checkers.verifyCryptedPassword(crypted, password),
            '%r supposed to be valid encrypted password for %s' % (
                crypted, password))


    def test_refuteCryptedPassword(self):
        """
        L{verifyCryptedPassword} returns C{False} if the plaintext password
        passed to it does not match the encrypted password passed to it.
        """
        password = 'string secret'
        wrong = 'secret string'
        crypted = crypt.crypt(password, password)
        self.assertFalse(
            checkers.verifyCryptedPassword(crypted, wrong),
            '%r not supposed to be valid encrypted password for %s' % (
                crypted, wrong))


    def test_pwdGetByName(self):
        """
        L{_pwdGetByName} returns a tuple of items from the UNIX /etc/passwd
        database if the L{pwd} module is present.
        """
        userdb = UserDatabase()
        userdb.addUser(
            'alice', 'secrit', 1, 2, 'first last', '/foo', '/bin/sh')
        self.patch(checkers, 'pwd', userdb)
        self.assertEqual(
            checkers._pwdGetByName('alice'), userdb.getpwnam('alice'))


    def test_pwdGetByNameWithoutPwd(self):
        """
        If the C{pwd} module isn't present, L{_pwdGetByName} returns C{None}.
        """
        self.patch(checkers, 'pwd', None)
        self.assertIs(checkers._pwdGetByName('alice'), None)


    def test_shadowGetByName(self):
        """
        L{_shadowGetByName} returns a tuple of items from the UNIX /etc/shadow
        database if the L{spwd} is present.
        """
        userdb = ShadowDatabase()
        userdb.addUser('bob', 'passphrase', 1, 2, 3, 4, 5, 6, 7)
        self.patch(checkers, 'spwd', userdb)

        self.mockos.euid = 2345
        self.mockos.egid = 1234
        self.patch(util, 'os', self.mockos)

        self.assertEqual(
            checkers._shadowGetByName('bob'), userdb.getspnam('bob'))
        self.assertEqual(self.mockos.seteuidCalls, [0, 2345])
        self.assertEqual(self.mockos.setegidCalls, [0, 1234])


    def test_shadowGetByNameWithoutSpwd(self):
        """
        L{_shadowGetByName} uses the C{shadow} module to return a tuple of items
        from the UNIX /etc/shadow database if the C{spwd} module is not present
        and the C{shadow} module is.
        """
        userdb = ShadowDatabase()
        userdb.addUser('bob', 'passphrase', 1, 2, 3, 4, 5, 6, 7)
        self.patch(checkers, 'spwd', None)
        self.patch(checkers, 'shadow', userdb)
        self.patch(util, 'os', self.mockos)

        self.mockos.euid = 2345
        self.mockos.egid = 1234

        self.assertEqual(
            checkers._shadowGetByName('bob'), userdb.getspnam('bob'))
        self.assertEqual(self.mockos.seteuidCalls, [0, 2345])
        self.assertEqual(self.mockos.setegidCalls, [0, 1234])


    def test_shadowGetByNameWithoutEither(self):
        """
        L{_shadowGetByName} returns C{None} if neither C{spwd} nor C{shadow} is
        present.
        """
        self.patch(checkers, 'spwd', None)
        self.patch(checkers, 'shadow', None)

        self.assertIs(checkers._shadowGetByName('bob'), None)
        self.assertEqual(self.mockos.seteuidCalls, [])
        self.assertEqual(self.mockos.setegidCalls, [])



class SSHPublicKeyDatabaseTestCase(TestCase):
    """
    Tests for L{SSHPublicKeyDatabase}.
    """
    skip = euidSkip or dependencySkip

    def setUp(self):
        self.checker = checkers.SSHPublicKeyDatabase()
        self.key1 = base64.encodestring("foobar")
        self.key2 = base64.encodestring("eggspam")
        self.content = "t1 %s foo\nt2 %s egg\n" % (self.key1, self.key2)

        self.mockos = MockOS()
        self.mockos.path = FilePath(self.mktemp())
        self.mockos.path.makedirs()
        self.patch(util, 'os', self.mockos)
        self.sshDir = self.mockos.path.child('.ssh')
        self.sshDir.makedirs()

        userdb = UserDatabase()
        userdb.addUser(
            'user', 'password', 1, 2, 'first last',
            self.mockos.path.path, '/bin/shell')
        self.checker._userdb = userdb


    def test_deprecated(self):
        """
        L{SSHPublicKeyDatabase} is deprecated as of version 14.0
        """
        warningsShown = self.flushWarnings(
            offendingFunctions=[self.setUp])
        self.assertEqual(warningsShown[0]['category'], DeprecationWarning)
        self.assertEqual(
            warningsShown[0]['message'],
            "twisted.conch.checkers.SSHPublicKeyDatabase "
            "was deprecated in Twisted 14.0.0: Please use "
            "twisted.conch.checkers.SSHPublicKeyChecker, "
            "initialized with an instance of "
            "twisted.conch.checkers.UNIXAuthorizedKeysFiles instead.")
        self.assertEqual(len(warningsShown), 1)


    def _testCheckKey(self, filename):
        self.sshDir.child(filename).setContent(self.content)
        user = UsernamePassword("user", "password")
        user.blob = "foobar"
        self.assertTrue(self.checker.checkKey(user))
        user.blob = "eggspam"
        self.assertTrue(self.checker.checkKey(user))
        user.blob = "notallowed"
        self.assertFalse(self.checker.checkKey(user))


    def test_checkKey(self):
        """
        L{SSHPublicKeyDatabase.checkKey} should retrieve the content of the
        authorized_keys file and check the keys against that file.
        """
        self._testCheckKey("authorized_keys")
        self.assertEqual(self.mockos.seteuidCalls, [])
        self.assertEqual(self.mockos.setegidCalls, [])


    def test_checkKey2(self):
        """
        L{SSHPublicKeyDatabase.checkKey} should retrieve the content of the
        authorized_keys2 file and check the keys against that file.
        """
        self._testCheckKey("authorized_keys2")
        self.assertEqual(self.mockos.seteuidCalls, [])
        self.assertEqual(self.mockos.setegidCalls, [])


    def test_checkKeyAsRoot(self):
        """
        If the key file is readable, L{SSHPublicKeyDatabase.checkKey} should
        switch its uid/gid to the ones of the authenticated user.
        """
        keyFile = self.sshDir.child("authorized_keys")
        keyFile.setContent(self.content)
        # Fake permission error by changing the mode
        keyFile.chmod(0000)
        self.addCleanup(keyFile.chmod, 0777)
        # And restore the right mode when seteuid is called
        savedSeteuid = self.mockos.seteuid
        def seteuid(euid):
            keyFile.chmod(0777)
            return savedSeteuid(euid)
        self.mockos.euid = 2345
        self.mockos.egid = 1234
        self.patch(self.mockos, "seteuid", seteuid)
        self.patch(util, 'os', self.mockos)
        user = UsernamePassword("user", "password")
        user.blob = "foobar"
        self.assertTrue(self.checker.checkKey(user))
        self.assertEqual(self.mockos.seteuidCalls, [0, 1, 0, 2345])
        self.assertEqual(self.mockos.setegidCalls, [2, 1234])


    def test_requestAvatarId(self):
        """
        L{SSHPublicKeyDatabase.requestAvatarId} should return the avatar id
        passed in if its C{_checkKey} method returns True.
        """
        def _checkKey(ignored):
            return True
        self.patch(self.checker, 'checkKey', _checkKey)
        credentials = SSHPrivateKey(
            'test', 'ssh-rsa', keydata.publicRSA_openssh, 'foo',
            keys.Key.fromString(keydata.privateRSA_openssh).sign('foo'))
        d = self.checker.requestAvatarId(credentials)
        def _verify(avatarId):
            self.assertEqual(avatarId, 'test')
        return d.addCallback(_verify)


    def test_requestAvatarIdWithoutSignature(self):
        """
        L{SSHPublicKeyDatabase.requestAvatarId} should raise L{ValidPublicKey}
        if the credentials represent a valid key without a signature.  This
        tells the user that the key is valid for login, but does not actually
        allow that user to do so without a signature.
        """
        def _checkKey(ignored):
            return True
        self.patch(self.checker, 'checkKey', _checkKey)
        credentials = SSHPrivateKey(
            'test', 'ssh-rsa', keydata.publicRSA_openssh, None, None)
        d = self.checker.requestAvatarId(credentials)
        return self.assertFailure(d, ValidPublicKey)


    def test_requestAvatarIdInvalidKey(self):
        """
        If L{SSHPublicKeyDatabase.checkKey} returns False,
        C{_cbRequestAvatarId} should raise L{UnauthorizedLogin}.
        """
        def _checkKey(ignored):
            return False
        self.patch(self.checker, 'checkKey', _checkKey)
        d = self.checker.requestAvatarId(None);
        return self.assertFailure(d, UnauthorizedLogin)


    def test_requestAvatarIdInvalidSignature(self):
        """
        Valid keys with invalid signatures should cause
        L{SSHPublicKeyDatabase.requestAvatarId} to return a {UnauthorizedLogin}
        failure
        """
        def _checkKey(ignored):
            return True
        self.patch(self.checker, 'checkKey', _checkKey)
        credentials = SSHPrivateKey(
            'test', 'ssh-rsa', keydata.publicRSA_openssh, 'foo',
            keys.Key.fromString(keydata.privateDSA_openssh).sign('foo'))
        d = self.checker.requestAvatarId(credentials)
        return self.assertFailure(d, UnauthorizedLogin)


    def test_requestAvatarIdNormalizeException(self):
        """
        Exceptions raised while verifying the key should be normalized into an
        C{UnauthorizedLogin} failure.
        """
        def _checkKey(ignored):
            return True
        self.patch(self.checker, 'checkKey', _checkKey)
        credentials = SSHPrivateKey('test', None, 'blob', 'sigData', 'sig')
        d = self.checker.requestAvatarId(credentials)
        def _verifyLoggedException(failure):
            errors = self.flushLoggedErrors(keys.BadKeyError)
            self.assertEqual(len(errors), 1)
            return failure
        d.addErrback(_verifyLoggedException)
        return self.assertFailure(d, UnauthorizedLogin)



class SSHProtocolCheckerTestCase(TestCase):
    """
    Tests for L{SSHProtocolChecker}.
    """

    skip = dependencySkip

    def test_registerChecker(self):
        """
        L{SSHProcotolChecker.registerChecker} should add the given checker to
        the list of registered checkers.
        """
        checker = checkers.SSHProtocolChecker()
        self.assertEqual(checker.credentialInterfaces, [])
        checker.registerChecker(checkers.SSHPublicKeyDatabase(), )
        self.assertEqual(checker.credentialInterfaces, [ISSHPrivateKey])
        self.assertIsInstance(checker.checkers[ISSHPrivateKey],
                              checkers.SSHPublicKeyDatabase)


    def test_registerCheckerWithInterface(self):
        """
        If a apecific interface is passed into
        L{SSHProtocolChecker.registerChecker}, that interface should be
        registered instead of what the checker specifies in
        credentialIntefaces.
        """
        checker = checkers.SSHProtocolChecker()
        self.assertEqual(checker.credentialInterfaces, [])
        checker.registerChecker(checkers.SSHPublicKeyDatabase(),
                                IUsernamePassword)
        self.assertEqual(checker.credentialInterfaces, [IUsernamePassword])
        self.assertIsInstance(checker.checkers[IUsernamePassword],
                              checkers.SSHPublicKeyDatabase)


    def test_requestAvatarId(self):
        """
        L{SSHProtocolChecker.requestAvatarId} should defer to one if its
        registered checkers to authenticate a user.
        """
        checker = checkers.SSHProtocolChecker()
        passwordDatabase = InMemoryUsernamePasswordDatabaseDontUse()
        passwordDatabase.addUser('test', 'test')
        checker.registerChecker(passwordDatabase)
        d = checker.requestAvatarId(UsernamePassword('test', 'test'))
        def _callback(avatarId):
            self.assertEqual(avatarId, 'test')
        return d.addCallback(_callback)


    def test_requestAvatarIdWithNotEnoughAuthentication(self):
        """
        If the client indicates that it is never satisfied, by always returning
        False from _areDone, then L{SSHProtocolChecker} should raise
        L{NotEnoughAuthentication}.
        """
        checker = checkers.SSHProtocolChecker()
        def _areDone(avatarId):
            return False
        self.patch(checker, 'areDone', _areDone)

        passwordDatabase = InMemoryUsernamePasswordDatabaseDontUse()
        passwordDatabase.addUser('test', 'test')
        checker.registerChecker(passwordDatabase)
        d = checker.requestAvatarId(UsernamePassword('test', 'test'))
        return self.assertFailure(d, NotEnoughAuthentication)


    def test_requestAvatarIdInvalidCredential(self):
        """
        If the passed credentials aren't handled by any registered checker,
        L{SSHProtocolChecker} should raise L{UnhandledCredentials}.
        """
        checker = checkers.SSHProtocolChecker()
        d = checker.requestAvatarId(UsernamePassword('test', 'test'))
        return self.assertFailure(d, UnhandledCredentials)


    def test_areDone(self):
        """
        The default L{SSHProcotolChecker.areDone} should simply return True.
        """
        self.assertEqual(checkers.SSHProtocolChecker().areDone(None), True)



class UNIXPasswordDatabaseTests(TestCase):
    """
    Tests for L{UNIXPasswordDatabase}.
    """
    skip = cryptSkip or dependencySkip

    def assertLoggedIn(self, d, username):
        """
        Assert that the L{Deferred} passed in is called back with the value
        'username'.  This represents a valid login for this TestCase.

        NOTE: To work, this method's return value must be returned from the
        test method, or otherwise hooked up to the test machinery.

        @param d: a L{Deferred} from an L{IChecker.requestAvatarId} method.
        @type d: L{Deferred}
        @rtype: L{Deferred}
        """
        result = []
        d.addBoth(result.append)
        self.assertEqual(len(result), 1, "login incomplete")
        if isinstance(result[0], Failure):
            result[0].raiseException()
        self.assertEqual(result[0], username)


    def test_defaultCheckers(self):
        """
        L{UNIXPasswordDatabase} with no arguments has checks the C{pwd} database
        and then the C{spwd} database.
        """
        checker = checkers.UNIXPasswordDatabase()

        def crypted(username, password):
            salt = crypt.crypt(password, username)
            crypted = crypt.crypt(password, '$1$' + salt)
            return crypted

        pwd = UserDatabase()
        pwd.addUser('alice', crypted('alice', 'password'),
                    1, 2, 'foo', '/foo', '/bin/sh')
        # x and * are convention for "look elsewhere for the password"
        pwd.addUser('bob', 'x', 1, 2, 'bar', '/bar', '/bin/sh')
        spwd = ShadowDatabase()
        spwd.addUser('alice', 'wrong', 1, 2, 3, 4, 5, 6, 7)
        spwd.addUser('bob', crypted('bob', 'password'),
                     8, 9, 10, 11, 12, 13, 14)

        self.patch(checkers, 'pwd', pwd)
        self.patch(checkers, 'spwd', spwd)

        mockos = MockOS()
        self.patch(util, 'os', mockos)

        mockos.euid = 2345
        mockos.egid = 1234

        cred = UsernamePassword("alice", "password")
        self.assertLoggedIn(checker.requestAvatarId(cred), 'alice')
        self.assertEqual(mockos.seteuidCalls, [])
        self.assertEqual(mockos.setegidCalls, [])
        cred.username = "bob"
        self.assertLoggedIn(checker.requestAvatarId(cred), 'bob')
        self.assertEqual(mockos.seteuidCalls, [0, 2345])
        self.assertEqual(mockos.setegidCalls, [0, 1234])


    def assertUnauthorizedLogin(self, d):
        """
        Asserts that the L{Deferred} passed in is erred back with an
        L{UnauthorizedLogin} L{Failure}.  This reprsents an invalid login for
        this TestCase.

        NOTE: To work, this method's return value must be returned from the
        test method, or otherwise hooked up to the test machinery.

        @param d: a L{Deferred} from an L{IChecker.requestAvatarId} method.
        @type d: L{Deferred}
        @rtype: L{None}
        """
        self.assertRaises(
            checkers.UnauthorizedLogin, self.assertLoggedIn, d, 'bogus value')


    def test_passInCheckers(self):
        """
        L{UNIXPasswordDatabase} takes a list of functions to check for UNIX
        user information.
        """
        password = crypt.crypt('secret', 'secret')
        userdb = UserDatabase()
        userdb.addUser('anybody', password, 1, 2, 'foo', '/bar', '/bin/sh')
        checker = checkers.UNIXPasswordDatabase([userdb.getpwnam])
        self.assertLoggedIn(
            checker.requestAvatarId(UsernamePassword('anybody', 'secret')),
            'anybody')


    def test_verifyPassword(self):
        """
        If the encrypted password provided by the getpwnam function is valid
        (verified by the L{verifyCryptedPassword} function), we callback the
        C{requestAvatarId} L{Deferred} with the username.
        """
        def verifyCryptedPassword(crypted, pw):
            return crypted == pw
        def getpwnam(username):
            return [username, username]
        self.patch(checkers, 'verifyCryptedPassword', verifyCryptedPassword)
        checker = checkers.UNIXPasswordDatabase([getpwnam])
        credential = UsernamePassword('username', 'username')
        self.assertLoggedIn(checker.requestAvatarId(credential), 'username')


    def test_failOnKeyError(self):
        """
        If the getpwnam function raises a KeyError, the login fails with an
        L{UnauthorizedLogin} exception.
        """
        def getpwnam(username):
            raise KeyError(username)
        checker = checkers.UNIXPasswordDatabase([getpwnam])
        credential = UsernamePassword('username', 'username')
        self.assertUnauthorizedLogin(checker.requestAvatarId(credential))


    def test_failOnBadPassword(self):
        """
        If the verifyCryptedPassword function doesn't verify the password, the
        login fails with an L{UnauthorizedLogin} exception.
        """
        def verifyCryptedPassword(crypted, pw):
            return False
        def getpwnam(username):
            return [username, username]
        self.patch(checkers, 'verifyCryptedPassword', verifyCryptedPassword)
        checker = checkers.UNIXPasswordDatabase([getpwnam])
        credential = UsernamePassword('username', 'username')
        self.assertUnauthorizedLogin(checker.requestAvatarId(credential))


    def test_loopThroughFunctions(self):
        """
        UNIXPasswordDatabase.requestAvatarId loops through each getpwnam
        function associated with it and returns a L{Deferred} which fires with
        the result of the first one which returns a value other than None.
        ones do not verify the password.
        """
        def verifyCryptedPassword(crypted, pw):
            return crypted == pw
        def getpwnam1(username):
            return [username, 'not the password']
        def getpwnam2(username):
            return [username, username]
        self.patch(checkers, 'verifyCryptedPassword', verifyCryptedPassword)
        checker = checkers.UNIXPasswordDatabase([getpwnam1, getpwnam2])
        credential = UsernamePassword('username', 'username')
        self.assertLoggedIn(checker.requestAvatarId(credential), 'username')


    def test_failOnSpecial(self):
        """
        If the password returned by any function is C{""}, C{"x"}, or C{"*"} it
        is not compared against the supplied password.  Instead it is skipped.
        """
        pwd = UserDatabase()
        pwd.addUser('alice', '', 1, 2, '', 'foo', 'bar')
        pwd.addUser('bob', 'x', 1, 2, '', 'foo', 'bar')
        pwd.addUser('carol', '*', 1, 2, '', 'foo', 'bar')
        self.patch(checkers, 'pwd', pwd)

        checker = checkers.UNIXPasswordDatabase([checkers._pwdGetByName])
        cred = UsernamePassword('alice', '')
        self.assertUnauthorizedLogin(checker.requestAvatarId(cred))

        cred = UsernamePassword('bob', 'x')
        self.assertUnauthorizedLogin(checker.requestAvatarId(cred))

        cred = UsernamePassword('carol', '*')
        self.assertUnauthorizedLogin(checker.requestAvatarId(cred))



class AuthorizedKeyFileReaderTestCase(TestCase):
    """
    Tests for L{checkers.readAuthorizedKeyFile}
    """
    def test_ignoresComments(self):
        """
        L{checkers.readAuthorizedKeyFile} does not attempt to turn comments
        into keys
        """
        fileobj = StringIO('# this comment is ignored\n'
                           'this is not\n'
                           '# this is again\n'
                           'and this is not')
        result = checkers.readAuthorizedKeyFile(fileobj, lambda x: x)
        self.assertEqual(['this is not', 'and this is not'], list(result))


    def test_ignoresLeadingWhitespaceAndEmptyLines(self):
        """
        L{checkers.readAuthorizedKeyFile} ignores leading whitespace in
        lines, as well as empty lines
        """
        fileobj = StringIO("""
                           # ignore
                           not ignored
                           """)
        result = checkers.readAuthorizedKeyFile(fileobj, parsekey=lambda x: x)
        self.assertEqual(['not ignored'], list(result))


    def test_ignoresUnparsableKeys(self):
        """
        L{checkers.readAuthorizedKeyFile} does not raise an exception
        when a key fails to parse, but rather just keeps going
        """
        def fail_on_some(line):
            if line.startswith('f'):
                raise Exception('failed to parse')
            return line

        fileobj = StringIO('failed key\ngood key')
        result = checkers.readAuthorizedKeyFile(fileobj,
                                                parsekey=fail_on_some)
        self.assertEqual(['good key'], list(result))



class InMemoryKeyMappingTestCase(TestCase):
    """
    Tests for L{checkers.InMemoryKeyMapping}
    """
    def test_implementsInterface(self):
        """
        L{checkers.InMemoryKeyMapping} implements
        L{checkers.IAuthorizedKeysDB}
        """
        keydb = checkers.InMemoryKeyMapping({'alice': ['key']})
        verifyObject(checkers.IAuthorizedKeysDB, keydb)


    def test_noKeysForUnauthorizedUser(self):
        """
        If the user is not in the mapping provided to
        L{checkers.InMemoryKeyMapping}, an empty iterator is returned
        by L{checkers.InMemoryKeyMapping.getAuthorizedKeys}
        """
        keydb = checkers.InMemoryKeyMapping({'alice': ['keys']})
        self.assertEqual([], list(keydb.getAuthorizedKeys('bob')))


    def test_allKeysForAuthorizedUser(self):
        """
        If the user is in the mapping provided to
        L{checkers.InMemoryKeyMapping}, an iterator with all the keys
        is returned by
        L{checkers.AuthorizedKeysFilesMapping.getAuthorizedKeys}
        """
        keydb = checkers.InMemoryKeyMapping({'alice': ['a', 'b']})
        self.assertEqual(['a', 'b'], list(keydb.getAuthorizedKeys('alice')))



class AuthorizedKeysFilesMappingTestCase(TestCase):
    """
    Tests for L{checkers.AuthorizedKeysFilesMapping}
    """
    def setUp(self):
        self.root = FilePath(self.mktemp())
        self.root.makedirs()

        authorizedKeys = [self.root.child('key{0}'.format(i))
                           for i in range(2)]
        for i, fp in enumerate(authorizedKeys):
            fp.setContent('file {0} key 1\nfile {0} key 2'.format(i))

        self.authorizedPaths = [fp.path for fp in authorizedKeys]


    def test_implementsInterface(self):
        """
        L{checkers.AuthorizedKeysFilesMapping} implements
        L{checkers.IAuthorizedKeysDB}
        """
        keydb = checkers.AuthorizedKeysFilesMapping(
            {'alice': self.authorizedPaths})
        verifyObject(checkers.IAuthorizedKeysDB, keydb)


    def test_noKeysForUnauthorizedUser(self):
        """
        If the user is not in the mapping provided to
        L{checkers.AuthorizedKeysFilesMapping}, an empty iterator is returned
        by L{checkers.AuthorizedKeysFilesMapping.getAuthorizedKeys}
        """
        keydb = checkers.AuthorizedKeysFilesMapping(
            {'alice': self.authorizedPaths}, lambda x: x)
        self.assertEqual([], list(keydb.getAuthorizedKeys('bob')))


    def test_allKeysInAllAuthorizedFilesForAuthorizedUser(self):
        """
        If the user is in the mapping provided to
        L{checkers.AuthorizedKeysFilesMapping}, an iterator with all the keys
        in all the authorized files is returned by
        L{checkers.AuthorizedKeysFilesMapping.getAuthorizedKeys}
        """
        keydb = checkers.AuthorizedKeysFilesMapping(
            {'alice': self.authorizedPaths}, lambda x: x)
        keys = ['file 0 key 1', 'file 0 key 2',
                'file 1 key 1', 'file 1 key 2']
        self.assertEqual(keys, list(keydb.getAuthorizedKeys('alice')))


    def test_ignoresNonexistantOrUnreadableFile(self):
        """
        L{checkers.AuthorizedKeysFilesMapping.getAuthorizedKeys} returns only
        the keys in the authorized files named that exist and are readable
        """
        directory = self.root.child('key2')
        directory.makedirs()

        keydb = checkers.AuthorizedKeysFilesMapping(
            {'alice': [directory.path,
                       self.root.child('key3').path,
                       self.authorizedPaths[0]]},
            lambda x: x)
        self.assertEqual(['file 0 key 1', 'file 0 key 2'],
                         list(keydb.getAuthorizedKeys('alice')))



class UNIXAuthorizedKeysFilesTestCase(TestCase):
    """
    Tests for L{checkers.UNIXAuthorizedKeysFiles}
    """
    def setUp(self):
        mockos = MockOS()
        mockos.path = FilePath(self.mktemp())
        mockos.path.makedirs()

        self.userdb = UserDatabase()
        self.userdb.addUser('alice', 'password', 1, 2, 'alice lastname',
                            mockos.path.path, '/bin/shell')

        self.sshDir = mockos.path.child('.ssh')
        self.sshDir.makedirs()
        authorizedKeys = self.sshDir.child('authorized_keys')
        authorizedKeys.setContent('key 1\nkey 2')


    def test_implementsInterface(self):
        """
        L{checkers.UNIXAuthorizedKeysFiles} implements
        L{checkers.IAuthorizedKeysDB}
        """
        keydb = checkers.UNIXAuthorizedKeysFiles(self.userdb)
        verifyObject(checkers.IAuthorizedKeysDB, keydb)


    def test_noKeysForUnauthorizedUser(self):
        """
        If the user is not in the user database provided to
        L{checkers.UNIXAuthorizedKeysFiles}, an empty iterator is returned
        by L{checkers.UNIXAuthorizedKeysFiles.getAuthorizedKeys}
        """
        keydb = checkers.UNIXAuthorizedKeysFiles(self.userdb,
                                                 parsekey=lambda x: x)
        self.assertEqual([], list(keydb.getAuthorizedKeys('bob')))


    def test_allKeysInAllAuthorizedFilesForAuthorizedUser(self):
        """
        If the user is in the user database provided to
        L{checkers.UNIXAuthorizedKeysFiles}, an iterator with all the keys in
        C{~/.ssh/authorized_keys} and C{~/.ssh/authorized_keys2} is returned
        by L{checkers.UNIXAuthorizedKeysFiles.getAuthorizedKeys}
        """
        self.sshDir.child('authorized_keys2').setContent('key 3')
        keydb = checkers.UNIXAuthorizedKeysFiles(self.userdb,
                                                 parsekey=lambda x: x)
        self.assertEqual(['key 1', 'key 2', 'key 3'],
                         list(keydb.getAuthorizedKeys('alice')))


    def test_ignoresNonexistantFile(self):
        """
        L{checkers.AuthorizedKeysFilesMapping.getAuthorizedKeys} returns only
        the keys in C{~/.ssh/authorized_keys} and C{~/.ssh/authorized_keys2}
        if they exist
        """
        keydb = checkers.UNIXAuthorizedKeysFiles(self.userdb,
                                                 parsekey=lambda x: x)
        self.assertEqual(['key 1', 'key 2'],
                         list(keydb.getAuthorizedKeys('alice')))


    def test_ignoresUnreadableFile(self):
        """
        L{checkers.AuthorizedKeysFilesMapping.getAuthorizedKeys} returns only
        the keys in C{~/.ssh/authorized_keys} and C{~/.ssh/authorized_keys2}
        if they are readable
        """
        self.sshDir.child('authorized_keys2').makedirs()
        keydb = checkers.UNIXAuthorizedKeysFiles(self.userdb,
                                                 parsekey=lambda x: x)
        self.assertEqual(['key 1', 'key 2'],
                         list(keydb.getAuthorizedKeys('alice')))



_KeyDB = namedtuple('KeyDB', ['getAuthorizedKeys'])



class _DummyException(Exception):
    pass



class SSHPublicKeyCheckerTestCase(TestCase):
    """
    Tests for L{checkers.SSHPublicKeyChecker}
    """
    skip = cryptSkip or dependencySkip


    def setUp(self):
        self.credentials = SSHPrivateKey(
            'alice', 'ssh-rsa', keydata.publicRSA_openssh, 'foo',
             keys.Key.fromString(keydata.privateRSA_openssh).sign('foo'))
        self.keydb = _KeyDB(lambda _: [
            keys.Key.fromString(keydata.publicRSA_openssh)])
        self.checker = checkers.SSHPublicKeyChecker(self.keydb)


    def test_credentialsWithoutSignature(self):
        """
        Calling L{checkers.SSHPublicKeyChecker.requestAvatarId} with
        credentials that do not have a signature fails with L{ValidPublicKey}
        """
        self.credentials.signature = None
        self.failureResultOf(self.checker.requestAvatarId(self.credentials),
                             ValidPublicKey)


    def test_credentialsWithBadKey(self):
        """
        Calling L{checkers.SSHPublicKeyChecker.requestAvatarId} with
        credentials that have a bad key fails with L{keys.BadKeyError}
        """
        self.credentials.blob = ''
        self.failureResultOf(self.checker.requestAvatarId(self.credentials),
                             keys.BadKeyError)


    def test_failureGettingAuthorizedKeys(self):
        """
        If L{checkers.IAuthorizedKeysDB.getAuthorizedKeys} raises an
        exception, L{checkers.SSHPublicKeyChecker.requestAvatarId} fails with
        L{UnauthorizedLogin}
        """
        def fail(_):
            raise _DummyException()

        self.keydb = _KeyDB(fail)
        self.checker = checkers.SSHPublicKeyChecker(self.keydb)
        self.failureResultOf(self.checker.requestAvatarId(self.credentials),
                             UnauthorizedLogin)
        self.flushLoggedErrors(_DummyException)


    def test_credentialsNoMatchingKey(self):
        """
        If L{checkers.IAuthorizedKeysDB.getAuthorizedKeys} returns no keys
        that match the credentials,
        L{checkers.SSHPublicKeyChecker.requestAvatarId} fails with
        L{UnauthorizedLogin}
        """
        self.credentials.blob = keydata.publicDSA_openssh
        self.failureResultOf(self.checker.requestAvatarId(self.credentials),
                             UnauthorizedLogin)


    def test_credentialsInvalidSignature(self):
        """
        Calling L{checkers.SSHPublicKeyChecker.requestAvatarId} with
        credentials that are incorrectly signed fails with
        L{UnauthorizedLogin}
        """
        self.credentials.signature = (
            keys.Key.fromString(keydata.privateDSA_openssh).sign('foo'))
        self.failureResultOf(self.checker.requestAvatarId(self.credentials),
                             UnauthorizedLogin)


    def test_failureVerifyingKey(self):
        """
        If L{keys.Key.verify} raises an exception,
        L{checkers.SSHPublicKeyChecker.requestAvatarId} fails with
        L{UnauthorizedLogin}
        """
        def fail(*args, **kwargs):
            raise _DummyException()

        self.patch(keys.Key, 'verify', fail)

        self.failureResultOf(self.checker.requestAvatarId(self.credentials),
                             UnauthorizedLogin)
        self.flushLoggedErrors(_DummyException)


    def test_usernameReturnedOnSuccess(self):
        """
        L{checker.SSHPublicKeyChecker.requestAvatarId}, if successful,
        callbacks with the username
        """
        d = self.checker.requestAvatarId(self.credentials)
        self.assertEqual('alice', self.successResultOf(d))
