##
# Copyright (c) 2010-2015 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

"""
Tests for txdav.caldav.datastore.postgres, mostly based on
L{txdav.caldav.datastore.test.common}.
"""

from pycalendar.datetime import DateTime
from pycalendar.timezone import Timezone


from txweb2 import responsecode
from txweb2.http_headers import MimeType
from txweb2.stream import MemoryStream

from twisted.python.filepath import FilePath
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue, DeferredList, \
    succeed, maybeDeferred
from twisted.internet.task import deferLater, Clock
from twisted.trial import unittest

from twistedcaldav import caldavxml, ical
from twistedcaldav.caldavxml import CalendarDescription
from twistedcaldav.stdconfig import config
from twistedcaldav.dateops import datetimeMktime
from twistedcaldav.ical import Component, normalize_iCalStr, diff_iCalStrs
from twistedcaldav.instance import InvalidOverriddenInstanceError
from twistedcaldav.timezones import TimezoneCache, readVTZ, TimezoneException

from txdav.base.propertystore.base import PropertyName
from txdav.caldav.datastore.query.filter import Filter
from txdav.caldav.datastore.scheduling.caldav.scheduler import CalDAVScheduler
from txdav.caldav.datastore.scheduling.cuaddress import RemoteCalendarUser, \
    LocalCalendarUser
from txdav.caldav.datastore.scheduling.implicit import ImplicitScheduler
from txdav.caldav.datastore.scheduling.itip import iTIPRequestStatus
from txdav.caldav.datastore.scheduling.processing import ImplicitProcessor
from txdav.caldav.datastore.scheduling.scheduler import ScheduleResponseQueue
from txdav.caldav.datastore.sql import CalendarStoreFeatures, CalendarObject
from txdav.common.datastore.sql import ECALENDARTYPE, CommonObjectResource, \
    CommonStoreTransactionMonitor
from txdav.common.datastore.sql_tables import schema, _BIND_MODE_DIRECT, \
    _BIND_STATUS_ACCEPTED, _TRANSP_OPAQUE
from txdav.caldav.datastore.test.common import CommonTests as CalendarCommonTests, \
    test_event_text
from txdav.caldav.datastore.test.test_file import setUpCalendarStore
from txdav.common.datastore.test.util import populateCalendarsFrom, \
    CommonCommonTests
from txdav.caldav.datastore.util import _migrateCalendar, migrateHome
from txdav.caldav.icalendarstore import ComponentUpdateState, InvalidDefaultCalendar, \
    InvalidSplit, UnknownTimezone
from txdav.common.icommondatastore import NoSuchObjectResourceError, \
    InvalidComponentForStoreError
from txdav.idav import ChangeCategory
from txdav.xml.rfc2518 import GETContentLanguage, ResourceType

from twext.enterprise.dal.syntax import Select, Parameter, Insert, Delete, \
    Update
from twext.enterprise.ienterprise import AlreadyFinishedError
from twext.enterprise.jobqueue import JobItem
from twext.enterprise.util import parseSQLTimestamp

import datetime
import os


class CalendarSQLStorageTests(CalendarCommonTests, unittest.TestCase):
    """
    Calendar SQL storage tests.
    """

    @inlineCallbacks
    def setUp(self):
        yield super(CalendarSQLStorageTests, self).setUp()
        yield self.buildStoreAndDirectory()
        yield self.populate()

        self.nowYear = {"now": DateTime.getToday().getYear()}


    @inlineCallbacks
    def populate(self):
        yield populateCalendarsFrom(self.requirements, self.storeUnderTest())
        self.notifierFactory.reset()


    @inlineCallbacks
    def assertCalendarsSimilar(self, a, b, bCalendarFilter=None):
        """
        Assert that two calendars have a similar structure (contain the same
        events).
        """
        @inlineCallbacks
        def namesAndComponents(x, filter=lambda x: x.component()):
            result = {}
            for fromObj in (yield x.calendarObjects()):
                result[fromObj.name()] = yield filter(fromObj)
            returnValue(result)
        if bCalendarFilter is not None:
            extra = [bCalendarFilter]
        else:
            extra = []
        self.assertEquals((yield namesAndComponents(a)),
                          (yield namesAndComponents(b, *extra)))


    def assertPropertiesSimilar(self, a, b, disregard=[]):
        """
        Assert that two objects with C{properties} methods have similar
        properties.

        @param disregard: a list of L{PropertyName} keys to discard from both
            input and output.
        """
        def sanitize(x):
            result = dict(x.properties().items())
            for key in disregard:
                result.pop(key, None)
            return result
        self.assertEquals(sanitize(a), sanitize(b))


    def fileTransaction(self):
        """
        Create a file-backed calendar transaction, for migration testing.
        """
        setUpCalendarStore(self)
        fileStore = self.calendarStore
        txn = fileStore.newTransaction()
        self.addCleanup(txn.commit)
        return txn


    @inlineCallbacks
    def test_purgingHome(self):
        """
        Purging a calendar homes changes its status.
        """

        home = yield self.homeUnderTest(name="home1")
        self.assertFalse(home.purging())
        self.assertFalse(home.external())

        yield home.purge()
        self.assertTrue(home.purging())
        self.assertFalse(home.external())

        yield self.commit()

        home = yield self.homeUnderTest(name="home1")
        self.assertTrue(home is not None)
        yield self.commit()


    @inlineCallbacks
    def test_migrateCalendarFromFile(self):
        """
        C{_migrateCalendar()} can migrate a file-backed calendar to a database-
        backed calendar.
        """
        fromCalendar = yield (yield self.fileTransaction().calendarHomeWithUID(
            "home1")).calendarWithName("calendar_1")
        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "new-home", create=True)
        toCalendar = yield toHome.calendarWithName("calendar")
        yield _migrateCalendar(fromCalendar, toCalendar,
                               lambda x: x.component())
        yield self.assertCalendarsSimilar(fromCalendar, toCalendar)


    @inlineCallbacks
    def test_migrateBadCalendarFromFile(self):
        """
        C{_migrateCalendar()} can migrate a file-backed calendar to a database-
        backed calendar. We need to test what happens when there is "bad" calendar data
        present in the file-backed calendar.
        """
        fromCalendar = yield (yield self.fileTransaction().calendarHomeWithUID(
            "home_bad")).calendarWithName("calendar_bad")
        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "new-home", create=True)
        toCalendar = yield toHome.calendarWithName("calendar")
        ok, bad = (yield _migrateCalendar(fromCalendar, toCalendar, lambda x: x.component()))
        self.assertEqual(ok, 1)
        self.assertEqual(bad, 2)


    @inlineCallbacks
    def test_migrateRecurrenceFixCalendarFromFile(self):
        """
        C{_migrateCalendar()} can migrate a file-backed calendar to a database-
        backed calendar. We need to test what happens when there is "bad" calendar data
        present in the file-backed calendar with a broken recurrence-id that we can fix.
        """

        self.storeUnderTest().setMigrating(True)
        fromCalendar = yield (yield self.fileTransaction().calendarHomeWithUID(
            "home_bad")).calendarWithName("calendar_fix_recurrence")
        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "new-home", create=True)
        toCalendar = yield toHome.calendarWithName("calendar")
        ok, bad = (yield _migrateCalendar(fromCalendar, toCalendar, lambda x: x.component()))
        self.assertEqual(ok, 3)
        self.assertEqual(bad, 0)

        self.transactionUnderTest().commit()
        self.storeUnderTest().setMigrating(False)

        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "new-home", create=True)
        toCalendar = yield toHome.calendarWithName("calendar")
        toResource = yield toCalendar.calendarObjectWithName("1.ics")
        caldata = yield toResource.componentForUser()
        self.assertEqual(str(caldata), """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VTIMEZONE
TZID:US/Eastern
LAST-MODIFIED:20040110T032845Z
BEGIN:DAYLIGHT
DTSTART:20000404T020000
RRULE:FREQ=YEARLY;BYDAY=1SU;BYMONTH=4
TZNAME:EDT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
END:DAYLIGHT
BEGIN:STANDARD
DTSTART:20001026T020000
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10
TZNAME:EST
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:uid2
DTSTART;TZID=US/Eastern:%(now)s0102T140000
DURATION:PT1H
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
RDATE;TZID=US/Eastern:%(now)s0104T160000
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:event 6-ctr
END:VEVENT
BEGIN:VEVENT
UID:uid2
RECURRENCE-ID;TZID=US/Eastern:%(now)s0104T160000
DTSTART;TZID=US/Eastern:%(now)s0104T160000
DURATION:PT1H
CREATED:20060102T190000Z
DESCRIPTION:Some notes
DTSTAMP:20051222T210507Z
SUMMARY:event 6-ctr changed again
BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n") % self.nowYear)

        toResource = yield toCalendar.calendarObjectWithName("2.ics")
        caldata = yield toResource.componentForUser()
        self.assertEqual(str(caldata), """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VTIMEZONE
TZID:US/Eastern
LAST-MODIFIED:20040110T032845Z
BEGIN:DAYLIGHT
DTSTART:20000404T020000
RRULE:FREQ=YEARLY;BYDAY=1SU;BYMONTH=4
TZNAME:EDT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
END:DAYLIGHT
BEGIN:STANDARD
DTSTART:20001026T020000
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10
TZNAME:EST
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:uid3
DTSTART;TZID=US/Eastern:%(now)s0102T140000
DURATION:PT1H
ATTENDEE:urn:uuid:home_bad
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
ORGANIZER:urn:uuid:home_bad
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:event 6-ctr
END:VEVENT
BEGIN:VEVENT
UID:uid3
RECURRENCE-ID;TZID=US/Eastern:%(now)s0104T140000
DTSTART;TZID=US/Eastern:%(now)s0104T160000
DURATION:PT1H
CREATED:20060102T190000Z
DESCRIPTION:Some notes
DTSTAMP:20051222T210507Z
ORGANIZER:urn:uuid:home_bad
SUMMARY:event 6-ctr changed again
BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n") % self.nowYear)

        toResource = yield toCalendar.calendarObjectWithName("3.ics")
        caldata = yield toResource.componentForUser()
        self.assertEqual(str(caldata), """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VTIMEZONE
TZID:US/Eastern
LAST-MODIFIED:20040110T032845Z
BEGIN:DAYLIGHT
DTSTART:20000404T020000
RRULE:FREQ=YEARLY;BYDAY=1SU;BYMONTH=4
TZNAME:EDT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
END:DAYLIGHT
BEGIN:STANDARD
DTSTART:20001026T020000
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10
TZNAME:EST
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:uid4
DTSTART;TZID=US/Eastern:%(now)s0104T160000
DURATION:PT1H
CREATED:20060102T190000Z
DESCRIPTION:Some notes
DTSTAMP:20051222T210507Z
RDATE;TZID=US/Eastern:%(now)s0104T160000
SUMMARY:event 6-ctr changed again
END:VEVENT
BEGIN:VEVENT
UID:uid4
RECURRENCE-ID;TZID=US/Eastern:%(now)s0104T160000
DTSTART;TZID=US/Eastern:%(now)s0104T160000
DURATION:PT1H
CREATED:20060102T190000Z
DESCRIPTION:Some notes
DTSTAMP:20051222T210507Z
SUMMARY:event 6-ctr changed again
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n") % self.nowYear)


    @inlineCallbacks
    def test_migrateDuplicateAttachmentsCalendarFromFile(self):
        """
        C{_migrateCalendar()} can migrate a file-backed calendar to a database-
        backed calendar. Test that migrating a calendar containing duplicate attachments
        will de-duplicate those attachments and proceed without error.
        """
        # Need to tweak config and settings to setup dropbox to work
        self.patch(config, "EnableDropBox", True)
        self.patch(config, "EnableManagedAttachments", False)
        self._sqlCalendarStore.enableManagedAttachments = False

        txn = self._sqlCalendarStore.newTransaction()
        cs = schema.CALENDARSERVER
        yield Delete(
            From=cs,
            Where=cs.NAME == "MANAGED-ATTACHMENTS"
        ).on(txn)
        yield txn.commit()

        fromCalendar = yield (yield self.fileTransaction().calendarHomeWithUID(
            "home_attachments")).calendarWithName("calendar_1")
        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "home_attachments", create=True)
        toCalendar = yield toHome.calendarWithName("calendar")
        ok, bad = (yield _migrateCalendar(fromCalendar, toCalendar, lambda x: x.component()))
        self.assertEqual(ok, 3)
        self.assertEqual(bad, 0)


    @inlineCallbacks
    def test_migrateCalendarFromFile_Transparency(self):
        """
        C{_migrateCalendar()} can migrate a file-backed calendar to a database-
        backed calendar.
        """
        fromCalendar = yield (yield self.fileTransaction().calendarHomeWithUID(
            "home1")).calendarWithName("calendar_1")
        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "new-home", create=True)
        toCalendar = yield toHome.calendarWithName("calendar")
        yield _migrateCalendar(fromCalendar, toCalendar,
                               lambda x: x.component())

        filter = caldavxml.Filter(
            caldavxml.ComponentFilter(
                caldavxml.ComponentFilter(
                    caldavxml.TimeRange(start="%(now)s0201T000000Z" % self.nowYear, end="%(now)s0202T000000Z" % self.nowYear),
                    name=("VEVENT", "VFREEBUSY", "VAVAILABILITY"),
                ),
                name="VCALENDAR",
            )
        )
        filter = Filter(filter)
        filter.settimezone(None)

        results = yield toCalendar.search(filter, 'user01', True)
        self.assertEquals(len(results), 1)
        _ignore_name, uid, _ignore_type, _ignore_organizer, _ignore_float, _ignore_start, _ignore_end, _ignore_fbtype, transp = results[0]
        self.assertEquals(uid, "uid4")
        self.assertEquals(transp, 'T')


    @inlineCallbacks
    def test_migrateHomeFromFile(self):
        """
        L{migrateHome} will migrate an L{ICalendarHome} provider from one
        backend to another; in this specific case, from the file-based backend
        to the SQL-based backend.
        """

        # Need to turn of split calendar behavior just for this test
        self.patch(config, "RestrictCalendarsToOneComponentType", False)

        fromHome = yield self.fileTransaction().calendarHomeWithUID("home1")

        builtinProperties = [PropertyName.fromElement(ResourceType)]

        # Populate an arbitrary / unused dead properties so there's something
        # to verify against.

        key = PropertyName.fromElement(GETContentLanguage)
        fromHome.properties()[key] = GETContentLanguage("C")
        (yield fromHome.calendarWithName("calendar_1")).properties()[key] = (
            GETContentLanguage("pig-latin")
        )
        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "new-home", create=True
        )
        yield migrateHome(fromHome, toHome, lambda x: x.component())
        toCalendars = yield toHome.calendars()
        self.assertEquals(set([c.name() for c in toCalendars if c.name() not in ("inbox",)]),
                          set([k for k in self.requirements['home1'].keys()
                               if self.requirements['home1'][k] is not None]))
        fromCalendars = yield fromHome.calendars()
        for c in fromCalendars:
            self.assertPropertiesSimilar(
                c, (yield toHome.calendarWithName(c.name())),
                builtinProperties
            )
        self.assertPropertiesSimilar(fromHome, toHome, builtinProperties)


    @inlineCallbacks
    def test_migrateHomeSplits(self):
        """
        Make sure L{migrateHome} also splits calendars by component type.
        """
        fromHome = yield self.fileTransaction().calendarHomeWithUID("home_splits")
        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "new-home", create=True
        )
        yield migrateHome(fromHome, toHome, lambda x: x.component())
        toCalendars = yield toHome.calendars()
        fromCalendars = yield fromHome.calendars()
        for c in fromCalendars:
            self.assertTrue(
                (yield toHome.calendarWithName(c.name())) is not None
            )

        supported_components = set()
        self.assertEqual(len(toCalendars), 2 + len(ical.allowedStoreComponents))
        for calendar in toCalendars:
            if calendar.name() in ("inbox",):
                continue
            result = yield calendar.getSupportedComponents()
            supported_components.add(result)

        self.assertEqual(supported_components, set(ical.allowedStoreComponents))


    @inlineCallbacks
    def test_migrateHomeNoSplits(self):
        """
        Make sure L{migrateHome} also splits calendars by component type.
        """
        fromHome = yield self.fileTransaction().calendarHomeWithUID("home_no_splits")
        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "new-home", create=True
        )
        yield migrateHome(fromHome, toHome, lambda x: x.component())
        toCalendars = yield toHome.calendars()
        fromCalendars = yield fromHome.calendars()
        for c in fromCalendars:
            self.assertTrue(
                (yield toHome.calendarWithName(c.name())) is not None
            )

        supported_components = set()
        self.assertEqual(len(toCalendars), 3)
        for calendar in toCalendars:
            if calendar.name() in ("inbox",):
                continue
            result = yield calendar.getSupportedComponents()
            supported_components.add(result)

        self.assertEqual(supported_components, set(ical.allowedStoreComponents))


    @inlineCallbacks
    def test_calendarHomeVersion(self):
        """
        The DATAVERSION column for new calendar homes must match the
        CALENDAR-DATAVERSION value.
        """

        self.transactionUnderTest()._homeClass[ECALENDARTYPE]._dataVersionValue = None
        home = yield self.transactionUnderTest().calendarHomeWithUID("home_version", create=True)
        self.assertTrue(home is not None)
        yield self.transactionUnderTest().commit

        txn = yield self.transactionUnderTest()
        version = yield txn.calendarserverValue("CALENDAR-DATAVERSION")
        ch = schema.CALENDAR_HOME
        homeVersion = (yield Select(
            [ch.DATAVERSION, ],
            From=ch,
            Where=ch.OWNER_UID == "home_version",
        ).on(txn))[0][0]
        self.assertEqual(int(homeVersion), int(version))


    @inlineCallbacks
    def test_homeProvisioningConcurrency(self):
        """
        Test that two concurrent attempts to provision a calendar home do not
        cause a race-condition whereby the second commit results in a second
        C{INSERT} that violates a unique constraint. Also verify that, while
        the two provisioning attempts are happening and doing various lock
        operations, that we do not block other reads of the table.
        """

        calendarStore = self._sqlCalendarStore

        txn1 = calendarStore.newTransaction()
        txn2 = calendarStore.newTransaction()
        txn3 = calendarStore.newTransaction()

        # Provision one home now - we will use this to later verify we can do
        # reads of existing data in the table
        home_uid2 = yield txn3.homeWithUID(ECALENDARTYPE, "uid2", create=True)
        self.assertNotEqual(home_uid2, None)
        yield txn3.commit()

        home_uid1_1 = yield txn1.homeWithUID(
            ECALENDARTYPE, "uid1", create=True
        )

        @inlineCallbacks
        def _defer_home_uid1_2():
            home_uid1_2 = yield txn2.homeWithUID(
                ECALENDARTYPE, "uid1", create=True
            )
            yield txn2.commit()
            returnValue(home_uid1_2)
        d1 = _defer_home_uid1_2()

        @inlineCallbacks
        def _pause_home_uid1_1():
            yield deferLater(reactor, 1.0, lambda : None)
            yield txn1.commit()
        d2 = _pause_home_uid1_1()

        # Verify that we can still get to the existing home - i.e. the lock
        # on the table allows concurrent reads
        txn4 = calendarStore.newTransaction()
        home_uid2 = yield txn4.homeWithUID(ECALENDARTYPE, "uid2", create=True)
        self.assertNotEqual(home_uid2, None)
        yield txn4.commit()

        # Now do the concurrent provision attempt
        yield d2
        home_uid1_2 = yield d1

        self.assertNotEqual(home_uid1_1, None)
        self.assertNotEqual(home_uid1_2, None)


    @inlineCallbacks
    def test_putConcurrency(self):
        """
        Test that two concurrent attempts to PUT different calendar object
        resources to the same calendar home does not cause a deadlock.
        """

        calendarStore = self._sqlCalendarStore

        # Provision the home and calendar now
        txn = calendarStore.newTransaction()
        home = yield txn.homeWithUID(ECALENDARTYPE, "user01", create=True)
        self.assertNotEqual(home, None)
        cal = yield home.calendarWithName("calendar")
        self.assertNotEqual(cal, None)
        yield txn.commit()

        txn1 = calendarStore.newTransaction()
        txn2 = calendarStore.newTransaction()

        home1 = yield txn1.homeWithUID(ECALENDARTYPE, "user01", create=True)
        home2 = yield txn2.homeWithUID(ECALENDARTYPE, "user01", create=True)

        cal1 = yield home1.calendarWithName("calendar")
        cal2 = yield home2.calendarWithName("calendar")

        @inlineCallbacks
        def _defer1():
            yield cal1.createObjectResourceWithName("1.ics", Component.fromString("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VTIMEZONE
TZID:US/Pacific
BEGIN:DAYLIGHT
TZOFFSETFROM:-0800
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
DTSTART:20070311T020000
TZNAME:PDT
TZOFFSETTO:-0700
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0700
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
DTSTART:20071104T020000
TZNAME:PST
TZOFFSETTO:-0800
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTEND;TZID=US/Pacific:%(now)s0207T173000
TRANSP:OPAQUE
SUMMARY:New Event
DTSTART;TZID=US/Pacific:%(now)s0207T170000
DTSTAMP:20100203T013909Z
SEQUENCE:3
BEGIN:VALARM
X-WR-ALARMUID:1377CCC7-F85C-4610-8583-9513D4B364E1
TRIGGER:-PT20M
ATTACH;VALUE=URI:Basso
ACTION:AUDIO
END:VALARM
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n") % self.nowYear
            ))
            yield txn1.commit()
        d1 = _defer1()

        @inlineCallbacks
        def _defer2():
            yield cal2.createObjectResourceWithName("2.ics", Component.fromString("""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VTIMEZONE
TZID:US/Pacific
BEGIN:DAYLIGHT
TZOFFSETFROM:-0800
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
DTSTART:20070311T020000
TZNAME:PDT
TZOFFSETTO:-0700
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0700
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
DTSTART:20071104T020000
TZNAME:PST
TZOFFSETTO:-0800
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid2
DTEND;TZID=US/Pacific:%(now)s0207T173000
TRANSP:OPAQUE
SUMMARY:New Event
DTSTART;TZID=US/Pacific:%(now)s0207T170000
DTSTAMP:20100203T013909Z
SEQUENCE:3
BEGIN:VALARM
X-WR-ALARMUID:1377CCC7-F85C-4610-8583-9513D4B364E1
TRIGGER:-PT20M
ATTACH;VALUE=URI:Basso
ACTION:AUDIO
END:VALARM
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n") % self.nowYear
            ))
            yield txn2.commit()
        d2 = _defer2()

        yield d1
        yield d2


    @inlineCallbacks
    def test_datetimes(self):
        calendarStore = self._sqlCalendarStore

        # Provision the home and calendar now
        txn = calendarStore.newTransaction()
        home = yield txn.homeWithUID(ECALENDARTYPE, "uid1", create=True)
        cal = yield home.calendarWithName("calendar")
        cal._created = parseSQLTimestamp("2011-02-05 11:22:47")
        cal._modified = parseSQLTimestamp("2011-02-06 11:22:47")
        self.assertEqual(cal.created(), datetimeMktime(datetime.datetime(2011, 2, 5, 11, 22, 47)))
        self.assertEqual(cal.modified(), datetimeMktime(datetime.datetime(2011, 2, 6, 11, 22, 47)))

        obj = yield self.calendarObjectUnderTest()
        obj._created = parseSQLTimestamp("2011-02-07 11:22:47")
        obj._modified = parseSQLTimestamp("2011-02-08 11:22:47")
        self.assertEqual(obj.created(), datetimeMktime(datetime.datetime(2011, 2, 7, 11, 22, 47)))
        self.assertEqual(obj.modified(), datetimeMktime(datetime.datetime(2011, 2, 8, 11, 22, 47)))


    @inlineCallbacks
    def test_notificationsProvisioningConcurrency(self):
        """
        Test that two concurrent attempts to provision a notifications collection do not
        cause a race-condition whereby the second commit results in a second
        C{INSERT} that violates a unique constraint.
        """

        calendarStore = self._sqlCalendarStore

        txn1 = calendarStore.newTransaction()
        txn2 = calendarStore.newTransaction()

        notification_uid1_1 = yield txn1.notificationsWithUID(
            "uid1", create=True
        )

        @inlineCallbacks
        def _defer_notification_uid1_2():
            notification_uid1_2 = yield txn2.notificationsWithUID(
                "uid1", create=True
            )
            yield txn2.commit()
            returnValue(notification_uid1_2)
        d1 = _defer_notification_uid1_2()

        @inlineCallbacks
        def _pause_notification_uid1_1():
            yield deferLater(reactor, 1.0, lambda : None)
            yield txn1.commit()
        d2 = _pause_notification_uid1_1()

        # Now do the concurrent provision attempt
        yield d2
        notification_uid1_2 = yield d1

        self.assertNotEqual(notification_uid1_1, None)
        self.assertNotEqual(notification_uid1_2, None)


    @inlineCallbacks
    def test_removeCalendarPropertiesOnDelete(self):
        """
        L{ICalendarHome.removeCalendarWithName} removes a calendar that already
        exists and makes sure properties are also removed.
        """

        # Create calendar and add a property
        home = yield self.homeUnderTest()
        name = "remove-me"
        calendar = yield home.createCalendarWithName(name)
        resourceID = calendar._resourceID
        calendarProperties = calendar.properties()

        prop = caldavxml.CalendarDescription.fromString("Calendar to be removed")
        calendarProperties[PropertyName.fromElement(prop)] = prop
        yield self.commit()

        prop = schema.RESOURCE_PROPERTY
        _allWithID = Select(
            [prop.NAME, prop.VIEWER_UID, prop.VALUE],
            From=prop,
            Where=prop.RESOURCE_ID == Parameter("resourceID")
        )

        # Check that one property is present
        home = yield self.homeUnderTest()
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 1)
        yield self.commit()

        # Remove calendar and check for no properties
        home = yield self.homeUnderTest()
        yield home.removeCalendarWithName(name, useTrash=False)
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()

        # Recheck it
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()


    @inlineCallbacks
    def test_removeCalendarObjectPropertiesOnDelete(self):
        """
        L{ICalendarHome.removeCalendarWithName} removes a calendar object that already
        exists and makes sure properties are also removed (which is always the case as right
        now calendar objects never have properties).
        """

        # Create calendar object
        calendar1 = yield self.calendarUnderTest()
        name = "test.ics"
        component = Component.fromString(test_event_text)
        metadata = {
            "accessMode": "PUBLIC",
            "isScheduleObject": True,
            "scheduleTag": "abc",
            "scheduleEtags": (),
            "hasPrivateComment": False,
        }
        calobject = yield calendar1.createCalendarObjectWithName(name, component, options=metadata)
        resourceID = calobject._resourceID

        prop = schema.RESOURCE_PROPERTY
        _allWithID = Select(
            [prop.NAME, prop.VIEWER_UID, prop.VALUE],
            From=prop,
            Where=prop.RESOURCE_ID == Parameter("resourceID")
        )

        # No properties on existing calendar object
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)

        yield self.commit()

        # Remove calendar and check for no properties
        calendar1 = yield self.calendarUnderTest()
        obj1 = yield calendar1.calendarObjectWithName(name)
        yield obj1.remove()
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()

        # Recheck it
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()


    @inlineCallbacks
    def test_removeInboxObjectPropertiesOnDelete(self):
        """
        L{ICalendarHome.removeCalendarWithName} removes an inbox calendar object that already
        exists and makes sure properties are also removed. Inbox calendar objects can have properties.
        """

        # Create calendar object and add a property
        home = yield self.homeUnderTest()
        inbox = yield home.createCalendarWithName("inbox")

        name = "test.ics"
        component = Component.fromString(test_event_text)
        metadata = {
            "accessMode": "PUBLIC",
            "isScheduleObject": True,
            "scheduleTag": "abc",
            "scheduleEtags": (),
            "hasPrivateComment": False,
        }
        calobject = yield inbox.createCalendarObjectWithName(name, component, options=metadata)
        resourceID = calobject._resourceID
        calobjectProperties = calobject.properties()

        prop = caldavxml.CalendarDescription.fromString("Calendar object to be removed")
        calobjectProperties[PropertyName.fromElement(prop)] = prop
        yield self.commit()

        prop = schema.RESOURCE_PROPERTY
        _allWithID = Select(
            [prop.NAME, prop.VIEWER_UID, prop.VALUE],
            From=prop,
            Where=prop.RESOURCE_ID == Parameter("resourceID")
        )

        # One property exists calendar object
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 1)

        yield self.commit()

        # Remove calendar object and check for no properties
        home = yield self.homeUnderTest()
        inbox = yield home.calendarWithName("inbox")
        obj1 = yield inbox.calendarObjectWithName(name)
        yield obj1.remove()
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()

        # Recheck it
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()


    @inlineCallbacks
    def test_removeNotifyCategoryInbox(self):
        """
        Inbox object removal should be categorized as ChangeCategory.inbox
        """
        home = yield self.homeUnderTest()
        inbox = yield home.createCalendarWithName("inbox")
        component = Component.fromString(test_event_text)
        inboxItem = yield inbox.createCalendarObjectWithName("inbox.ics", component)
        self.assertEquals(ChangeCategory.inbox, inboxItem.removeNotifyCategory())
        yield self.commit()


    @inlineCallbacks
    def test_removeNotifyCategoryNonInbox(self):
        """
        Non-Inbox object removal should be categorized as ChangeCategory.default
        """
        home = yield self.homeUnderTest()
        nonInbox = yield home.createCalendarWithName("noninbox")
        component = Component.fromString(test_event_text)
        nonInboxItem = yield nonInbox.createCalendarObjectWithName("inbox.ics", component)
        self.assertEquals(ChangeCategory.default, nonInboxItem.removeNotifyCategory())
        yield self.commit()


    @inlineCallbacks
    def test_directShareCreateConcurrency(self):
        """
        Test that two concurrent attempts to create a direct shared calendar
        work concurrently without an exception.
        """

        calendarStore = self._sqlCalendarStore

        # Provision the home and calendar now
        txn = calendarStore.newTransaction()
        sharerHome = yield txn.homeWithUID(ECALENDARTYPE, "uid1", create=True)
        self.assertNotEqual(sharerHome, None)
        cal = yield sharerHome.calendarWithName("calendar")
        self.assertNotEqual(cal, None)
        shareeHome = yield txn.homeWithUID(ECALENDARTYPE, "uid2", create=True)
        self.assertNotEqual(shareeHome, None)
        yield txn.commit()

        txn1 = calendarStore.newTransaction()
        txn2 = calendarStore.newTransaction()

        sharerHome1 = yield txn1.homeWithUID(ECALENDARTYPE, "uid1", create=True)
        self.assertNotEqual(sharerHome1, None)
        cal1 = yield sharerHome1.calendarWithName("calendar")
        self.assertNotEqual(cal1, None)
        shareeHome1 = yield txn1.homeWithUID(ECALENDARTYPE, "uid2", create=True)
        self.assertNotEqual(shareeHome1, None)

        sharerHome2 = yield txn2.homeWithUID(ECALENDARTYPE, "uid1", create=True)
        self.assertNotEqual(sharerHome2, None)
        cal2 = yield sharerHome2.calendarWithName("calendar")
        self.assertNotEqual(cal2, None)
        shareeHome2 = yield txn1.homeWithUID(ECALENDARTYPE, "uid2", create=True)
        self.assertNotEqual(shareeHome2, None)

        @inlineCallbacks
        def _defer1():
            yield cal1.directShareWithUser("uid2")
            yield txn1.commit()
        d1 = _defer1()

        @inlineCallbacks
        def _defer2():
            yield cal2.directShareWithUser("uid2")
            yield txn2.commit()
        d2 = _defer2()

        yield d1
        yield d2


    @inlineCallbacks
    def test_transferSharingDetails(self):
        """
        Test Calendar._transferSharingDetails to make sure sharing details are transferred.
        """

        shareeHome = yield self.transactionUnderTest().calendarHomeWithUID("home_splits_shared")

        calendar = yield (yield self.transactionUnderTest().calendarHomeWithUID(
            "home_splits")).calendarWithName("calendar_1")

        # Fake a shared binding on the original calendar
        bind = calendar._bindSchema
        _bindCreate = Insert({
            bind.HOME_RESOURCE_ID: shareeHome._resourceID,
            bind.RESOURCE_ID: calendar._resourceID,
            bind.RESOURCE_NAME: "shared_1",
            bind.MESSAGE: "Shared to you",
            bind.BIND_MODE: _BIND_MODE_DIRECT,
            bind.BIND_STATUS: _BIND_STATUS_ACCEPTED,
        })
        yield _bindCreate.on(self.transactionUnderTest())
        sharedCalendar = yield shareeHome.childWithName("shared_1")
        self.assertTrue(sharedCalendar is not None)
        sharedCalendar = yield shareeHome.childWithName("shared_1_vtodo")
        self.assertTrue(sharedCalendar is None)

        # Now do the transfer and see if a new binding exists
        newcalendar = yield (yield self.transactionUnderTest().calendarHomeWithUID(
            "home_splits")).createCalendarWithName("calendar_new")
        yield calendar._transferSharingDetails(newcalendar, "VTODO")

        sharedCalendar = yield shareeHome.childWithName("shared_1")
        self.assertTrue(sharedCalendar is not None)
        self.assertEqual(sharedCalendar._resourceID, calendar._resourceID)

        sharedCalendar = yield shareeHome.childWithName("shared_1-vtodo")
        self.assertTrue(sharedCalendar is not None)
        self.assertEqual(sharedCalendar._resourceID, newcalendar._resourceID)


    @inlineCallbacks
    def test_moveCalendarObjectResource(self):
        """
        Test Calendar._transferSharingDetails to make sure sharing details are transferred.
        """

        calendar1 = yield (yield self.transactionUnderTest().calendarHomeWithUID(
            "home_splits")).calendarWithName("calendar_1")
        calendar2 = yield (yield self.transactionUnderTest().calendarHomeWithUID(
            "home_splits")).calendarWithName("calendar_2")

        child = yield calendar2.calendarObjectWithName("5.ics")

        yield child.moveTo(calendar1, child.name())

        child = yield calendar2.calendarObjectWithName("5.ics")
        self.assertTrue(child is None)

        child = yield calendar1.calendarObjectWithName("5.ics")
        self.assertTrue(child is not None)


    @inlineCallbacks
    def test_splitCalendars(self):
        """
        Test Calendar.splitCollectionByComponentTypes to make sure components are split out,
        sync information is updated.
        """

        # calendar_2 add a dead property to make sure it gets copied over
        home = yield self.transactionUnderTest().calendarHomeWithUID("home_splits")
        calendar2 = yield home.calendarWithName("calendar_2")
        pkey = PropertyName.fromElement(CalendarDescription)
        calendar2.properties()[pkey] = CalendarDescription.fromString("A birthday calendar")
        yield self.commit()

        # calendar_1 no change
        home = yield self.transactionUnderTest().calendarHomeWithUID("home_splits")
        calendar1 = yield home.calendarWithName("calendar_1")
        original_sync_token1 = yield calendar1.syncToken()
        yield calendar1.splitCollectionByComponentTypes()
        yield self.commit()

        home = yield self.transactionUnderTest().calendarHomeWithUID("home_splits")

        child = yield home.calendarWithName("calendar_1-vtodo")
        self.assertTrue(child is None)

        calendar1 = yield home.calendarWithName("calendar_1")
        children = yield calendar1.listCalendarObjects()
        self.assertEqual(len(children), 3)
        new_sync_token1 = yield calendar1.syncToken()
        self.assertNotEqual(new_sync_token1, original_sync_token1)
        result = yield calendar1.getSupportedComponents()
        self.assertEquals(result, "VEVENT")

        yield self.commit()

        # calendar_2 does split
        home = yield self.transactionUnderTest().calendarHomeWithUID("home_splits")
        calendar2 = yield home.calendarWithName("calendar_2")
        original_sync_token2 = yield calendar2.syncToken()
        yield calendar2.splitCollectionByComponentTypes()
        yield self.commit()

        home = yield self.transactionUnderTest().calendarHomeWithUID("home_splits")

        calendar2_vtodo = yield home.calendarWithName("calendar_2-vtodo")
        self.assertTrue(calendar2_vtodo is not None)
        children = yield calendar2_vtodo.listCalendarObjects()
        self.assertEqual(len(children), 2)
        changed, deleted, invalid = yield calendar2_vtodo.resourceNamesSinceToken(None)
        self.assertEqual(sorted(changed), ["3.ics", "5.ics"])
        self.assertEqual(len(deleted), 0)
        self.assertEqual(len(invalid), 0)
        result = yield calendar2_vtodo.getSupportedComponents()
        self.assertEquals(result, "VTODO")
        self.assertTrue(pkey in calendar2_vtodo.properties())
        self.assertEqual(str(calendar2_vtodo.properties()[pkey]), "A birthday calendar")

        calendar2 = yield home.calendarWithName("calendar_2")
        children = yield calendar2.listCalendarObjects()
        self.assertEqual(len(children), 3)
        new_sync_token2 = yield calendar2.syncToken()
        self.assertNotEqual(new_sync_token2, original_sync_token2)
        changed, deleted, invalid = yield calendar2.resourceNamesSinceToken(original_sync_token2)
        self.assertEqual(len(changed), 0)
        self.assertEqual(sorted(deleted), ["3.ics", "5.ics"])
        self.assertEqual(len(invalid), 0)
        result = yield calendar2.getSupportedComponents()
        self.assertEquals(result, "VEVENT")
        self.assertTrue(pkey in calendar2.properties())
        self.assertEqual(str(calendar2.properties()[pkey]), "A birthday calendar")


    @inlineCallbacks
    def test_noSplitCalendars(self):
        """
        Test CalendarHome.splitCalendars to make sure we end up with at least two collections
        with different supported components.
        """

        # Do split
        home = yield self.transactionUnderTest().calendarHomeWithUID("home_no_splits")
        calendars = yield home.calendars()
        self.assertEqual(len(calendars), 1)
        yield home.splitCalendars()
        yield self.commit()

        # Make sure we have calendars supporting both VEVENT and VTODO
        home = yield self.transactionUnderTest().calendarHomeWithUID("home_no_splits")
        supported_components = set()
        calendars = yield home.calendars()
        for calendar in calendars:
            if calendar.name() == "inbox":
                continue
            result = yield calendar.getSupportedComponents()
            supported_components.add(result)

        self.assertEqual(supported_components, set(ical.allowedStoreComponents))


    @inlineCallbacks
    def test_defaultCalendar(self):
        """
        Make sure a default_events calendar is assigned.
        """

        home = yield self.transactionUnderTest().calendarHomeWithUID("home_defaults")
        calendar1 = yield home.calendarWithName("calendar_1")
        yield calendar1.splitCollectionByComponentTypes()
        yield self.commit()

        home = yield self.transactionUnderTest().calendarHomeWithUID("home_defaults")
        self.assertEqual(home._default_events, None)
        self.assertEqual(home._default_tasks, None)

        default_events = yield home.defaultCalendar("VEVENT")
        self.assertTrue(default_events is not None)
        self.assertEqual(home._default_events, default_events._resourceID)
        self.assertEqual(home._default_tasks, None)
        yield self.commit()

        home = yield self.transactionUnderTest().calendarHomeWithUID("home_defaults")
        self.assertEqual(home._default_events, default_events._resourceID)
        self.assertEqual(home._default_tasks, None)

        default_tasks = yield home.defaultCalendar("VTODO")
        self.assertTrue(default_tasks is not None)
        self.assertEqual(home._default_events, default_events._resourceID)
        self.assertEqual(home._default_tasks, default_tasks._resourceID)
        yield self.commit()

        home = yield self.transactionUnderTest().calendarHomeWithUID("home_defaults")
        self.assertEqual(home._default_events, default_events._resourceID)
        self.assertEqual(home._default_tasks, default_tasks._resourceID)
        yield home.removeCalendarWithName("calendar_1-vtodo", useTrash=True)
        yield self.commit()

        home = yield self.transactionUnderTest().calendarHomeWithUID("home_defaults")
        default_events = yield home.defaultCalendar("VEVENT", create=False)
        self.assertTrue(default_events is not None)
        default_tasks = yield home.defaultCalendar("VTODO", create=False)
        self.assertTrue(default_tasks is None)

        default_tasks2 = yield home.defaultCalendar("VTODO")
        self.assertTrue(default_tasks2 is not None)
        self.assertEqual(home._default_events, default_events._resourceID)
        self.assertEqual(home._default_tasks, default_tasks2._resourceID)
        yield self.commit()


    @inlineCallbacks
    def test_setDefaultCalendar(self):
        """
        Make sure a default_events calendar is assigned.
        """

        home = yield self.homeUnderTest(name="home_defaults")
        calendar1 = yield home.calendarWithName("calendar_1")
        yield calendar1.splitCollectionByComponentTypes()
        yield self.commit()

        home = yield self.homeUnderTest(name="home_defaults")
        self.assertEqual(home._default_events, None)
        self.assertEqual(home._default_tasks, None)
        calendar1 = yield home.calendarWithName("calendar_1")
        yield home.setDefaultCalendar(calendar1, "VEVENT")
        self.assertEqual(home._default_events, calendar1._resourceID)
        self.assertEqual(home._default_tasks, None)
        yield self.commit()

        home = yield self.homeUnderTest(name="home_defaults")
        calendar1 = yield home.calendarWithName("calendar_1")
        calendar2 = yield home.calendarWithName("calendar_1-vtodo")
        yield self.failUnlessFailure(home.setDefaultCalendar(calendar2, "VEVENT"), InvalidDefaultCalendar)
        self.assertEqual(home._default_events, calendar1._resourceID)
        self.assertEqual(home._default_tasks, None)
        yield self.commit()

        home = yield self.homeUnderTest(name="home_defaults")
        calendar1 = yield home.calendarWithName("calendar_1")
        calendar2 = yield home.calendarWithName("calendar_1-vtodo")
        yield home.setDefaultCalendar(calendar2, "VTODO")
        self.assertEqual(home._default_events, calendar1._resourceID)
        self.assertEqual(home._default_tasks, calendar2._resourceID)
        yield self.commit()

        home = yield self.homeUnderTest(name="home_defaults")
        calendar1 = yield home.calendarWithName("inbox")
        yield self.failUnlessFailure(home.setDefaultCalendar(calendar1, "VEVENT"), InvalidDefaultCalendar)
        yield self.commit()

        home = yield self.homeUnderTest(name="home_defaults")
        home_other = yield self.homeUnderTest(name="home_splits")
        calendar1 = yield home_other.calendarWithName("calendar_1")
        yield self.failUnlessFailure(home.setDefaultCalendar(calendar1, "VEVENT"), InvalidDefaultCalendar)
        yield self.commit()


    @inlineCallbacks
    def test_defaultCalendar_delete(self):
        """
        Make sure a default_events calendar is assigned after existing one is deleted.
        """

        home = yield self.homeUnderTest(name="home_defaults")
        calendar1 = yield home.calendarWithName("calendar_1")
        default_events = yield home.defaultCalendar("VEVENT")
        self.assertTrue(default_events is not None)
        self.assertEqual(default_events._resourceID, calendar1._resourceID)
        yield self.commit()

        home = yield self.homeUnderTest(name="home_defaults")
        calendar1 = yield home.calendarWithName("calendar_1")
        yield calendar1.remove()
        yield self.commit()

        home = yield self.homeUnderTest(name="home_defaults")
        default_events = yield home.defaultCalendar("VEVENT", create=False)
        self.assertEqual(default_events, None)
        default_tasks = yield home.defaultCalendar("VTODO", create=False)
        self.assertEqual(default_tasks, None)
        yield self.commit()

        home = yield self.homeUnderTest(name="home_defaults")
        default_events = yield home.defaultCalendar("VEVENT")
        self.assertTrue(default_events is not None)
        yield self.commit()

        home = yield self.homeUnderTest(name="home_defaults")
        calendar1 = yield home.calendarWithName(default_events.name())
        default_events = yield home.defaultCalendar("VEVENT")
        self.assertTrue(default_events is not None)
        self.assertEqual(default_events._resourceID, calendar1._resourceID)
        yield self.commit()


    @inlineCallbacks
    def test_defaultCalendar_delete_and_recover(self):
        """
        Make sure a default_events calendar is re-assigned after recovering from trash
        """

        if config.EnableTrashCollection:
            home = yield self.homeUnderTest(name="home_defaults")
            calendar1 = yield home.calendarWithName("calendar_1")
            default_events = yield home.defaultCalendar("VEVENT")
            self.assertEqual(default_events._resourceID, calendar1._resourceID)
            yield self.commit()

            home = yield self.homeUnderTest(name="home_defaults")
            calendar1 = yield home.calendarWithName("calendar_1")
            yield calendar1.remove()
            default_events = yield home.defaultCalendar("VEVENT", create=False)
            self.assertTrue(default_events is None)
            yield self.commit()

            home = yield self.homeUnderTest(name="home_defaults")
            trashChildren = yield home.children(onlyInTrash=True)
            calendar1 = trashChildren[0]
            yield calendar1.fromTrash()
            default_events = yield home.defaultCalendar("VEVENT")
            self.assertEqual(default_events._resourceID, calendar1._resourceID)
            yield self.commit()


    @inlineCallbacks
    def test_resourceLock(self):
        """
        Test CommonObjectResource.lock to make sure it locks, raises on missing resource,
        and raises when locked and wait=False used.
        """

        # Valid object
        resource = yield self.calendarObjectUnderTest()

        # Valid lock
        yield resource.lock()
        self.assertTrue(resource._locked)

        # Setup a new transaction to verify the lock and also verify wait behavior
        newTxn = self._sqlCalendarStore.newTransaction()
        newResource = yield self.calendarObjectUnderTest(txn=newTxn)
        try:
            yield newResource.lock(wait=False)
        except:
            pass # OK
        else:
            self.fail("Expected an exception")
        self.assertFalse(newResource._locked)
        yield newTxn.abort()

        # Commit existing transaction and verify we can get the lock using
        yield self.commit()

        resource = yield self.calendarObjectUnderTest()
        yield resource.lock()
        self.assertTrue(resource._locked)

        # Setup a new transaction to verify the lock but pass in an alternative txn directly
        newTxn = self._sqlCalendarStore.newTransaction()

        # FIXME: not sure why, but without this statement here, this portion of the test fails in a funny way.
        # Basically the query in the try block seems to execute twice, failing each time, one of which is caught,
        # and the other not - causing the test to fail. Seems like some state on newTxn is not being initialized?
        yield self.calendarObjectUnderTest(txn=newTxn, name="2.ics")

        try:
            yield resource.lock(wait=False, useTxn=newTxn)
        except:
            pass # OK
        else:
            self.fail("Expected an exception")
        self.assertTrue(resource._locked)

        # Test missing resource
        resource2 = yield self.calendarObjectUnderTest(name="2.ics")
        resource2._resourceID = 123456789
        try:
            yield resource2.lock()
        except NoSuchObjectResourceError:
            pass # OK
        except:
            self.fail("Expected a NoSuchObjectResourceError exception")
        else:
            self.fail("Expected an exception")
        self.assertFalse(resource2._locked)


    @inlineCallbacks
    def test_recurrenceMinMax(self):
        """
        Test CalendarObjectResource.recurrenceMinMax to make sure it handles a None value.
        """

        # Valid object
        resource = yield self.calendarObjectUnderTest()

        # Valid lock
        rMin, rMax = yield resource.recurrenceMinMax()
        self.assertEqual(rMin, None)
        self.assertEqual(rMax, None)


    @inlineCallbacks
    def test_notExpandedWithin(self):
        """
        Test Calendar.notExpandedWithin to make sure it returns the correct
        result based on the ranges passed in.
        """

        self.patch(config, "FreeBusyIndexDelayedExpand", False)

        # Create the index on a new calendar
        home = yield self.homeUnderTest()
        newcalendar = yield home.createCalendarWithName("index_testing")

        # Create the calendar object to use for testing
        nowYear = self.nowYear["now"]
        caldata = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:instance
DTSTART:%04d0102T140000Z
DURATION:PT1H
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
RRULE:FREQ=WEEKLY
SUMMARY:instance
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n") % (nowYear - 3,)
        component = Component.fromString(caldata)
        calendarObject = yield newcalendar.createCalendarObjectWithName("indexing.ics", component)
        rmin, rmax = yield calendarObject.recurrenceMinMax()
        self.assertEqual(rmin.getYear(), nowYear - 1)
        self.assertEqual(rmax.getYear(), nowYear + 1)

        # Fully within range
        testMin = DateTime(nowYear, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
        testMax = DateTime(nowYear + 1, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
        result = yield newcalendar.notExpandedWithin(testMin, testMax)
        self.assertEqual(result, [])

        # Upper bound exceeded
        testMin = DateTime(nowYear, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
        testMax = DateTime(nowYear + 5, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
        result = yield newcalendar.notExpandedWithin(testMin, testMax)
        self.assertEqual(result, ["indexing.ics"])

        # Lower bound exceeded
        testMin = DateTime(nowYear - 5, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
        testMax = DateTime(nowYear + 1, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
        result = yield newcalendar.notExpandedWithin(testMin, testMax)
        self.assertEqual(result, ["indexing.ics"])

        # Lower and upper bounds exceeded
        testMin = DateTime(nowYear - 5, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
        testMax = DateTime(nowYear + 5, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
        result = yield newcalendar.notExpandedWithin(testMin, testMax)
        self.assertEqual(result, ["indexing.ics"])

        # Lower none within range
        testMin = None
        testMax = DateTime(nowYear + 1, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
        result = yield newcalendar.notExpandedWithin(testMin, testMax)
        self.assertEqual(result, [])

        # Lower none and upper bounds exceeded
        testMin = None
        testMax = DateTime(nowYear + 5, 1, 1, 0, 0, 0, tzid=Timezone.UTCTimezone)
        result = yield newcalendar.notExpandedWithin(testMin, testMax)
        self.assertEqual(result, ["indexing.ics"])


    @inlineCallbacks
    def test_setComponent_no_instance_indexing(self):
        """
        L{ICalendarObject.setComponent} raises L{InvalidCalendarComponentError}
        when given a L{VComponent} whose UID does not match its existing UID.
        """

        caldata = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:instance
DTSTART:%(now)s0102T140000Z
DURATION:PT1H
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
RRULE:FREQ=DAILY
SUMMARY:instance
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n") % self.nowYear

        self.patch(config, "FreeBusyIndexDelayedExpand", False)

        # Add event to store
        calendar = yield self.calendarUnderTest()
        component = Component.fromString(caldata)
        calendarObject = yield calendar.createCalendarObjectWithName("indexing.ics", component)
        rmin, rmax = yield calendarObject.recurrenceMinMax()
        self.assertEqual(rmin, None)
        self.assertNotEqual(rmax.getYear(), 1900)
        instances = yield calendarObject.instances()
        self.assertNotEqual(len(instances), 0)
        yield self.commit()

        # Re-add event with re-indexing
        calendar = yield self.calendarUnderTest()
        calendarObject = yield self.calendarObjectUnderTest(name="indexing.ics")
        calendarObject.tr_change = True
        yield calendarObject.setComponent(component)
        instances2 = yield calendarObject.instances()
        self.assertNotEqual(
            sorted(instances, key=lambda x: x[0])[0],
            sorted(instances2, key=lambda x: x[0])[0],
        )
        yield self.commit()

        # Re-add event without re-indexing
        calendar = yield self.calendarUnderTest()
        calendarObject = yield self.calendarObjectUnderTest(name="indexing.ics")
        component.noInstanceIndexing = True
        yield calendarObject.setComponent(component)
        instances3 = yield calendarObject.instances()
        self.assertEqual(
            sorted(instances2, key=lambda x: x[0])[0],
            sorted(instances3, key=lambda x: x[0])[0],
        )

        obj1 = yield calendar.calendarObjectWithName("indexing.ics")
        yield obj1.remove()
        yield self.commit()


    @inlineCallbacks
    def test_loadObjectResourcesWithName(self):
        """
        L{CommonHomeChild.objectResourcesWithNames} returns the correct set of object resources
        properly configured with a loaded property store. make sure batching works.
        """

        @inlineCallbacks
        def _tests(cal):
            resources = yield cal.objectResourcesWithNames(("1.ics",))
            self.assertEqual(set([resource.name() for resource in resources]), set(("1.ics",)))

            resources = yield cal.objectResourcesWithNames(("1.ics", "2.ics",))
            self.assertEqual(set([resource.name() for resource in resources]), set(("1.ics", "2.ics",)))

            resources = yield cal.objectResourcesWithNames(("1.ics", "2.ics", "3.ics",))
            self.assertEqual(set([resource.name() for resource in resources]), set(("1.ics", "2.ics", "3.ics",)))

            resources = yield cal.objectResourcesWithNames(("1.ics", "2.ics", "3.ics", "4.ics",))
            self.assertEqual(set([resource.name() for resource in resources]), set(("1.ics", "2.ics", "3.ics", "4.ics",)))

            resources = yield cal.objectResourcesWithNames(("bogus1.ics",))
            self.assertEqual(set([resource.name() for resource in resources]), set())

            resources = yield cal.objectResourcesWithNames(("bogus1.ics", "2.ics",))
            self.assertEqual(set([resource.name() for resource in resources]), set(("2.ics",)))

        # Basic load tests
        cal = yield self.calendarUnderTest()
        yield _tests(cal)

        # Adjust batch size and try again
        self.patch(CommonObjectResource, "BATCH_LOAD_SIZE", 2)
        yield _tests(cal)

        yield self.commit()

        # Tests on inbox - resources with properties
        txn = self.transactionUnderTest()
        yield txn.homeWithUID(ECALENDARTYPE, "user01", create=True)
        inbox = yield self.calendarUnderTest(txn=txn, name="inbox", home="user01")
        caldata = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
METHOD:REQUEST
BEGIN:VEVENT
UID:instance
DTSTART:%(now)s0102T140000Z
DURATION:PT1H
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
RRULE:FREQ=DAILY
SUMMARY:instance
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n") % self.nowYear
        component = Component.fromString(caldata)

        @inlineCallbacks
        def _createInboxItem(rname, pvalue):
            obj = yield inbox._createCalendarObjectWithNameInternal(rname, component, internal_state=ComponentUpdateState.ATTENDEE_ITIP_UPDATE)
            prop = caldavxml.CalendarDescription.fromString(pvalue)
            obj.properties()[PropertyName.fromElement(prop)] = prop

        yield _createInboxItem("1.ics", "p1")
        yield _createInboxItem("2.ics", "p2")
        yield _createInboxItem("3.ics", "p3")
        yield _createInboxItem("4.ics", "p4")
        yield self.commit()

        inbox = yield self.calendarUnderTest(name="inbox", home="user01")
        yield _tests(inbox)

        resources = yield inbox.objectResourcesWithNames(("1.ics",))
        prop = caldavxml.CalendarDescription.fromString("p1")
        self.assertEqual(resources[0].properties()[PropertyName.fromElement(prop)], prop)

        resources = yield inbox.objectResourcesWithNames(("1.ics", "2.ics",))
        resources.sort(key=lambda x: x._name)
        prop = caldavxml.CalendarDescription.fromString("p1")
        self.assertEqual(resources[0].properties()[PropertyName.fromElement(prop)], prop)
        prop = caldavxml.CalendarDescription.fromString("p2")
        self.assertEqual(resources[1].properties()[PropertyName.fromElement(prop)], prop)

        resources = yield inbox.objectResourcesWithNames(("bogus1.ics", "2.ics",))
        resources.sort(key=lambda x: x._name)
        prop = caldavxml.CalendarDescription.fromString("p2")
        self.assertEqual(resources[0].properties()[PropertyName.fromElement(prop)], prop)


    @inlineCallbacks
    def test_objectResourceWithID(self):
        """
        L{ICalendarHome.objectResourceWithID} will return the calendar object.
        """
        home = yield self.homeUnderTest()
        calendarObject = (yield home.objectResourceWithID(9999))
        self.assertEquals(calendarObject, None)

        obj = (yield self.calendarObjectUnderTest())
        calendarObject = (yield home.objectResourceWithID(obj._resourceID))
        self.assertNotEquals(calendarObject, None)


    @inlineCallbacks
    def test_defaultAlarms(self):
        """
        L{ICalendarHome.objectResourceWithID} will return the calendar object.
        """

        alarmhome1 = """BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT1M
END:VALARM
"""

        alarmhome2 = """BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT2M
END:VALARM
"""

        alarmhome3 = """BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT3M
END:VALARM
"""

        alarmhome4 = """BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT4M
END:VALARM
"""

        alarmcalendar1 = """BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT1M
END:VALARM
"""

        alarmcalendar2 = """BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT2M
END:VALARM
"""

        alarmcalendar3 = """BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT3M
END:VALARM
"""

        alarmcalendar4 = """BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT4M
END:VALARM
"""

        detailshome = (
            (True, True, alarmhome1,),
            (True, False, alarmhome2,),
            (False, True, alarmhome3,),
            (False, False, alarmhome4,),
        )

        home = yield self.homeUnderTest()
        for vevent, timed, _ignore_alarm in detailshome:
            alarm_result = (yield home.getDefaultAlarm(vevent, timed))
            self.assertEquals(alarm_result, None)

        for vevent, timed, alarm in detailshome:
            yield home.setDefaultAlarm(alarm, vevent, timed)

        yield self.commit()

        home = yield self.homeUnderTest()
        for vevent, timed, alarm in detailshome:
            alarm_result = (yield home.getDefaultAlarm(vevent, timed))
            self.assertEquals(alarm_result, alarm)

        for vevent, timed, alarm in detailshome:
            yield home.setDefaultAlarm(None, vevent, timed)

        yield self.commit()

        home = yield self.homeUnderTest()
        for vevent, timed, _ignore_alarm in detailshome:
            alarm_result = (yield home.getDefaultAlarm(vevent, timed))
            self.assertEquals(alarm_result, None)

        yield self.commit()

        detailscalendar = (
            (True, True, alarmcalendar1,),
            (True, False, alarmcalendar2,),
            (False, True, alarmcalendar3,),
            (False, False, alarmcalendar4,),
        )

        calendar = yield self.calendarUnderTest()
        for vevent, timed, _ignore_alarm in detailscalendar:
            alarm_result = (yield calendar.getDefaultAlarm(vevent, timed))
            self.assertEquals(alarm_result, None)

        for vevent, timed, alarm in detailscalendar:
            yield calendar.setDefaultAlarm(alarm, vevent, timed)

        yield self.commit()

        calendar = yield self.calendarUnderTest()
        for vevent, timed, alarm in detailscalendar:
            alarm_result = (yield calendar.getDefaultAlarm(vevent, timed))
            self.assertEquals(alarm_result, alarm)

        yield self.commit()

        calendar = yield self.calendarUnderTest()
        for vevent, timed, alarm in detailscalendar:
            yield calendar.setDefaultAlarm(None, vevent, timed)

        yield self.commit()

        calendar = yield self.calendarUnderTest()
        for vevent, timed, _ignore_alarm in detailscalendar:
            alarm_result = (yield calendar.getDefaultAlarm(vevent, timed))
            self.assertEquals(alarm_result, None)

        yield self.commit()


    @inlineCallbacks
    def test_setAvailability(self):
        """
        Make sure a L{CalendarHome}.setAvailability() works.
        """

        av1 = Component.fromString("""BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//calendarserver.org//Zonal//EN
BEGIN:VAVAILABILITY
ORGANIZER:mailto:user01@example.com
UID:1@example.com
DTSTAMP:20061005T133225Z
DTEND:20140101T000000Z
BEGIN:AVAILABLE
UID:1-1@example.com
DTSTAMP:20061005T133225Z
SUMMARY:Monday to Friday from 9:00 to 17:00
DTSTART:20130101T090000Z
DTEND:20130101T170000Z
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
END:AVAILABLE
END:VAVAILABILITY
END:VCALENDAR
""")

        home = yield self.homeUnderTest(name="home_defaults")
        self.assertEqual(home.getAvailability(), None)
        yield home.setAvailability(av1)
        self.assertEqual(home.getAvailability(), av1)
        yield self.commit()

        home = yield self.homeUnderTest(name="home_defaults")
        self.assertEqual(home.getAvailability(), av1)
        yield home.setAvailability(None)
        yield self.commit()

        home = yield self.homeUnderTest(name="home_defaults")
        self.assertEqual(home.getAvailability(), None)
        yield self.commit()


    @inlineCallbacks
    def test_setTimezone(self):
        """
        Make sure a L{CalendarHomeChild}.setTimezone() works.
        """

        TimezoneCache.create()
        self.addCleanup(TimezoneCache.clear)

        tzid1 = "Etc/GMT+1"
        tz1 = Component(None, pycalendar=readVTZ(tzid1))

        cal = yield self.calendarUnderTest()
        self.assertEqual(cal.getTimezone(), None)
        self.assertEqual(cal.getTimezoneID(), None)
        yield cal.setTimezone(tz1)
        self.assertEqual(cal.getTimezone(), tz1)
        self.assertEqual(cal.getTimezoneID(), tzid1)
        yield self.commit()

        cal = yield self.calendarUnderTest()
        self.assertEqual(cal.getTimezone(), tz1)
        self.assertEqual(cal.getTimezoneID(), tzid1)
        yield cal.setTimezone(None)
        yield self.commit()

        cal = yield self.calendarUnderTest()
        self.assertEqual(cal.getTimezone(), None)
        self.assertEqual(cal.getTimezoneID(), None)
        yield self.commit()


    @inlineCallbacks
    def test_setTimezoneID(self):
        """
        Make sure a L{CalendarHomeChild}.setTimezoneID() works.
        """

        TimezoneCache.create()
        self.addCleanup(TimezoneCache.clear)

        tzid1 = "Etc/GMT+1"
        tz1 = Component(None, pycalendar=readVTZ(tzid1))

        cal = yield self.calendarUnderTest()
        self.assertEqual(cal.getTimezone(), None)
        self.assertEqual(cal.getTimezoneID(), None)
        yield cal.setTimezoneID(tzid1)
        self.assertEqual(cal.getTimezone(), tz1)
        self.assertEqual(cal.getTimezoneID(), tzid1)
        yield self.commit()

        cal = yield self.calendarUnderTest()
        self.assertEqual(cal.getTimezone(), tz1)
        self.assertEqual(cal.getTimezoneID(), tzid1)
        yield cal.setTimezoneID(None)
        yield self.commit()

        cal = yield self.calendarUnderTest()
        self.assertEqual(cal.getTimezone(), None)
        self.assertEqual(cal.getTimezoneID(), None)
        yield self.commit()

        # Invalid TZID
        cal = yield self.calendarUnderTest()
        self.assertEqual(cal.getTimezone(), None)
        self.assertEqual(cal.getTimezoneID(), None)
        yield self.failUnlessFailure(maybeDeferred(cal.setTimezoneID, "bogus"), TimezoneException)
        self.assertEqual(cal.getTimezone(), None)
        self.assertEqual(cal.getTimezoneID(), None)
        yield self.commit()


    @inlineCallbacks
    def test_calendarRevisionChangeConcurrency(self):
        """
        Test that two concurrent attempts to add resources in two separate
        calendar homes does not deadlock on the revision table update.
        """

        calendarStore = self._sqlCalendarStore

        # Make sure homes are provisioned
        txn = self.transactionUnderTest()
        home_uid1 = yield txn.homeWithUID(ECALENDARTYPE, "user01", create=True)
        home_uid2 = yield txn.homeWithUID(ECALENDARTYPE, "user02", create=True)
        self.assertNotEqual(home_uid1, None)
        self.assertNotEqual(home_uid2, None)
        yield self.commit()

        # Create first events in different calendar homes
        txn1 = calendarStore.newTransaction()
        txn2 = calendarStore.newTransaction()

        calendar_uid1_in_txn1 = yield self.calendarUnderTest(txn1, "calendar", "user01")
        calendar_uid2_in_txn2 = yield self.calendarUnderTest(txn2, "calendar", "user02")

        data = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:data%(ctr)s
DTSTART:20130102T140000Z
DURATION:PT1H
CREATED:20060102T190000Z
DTSTAMP:20051222T210507Z
SUMMARY:data%(ctr)s
END:VEVENT
END:VCALENDAR
"""

        component = Component.fromString(data % {"ctr": 1})
        yield calendar_uid1_in_txn1.createCalendarObjectWithName("data1.ics", component)

        component = Component.fromString(data % {"ctr": 2})
        yield calendar_uid2_in_txn2.createCalendarObjectWithName("data2.ics", component)

        # Setup deferreds to run concurrently and create second events in the calendar homes
        # previously used by the other transaction - this could create the deadlock.
        @inlineCallbacks
        def _defer_uid3():
            calendar_uid1_in_txn2 = yield self.calendarUnderTest(txn2, "calendar", "user01")
            component = Component.fromString(data % {"ctr": 3})
            yield calendar_uid1_in_txn2.createCalendarObjectWithName("data3.ics", component)
            yield txn2.commit()
        d1 = _defer_uid3()

        @inlineCallbacks
        def _defer_uid4():
            calendar_uid2_in_txn1 = yield self.calendarUnderTest(txn1, "calendar", "user02")
            component = Component.fromString(data % {"ctr": 4})
            yield calendar_uid2_in_txn1.createCalendarObjectWithName("data4.ics", component)
            yield txn1.commit()
        d2 = _defer_uid4()

        # Now do the concurrent provision attempt
        yield DeferredList([d1, d2])

        # Verify we did not have a deadlock and all resources have been created.
        caldata1 = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        caldata2 = yield self.calendarObjectUnderTest(name="data2.ics", calendar_name="calendar", home="user02")
        caldata3 = yield self.calendarObjectUnderTest(name="data3.ics", calendar_name="calendar", home="user01")
        caldata4 = yield self.calendarObjectUnderTest(name="data4.ics", calendar_name="calendar", home="user02")
        self.assertNotEqual(caldata1, None)
        self.assertNotEqual(caldata2, None)
        self.assertNotEqual(caldata3, None)
        self.assertNotEqual(caldata4, None)


    @inlineCallbacks
    def test_calendarMissingRevision(self):
        """
        Test that two concurrent attempts to add resources in two separate
        calendar homes does not deadlock on the revision table update.
        """

        # Get details
        home = yield self.homeUnderTest(name="user01", create=True)
        self.assertNotEqual(home, None)
        calendar = yield home.childWithName("calendar")
        self.assertNotEqual(calendar, None)

        rev = calendar._revisionsSchema
        yield Delete(
            From=rev,
            Where=(
                rev.HOME_RESOURCE_ID == Parameter("homeID")).And(
                rev.COLLECTION_NAME == Parameter("collectionName")
            )
        ).on(self.transactionUnderTest(), homeID=home.id(), collectionName="calendar")

        yield self.commit()

        home = yield self.homeUnderTest(name="user01")
        children = yield home.loadChildren()
        self.assertEqual(len(children), 3)
        yield self.commit()

        calendar = yield self.calendarUnderTest(home="user01", name="calendar")
        token = yield calendar.syncToken()
        self.assertTrue(token is not None)


    @inlineCallbacks
    def test_inboxTransp(self):
        """
        Make sure inbox is always transparent no matter what is stored in the DB.
        """

        home = yield self.homeUnderTest(name="user01", create=True)
        self.assertNotEqual(home, None)
        inbox = yield self.calendarUnderTest(home="user01", name="inbox")
        self.assertFalse(inbox.isUsedForFreeBusy())
        yield inbox.setUsedForFreeBusy(True)
        self.assertFalse(inbox.isUsedForFreeBusy())
        yield self.commit()

        inbox = yield self.calendarUnderTest(home="user01", name="inbox")
        self.assertFalse(inbox.isUsedForFreeBusy())

        cb = schema.CALENDAR_BIND
        yield Update(
            {cb.TRANSP: _TRANSP_OPAQUE},
            Where=(cb.CALENDAR_RESOURCE_NAME == "inbox").And(
                cb.CALENDAR_RESOURCE_ID == inbox.id()
            )
        ).on(self.transactionUnderTest())
        yield self.commit()

        inbox = yield self.calendarUnderTest(home="user01", name="inbox")
        self.assertFalse(inbox.isUsedForFreeBusy())


    @inlineCallbacks
    def test_tasksTransp(self):
        """
        Make sure tasks is always transparent no matter what is stored in the DB.
        """

        home = yield self.homeUnderTest(name="user01", create=True)
        self.assertNotEqual(home, None)
        tasks = yield self.calendarUnderTest(home="user01", name="tasks")
        self.assertFalse(tasks.isUsedForFreeBusy())
        yield tasks.setUsedForFreeBusy(True)
        self.assertFalse(tasks.isUsedForFreeBusy())
        yield self.commit()

        tasks = yield self.calendarUnderTest(home="user01", name="tasks")
        self.assertFalse(tasks.isUsedForFreeBusy())

        cb = schema.CALENDAR_BIND
        yield Update(
            {cb.TRANSP: _TRANSP_OPAQUE},
            Where=(cb.CALENDAR_RESOURCE_NAME == "tasks").And(
                cb.CALENDAR_RESOURCE_ID == tasks.id()
            )
        ).on(self.transactionUnderTest())
        yield self.commit()

        tasks = yield self.calendarUnderTest(home="user01", name="tasks")
        self.assertFalse(tasks.isUsedForFreeBusy())


    @inlineCallbacks
    def test_missingTimezone(self):
        """
        Make sure missing timezone causes an exception, whether or timezones by reference is on.
        """
        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART;TZID=BOGUS:20130806T000000
DURATION:PT1H
DTSTAMP:20051222T210507Z
SUMMARY:1
END:VEVENT
END:VCALENDAR
"""

        self.patch(config, "EnableTimezonesByReference", False)

        yield self.homeUnderTest(name="user01", create=True)
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        yield self.failUnlessFailure(
            calendar.createCalendarObjectWithName("data1.ics", Component.fromString(data)),
            InvalidComponentForStoreError,
        )
        yield self.abort()

        self.patch(config, "EnableTimezonesByReference", True)

        yield self.homeUnderTest(name="user01", create=True)
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        yield self.failUnlessFailure(
            calendar.createCalendarObjectWithName("data1.ics", Component.fromString(data)),
            UnknownTimezone,
        )
        yield self.abort()


    @inlineCallbacks
    def test_standardTimezone(self):
        """
        Make sure a standard timezone is not stored and not returned in the calendar data when timezones
        by reference is on.
        """
        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VTIMEZONE
TZID:America/New_York
X-LIC-LOCATION:America/New_York
BEGIN:STANDARD
DTSTART:18000101T000000
RDATE:18000101T000000
TZNAME:GMT+1
TZOFFSETFROM:-0100
TZOFFSETTO:-0100
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:12345-67890
DTSTART;TZID=America/New_York:20130806T000000
DURATION:PT1H
DTSTAMP:20051222T210507Z
SUMMARY:1
END:VEVENT
END:VCALENDAR
"""

        self.patch(config, "EnableTimezonesByReference", True)
        TimezoneCache.create()
        self.addCleanup(TimezoneCache.clear)

        yield self.homeUnderTest(name="user01", create=True)
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        yield calendar.createCalendarObjectWithName("data1.ics", Component.fromString(data))
        yield self.commit()

        obj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        txt = yield obj._text()
        self.assertTrue("BEGIN:VTIMEZONE" not in txt)
        cal = yield obj.componentForUser("user01")
        self.assertEqual(len(tuple(cal.subcomponents())), 1)
        txt = cal.getTextWithTimezones(False)
        self.assertTrue("BEGIN:VTIMEZONE" not in txt)


    @inlineCallbacks
    def test_nonStandardTimezone(self):
        """
        Make sure a non-standard timezone is stored and returned in the calendar data when timezones
        by reference is on.
        """
        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VTIMEZONE
TZID:GMTPlusOne
X-LIC-LOCATION:GMTPlusOne
BEGIN:STANDARD
DTSTART:18000101T000000
RDATE:18000101T000000
TZNAME:GMT+1
TZOFFSETFROM:-0100
TZOFFSETTO:-0100
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:12345-67890
DTSTART;TZID=GMTPlusOne:20130806T000000
DURATION:PT1H
DTSTAMP:20051222T210507Z
SUMMARY:1
END:VEVENT
END:VCALENDAR
"""

        self.patch(config, "EnableTimezonesByReference", True)
        TimezoneCache.create()
        self.addCleanup(TimezoneCache.clear)

        yield self.homeUnderTest(name="user01", create=True)
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        yield calendar.createCalendarObjectWithName("data1.ics", Component.fromString(data))
        yield self.commit()

        obj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        txt = yield obj._text()
        self.assertTrue("TZID:GMTPlusOne" in txt)
        cal = yield obj.componentForUser("user01")
        self.assertEqual(len(tuple(cal.subcomponents())), 2)
        txt = cal.getTextWithTimezones(False)
        self.assertTrue("BEGIN:VTIMEZONE" in txt)


    @inlineCallbacks
    def test_dataVersion(self):
        """
        Make sure L{CalendarObject}'s data version is set when object is created.
        """
        olddata = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-dataversion
DTSTART:20130806T000000Z
DURATION:PT1H
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
SUMMARY:1
END:VEVENT
END:VCALENDAR
"""

        yield self.homeUnderTest(name="user01", create=True)
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        yield calendar.createCalendarObjectWithName("data1.ics", Component.fromString(olddata))
        yield self.commit()

        obj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        self.assertEqual(obj._dataversion, obj._currentDataVersion)
        yield self.commit()


    @inlineCallbacks
    def test_dataUpgrade(self):
        """
        Make sure L{CalendarObject.upgradeData} works correctly.
        """
        olddata = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20130806T000000Z
DURATION:PT1H
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
ATTENDEE:mailto:user01@example.com
SUMMARY:1
END:VEVENT
END:VCALENDAR
"""

        self.patch(CalendarObject.CalendarObjectUpgradeWork, "delay", 1)

        yield self.homeUnderTest(name="user01", create=True)
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        yield calendar.createCalendarObjectWithName("data1.ics", Component.fromString(olddata))
        yield self.commit()

        # Make it look old
        obj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        co = schema.CALENDAR_OBJECT
        yield Update(
            {
                co.ICALENDAR_TEXT: olddata,
                co.DATAVERSION: 0,
            },
            Where=co.RESOURCE_ID == obj._resourceID,
        ).on(self.transactionUnderTest())
        yield self.commit()

        # Still looks old
        obj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        txt = yield obj._text()
        self.assertTrue("mailto:user01@example.com" in txt)
        self.assertEqual(obj._dataversion, 0)
        yield self.commit()

        # Now it is new
        obj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        cal = yield obj.componentForUser("user01")
        txt = cal.getTextWithTimezones(False)
        self.assertTrue("mailto:user01@example.com" not in txt)
        self.assertTrue("urn:x-uid:user01" in txt)
        self.assertEqual(obj._dataversion, obj._currentDataVersion)
        yield self.commit()

        jobs = yield JobItem.all(self.transactionUnderTest())
        yield self.commit()
        self.assertEqual(len(jobs), 1)

        # Wait for it to complete
        yield JobItem.waitEmpty(self._sqlCalendarStore.newTransaction, reactor, 60)

        # Still new
        obj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        txt = yield obj._text()
        self.assertTrue("mailto:user01@example.com" not in txt)
        self.assertTrue("urn:x-uid:user01" in txt)
        self.assertEqual(obj._dataversion, obj._currentDataVersion)
        yield self.commit()


    @inlineCallbacks
    def test_removeAfterRevisionCleanup(self):
        """
        Make sure L{Calendar}'s can be renamed after revision cleanup
        removes their revision table entry..
        """
        yield self.homeUnderTest(name="user01", create=True)
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        self.assertTrue(cal is not None)
        yield self.commit()

        # Remove the revision
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.syncToken()
        yield self.transactionUnderTest().deleteRevisionsBefore(cal._syncTokenRevision + 1)
        yield self.commit()

        # Rename the calendar
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        self.assertTrue(cal is not None)
        yield cal.rename("calendar_renamed")
        yield self.commit()

        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        self.assertTrue(cal is None)
        cal = yield self.calendarUnderTest(home="user01", name="calendar_renamed")
        self.assertTrue(cal is not None)
        yield self.commit()


    @inlineCallbacks
    def test_homeSyncTokenWithTrash_Visible(self):
        """
        L{ICalendarHome.resourceNamesSinceToken} will return the names of
        calendar objects created since L{ICalendarHome.syncToken} last returned
        a particular value.
        """

        self.patch(config, "EnableTrashCollection", True)
        self.patch(config, "ExposeTrashCollection", True)

        home = yield self.homeUnderTest()
        cal = yield self.calendarUnderTest()
        st = yield home.syncToken()
        yield cal.createCalendarObjectWithName("new.ics", Component.fromString(
            test_event_text
        ))

        obj1 = yield cal.calendarObjectWithName("2.ics")
        yield obj1.remove()
        yield home.createCalendarWithName("other-calendar")
        st2 = yield home.syncToken()
        self.failIfEquals(st, st2)

        home = yield self.homeUnderTest()

        expected = [
            "calendar_1/",
            "calendar_1/new.ics",
            "calendar_1/2.ics",
            "other-calendar/"
        ]

        trash = yield home.getTrash()
        if trash is not None:
            trashed = yield trash.calendarObjects()
            expected.extend([
                "{}/".format(trash.name()),
                "{}/{}".format(trash.name(), trashed[0].name()),
            ])

        changed, deleted, invalid = yield home.resourceNamesSinceToken(
            self.token2revision(st), "infinity")

        self.assertEquals(set(changed), set(expected))
        self.assertEquals(set(deleted), set(["calendar_1/2.ics"]))
        self.assertEquals(invalid, [])

        changed, deleted, invalid = yield home.resourceNamesSinceToken(
            self.token2revision(st2), "infinity")
        self.assertEquals(changed, [])
        self.assertEquals(deleted, [])
        self.assertEquals(invalid, [])


    @inlineCallbacks
    def test_homeSyncTokenWithTrash_Invisible(self):
        """
        L{ICalendarHome.resourceNamesSinceToken} will return the names of
        calendar objects created since L{ICalendarHome.syncToken} last returned
        a particular value.
        """

        self.patch(config, "EnableTrashCollection", True)

        home = yield self.homeUnderTest()
        cal = yield self.calendarUnderTest()
        st = yield home.syncToken()
        yield cal.createCalendarObjectWithName("new.ics", Component.fromString(
            test_event_text
        ))

        obj1 = yield cal.calendarObjectWithName("2.ics")
        yield obj1.remove()
        yield home.createCalendarWithName("other-calendar")
        st2 = yield home.syncToken()
        self.failIfEquals(st, st2)

        home = yield self.homeUnderTest()

        expected = [
            "calendar_1/",
            "calendar_1/new.ics",
            "calendar_1/2.ics",
            "other-calendar/"
        ]

        changed, deleted, invalid = yield home.resourceNamesSinceToken(
            self.token2revision(st), "infinity")

        self.assertEquals(set(changed), set(expected))
        self.assertEquals(set(deleted), set(["calendar_1/2.ics"]))
        self.assertEquals(invalid, [])

        changed, deleted, invalid = yield home.resourceNamesSinceToken(
            self.token2revision(st2), "infinity")
        self.assertEquals(changed, [])
        self.assertEquals(deleted, [])
        self.assertEquals(invalid, [])



class SchedulingTests(CommonCommonTests, unittest.TestCase):
    """
    CalendarObject splitting tests
    """

    @inlineCallbacks
    def setUp(self):
        yield super(SchedulingTests, self).setUp()
        yield self.buildStoreAndDirectory()

        # Make sure homes are provisioned
        txn = self.transactionUnderTest()
        for ctr in range(1, 5):
            home_uid = yield txn.homeWithUID(ECALENDARTYPE, "user%02d" % (ctr,), create=True)
            self.assertNotEqual(home_uid, None)
        yield self.commit()


    @inlineCallbacks
    def populate(self):
        yield populateCalendarsFrom(self.requirements, self.storeUnderTest())
        self.notifierFactory.reset()


    @inlineCallbacks
    def test_doImplicitAttendeeEventFix(self):
        """
        Test that processing.doImplicitAttendeeEventFix.
        """

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20130806T000000Z
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
RRULE:FREQ=DAILY
SUMMARY:1
END:VEVENT
END:VCALENDAR
"""

        data_broken = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20130806T000000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SUMMARY:1
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20130807T120000Z
DTSTART:20130807T000000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SUMMARY:1
END:VEVENT
END:VCALENDAR
"""

        data_update1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20130806T000000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1-2
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20130807T000000Z
DTSTART:20130807T000000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:1
SUMMARY:1-3
END:VEVENT
END:VCALENDAR
"""

        data_fixed2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:20130806T000000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1-2
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:20130807T000000Z
DTSTART:20130807T000000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:1
SUMMARY:1-3
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        # Create one event
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        yield calendar.createCalendarObjectWithName("data1.ics", Component.fromString(data))
        yield self.commit()

        # Write corrupt user02 data directly to trigger fix later
        cal = yield self.calendarUnderTest(name="calendar", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        cobj = cobjs[0]
        name02 = cobj.name()
        co = schema.CALENDAR_OBJECT
        yield Update(
            {co.ICALENDAR_TEXT: str(Component.fromString(data_broken))},
            Where=co.RESOURCE_NAME == name02,
        ).on(self.transactionUnderTest())
        yield self.commit()

        # Write user01 data - will trigger fix
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        yield cobj.setComponent(Component.fromString(data_update1))
        yield self.commit()

        # Verify user02 data is now fixed
        cobj = yield self.calendarObjectUnderTest(name=name02, calendar_name="calendar", home="user02")
        ical = yield cobj.component()

        self.assertEqual(normalize_iCalStr(ical), normalize_iCalStr(data_fixed2), "Failed attendee fix:\n%s" % (diff_iCalStrs(ical, data_fixed2),))
        yield self.commit()

        self.assertEqual(len(self.flushLoggedErrors(InvalidOverriddenInstanceError)), 1)


    @inlineCallbacks
    def test_setComponent_structuredLocation(self):
        """
        Verify ROOM attendees who have street address and geo information
        within the directory will get X-APPLE-STRUCTURED-LOCATION properties
        added, as well as updated LOCATION properties.
        """

        data = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//Apple Inc.//Mac OS X 10.9.1//EN
BEGIN:VEVENT
UID:561F5DBB-3F38-4B3A-986F-DD05CBAF554F
DTSTART:20131211T164500Z
DURATION:PT1H
ATTENDEE;CN=Conference Room One;CUTYPE=ROOM;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPAN
 T;SCHEDULE-STATUS=2.0:urn:x-uid:room-addr-1
ATTENDEE;CN=User 01;CUTYPE=INDIVIDUAL;EMAIL=user01@example.com;PARTSTAT=AC
 CEPTED:urn:x-uid:user01
CREATED:20131211T221854Z
DTSTAMP:20131211T230632Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY;COUNT=5
SEQUENCE:8
SUMMARY:locations
TRANSP:OPAQUE
END:VEVENT
BEGIN:VEVENT
UID:561F5DBB-3F38-4B3A-986F-DD05CBAF554F
RECURRENCE-ID:20131214T164500Z
DTSTART:20131214T160000Z
DURATION:PT1H
ATTENDEE;CN=Conference Room Two;CUTYPE=ROOM;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPAN
 T;SCHEDULE-STATUS=2.0:urn:x-uid:room-addr-2
ATTENDEE;CN=User 01;CUTYPE=INDIVIDUAL;EMAIL=user01@example.com;PARTSTAT=AC
 CEPTED:urn:x-uid:user01
CREATED:20131211T221854Z
DTSTAMP:20131211T230632Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:8
SUMMARY:locations
TRANSP:OPAQUE
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n")

        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        yield calendar.createCalendarObjectWithName(
            "structured.ics",
            Component.fromString(data)
        )
        cobj = yield self.calendarObjectUnderTest(
            name="structured.ics",
            calendar_name="calendar",
            home="user01"
        )
        comp = yield cobj.component()
        components = list(comp.subcomponents())

        # Check first component
        locProp = components[0].getProperty("LOCATION")
        self.assertEquals(
            locProp.value(),
            "Room with Address 1\n1 Infinite Loop, Cupertino, CA 95014"
        )
        structProp = components[0].getProperty("X-APPLE-STRUCTURED-LOCATION")
        self.assertEquals(
            structProp.value(),
            "geo:37.331741,-122.030333"
        )

        # Check second component
        locProp = components[1].getProperty("LOCATION")
        self.assertEquals(
            locProp.value(),
            "Room with Address 2\n2 Infinite Loop, Cupertino, CA 95014"
        )
        structProp = components[1].getProperty("X-APPLE-STRUCTURED-LOCATION")
        self.assertEquals(
            structProp.value(),
            "geo:37.332633,-122.030502"
        )

        yield self.commit()


    @inlineCallbacks
    def test_setComponent_structuredLocation_Multiple(self):
        """
        Verify multiple ROOM attendees result in multiple X-APPLE-STRUCTURED-LOCATION properties
        added, as well as updated multi-valued LOCATION properties.
        """

        data = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//Apple Inc.//Mac OS X 10.9.1//EN
BEGIN:VEVENT
UID:561F5DBB-3F38-4B3A-986F-DD05CBAF554F
DTSTART:20131211T164500Z
DURATION:PT1H
ATTENDEE;CN=Conference Room One;CUTYPE=ROOM;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPAN
 T;SCHEDULE-STATUS=2.0:urn:x-uid:room-addr-1
ATTENDEE;CN=Conference Room Two;CUTYPE=ROOM;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPAN
 T;SCHEDULE-STATUS=2.0:urn:x-uid:room-addr-2
ATTENDEE;CN=User 01;CUTYPE=INDIVIDUAL;EMAIL=user01@example.com;PARTSTAT=AC
 CEPTED:urn:x-uid:user01
CREATED:20131211T221854Z
DTSTAMP:20131211T230632Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY;COUNT=5
SEQUENCE:8
SUMMARY:locations
TRANSP:OPAQUE
END:VEVENT
BEGIN:VEVENT
UID:561F5DBB-3F38-4B3A-986F-DD05CBAF554F
RECURRENCE-ID:20131214T164500Z
DTSTART:20131214T160000Z
DURATION:PT1H
ATTENDEE;CN=Conference Room Two;CUTYPE=ROOM;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPAN
 T;SCHEDULE-STATUS=2.0:urn:x-uid:room-addr-2
ATTENDEE;CN=User 01;CUTYPE=INDIVIDUAL;EMAIL=user01@example.com;PARTSTAT=AC
 CEPTED:urn:x-uid:user01
CREATED:20131211T221854Z
DTSTAMP:20131211T230632Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:8
SUMMARY:locations
TRANSP:OPAQUE
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n")

        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        yield calendar.createCalendarObjectWithName(
            "structured.ics",
            Component.fromString(data)
        )

        yield self.commit()

        cobj = yield self.calendarObjectUnderTest(
            name="structured.ics",
            calendar_name="calendar",
            home="user01"
        )
        comp = yield cobj.component()
        components = list(comp.subcomponents())

        # Check first component
        locProp = components[0].getProperty("LOCATION")
        self.assertEquals(
            locProp.value(),
            "Room with Address 1\n1 Infinite Loop, Cupertino, CA 95014; Room with Address 2\n2 Infinite Loop, Cupertino, CA 95014"
        )
        structProps = tuple(components[0].properties("X-APPLE-STRUCTURED-LOCATION"))
        self.assertEqual(len(structProps), 2)
        self.assertEquals(
            set([structProp.value() for structProp in structProps]),
            set(("geo:37.331741,-122.030333", "geo:37.332633,-122.030502",))
        )

        # Check second component
        locProp = components[1].getProperty("LOCATION")
        self.assertEquals(
            locProp.value(),
            "Room with Address 2\n2 Infinite Loop, Cupertino, CA 95014"
        )
        structProps = tuple(components[1].properties("X-APPLE-STRUCTURED-LOCATION"))
        self.assertEqual(len(structProps), 1)
        self.assertEquals(
            structProps[0].value(),
            "geo:37.332633,-122.030502"
        )

        yield self.commit()

        cal = yield self.calendarUnderTest(
            name="calendar",
            home="room-addr-1"
        )
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        comp = yield cobjs[0].component()
        components = list(comp.subcomponents())

        # Check first component
        locProp = components[0].getProperty("LOCATION")
        self.assertEquals(
            locProp.value(),
            "Room with Address 1\n1 Infinite Loop, Cupertino, CA 95014; Room with Address 2\n2 Infinite Loop, Cupertino, CA 95014"
        )
        structProps = tuple(components[0].properties("X-APPLE-STRUCTURED-LOCATION"))
        self.assertEqual(len(structProps), 2)
        self.assertEquals(
            set([structProp.value() for structProp in structProps]),
            set(("geo:37.331741,-122.030333", "geo:37.332633,-122.030502",))
        )

        yield self.commit()


    @inlineCallbacks
    def test_setComponent_externalPrincipal(self):
        """
        Verify attendees who are not locally hosted have X-APPLE-HOSTED-STATUS=EXTERNAL
        attribute added.
        """

        data = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//Apple Inc.//Mac OS X 10.9.1//EN
BEGIN:VEVENT
UID:561F5DBB-3F38-4B3A-986F-DD05CBAF554F
DTSTART:20131211T164500Z
DURATION:PT1H
ATTENDEE;X-APPLE-HOSTED-STATUS=EXTERNAL:urn:x-uid:user01
ATTENDEE:mailto:someone_external@example.com
CREATED:20131211T221854Z
DTSTAMP:20131211T230632Z
ORGANIZER:urn:x-uid:user01
SUMMARY:external
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n")

        self.patch(config.HostedStatus, "Enabled", True)

        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        yield calendar.createCalendarObjectWithName(
            "external.ics",
            Component.fromString(data)
        )
        cobj = yield self.calendarObjectUnderTest(
            name="external.ics",
            calendar_name="calendar",
            home="user01"
        )
        comp = yield cobj.component()
        components = list(comp.subcomponents())

        # Check attendees...

        # The local user will have the X-APPLE-HOSTED-STATUS param removed...
        local = components[0].getAttendeeProperty(["urn:x-uid:user01"])
        self.assertFalse(local.hasParameter(config.HostedStatus.Parameter))

        # The external one will have it added...
        external = components[0].getAttendeeProperty(["mailto:someone_external@example.com"])
        self.assertEquals(
            external.parameterValue(config.HostedStatus.Parameter),
            config.HostedStatus.Values["external"]
        )

        yield self.commit()


    @inlineCallbacks
    def test_setComponent_noInstances(self):
        """
        Verify that an event with no instances (entirely EXDATEd) can be stored when group expansion
        is on.
        """

        self.patch(config.GroupAttendees, "Enabled", True)

        data = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//Apple Inc.//Mac OS X 10.9.1//EN
BEGIN:VEVENT
UID:561F5DBB-3F38-4B3A-986F-DD05CBAF554F
DTSTART:20131211T164500Z
DURATION:PT1H
ATTENDEE:urn:x-uid:user01
CREATED:20131211T221854Z
DTSTAMP:20131211T230632Z
ORGANIZER:urn:x-uid:user01
RRULE:FREQ=DAILY;COUNT=1
EXDATE:20131211T164500Z
SUMMARY:external
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n")

        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        yield calendar.createCalendarObjectWithName("exdated.ics", Component.fromString(data))
        yield self.commit()

        cobj = yield self.calendarObjectUnderTest(name="exdated.ics", calendar_name="calendar", home="user01")
        self.assertTrue(cobj is not None)



class CalendarObjectSplitting(CommonCommonTests, unittest.TestCase):
    """
    CalendarObject splitting tests
    """

    @inlineCallbacks
    def setUp(self):
        yield super(CalendarObjectSplitting, self).setUp()
        yield self.buildStoreAndDirectory()

        # Make sure homes are provisioned
        txn = self.transactionUnderTest()
        for ctr in range(1, 5):
            home_uid = yield txn.homeWithUID(ECALENDARTYPE, "user%02d" % (ctr,), create=True)
            self.assertNotEqual(home_uid, None)
        yield self.commit()

        self.subs = {}

        self.now = DateTime.getNowUTC()
        self.now.setHHMMSS(0, 0, 0)

        self.subs["now"] = self.now

        for i in range(30):
            attrname = "now_back%s" % (i + 1,)
            setattr(self, attrname, self.now.duplicate())
            getattr(self, attrname).offsetDay(-(i + 1))
            self.subs[attrname] = getattr(self, attrname)

            attrname_12h = "now_back%s_12h" % (i + 1,)
            setattr(self, attrname_12h, getattr(self, attrname).duplicate())
            getattr(self, attrname_12h).offsetHours(12)
            self.subs[attrname_12h] = getattr(self, attrname_12h)

            attrname_1 = "now_back%s_1" % (i + 1,)
            setattr(self, attrname_1, getattr(self, attrname).duplicate())
            getattr(self, attrname_1).offsetSeconds(-1)
            self.subs[attrname_1] = getattr(self, attrname_1)

        for i in range(30):
            attrname = "now_fwd%s" % (i + 1,)
            setattr(self, attrname, self.now.duplicate())
            getattr(self, attrname).offsetDay(i + 1)
            self.subs[attrname] = getattr(self, attrname)

            attrname_12h = "now_fwd%s_12h" % (i + 1,)
            setattr(self, attrname_12h, getattr(self, attrname).duplicate())
            getattr(self, attrname_12h).offsetHours(12)
            self.subs[attrname_12h] = getattr(self, attrname_12h)

        self.patch(config, "MaxAllowedInstances", 500)


    @inlineCallbacks
    def populate(self):
        yield populateCalendarsFrom(self.requirements, self.storeUnderTest())
        self.notifierFactory.reset()


    @inlineCallbacks
    def _splitDetails(self, home):
        # Get home data
        cal = yield self.calendarUnderTest(name="calendar", home=home)
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 2)
        for cobj in cobjs:
            self.assertTrue(cobj.isScheduleObject)
            ical = yield cobj.component()
            if ical.resourceUID() == "12345-67890":
                ical_future = ical
            else:
                ical_past = ical
                new_name = cobj.name()

        relID = ical_future.mainComponent().propertyValue("RELATED-TO")
        uid = ical_past.resourceUID()

        returnValue((ical_future, ical_past, uid, relID, new_name,))


    @inlineCallbacks
    def test_calendarObjectSplit(self):
        """
        Test that (manual) splitting of calendar objects works.
        """

        self.patch(config.Scheduling.Options.Splitting, "Enabled", False)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)

        # Create one event that will split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE:mailto:user1@example.org
ATTENDEE:mailto:user2@example.org
ATTENDEE:mailto:user3@example.org
ATTENDEE:mailto:user4@example.org
ATTENDEE:mailto:user5@example.org
ATTENDEE:mailto:user6@example.org
ATTENDEE:mailto:user7@example.org
ATTENDEE:mailto:user8@example.org
ATTENDEE:mailto:user9@example.org
ATTENDEE:mailto:user10@example.org
ATTENDEE:mailto:user11@example.org
ATTENDEE:mailto:user12@example.org
ATTENDEE:mailto:user13@example.org
ATTENDEE:mailto:user14@example.org
ATTENDEE:mailto:user15@example.org
ATTENDEE:mailto:user16@example.org
ATTENDEE:mailto:user17@example.org
ATTENDEE:mailto:user18@example.org
ATTENDEE:mailto:user19@example.org
ATTENDEE:mailto:user20@example.org
ATTENDEE:mailto:user21@example.org
ATTENDEE:mailto:user22@example.org
ATTENDEE:mailto:user23@example.org
ATTENDEE:mailto:user24@example.org
ATTENDEE:mailto:user25@example.org
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user1@example.org
RRULE:FREQ=DAILY
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE:mailto:user1@example.org
ATTENDEE:mailto:user2@example.org
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user1@example.org
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE:mailto:user1@example.org
ATTENDEE:mailto:user2@example.org
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user1@example.org
END:VEVENT
END:VCALENDAR
"""

        data_future = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE:mailto:user1@example.org
ATTENDEE:mailto:user2@example.org
ATTENDEE:mailto:user3@example.org
ATTENDEE:mailto:user4@example.org
ATTENDEE:mailto:user5@example.org
ATTENDEE:mailto:user6@example.org
ATTENDEE:mailto:user7@example.org
ATTENDEE:mailto:user8@example.org
ATTENDEE:mailto:user9@example.org
ATTENDEE:mailto:user10@example.org
ATTENDEE:mailto:user11@example.org
ATTENDEE:mailto:user12@example.org
ATTENDEE:mailto:user13@example.org
ATTENDEE:mailto:user14@example.org
ATTENDEE:mailto:user15@example.org
ATTENDEE:mailto:user16@example.org
ATTENDEE:mailto:user17@example.org
ATTENDEE:mailto:user18@example.org
ATTENDEE:mailto:user19@example.org
ATTENDEE:mailto:user20@example.org
ATTENDEE:mailto:user21@example.org
ATTENDEE:mailto:user22@example.org
ATTENDEE:mailto:user23@example.org
ATTENDEE:mailto:user24@example.org
ATTENDEE:mailto:user25@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE;SCHEDULE-STATUS=5.3:mailto:user1@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_past = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE:mailto:user1@example.org
ATTENDEE:mailto:user2@example.org
ATTENDEE:mailto:user3@example.org
ATTENDEE:mailto:user4@example.org
ATTENDEE:mailto:user5@example.org
ATTENDEE:mailto:user6@example.org
ATTENDEE:mailto:user7@example.org
ATTENDEE:mailto:user8@example.org
ATTENDEE:mailto:user9@example.org
ATTENDEE:mailto:user10@example.org
ATTENDEE:mailto:user11@example.org
ATTENDEE:mailto:user12@example.org
ATTENDEE:mailto:user13@example.org
ATTENDEE:mailto:user14@example.org
ATTENDEE:mailto:user15@example.org
ATTENDEE:mailto:user16@example.org
ATTENDEE:mailto:user17@example.org
ATTENDEE:mailto:user18@example.org
ATTENDEE:mailto:user19@example.org
ATTENDEE:mailto:user20@example.org
ATTENDEE:mailto:user21@example.org
ATTENDEE:mailto:user22@example.org
ATTENDEE:mailto:user23@example.org
ATTENDEE:mailto:user24@example.org
ATTENDEE:mailto:user25@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE;SCHEDULE-STATUS=5.3:mailto:user1@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE:mailto:user1@example.org
ATTENDEE:mailto:user2@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE;SCHEDULE-STATUS=5.3:mailto:user1@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE:mailto:user1@example.org
ATTENDEE:mailto:user2@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE;SCHEDULE-STATUS=5.3:mailto:user1@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data1.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        # Do manual split
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        will = yield cobj.willSplit()
        self.assertTrue(will)

        yield cobj.split()
        yield self.commit()

        ical_future, ical_past, pastUID, relID, _ignore_new_name = yield self._splitDetails("user01")

        title = "temp"
        relsubs = dict(self.subs)
        relsubs["uid"] = pastUID
        relsubs["relID"] = relID
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s" % (title,))


    @inlineCallbacks
    def test_calendarObjectSplit_work(self):
        """
        Test that splitting of calendar objects works.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", True)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event that will split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user04@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user05@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
END:VCALENDAR
"""

        data_future = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ATTENDEE;CN=User 03;EMAIL=user03@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user03
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 05;EMAIL=user05@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user05
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_past = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ATTENDEE;CN=User 03;EMAIL=user03@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user03
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ATTENDEE;CN=User 04;EMAIL=user04@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user04
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_future2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;CN=User 03;EMAIL=user03@example.com;RSVP=TRUE:urn:x-uid:user03
DTSTAMP:20051222T210507Z
EXDATE:%(now_fwd10)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_past2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;CN=User 03;EMAIL=user03@example.com;RSVP=TRUE:urn:x-uid:user03
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;CN=User 04;EMAIL=user04@example.com;RSVP=TRUE:urn:x-uid:user04
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:%(uid)s
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_inbox2 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;CN=User 03;EMAIL=user03@example.com;RSVP=TRUE:urn:x-uid:user03
DTSTAMP:20051222T210507Z
EXDATE:%(now_fwd10)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
END:VCALENDAR
"""

        data_future3 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;CN=User 03;EMAIL=user03@example.com;RSVP=TRUE:urn:x-uid:user03
DTSTAMP:20051222T210507Z
EXDATE:%(now_fwd10)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user03
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_past3 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;CN=User 03;EMAIL=user03@example.com;RSVP=TRUE:urn:x-uid:user03
DTSTAMP:20051222T210507Z
EXDATE:%(now_back25)s
EXDATE:%(now_back24)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:%(uid)s
X-CALENDARSERVER-PERUSER-UID:user03
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_inbox3 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;CN=User 03;EMAIL=user03@example.com;RSVP=TRUE:urn:x-uid:user03
DTSTAMP:20051222T210507Z
EXDATE:%(now_fwd10)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
END:VCALENDAR
"""

        data_past4 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;CN=User 04;EMAIL=user04@example.com;RSVP=TRUE:urn:x-uid:user04
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:%(uid)s
X-CALENDARSERVER-PERUSER-UID:user04
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:%(now_back25)s
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_future5 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 05;EMAIL=user05@example.com;RSVP=TRUE:urn:x-uid:user05
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user05
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:%(now_fwd10)s
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_inbox5 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 05;EMAIL=user05@example.com;RSVP=TRUE:urn:x-uid:user05
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data1.ics", component)
        self.assertTrue(hasattr(cobj, "_workItems"))
        yield self.commit()

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], cobj._resourceID)
        yield self.abort()

        # Wait for it to complete
        yield JobItem.waitEmpty(self._sqlCalendarStore.newTransaction, reactor, 60)

        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        # Get the existing and new object data
        ical_future, ical_past, pastUID, relID, _ignore_new_name = yield self._splitDetails("user01")

        # Verify user01 data
        title = "user01"
        relsubs = dict(self.subs)
        relsubs["uid"] = pastUID
        relsubs["relID"] = relID
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s" % (title,))

        # Get user02 data
        ical_future, ical_past, pastUID, relID, _ignore_new_name = yield self._splitDetails("user02")

        cal = yield self.calendarUnderTest(name="inbox", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical_inbox = yield cobjs[0].component()

        # Verify user02 data
        title = "user02"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future2) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past2) % relsubs, "Failed past: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_inbox), normalize_iCalStr(data_inbox2) % relsubs, "Failed inbox: %s" % (title,))

        # Get user03 data
        ical_future, ical_past, pastUID, relID, _ignore_new_name = yield self._splitDetails("user03")

        cal = yield self.calendarUnderTest(name="inbox", home="user03")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical_inbox = yield cobjs[0].component()

        # Verify user03 data
        title = "user03"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future3) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past3) % relsubs, "Failed past: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_inbox), normalize_iCalStr(data_inbox3) % relsubs, "Failed inbox: %s" % (title,))

        # Get user04 data
        cal = yield self.calendarUnderTest(name="calendar", home="user04")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical_past = yield cobjs[0].component()
        self.assertTrue(cobjs[0].isScheduleObject)

        cal = yield self.calendarUnderTest(name="inbox", home="user04")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 0)

        # Verify user04 data
        title = "user04"
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past4) % relsubs, "Failed past: %s" % (title,))

        # Get user05 data
        cal = yield self.calendarUnderTest(name="calendar", home="user05")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical_future = yield cobjs[0].component()
        self.assertTrue(cobjs[0].isScheduleObject)

        cal = yield self.calendarUnderTest(name="inbox", home="user05")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical_inbox = yield cobjs[0].component()

        # Verify user05 data
        title = "user05"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future5) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_inbox), normalize_iCalStr(data_inbox5) % relsubs, "Failed inbox: %s" % (title,))


    @inlineCallbacks
    def test_calendarObjectSplit_removed(self):
        """
        Test that splitting of calendar objects does not occur when the object is
        removed before the work can be done.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", True)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 10)

        # Create one event that will split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user03@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:user04@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user05@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
END:VCALENDAR
"""

        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data1.ics", component)
        self.assertTrue(hasattr(cobj, "_workItems"))
        yield self.commit()

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], cobj._resourceID)
        yield self.abort()

        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        yield cobj.purge(implicitly=True)
        yield self.commit()

        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        # Wait for it to complete
        yield JobItem.waitEmpty(self._sqlCalendarStore.newTransaction, reactor, 60)

        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        cal = yield self.calendarUnderTest(name="calendar", home="user01")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 0)


    @inlineCallbacks
    def test_calendarObjectSplit_no_attendee_split(self):
        """
        Test that calendar objects do not split on attendee change.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", True)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event that will not split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
END:VCALENDAR
"""

        data_1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
END:VCALENDAR
"""

        data_2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_2_update = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
TRANSP:TRANSPARENT
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT5M
END:VALARM
END:VEVENT
END:VCALENDAR
"""

        data_2_changed = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:%(now_back25)s
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:%(now_fwd10)s
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=START:-PT5M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data1.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        # Get user02 data
        cal = yield self.calendarUnderTest(name="calendar", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        cobj = cobjs[0]
        cname2 = cobj.name()
        ical = yield cobj.component()
        self.assertEqual(normalize_iCalStr(ical), normalize_iCalStr(data_2) % self.subs, "Failed 2")
        yield cobj.setComponent(Component.fromString(data_2_update % self.subs))
        yield self.commit()

        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        ical = yield cobj.component()
        self.assertEqual(normalize_iCalStr(ical), normalize_iCalStr(data_1) % self.subs, "Failed 2")
        cobj = yield self.calendarObjectUnderTest(name=cname2, calendar_name="calendar", home="user02")
        ical = yield cobj.component()
        self.assertEqual(normalize_iCalStr(ical), normalize_iCalStr(data_2_changed) % self.subs, "Failed 2")
        yield self.commit()


    @inlineCallbacks
    def test_calendarObjectSplit_no_non_organizer_split(self):
        """
        Test that calendar objects do not split on attendee change.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", True)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event that will not split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
END:VCALENDAR
"""

        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data1.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()


    @inlineCallbacks
    def test_calendarObjectSplit_attachments(self):
        """
        Test that splitting of calendar objects with managed attachments works.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", True)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event that will split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data_1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
RRULE:FREQ=DAILY
END:VEVENT
END:VCALENDAR
"""

        data_attach_1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(mid)s;SIZE=14:%(att_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_split_1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(mid)s;SIZE=14:%(att_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(mid)s;SIZE=14:%(att_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(mid)s;SIZE=14:%(att_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(mid)s;SIZE=14:%(att_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_future = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(mid)s;SIZE=14:%(att_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:3
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(mid)s;SIZE=14:%(att_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:3
END:VEVENT
END:VCALENDAR
"""

        data_past = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(past_mid)s;SIZE=14:%(att_past_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:3
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(past_mid)s;SIZE=14:%(att_past_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:3
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(past_mid)s;SIZE=14:%(att_past_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:3
END:VEVENT
END:VCALENDAR
"""

        data_future2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(mid)s;SIZE=14:%(att_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:3
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(mid)s;SIZE=14:%(att_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:3
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_past2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(past_mid)s;SIZE=14:%(att_past_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:3
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(past_mid)s;SIZE=14:%(att_past_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:3
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTACH;FILENAME=new.attachment;FMTTYPE=text/x-fixture;MANAGED-ID=%(past_mid)s;SIZE=14:%(att_past_uri)s
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:%(dtstamp)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:3
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:%(uid)s
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        # Create initial non-split event
        cobj = yield calendar.createCalendarObjectWithName("data1.ics", Component.fromString(data_1 % self.subs))
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        # Add a managed attachment
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        attachment, location = yield cobj.addAttachment(None, MimeType("text", "x-fixture"), "new.attachment", MemoryStream("new attachment"))
        mid = attachment.managedID()
        yield self.commit()

        # Get attachment details
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        ical = yield cobj.component()
        attachment = ical.masterComponent().getProperty("ATTACH")
        self.assertEqual(attachment.parameterValue("MANAGED-ID"), mid)
        self.assertEqual(attachment.value(), location)

        relsubs = dict(self.subs)
        relsubs["mid"] = mid
        relsubs["att_uri"] = location
        relsubs["dtstamp"] = str(ical.masterComponent().propertyValue("DTSTAMP"))
        self.assertEqual(normalize_iCalStr(ical), normalize_iCalStr(data_attach_1) % relsubs, "Failed attachment user01")
        yield self.commit()

        # Add overrides to cause a split
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        yield cobj.setComponent(Component.fromString(data_split_1 % relsubs))
        self.assertTrue(hasattr(cobj, "_workItems"))
        yield self.commit()

        # Wait for it to complete
        yield JobItem.waitEmpty(self._sqlCalendarStore.newTransaction, reactor, 60)

        # Get the existing and new object data
        ical_future, ical_past, pastUID, relID, _ignore_new_name = yield self._splitDetails("user01")
        relsubs["uid"] = pastUID
        relsubs["relID"] = relID
        relsubs["dtstamp"] = str(ical_future.masterComponent().propertyValue("DTSTAMP"))

        attachment = ical.masterComponent().getProperty("ATTACH")
        self.assertEqual(attachment.parameterValue("MANAGED-ID"), mid)
        self.assertEqual(attachment.value(), location)

        relsubs["past_mid"] = attachment.parameterValue("MANAGED-ID")
        relsubs["att_past_uri"] = attachment.value()

        # Verify user01 data
        title = "user01"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s" % (title,))

        # Get user02 data
        ical_future, ical_past, pastUID, relID, _ignore_new_name = yield self._splitDetails("user02")

        # Verify user02 data
        title = "user02"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future2) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past2) % relsubs, "Failed past: %s" % (title,))


    @inlineCallbacks
    def test_calendarObjectSplit_processing_simple(self):
        """
        Test that splitting of calendar objects works when outside invites are processed.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", True)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event from outside organizer that will not split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Master
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_back25
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=DECLINED:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=DECLINED:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_back24
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=TENTATIVE:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_fwd10
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
END:VCALENDAR
"""

        itip1 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
X-CALENDARSERVER-SPLIT-OLDER-UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
X-CALENDARSERVER-SPLIT-RID;VALUE=DATE-TIME:%(now_back14)s
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:cuser01@example.org
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=TENTATIVE:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:cuser01@example.org
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_future = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=TENTATIVE:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Master
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:%(now_fwd10)s
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_fwd10
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_past = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=NEEDS-ACTION:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=DECLINED:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=DECLINED:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Master
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:%(now_back25)s
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_back25
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:%(now_back24)s
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_back24
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        itip2 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
X-CALENDARSERVER-SPLIT-NEWER-UID:12345-67890
X-CALENDARSERVER-SPLIT-RID;VALUE=DATE-TIME:%(now_back14)s
BEGIN:VEVENT
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=NEEDS-ACTION:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=DECLINED:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=DECLINED:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        # Now inject an iTIP with split
        processor = ImplicitProcessor()
        processor.getRecipientsCopy = lambda : succeed(None)

        cobj = yield self.calendarObjectUnderTest(name="data.ics", calendar_name="calendar", home="user01")
        processor.recipient_calendar_resource = cobj
        processor.recipient_calendar = (yield cobj.componentForUser("user01"))
        processor.message = Component.fromString(itip1 % self.subs)
        processor.originator = RemoteCalendarUser("mailto:cuser01@example.org")
        processor.recipient = LocalCalendarUser("urn:x-uid:user01", None)
        processor.method = "REQUEST"
        processor.uid = "12345-67890"

        result = yield processor.doImplicitAttendee()
        self.assertEqual(result, (True, False, False, None,))
        yield self.commit()

        new_names = []
        relsubs = dict(self.subs)

        @inlineCallbacks
        def _verify_state():
            # Get user01 data
            ical_future, ical_past, pastUID, relID, new_name = yield self._splitDetails("user01")
            relsubs["uid"] = pastUID
            relsubs["relID"] = relID
            new_names.append(new_name)

            # Verify user01 data
            title = "user01"
            self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s\n%s" % (title, diff_iCalStrs(ical_future, data_future % relsubs),))
            self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s\n%s" % (title, diff_iCalStrs(ical_past, data_past % relsubs),))

            # No inbox
            cal = yield self.calendarUnderTest(name="inbox", home="user01")
            cobjs = yield cal.calendarObjects()
            self.assertEqual(len(cobjs), 0)
            yield self.commit()

        yield _verify_state()

        # Now inject an iTIP with split
        processor = ImplicitProcessor()
        processor.getRecipientsCopy = lambda : succeed(None)

        cobj = yield self.calendarObjectUnderTest(name=new_names[0], calendar_name="calendar", home="user01")
        self.assertTrue(cobj is not None)
        processor.recipient_calendar_resource = cobj
        processor.recipient_calendar = (yield cobj.componentForUser("user01"))
        processor.message = Component.fromString(itip2 % relsubs)
        processor.originator = RemoteCalendarUser("mailto:cuser01@example.org")
        processor.recipient = LocalCalendarUser("urn:x-uid:user01", None)
        processor.method = "REQUEST"
        processor.uid = "C4526F4C-4324-4893-B769-BD766E4A4E7C"

        result = yield processor.doImplicitAttendee()
        self.assertEqual(result, (True, False, False, None,))
        yield self.commit()

        yield _verify_state()


    @inlineCallbacks
    def test_calendarObjectSplit_processing_one_past_instance(self):
        """
        Test that splitting of calendar objects works when outside invites are processed.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", True)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event from outside organizer that will not split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_back25
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
END:VCALENDAR
"""

        itip1 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:CANCEL
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
X-CALENDARSERVER-SPLIT-OLDER-UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
X-CALENDARSERVER-SPLIT-RID;VALUE=DATE-TIME:%(now_back14)s
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
END:VEVENT
END:VCALENDAR
"""

        data_past = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=NEEDS-ACTION:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:%(now_back25)s
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_back25
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        # Now inject an iTIP with split
        processor = ImplicitProcessor()
        processor.getRecipientsCopy = lambda : succeed(None)

        cobj = yield self.calendarObjectUnderTest(name="data.ics", calendar_name="calendar", home="user01")
        processor.recipient_calendar_resource = cobj
        processor.recipient_calendar = (yield cobj.componentForUser("user01"))
        processor.message = Component.fromString(itip1 % self.subs)
        processor.originator = RemoteCalendarUser("mailto:cuser01@example.org")
        processor.recipient = LocalCalendarUser("urn:x-uid:user01", None)
        processor.method = "CANCEL"
        processor.uid = "12345-67890"

        result = yield processor.doImplicitAttendee()
        self.assertEqual(result, (True, False, False, None,))
        yield self.commit()

        # Get user01 data
        relsubs = dict(self.subs)
        cal = yield self.calendarUnderTest(name="calendar", home="user01")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical = yield cobjs[0].component()
        ical_past = ical
        relsubs["uid"] = ical.resourceUID()
        relsubs["relID"] = ical.mainComponent().propertyValue("RELATED-TO")

        # Verify user01 data
        title = "user01"
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s\n%s" % (title, diff_iCalStrs(ical_past, data_past % relsubs),))


    @inlineCallbacks
    def test_calendarObjectSplit_processing_one_future_instance(self):
        """
        Test that splitting of calendar objects works when outside invites are processed.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", True)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event from outside organizer that will not split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=TENTATIVE:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_fwd10
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
END:VCALENDAR
"""

        itip1 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
X-CALENDARSERVER-SPLIT-OLDER-UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
X-CALENDARSERVER-SPLIT-RID;VALUE=DATE-TIME:%(now_back14)s
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=TENTATIVE:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:cuser01@example.org
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_future = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=TENTATIVE:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:%(now_fwd10)s
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_fwd10
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        # Now inject an iTIP with split
        processor = ImplicitProcessor()
        processor.getRecipientsCopy = lambda : succeed(None)

        cobj = yield self.calendarObjectUnderTest(name="data.ics", calendar_name="calendar", home="user01")
        processor.recipient_calendar_resource = cobj
        processor.recipient_calendar = (yield cobj.componentForUser("user01"))
        processor.message = Component.fromString(itip1 % self.subs)
        processor.originator = RemoteCalendarUser("mailto:cuser01@example.org")
        processor.recipient = LocalCalendarUser("urn:x-uid:user01", None)
        processor.method = "REQUEST"
        processor.uid = "12345-67890"

        result = yield processor.doImplicitAttendee()
        self.assertEqual(result, (True, False, False, None,))
        yield self.commit()

        # Get user01 data
        relsubs = dict(self.subs)
        cal = yield self.calendarUnderTest(name="calendar", home="user01")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical = yield cobjs[0].component()
        ical_future = ical
        relsubs["uid"] = ical.resourceUID()
        relsubs["relID"] = ical.mainComponent().propertyValue("RELATED-TO")

        # Verify user01 data
        title = "user01"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s\n%s" % (title, diff_iCalStrs(ical_future, data_future % relsubs),))


    @inlineCallbacks
    def test_calendarObjectSplit_processing_one_past_and_one_future(self):
        """
        Test that splitting of calendar objects works when outside invites are processed.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", True)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event from outside organizer that will not split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_back25
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=TENTATIVE:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_fwd10
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
END:VCALENDAR
"""

        itip1 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:CANCEL
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
X-CALENDARSERVER-SPLIT-OLDER-UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
X-CALENDARSERVER-SPLIT-RID;VALUE=DATE-TIME:%(now_back14)s
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_back25
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
END:VCALENDAR
"""

        data_future = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=TENTATIVE:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:%(now_fwd10)s
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_fwd10
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_past = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=NEEDS-ACTION:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
X-CALENDARSERVER-PERUSER-UID:user01
BEGIN:X-CALENDARSERVER-PERINSTANCE
RECURRENCE-ID:%(now_back25)s
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_back25
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        # Now inject an iTIP with split
        processor = ImplicitProcessor()
        processor.getRecipientsCopy = lambda : succeed(None)

        cobj = yield self.calendarObjectUnderTest(name="data.ics", calendar_name="calendar", home="user01")
        processor.recipient_calendar_resource = cobj
        processor.recipient_calendar = (yield cobj.componentForUser("user01"))
        processor.message = Component.fromString(itip1 % self.subs)
        processor.originator = RemoteCalendarUser("mailto:cuser01@example.org")
        processor.recipient = LocalCalendarUser("urn:x-uid:user01", None)
        processor.method = "REQUEST"
        processor.uid = "12345-67890"

        result = yield processor.doImplicitAttendee()
        self.assertEqual(result, (True, False, False, None,))
        yield self.commit()

        # Get user01 data
        ical_future, ical_past, pastUID, relID, _ignore_new_name = yield self._splitDetails("user01")
        relsubs = dict(self.subs)
        relsubs["uid"] = pastUID
        relsubs["relID"] = relID

        # Verify user01 data
        title = "user01"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s\n%s" % (title, diff_iCalStrs(ical_future, data_future % relsubs),))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s\n%s" % (title, diff_iCalStrs(ical_past, data_past % relsubs),))


    @inlineCallbacks
    def test_calendarObjectSplit_processing_disabled(self):
        """
        Test that splitting of calendar objects works when outside invites are processed.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", False)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event from outside organizer that will not split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Master
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_back25
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=DECLINED:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=DECLINED:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
TRANSP:TRANSPARENT
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_back24
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=TENTATIVE:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:now_fwd10
TRIGGER;RELATED=START:-PT10M
END:VALARM
END:VEVENT
END:VCALENDAR
"""

        itip1 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
X-CALENDARSERVER-SPLIT-OLDER-UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
X-CALENDARSERVER-SPLIT-RID;VALUE=DATE-TIME:%(now_back14)s
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:cuser01@example.org
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;PARTSTAT=TENTATIVE:mailto:user01@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:cuser01@example.org
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        itip2 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
X-CALENDARSERVER-SPLIT-NEWER-UID:12345-67890
X-CALENDARSERVER-SPLIT-RID;VALUE=DATE-TIME:%(now_back14)s
BEGIN:VEVENT
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:C4526F4C-4324-4893-B769-BD766E4A4E7C
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=TENTATIVE:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=NEEDS-ACTION:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:C4526F4C-4324-4893-B769-BD766E4A4E7C
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:C4526F4C-4324-4893-B769-BD766E4A4E7C
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=DECLINED:mailto:cuser01@example.org
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=DECLINED:urn:x-uid:user01
DTSTAMP:20051222T210507Z
ORGANIZER;SCHEDULE-AGENT=NONE:mailto:cuser01@example.org
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:C4526F4C-4324-4893-B769-BD766E4A4E7C
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        # Now inject an iTIP with split
        processor_action = [False, False, ]
        def _doImplicitAttendeeRequest():
            processor_action[0] = True
            return succeed(True)
        def _doImplicitAttendeeCancel():
            processor_action[1] = True
            return succeed(True)
        processor = ImplicitProcessor()
        processor.getRecipientsCopy = lambda : succeed(None)
        processor.doImplicitAttendeeRequest = _doImplicitAttendeeRequest
        processor.doImplicitAttendeeCancel = _doImplicitAttendeeCancel

        cobj = yield self.calendarObjectUnderTest(name="data.ics", calendar_name="calendar", home="user01")
        processor.recipient_calendar_resource = cobj
        processor.recipient_calendar = (yield cobj.componentForUser("user01"))
        processor.message = Component.fromString(itip1 % self.subs)
        processor.originator = RemoteCalendarUser("mailto:cuser01@example.org")
        processor.recipient = LocalCalendarUser("urn:x-uid:user01", None)
        processor.method = "REQUEST"
        processor.uid = "12345-67890"

        yield processor.doImplicitAttendee()
        self.assertTrue(processor_action[0])
        self.assertFalse(processor_action[1])
        yield self.commit()

        # Now inject an iTIP with split
        processor_action = [False, False, ]
        processor.getRecipientsCopy = lambda : succeed(None)
        processor.doImplicitAttendeeRequest = _doImplicitAttendeeRequest
        processor.doImplicitAttendeeCancel = _doImplicitAttendeeCancel

        processor.recipient_calendar_resource = None
        processor.recipient_calendar = None
        processor.message = Component.fromString(itip2 % self.subs)
        processor.originator = RemoteCalendarUser("mailto:cuser01@example.org")
        processor.recipient = LocalCalendarUser("urn:x-uid:user01", None)
        processor.method = "REQUEST"
        processor.uid = "C4526F4C-4324-4893-B769-BD766E4A4E7C"

        yield processor.doImplicitAttendee()
        self.assertTrue(processor_action[0])
        self.assertFalse(processor_action[1])


    @inlineCallbacks
    def test_calendarObjectSplit_external(self):
        """
        Test that splitting of calendar objects works.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", True)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event that will split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
ATTENDEE:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
END:VCALENDAR
"""

        data_future = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ATTENDEE;RSVP=TRUE;SCHEDULE-STATUS=3.7:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;RSVP=TRUE;SCHEDULE-STATUS=3.7:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_past = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ATTENDEE;RSVP=TRUE;SCHEDULE-STATUS=3.7:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ATTENDEE;RSVP=TRUE;SCHEDULE-STATUS=3.7:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_future2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;RSVP=TRUE:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
EXDATE:%(now_fwd10)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_past2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;RSVP=TRUE:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;RSVP=TRUE:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:%(uid)s
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_inbox2 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;RSVP=TRUE:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
EXDATE:%(now_fwd10)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
END:VCALENDAR
"""

        data_future_external = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
X-CALENDARSERVER-SPLIT-OLDER-UID:%(uid)s
X-CALENDARSERVER-SPLIT-RID;VALUE=DATE-TIME:%(now_back14)s
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;RSVP=TRUE:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;RSVP=TRUE:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_past_external = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
X-CALENDARSERVER-SPLIT-NEWER-UID:12345-67890
X-CALENDARSERVER-SPLIT-RID;VALUE=DATE-TIME:%(now_back14)s
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;RSVP=TRUE:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
EXDATE:%(now_back24)s
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
ATTENDEE;RSVP=TRUE:mailto:cuser01@example.org
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        # Patch CalDAVScheduler to trap external schedules
        details = []
        def _doSchedulingViaPUT(self, originator, recipients, calendar, internal_request=False, suppress_refresh=False):
            details.append((originator, recipients, calendar,))

            responses = ScheduleResponseQueue("REQUEST", responsecode.OK)
            for recipient in recipients:
                if recipient.startswith("urn:x-uid"):
                    responses.add(recipient, responsecode.OK, reqstatus=iTIPRequestStatus.MESSAGE_DELIVERED)
                else:
                    responses.add(recipient, responsecode.NOT_FOUND, reqstatus=iTIPRequestStatus.INVALID_CALENDAR_USER)
            return succeed(responses)

        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data1.ics", component)
        self.assertTrue(hasattr(cobj, "_workItems"))
        yield self.commit()

        self.patch(CalDAVScheduler, "doSchedulingViaPUT", _doSchedulingViaPUT)

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], cobj._resourceID)
        yield self.abort()

        # Wait for it to complete
        yield JobItem.waitEmpty(self._sqlCalendarStore.newTransaction, reactor, 60)

        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        # Get the existing and new object data
        cal = yield self.calendarUnderTest(name="calendar", home="user01")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 2)
        for cobj in cobjs:
            ical = yield cobj.component()
            if ical.resourceUID() == "12345-67890":
                ical_future = ical
            else:
                ical_past = ical
        relID = ical_future.masterComponent().propertyValue("RELATED-TO")
        newUID = ical_past.masterComponent().propertyValue("UID")

        # Verify user01 data
        title = "user01"
        relsubs = dict(self.subs)
        relsubs["uid"] = newUID
        relsubs["relID"] = relID
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s\n%s" % (title, diff_iCalStrs(ical_future, data_future % relsubs),))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s\n%s" % (title, diff_iCalStrs(ical_past, data_past % relsubs),))

        # Get user02 data
        cal = yield self.calendarUnderTest(name="calendar", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 2)
        for cobj in cobjs:
            ical = yield cobj.component()
            if ical.resourceUID() == "12345-67890":
                ical_future = ical
            else:
                ical_past = ical

        cal = yield self.calendarUnderTest(name="inbox", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical_inbox = yield cobjs[0].component()

        # Verify user02 data
        title = "user02"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future2) % relsubs, "Failed future: %s\n%s" % (title, diff_iCalStrs(ical_future, data_future2 % relsubs),))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past2) % relsubs, "Failed past: %s\n%s" % (title, diff_iCalStrs(ical_past, data_past2 % relsubs),))
        self.assertEqual(normalize_iCalStr(ical_inbox), normalize_iCalStr(data_inbox2) % relsubs, "Failed past: %s\n%s" % (title, diff_iCalStrs(ical_inbox, data_inbox2 % relsubs),))

        # Verify cuser02 data
        self.assertEqual(len(details), 2)
        self.assertEqual(details[0][0], "urn:x-uid:user01")
        self.assertEqual(details[0][1], ("mailto:cuser01@example.org",))
        self.assertEqual(normalize_iCalStr(details[0][2]), normalize_iCalStr(data_future_external) % relsubs, "Failed future: %s\n%s" % (title, diff_iCalStrs(details[0][2], data_future_external % relsubs),))

        self.assertEqual(details[1][0], "urn:x-uid:user01")
        self.assertEqual(details[1][1], ("mailto:cuser01@example.org",))
        self.assertEqual(normalize_iCalStr(details[1][2]), normalize_iCalStr(data_past_external) % relsubs, "Failed past: %s\n%s" % (title, diff_iCalStrs(details[1][2], data_past_external % relsubs),))


    @inlineCallbacks
    def test_calendarObjectSplit_timeout(self):
        """
        Test that splitting of calendar objects works.
        """
        data_init = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT2H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
END:VCALENDAR
"""

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT2H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
END:VCALENDAR
"""

        data_future = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT2H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_past = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT2H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_future2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT2H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_past2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT2H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:%(uid)s
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_inbox2 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT2H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        # Create one event without active split
        self.patch(config.Scheduling.Options.Splitting, "Enabled", False)
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        component = Component.fromString(data_init % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data1.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        # Turn on splitting
        self.patch(config.Scheduling.Options.Splitting, "Enabled", True)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Setup timeouts
        c = Clock()
        self.patch(CommonStoreTransactionMonitor, "callLater", c.callLater)

        # Patch config to turn on transaction timeouts then rebuild the store
        self.patch(self.storeUnderTest(), "timeoutTransactions", 1)
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")

        self.assertFalse(self.transactionUnderTest().timedout)

        oldScheduling = ImplicitScheduler.doImplicitScheduling
        def newScheduling(self, do_smart_merge=False, split_details=None):
            c.advance(2)
            return oldScheduling(self, do_smart_merge, split_details)
        self.patch(ImplicitScheduler, "doImplicitScheduling", newScheduling)

        component = Component.fromString(data % self.subs)
        yield self.failUnlessFailure(cobj.setComponent(component), AlreadyFinishedError)
        self.assertTrue(self.transactionUnderTest().timedout)

        # Clear out timed out state
        self.lastTransaction = None
        self.patch(self.storeUnderTest(), "timeoutTransactions", 0)

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], cobj._resourceID)
        yield self.abort()

        # Wait for it to complete
        yield JobItem.waitEmpty(self._sqlCalendarStore.newTransaction, reactor, 60)

        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        # Get the existing and new object data
        cal = yield self.calendarUnderTest(name="calendar", home="user01")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 2)
        for cobj in cobjs:
            self.assertTrue(cobj.isScheduleObject)
            ical = yield cobj.component()
            if ical.resourceUID() == "12345-67890":
                ical_future = ical
            else:
                ical_past = ical

        relID = ical_future.masterComponent().propertyValue("RELATED-TO")
        newUID = ical_past.masterComponent().propertyValue("UID")

        # Verify user01 data
        title = "user01"
        relsubs = dict(self.subs)
        relsubs["uid"] = newUID
        relsubs["relID"] = relID
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s" % (title,))

        # Get user02 data
        cal = yield self.calendarUnderTest(name="calendar", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 2)
        for cobj in cobjs:
            ical = yield cobj.component()
            if ical.resourceUID() == "12345-67890":
                ical_future = ical
            else:
                ical_past = ical

        cal = yield self.calendarUnderTest(name="inbox", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical_inbox = yield cobjs[0].component()

        # Verify user02 data
        title = "user02"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future2) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past2) % relsubs, "Failed past: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_inbox), normalize_iCalStr(data_inbox2) % relsubs, "Failed inbox: %s" % (title,))


    @inlineCallbacks
    def test_calendarObjectSplit_inbox_delete(self):
        """
        Test that splitting of calendar objects works when an inbox item is deleted
        whilst split processing is happening.
        """
        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back30)s
DURATION:PT2H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
RRULE:FREQ=DAILY
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
END:VCALENDAR
"""

        data_future = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT2H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_past = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT2H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_future2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT2H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_past2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back30)s
DURATION:PT2H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:%(uid)s
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        # Create one event without active split
        self.patch(config.Scheduling.Options.Splitting, "Enabled", False)
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data1.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        # Patch resource lookup code so that it deletes the inbox resource after lookup is done
        oldLookup = CalendarStoreFeatures.calendarObjectsWithUID
        @inlineCallbacks
        def _lookup(csself, txn, uid):
            results = yield oldLookup(csself, txn, uid)

            # Delete inbox item in another txn
            newtxn = self.concurrentTransaction()
            cal = yield self.calendarUnderTest(name="inbox", home="user02")
            cobjs = yield cal.calendarObjects()
            yield cobjs[0].remove()
            yield newtxn.commit()

            returnValue(results)

        self.patch(CalendarStoreFeatures, "calendarObjectsWithUID", _lookup)

        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        yield cobj.split()
        yield self.commit()

        # Get the existing and new object data
        cal = yield self.calendarUnderTest(name="calendar", home="user01")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 2)
        for cobj in cobjs:
            self.assertTrue(cobj.isScheduleObject)
            ical = yield cobj.component()
            if ical.resourceUID() == "12345-67890":
                ical_future = ical
            else:
                ical_past = ical

        relID = ical_future.masterComponent().propertyValue("RELATED-TO")
        newUID = ical_past.masterComponent().propertyValue("UID")

        # Verify user01 data
        title = "user01"
        relsubs = dict(self.subs)
        relsubs["uid"] = newUID
        relsubs["relID"] = relID
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s" % (title,))

        # Get user02 data
        cal = yield self.calendarUnderTest(name="calendar", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 2)
        for cobj in cobjs:
            ical = yield cobj.component()
            if ical.resourceUID() == "12345-67890":
                ical_future = ical
            else:
                ical_past = ical

        cal = yield self.calendarUnderTest(name="inbox", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 0)

        # Verify user02 data
        title = "user02"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future2) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past2) % relsubs, "Failed past: %s" % (title,))


    @inlineCallbacks
    def _setupSplitAt(self):
        """
        Test that user triggered splitting of calendar objects works.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", False)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event that will split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back28)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
RRULE:FREQ=DAILY;COUNT=50
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:mailto:user02@example.com
DTSTAMP:20051222T210507Z
ORGANIZER:mailto:user01@example.com
END:VEVENT
END:VCALENDAR
"""

        data_future = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;COUNT=36
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_past = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back28)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        data_future2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;COUNT=36
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:12345-67890
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_past2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back28)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
BEGIN:X-CALENDARSERVER-PERUSER
UID:%(uid)s
X-CALENDARSERVER-PERUSER-UID:user02
BEGIN:X-CALENDARSERVER-PERINSTANCE
TRANSP:TRANSPARENT
END:X-CALENDARSERVER-PERINSTANCE
END:X-CALENDARSERVER-PERUSER
END:VCALENDAR
"""

        data_inbox2 = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;COUNT=36
SEQUENCE:1
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;RSVP=TRUE:urn:x-uid:user02
DTSTAMP:20051222T210507Z
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

        # Create it
        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data1.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        returnValue((data_future, data_past, data_future2, data_past2, data_inbox2,))


    @inlineCallbacks
    def _setupSplitAt2(self):
        """
        Test that user triggered splitting of calendar objects works.
        """
        self.patch(config.Scheduling.Options.Splitting, "Enabled", False)
        self.patch(config.Scheduling.Options.Splitting, "Size", 1024)
        self.patch(config.Scheduling.Options.Splitting, "PastDays", 14)
        self.patch(config.Scheduling.Options.Splitting, "Delay", 2)

        # Create one event that will split
        calendar = yield self.calendarUnderTest(name="calendar", home="user01")

        data = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back28)s
DURATION:PT1H
DTSTAMP:20051222T210507Z
RRULE:FREQ=DAILY;COUNT=50
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
DTSTAMP:20051222T210507Z
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
DTSTAMP:20051222T210507Z
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
DTSTAMP:20051222T210507Z
END:VEVENT
END:VCALENDAR
"""

        data_future = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890
DTSTART:%(now_back14)s
DURATION:PT1H
DTSTAMP:20051222T210507Z
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;COUNT=36
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:12345-67890
RECURRENCE-ID:%(now_fwd10)s
DTSTART:%(now_fwd10)s
DURATION:PT1H
DTSTAMP:20051222T210507Z
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
END:VEVENT
END:VCALENDAR
"""

        data_past = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:%(uid)s
DTSTART:%(now_back28)s
DURATION:PT1H
DTSTAMP:20051222T210507Z
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
RRULE:FREQ=DAILY;UNTIL=%(now_back14_1)s
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back25)s
DTSTART:%(now_back25)s
DURATION:PT1H
DTSTAMP:20051222T210507Z
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
END:VEVENT
BEGIN:VEVENT
UID:%(uid)s
RECURRENCE-ID:%(now_back24)s
DTSTART:%(now_back24)s
DURATION:PT1H
DTSTAMP:20051222T210507Z
RELATED-TO;RELTYPE=X-CALENDARSERVER-RECURRENCE-SET:%(relID)s
END:VEVENT
END:VCALENDAR
"""

        # Create it
        component = Component.fromString(data % self.subs)
        cobj = yield calendar.createCalendarObjectWithName("data1.ics", component)
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        returnValue((data_future, data_past,))


    @inlineCallbacks
    def test_calendarObjectSplit_splitat_ok(self):
        """
        Test that user triggered splitting of calendar objects works.
        """

        data_future, data_past, data_future2, data_past2, data_inbox2 = yield self._setupSplitAt()

        # Update it
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        oldobj = yield cobj.splitAt(DateTime.parseText("%(now_back14)s" % self.subs))
        oldname = oldobj.name()
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        # Get the existing and new object data
        cobj1 = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        self.assertTrue(cobj1.isScheduleObject)
        ical1 = yield cobj1.component()
        relID = ical1.masterComponent().propertyValue("RELATED-TO")

        cobj2 = yield self.calendarObjectUnderTest(name=oldname, calendar_name="calendar", home="user01")
        self.assertTrue(cobj2 is not None)
        self.assertTrue(cobj2.isScheduleObject)
        ical2 = yield cobj2.component()
        newUID = ical2.masterComponent().propertyValue("UID")

        ical_future = yield cobj1.component()
        ical_past = yield cobj2.component()

        # Verify user01 data
        title = "user01"
        relsubs = dict(self.subs)
        relsubs["uid"] = newUID
        relsubs["relID"] = relID
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s" % (title,))

        # Get user02 data
        cal = yield self.calendarUnderTest(name="calendar", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 2)
        for cobj in cobjs:
            ical = yield cobj.component()
            if ical.resourceUID() == "12345-67890":
                ical_future = ical
            else:
                ical_past = ical

        cal = yield self.calendarUnderTest(name="inbox", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical_inbox = yield cobjs[0].component()

        # Verify user02 data
        title = "user02"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future2) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past2) % relsubs, "Failed past: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_inbox), normalize_iCalStr(data_inbox2) % relsubs, "Failed inbox: %s" % (title,))


    @inlineCallbacks
    def test_calendarObjectSplit_splitat_ok_not_instance_rid(self):
        """
        Test that user triggered splitting of calendar objects works when split between instances.
        """

        data_future, data_past, data_future2, data_past2, data_inbox2 = yield self._setupSplitAt()

        # Update it
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        oldobj = yield cobj.splitAt(DateTime.parseText("%(now_back15_12h)s" % self.subs))
        oldname = oldobj.name()
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        # Get the existing and new object data
        cobj1 = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        self.assertTrue(cobj1.isScheduleObject)
        ical1 = yield cobj1.component()
        relID = ical1.masterComponent().propertyValue("RELATED-TO")

        cobj2 = yield self.calendarObjectUnderTest(name=oldname, calendar_name="calendar", home="user01")
        self.assertTrue(cobj2 is not None)
        self.assertTrue(cobj2.isScheduleObject)
        ical2 = yield cobj2.component()
        newUID = ical2.masterComponent().propertyValue("UID")

        ical_future = yield cobj1.component()
        ical_past = yield cobj2.component()

        # Verify user01 data
        title = "user01"
        relsubs = dict(self.subs)
        relsubs["uid"] = newUID
        relsubs["relID"] = relID
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s" % (title,))

        # Get user02 data
        cal = yield self.calendarUnderTest(name="calendar", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 2)
        for cobj in cobjs:
            ical = yield cobj.component()
            if ical.resourceUID() == "12345-67890":
                ical_future = ical
            else:
                ical_past = ical

        cal = yield self.calendarUnderTest(name="inbox", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical_inbox = yield cobjs[0].component()

        # Verify user02 data
        title = "user02"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future2) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past2) % relsubs, "Failed past: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_inbox), normalize_iCalStr(data_inbox2) % relsubs, "Failed inbox: %s" % (title,))


    @inlineCallbacks
    def test_calendarObjectSplit_splitat_no_organizer(self):
        """
        Test that user triggered splitting of calendar objects works when no organizer is present.
        """

        data_future, data_past = yield self._setupSplitAt2()

        # Update it
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        oldobj = yield cobj.splitAt(DateTime.parseText("%(now_back14)s" % self.subs))
        oldname = oldobj.name()
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        # Get the existing and new object data
        cobj1 = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        self.assertFalse(cobj1.isScheduleObject)
        ical1 = yield cobj1.component()
        relID = ical1.masterComponent().propertyValue("RELATED-TO")

        cobj2 = yield self.calendarObjectUnderTest(name=oldname, calendar_name="calendar", home="user01")
        self.assertTrue(cobj2 is not None)
        self.assertFalse(cobj2.isScheduleObject)
        ical2 = yield cobj2.component()
        newUID = ical2.masterComponent().propertyValue("UID")

        ical_future = yield cobj1.component()
        ical_past = yield cobj2.component()

        # Verify user01 data
        title = "user01"
        relsubs = dict(self.subs)
        relsubs["uid"] = newUID
        relsubs["relID"] = relID
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s" % (title,))


    @inlineCallbacks
    def test_calendarObjectSplit_splitat_no_attendee_split(self):
        """
        Test that user triggered splitting of calendar objects does not work if done by attendee.
        """

        yield self._setupSplitAt()

        # Update it
        cal = yield self.calendarUnderTest(name="calendar", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        yield self.failUnlessFailure(cobjs[0].splitAt(DateTime.parseText("%(now_back14)s" % self.subs)), InvalidSplit)


    @inlineCallbacks
    def test_calendarObjectSplit_splitat_too_old(self):
        """
        Test that user triggered splitting of calendar objects does not work if split before first instance.
        """

        yield self._setupSplitAt()

        # Update it
        cal = yield self.calendarUnderTest(name="calendar", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        yield self.failUnlessFailure(cobjs[0].splitAt(DateTime.parseText("%(now_back30)s" % self.subs)), InvalidSplit)


    @inlineCallbacks
    def test_calendarObjectSplit_splitat_too_new(self):
        """
        Test that user triggered splitting of calendar objects does not work if split after last instance.
        """

        yield self._setupSplitAt()

        # Update it
        cal = yield self.calendarUnderTest(name="calendar", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        yield self.failUnlessFailure(cobjs[0].splitAt(DateTime.parseText("%(now_fwd25)s" % self.subs)), InvalidSplit)


    @inlineCallbacks
    def test_calendarObjectSplit_splitat_ok_pastuid(self):
        """
        Test that user triggered splitting of calendar objects works.
        """

        pastUID = "XXXX-YYYY-ZZZZ"
        data_future, data_past, data_future2, data_past2, data_inbox2 = yield self._setupSplitAt()

        # Update it
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        oldobj = yield cobj.splitAt(DateTime.parseText("%(now_back14)s" % self.subs), pastUID=pastUID)
        oldname = oldobj.name()
        self.assertFalse(hasattr(cobj, "_workItems"))
        yield self.commit()

        w = schema.CALENDAR_OBJECT_SPLITTER_WORK
        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        rows = yield Select(
            [w.RESOURCE_ID, ],
            From=w
        ).on(self.transactionUnderTest())
        self.assertEqual(len(rows), 0)
        yield self.abort()

        # Get the existing and new object data
        cobj1 = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        self.assertTrue(cobj1.isScheduleObject)
        ical1 = yield cobj1.component()
        relID = ical1.masterComponent().propertyValue("RELATED-TO")

        cobj2 = yield self.calendarObjectUnderTest(name=oldname, calendar_name="calendar", home="user01")
        self.assertTrue(cobj2 is not None)
        self.assertTrue(cobj2.isScheduleObject)
        ical2 = yield cobj2.component()
        newUID = ical2.masterComponent().propertyValue("UID")

        self.assertEqual(newUID, pastUID)

        ical_future = yield cobj1.component()
        ical_past = yield cobj2.component()

        # Verify user01 data
        title = "user01"
        relsubs = dict(self.subs)
        relsubs["uid"] = newUID
        relsubs["relID"] = relID
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past) % relsubs, "Failed past: %s" % (title,))

        # Get user02 data
        cal = yield self.calendarUnderTest(name="calendar", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 2)
        for cobj in cobjs:
            ical = yield cobj.component()
            if ical.resourceUID() == "12345-67890":
                ical_future = ical
            else:
                ical_past = ical

        cal = yield self.calendarUnderTest(name="inbox", home="user02")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        ical_inbox = yield cobjs[0].component()

        # Verify user02 data
        title = "user02"
        self.assertEqual(normalize_iCalStr(ical_future), normalize_iCalStr(data_future2) % relsubs, "Failed future: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_past), normalize_iCalStr(data_past2) % relsubs, "Failed past: %s" % (title,))
        self.assertEqual(normalize_iCalStr(ical_inbox), normalize_iCalStr(data_inbox2) % relsubs, "Failed inbox: %s" % (title,))


    @inlineCallbacks
    def test_calendarObjectSplit_splitat_no_same_uid(self):
        """
        Test that user triggered splitting of calendar objects does not work if the specified UID is the same
        as the resource being split.
        """

        yield self._setupSplitAt()

        # Update it
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        yield self.failUnlessFailure(cobj.splitAt(DateTime.parseText("%(now_back14)s" % self.subs), pastUID="12345-67890"), InvalidSplit)


    @inlineCallbacks
    def test_calendarObjectSplit_splitat_no_existing_uid(self):
        """
        Test that user triggered splitting of calendar objects does not work if the specified UID is the same
        as another resource.
        """

        data_existing = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:12345-67890-existing
DTSTART:%(now_back28)s
DURATION:PT1H
DTSTAMP:20051222T210507Z
RRULE:FREQ=DAILY;COUNT=50
SUMMARY:1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
 1234567890123456789012345678901234567890
END:VEVENT
END:VCALENDAR
"""

        yield self._setupSplitAt()

        calendar = yield self.calendarUnderTest(name="calendar", home="user01")
        component = Component.fromString(data_existing % self.subs)
        yield calendar.createCalendarObjectWithName("data2.ics", component)
        yield self.commit()

        # Update it
        cobj = yield self.calendarObjectUnderTest(name="data1.ics", calendar_name="calendar", home="user01")
        yield self.failUnlessFailure(cobj.splitAt(DateTime.parseText("%(now_back14)s" % self.subs), pastUID="12345-67890-existing"), InvalidSplit)



class TimeRangeUpdateOptimization(CommonCommonTests, unittest.TestCase):
    """
    CalendarObject time range optimization tests.
    """

    EVENT1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
END:VEVENT
END:VCALENDAR
"""

    EVENT2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event #2
DTSTAMP:20100203T013909Z
END:VEVENT
END:VCALENDAR
"""

    EVENT3 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T130000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
END:VEVENT
END:VCALENDAR
"""

    EVENT4 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
TRANSP:TRANSPARENT
END:VEVENT
END:VCALENDAR
"""

    EVENT5 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
STATUS:CANCELLED
DTSTAMP:20100203T013909Z
END:VEVENT
END:VCALENDAR
"""

    EVENT6 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT1H
END:VEVENT
END:VCALENDAR
"""

    EVENT7 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
RRULE:FREQ=DAILY
END:VEVENT
END:VCALENDAR
"""

    EVENT8 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
RRULE:FREQ=DAILY;COUNT=10
END:VEVENT
END:VCALENDAR
"""

    EVENT9 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
RRULE:FREQ=DAILY
EXDATE:{now}T120000Z
END:VEVENT
END:VCALENDAR
"""

    EVENT10 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
RRULE:FREQ=DAILY
RDATE:{now}T150000Z
END:VEVENT
END:VCALENDAR
"""


    @inlineCallbacks
    def setUp(self):
        yield super(TimeRangeUpdateOptimization, self).setUp()
        yield self.buildStoreAndDirectory()
        yield self.populate()

        self.now = DateTime.getNowUTC()
        self.now.setDateOnly(True)
        self.now1 = self.now.duplicate()
        self.now1.offsetDay(1)
        self.now2 = self.now.duplicate()
        self.now2.offsetDay(2)

        self.trcount = 0
        base_addInstances = CalendarObject._addInstances
        def _addInstances(*args):
            self.trcount += 1
            return base_addInstances(*args)
        self.patch(CalendarObject, "_addInstances", _addInstances)

        self.patch(config, "FreeBusyIndexDelayedExpand", False)
        self.patch(config, "FreeBusyIndexSmartUpdate", True)


    @inlineCallbacks
    def populate(self):
        yield populateCalendarsFrom(self.requirements, self.storeUnderTest())
        self.notifierFactory.reset()


    @property
    def requirements(self):
        return {
            "home1": {
                "calendar_1": {},
            },
            "user01": {
                "calendar": {},
                "inbox": {},
            },
            "user02": {
                "calendar": {},
                "inbox": {},
            },
        }


    @inlineCallbacks
    def test_initalPUT(self):
        """
        Test that initial PUT causes a TIME_RANGE update
        """

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest()
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.EVENT1.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 1)


    @inlineCallbacks
    def test_updatePUT_withoutTRChange(self):
        """
        Test that second PUT withe time change causes a TIME_RANGE update
        """

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest()
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.EVENT1.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 1)

        # Second PUT does not cause T-R change
        cobj = yield self.calendarObjectUnderTest()
        yield cobj.setComponent(Component.fromString(self.EVENT2.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 1)


    @inlineCallbacks
    def test_updatePUT_withoutOptimization(self):
        """
        Test that second PUT withe time change causes a TIME_RANGE update
        """

        self.patch(config, "FreeBusyIndexSmartUpdate", False)

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest()
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.EVENT1.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 1)

        # Second PUT does cause T-R change
        cobj = yield self.calendarObjectUnderTest()
        yield cobj.setComponent(Component.fromString(self.EVENT2.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 2)


    @inlineCallbacks
    def test_updatePUT_withTRChange(self):
        """
        Test that second PUT withe time change causes a TIME_RANGE update
        """

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest()
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.EVENT1.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 1)

        # Second PUT causes T-R change
        cobj = yield self.calendarObjectUnderTest()
        yield cobj.setComponent(Component.fromString(self.EVENT3.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 2)


    @inlineCallbacks
    def test_updatePUT_withTranspChange(self):
        """
        Test that second PUT withe time change causes a TIME_RANGE update
        """

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest()
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.EVENT1.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 1)

        # Second PUT causes T-R change
        cobj = yield self.calendarObjectUnderTest()
        yield cobj.setComponent(Component.fromString(self.EVENT4.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 2)


    @inlineCallbacks
    def test_updatePUT_withStatusChange(self):
        """
        Test that second PUT withe time change causes a TIME_RANGE update
        """

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest()
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.EVENT1.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 1)

        # Second PUT causes T-R change
        cobj = yield self.calendarObjectUnderTest()
        yield cobj.setComponent(Component.fromString(self.EVENT5.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 2)


    @inlineCallbacks
    def test_updatePUT_withTravelTimeChange(self):
        """
        Test that second PUT withe time change causes a TIME_RANGE update
        """

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest()
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.EVENT1.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 1)

        # Second PUT causes T-R change
        cobj = yield self.calendarObjectUnderTest()
        yield cobj.setComponent(Component.fromString(self.EVENT6.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 2)


    @inlineCallbacks
    def test_updatePUT_withRRULEChange(self):
        """
        Test that second PUT withe time change causes a TIME_RANGE update
        """

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest()
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.EVENT7.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 1)

        # Second PUT causes T-R change
        cobj = yield self.calendarObjectUnderTest()
        yield cobj.setComponent(Component.fromString(self.EVENT8.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 2)


    @inlineCallbacks
    def test_updatePUT_withEXDATEAdd(self):
        """
        Test that second PUT withe time change causes a TIME_RANGE update
        """

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest()
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.EVENT7.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 1)

        # Second PUT causes T-R change
        cobj = yield self.calendarObjectUnderTest()
        yield cobj.setComponent(Component.fromString(self.EVENT9.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 2)


    @inlineCallbacks
    def test_updatePUT_withRDATEAdd(self):
        """
        Test that second PUT withe time change causes a TIME_RANGE update
        """

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest()
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.EVENT7.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 1)

        # Second PUT causes T-R change
        cobj = yield self.calendarObjectUnderTest()
        yield cobj.setComponent(Component.fromString(self.EVENT10.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 2)


    INVITE1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""

    INVITE2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""

    INVITE3 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event #2
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""

    INVITE4 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T140000Z
DURATION:PT1H
SUMMARY:New Event #2
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user02@example.com
END:VEVENT
END:VCALENDAR
"""

    @inlineCallbacks
    def test_schedulingPUT(self):
        """
        Test that second PUT with time change causes a TIME_RANGE update
        """

        # Need schedule-q off for this test
        self.patch(config.Scheduling.Options.WorkQueues, "Enabled", False)

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.INVITE1.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 3)

        # Attendee reply does not cause T-R change (except for inbox item and attendee resource transp change)
        cal = yield self.calendarUnderTest(home="user02", name="calendar")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        yield cobjs[0].setComponent(Component.fromString(self.INVITE2.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 5)

        # Organizer summary change does not cause T-R change (except for inbox item)
        cobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar")
        yield cobj.setComponent(Component.fromString(self.INVITE3.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 6)

        # Organizer dtstart change causes T-R change
        cobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar")
        yield cobj.setComponent(Component.fromString(self.INVITE4.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 9)


    @inlineCallbacks
    def test_schedulingPUT_withoutOptimization(self):
        """
        Test that second PUT with time change causes a TIME_RANGE update
        """

        self.patch(config, "FreeBusyIndexSmartUpdate", False)

        # Need schedule-q off for this test
        self.patch(config.Scheduling.Options.WorkQueues, "Enabled", False)

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.INVITE1.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 3)

        # Attendee reply does cause T-R change (except for organizer update)
        cal = yield self.calendarUnderTest(home="user02", name="calendar")
        cobjs = yield cal.calendarObjects()
        self.assertEqual(len(cobjs), 1)
        yield cobjs[0].setComponent(Component.fromString(self.INVITE2.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 5)

        # Organizer summary change causes T-R change
        cobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar")
        yield cobj.setComponent(Component.fromString(self.INVITE3.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 8)

        # Organizer dtstart change causes T-R change
        cobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar")
        yield cobj.setComponent(Component.fromString(self.INVITE4.format(now=self.now.getText())))
        yield self.commit()

        self.assertEqual(self.trcount, 11)


    INVITE_OVERRIDE1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
RRULE:FREQ=DAILY
END:VEVENT
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
RECURRENCE-ID:{now1}T120000Z
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event now1
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE;PARTSTAT=NEEDS-ACTION:urn:x-uid:75EA36BE-F71B-40F9-81F9-CF59BF40CA8F
END:VEVENT
END:VCALENDAR
"""

    INVITE_OVERRIDE2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
RECURRENCE-ID:{now1}T120000Z
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event now1
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:urn:x-uid:75EA36BE-F71B-40F9-81F9-CF59BF40CA8F
END:VEVENT
END:VCALENDAR
"""

    INVITE_OVERRIDE3 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
RRULE:FREQ=DAILY
END:VEVENT
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
RECURRENCE-ID:{now1}T120000Z
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event now1
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:urn:x-uid:75EA36BE-F71B-40F9-81F9-CF59BF40CA8F
END:VEVENT
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
RECURRENCE-ID:{now2}T120000Z
DTSTART:{now2}T120000Z
DURATION:PT1H
SUMMARY:New Event now2
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE;PARTSTAT=NEEDS-ACTION:urn:x-uid:75EA36BE-F71B-40F9-81F9-CF59BF40CA8F
END:VEVENT
END:VCALENDAR
"""

    INVITE_OVERRIDE4 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
DTSTART:{now}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
RRULE:FREQ=DAILY
END:VEVENT
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
RECURRENCE-ID:{now1}T120000Z
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event now1
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
END:VEVENT
BEGIN:VEVENT
CREATED:20100203T013849Z
UID:uid1
RECURRENCE-ID:{now2}T120000Z
DTSTART:{now2}T120000Z
DURATION:PT1H
SUMMARY:New Event now2
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:urn:x-uid:75EA36BE-F71B-40F9-81F9-CF59BF40CA8F
END:VEVENT
END:VCALENDAR
"""


    @inlineCallbacks
    def test_schedulingPUT_AddRemoveOverride_AutoAccept(self):
        """
        Test that second PUT with override change causes a TIME_RANGE update
        """

        # Need schedule-q off for this test
        self.patch(config.Scheduling.Options.WorkQueues, "Enabled", False)
        self.patch(config.Scheduling.Options.WorkQueues, "AutoReplyDelaySeconds", 1)

        # First PUT causes T-R change
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(self.INVITE_OVERRIDE1.format(
            now=self.now.getText(),
            now1=self.now1.getText(),
            now2=self.now2.getText(),
        )))
        yield self.commit()

        # Wait for it to complete
        yield JobItem.waitEmpty(self._sqlCalendarStore.newTransaction, reactor, 60)

        self.assertEqual(self.trcount, 3)

        # Organizer adds attendee to override causes T-R change (except for their item)
        cobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar")
        yield cobj.setComponent(Component.fromString(self.INVITE_OVERRIDE3.format(
            now=self.now.getText(),
            now1=self.now1.getText(),
            now2=self.now2.getText(),
        )))
        yield self.commit()

        # Wait for it to complete
        yield JobItem.waitEmpty(self._sqlCalendarStore.newTransaction, reactor, 60)

        self.assertEqual(self.trcount, 5)

        # Organizer removes attendee from override causes T-R change (except for their item)
        cobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar")
        yield cobj.setComponent(Component.fromString(self.INVITE_OVERRIDE4.format(
            now=self.now.getText(),
            now1=self.now1.getText(),
            now2=self.now2.getText(),
        )))
        yield self.commit()

        # Wait for it to complete
        yield JobItem.waitEmpty(self._sqlCalendarStore.newTransaction, reactor, 60)

        self.assertEqual(self.trcount, 6)



class GroupExpand(CommonCommonTests, unittest.TestCase):
    """
    CalendarObject group attendee expansion.
    """

    @inlineCallbacks
    def setUp(self):
        yield super(GroupExpand, self).setUp()

        accountsFilePath = FilePath(
            os.path.join(os.path.dirname(__file__), "accounts")
        )
        yield self.buildStoreAndDirectory(
            accounts=accountsFilePath.child("groupAttendeeAccounts.xml"),
        )

        yield self.populate()

        now = DateTime.getNowUTC()
        now.setDateOnly(True)
        past1 = now.duplicate()
        past1.offsetDay(-1)
        past2 = now.duplicate()
        past2.offsetDay(-2)
        past400 = now.duplicate()
        past400.offsetDay(-400)
        now1 = now.duplicate()
        now1.offsetDay(1)
        now2 = now.duplicate()
        now2.offsetDay(2)
        self.subs = {
            "now": now,
            "past1": past1,
            "past2": past2,
            "past400": past400,
            "now1": now1,
            "now2": now2,
        }


    @inlineCallbacks
    def populate(self):
        yield populateCalendarsFrom(self.requirements, self.storeUnderTest())
        self.notifierFactory.reset()


    @property
    def requirements(self):
        return {
            "user01": {
                "calendar": {},
                "inbox": {},
            },
            "user02": {
                "calendar": {},
                "inbox": {},
            },
            "user03": {
                "calendar": {},
                "inbox": {},
            },
        }


    @inlineCallbacks
    def test_expand_insert(self):
        """
        Test that creating an event with a group attendee triggers expansion.
        """

        event = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:urn:x-uid:group01
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        result = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{now1}T120000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SUMMARY:New Event
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        # PUT causes expansion
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(event))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        comp = yield calobj.componentForUser()
        self.assertEqual(normalize_iCalStr(comp), normalize_iCalStr(result), msg=diff_iCalStrs(comp, result))

        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)


    @inlineCallbacks
    def test_expand_update_new(self):
        """
        Test that updating an event with a new group attendee triggers expansion, both with
        and without a time range change.
        """
        event1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        event2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:urn:x-uid:group01
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        result = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{now1}T120000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:1
SUMMARY:New Event
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        # PUT does not cause expansion
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(event1))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 0)
        yield self.commit()

        # PUT causes expansion
        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        yield calobj.setComponent(Component.fromString(event2))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        comp = yield calobj.componentForUser()
        self.assertEqual(normalize_iCalStr(comp), normalize_iCalStr(result), msg=diff_iCalStrs(comp, result))

        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)


    @inlineCallbacks
    def test_expand_update_existing(self):
        """
        Test that updating an event with an existing group attendee leaves expansion in place,
        both with and without a time range change.
        """
        event1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:urn:x-uid:group01
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        event2 = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{now1}T120000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:1
SUMMARY:New Event #2
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        event3 = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{now1}T130000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:1
SUMMARY:New Event #2
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        result = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{now1}T130000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:2
SUMMARY:New Event #2
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        # PUT causes expansion
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(event1))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)
        yield self.commit()

        # PUT expansion done - no time-range change
        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        yield calobj.setComponent(Component.fromString(event2))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)

        # PUT expansion done - time-range change
        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        yield calobj.setComponent(Component.fromString(event3))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        comp = yield calobj.componentForUser()
        self.assertEqual(normalize_iCalStr(comp), normalize_iCalStr(result), msg=diff_iCalStrs(comp, result))

        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)


    @inlineCallbacks
    def test_expand_insert_recurring(self):
        """
        Test that creating an event with a group attendee triggers expansion.
        """

        event = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:urn:x-uid:group01
RRULE:FREQ=DAILY
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        result = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{now1}T120000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SUMMARY:New Event
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        # PUT causes expansion
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(event))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        comp = yield calobj.componentForUser()
        self.assertEqual(normalize_iCalStr(comp), normalize_iCalStr(result), msg=diff_iCalStrs(comp, result))

        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)


    @inlineCallbacks
    def test_expand_update_new_recurring(self):
        """
        Test that updating an event with a new group attendee triggers expansion, both with
        and without a time range change.
        """
        event1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
RRULE:FREQ=DAILY
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        event2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:urn:x-uid:group01
RRULE:FREQ=DAILY
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        result = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{now1}T120000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:New Event
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        # PUT does not cause expansion
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(event1))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 0)
        yield self.commit()

        # PUT causes expansion
        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        yield calobj.setComponent(Component.fromString(event2))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        comp = yield calobj.componentForUser()
        self.assertEqual(normalize_iCalStr(comp), normalize_iCalStr(result), msg=diff_iCalStrs(comp, result))

        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)


    @inlineCallbacks
    def test_expand_update_existing_recurring(self):
        """
        Test that updating an event with an existing group attendee leaves expansion in place,
        both with and without a time range change.
        """
        event1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{now1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:urn:x-uid:group01
RRULE:FREQ=DAILY
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        event2 = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{now1}T120000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:New Event #2
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        event3 = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{now1}T130000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SEQUENCE:1
SUMMARY:New Event #2
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        result = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{now1}T130000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=DAILY
SEQUENCE:2
SUMMARY:New Event #2
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        # PUT causes expansion
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(event1))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)
        yield self.commit()

        # PUT expansion done - no time-range change
        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        yield calobj.setComponent(Component.fromString(event2))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)

        # PUT expansion done - time-range change
        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        yield calobj.setComponent(Component.fromString(event3))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        comp = yield calobj.componentForUser()
        self.assertEqual(normalize_iCalStr(comp), normalize_iCalStr(result), msg=diff_iCalStrs(comp, result))

        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)


    @inlineCallbacks
    def test_expand_insert_past(self):
        """
        Test that creating an event with a group attendee triggers expansion.
        """

        event = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{past1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:urn:x-uid:group01
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        result = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{past1}T120000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SUMMARY:New Event
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        # PUT causes expansion
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(event))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        comp = yield calobj.componentForUser()
        self.assertEqual(normalize_iCalStr(comp), normalize_iCalStr(result), msg=diff_iCalStrs(comp, result))

        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 0)


    @inlineCallbacks
    def test_expand_update_new_past(self):
        """
        Test that updating an event with a new group attendee triggers expansion, both with
        and without a time range change.
        """
        event1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{past1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        event2 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{past1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:urn:x-uid:group01
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        result = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{past1}T120000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:1
SUMMARY:New Event
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        # PUT does not cause expansion
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(event1))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 0)
        yield self.commit()

        # PUT causes expansion
        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        yield calobj.setComponent(Component.fromString(event2))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        comp = yield calobj.componentForUser()
        self.assertEqual(normalize_iCalStr(comp), normalize_iCalStr(result), msg=diff_iCalStrs(comp, result))

        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 0)


    @inlineCallbacks
    def test_expand_update_existing_past(self):
        """
        Test that updating an event with an existing group attendee leaves expansion in place,
        both with and without a time range change.
        """
        event1 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{past1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:urn:x-uid:group01
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        event2 = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{past1}T120000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:1
SUMMARY:New Event #2
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        event3 = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{past1}T130000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:1
SUMMARY:New Event #2
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        result = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{past1}T130000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
SEQUENCE:2
SUMMARY:New Event #2
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        # PUT causes expansion
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(event1))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 0)
        yield self.commit()

        # PUT expansion done - no time-range change
        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        yield calobj.setComponent(Component.fromString(event2))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 0)

        # PUT expansion done - time-range change
        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        yield calobj.setComponent(Component.fromString(event3))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        comp = yield calobj.componentForUser()
        self.assertEqual(normalize_iCalStr(comp), normalize_iCalStr(result), msg=diff_iCalStrs(comp, result))

        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 0)


    @inlineCallbacks
    def test_expand_insert_recurrence_big_future_step(self):
        """
        Test that creating an event with a recurrence with one instance in the past and one in the distant
        future is properly linked.
        """

        event = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{past1}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:urn:x-uid:group01
RRULE:FREQ=YEARLY;INTERVAL=2
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        result = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{past1}T120000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=YEARLY;INTERVAL=2
SUMMARY:New Event
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        # PUT causes expansion
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(event))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        comp = yield calobj.componentForUser()
        self.assertEqual(normalize_iCalStr(comp), normalize_iCalStr(result), msg=diff_iCalStrs(comp, result))

        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)


    @inlineCallbacks
    def test_expand_insert_recurrence_big_past_step(self):
        """
        Test that creating an event with a recurrence with one instance in the distant past and one in the distant
        future is properly linked.
        """

        event = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:uid1
DTSTART:{past400}T120000Z
DURATION:PT1H
SUMMARY:New Event
DTSTAMP:20100203T013909Z
ORGANIZER:mailto:user01@example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:user01@example.com
ATTENDEE:urn:x-uid:group01
RRULE:FREQ=YEARLY;INTERVAL=4
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        result = """BEGIN:VCALENDAR
VERSION:2.0
CALSCALE:GREGORIAN
PRODID:-//CALENDARSERVER.ORG//NONSGML Version 1//EN
BEGIN:VEVENT
UID:uid1
DTSTAMP:20100203T013909Z
DTSTART:{past400}T120000Z
DURATION:PT1H
ATTENDEE;CN=User 01;EMAIL=user01@example.com;PARTSTAT=ACCEPTED:urn:x-uid:user01
ATTENDEE;CN=Group 01;CUTYPE=X-SERVER-GROUP;EMAIL=group01@example.com;SCHEDULE-STATUS=2.7:urn:x-uid:group01
ATTENDEE;CN=User 02;EMAIL=user02@example.com;MEMBER="urn:x-uid:group01";PARTSTAT=NEEDS-ACTION;RSVP=TRUE;SCHEDULE-STATUS=1.2:urn:x-uid:user02
ORGANIZER;CN=User 01;EMAIL=user01@example.com:urn:x-uid:user01
RRULE:FREQ=YEARLY;INTERVAL=4
SUMMARY:New Event
END:VEVENT
END:VCALENDAR
""".format(**self.subs)

        # PUT causes expansion
        cal = yield self.calendarUnderTest(home="user01", name="calendar")
        yield cal.createObjectResourceWithName("1.ics", Component.fromString(event))
        yield self.commit()

        calobj = yield self.calendarObjectUnderTest(home="user01", calendar_name="calendar", name="1.ics")
        comp = yield calobj.componentForUser()
        self.assertEqual(normalize_iCalStr(comp), normalize_iCalStr(result), msg=diff_iCalStrs(comp, result))

        links = yield calobj.groupEventLinks()
        self.assertEqual(len(links), 1)
