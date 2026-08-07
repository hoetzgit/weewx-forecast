#!/usr/bin/env python
# Copyright: 2016-2020 Matthew Wall
# Copyright: 2020-2026 John A Kline (john@johnkline.com)
# License: GPLv3

"""Tests for weewx forecast comparison generator."""

import math
import os
import shutil
import sys
import threading
import time
import unittest

import configobj

import weewx
import weewx.manager
import weewx.station
import weewx.reportengine

import forecast

# this is where to look for the unit test data files
TSTDIR = os.path.dirname(os.path.realpath(__file__))

# FIXME: these belong in a common testing library for weewx
TMPDIR = '/var/tmp/weewx_test'

def rmdir(d):
    try:
        os.rmdir(d)
    except:
        pass

def rmtree(d):
    try:
        shutil.rmtree(d)
    except:
        pass

def mkdir(d):
    try:
        os.makedirs(d)
    except:
        pass

def rmfile(name):
    try:
        os.remove(name)
    except:
        pass

def readfile(name, dir=TSTDIR):
    data = []
    fn = name if dir is None else dir + '/' + name
    with open(fn, 'r') as f:
        for line in f:
            data.append(line)
    return ''.join(data)
    
def get_tmpdir():
    return TMPDIR + '/test_forecast'

def get_testdir(name):
    return get_tmpdir() + '/' + name

# common methods to set up and tear down forecasting unit tests

def create_skin_config(test_dir, contents, skin_dir='testskin'):
    mkdir(test_dir + '/' + skin_dir)
    fn = test_dir + '/' + skin_dir + '/skin.conf'
    with open(fn, 'w') as f:
        f.write(contents)

# The comparison generator plots forecasts against each other, so the test
# database needs more than one source, and more than one issue per source.
# Everything is anchored to 'now' so that a relative issued_since (-86400)
# selects the recent issue and an absolute one selects both.

SOURCES = ['NWS', 'WU', 'Aeris']
ISSUE_AGES = [36, 3]  # hours before the top of the current hour
FORECAST_HOURS = 24   # hourly events out from each issue

# the simplest skin that plots something; used by the tests that drive the
# generator directly rather than through the report engine
ONE_PLOT_SKIN = '''
[ForecastPlotGenerator]
    source = NWS
    [[plots]]
        [[[temp]]]
[Generators]
    generator_list = forecast.ForecastPlotGenerator
'''

def create_empty_forecast_database(test_dir):
    """Create the forecast database with the right schema but no records."""
    db_dict = {
        'database_name': test_dir + '/forecast.sdb',
        'driver': 'weedb.sqlite'}
    with weewx.manager.Manager.open_with_create(
            db_dict, schema=forecast.schema):
        pass

def create_forecast_database(test_dir, now):
    """Create a forecast database with hourly forecasts from several sources."""
    base = int(now) - int(now) % 3600
    db_dict = {
        'database_name': test_dir + '/forecast.sdb',
        'driver': 'weedb.sqlite'}
    with weewx.manager.Manager.open_with_create(
            db_dict, schema=forecast.schema) as dbm:
        for source in SOURCES:
            # offset each source a bit; identical lines make for a dull plot
            bias = SOURCES.index(source)
            for age in ISSUE_AGES:
                issued_ts = base - age * 3600
                for h in range(FORECAST_HOURS + 1):
                    event_ts = issued_ts + h * 3600
                    hour = time.localtime(event_ts).tm_hour
                    # a plain diurnal swing: coolest at 05:00, warmest at 17:00
                    temp = 55.0 + 12.0 * math.sin(
                        (hour - 11) * math.pi / 12.0) + bias
                    dbm.addRecord({
                        'method': source,
                        'usUnits': weewx.US,
                        'dateTime': issued_ts,
                        'issued_ts': issued_ts,
                        'event_ts': event_ts,
                        'duration': 3600,
                        'location': 'Boston, MA',
                        'hour': hour,
                        'temp': round(temp, 1),
                        'humidity': round(95.0 - (temp - 43.0), 1),
                        'pop': 10.0 * bias + (30.0 if 12 <= hour < 18 else 5.0),
                        'windSpeed': 8.0 + bias,
                        'windDir': 'SW'})

def create_weewx_config(test_dir, service='', skin_dir='testskin'):
    cd = configobj.ConfigObj()
    cd['debug'] = 1
    cd['WEEWX_ROOT'] = test_dir
    cd['Station'] = {
        'station_type': 'Simulator',
        'altitude': [10,'foot'],
        'latitude': 42.358,
        'longitude': -71.106}
    cd['Simulator'] = {
        'driver': 'weewx.drivers.simulator',
        'mode': 'generator'}
    cd['Engine'] = {
        'Services': {
            'service_list' : service}}
    cd['DataBindings'] = {
        'wx_binding': {
            'database': 'wx_sqlite'},
        'forecast_binding': {
            'database': 'forecast_sqlite',
            'manager': 'weewx.manager.Manager',
            'schema': 'forecast.schema'}}
    cd['Databases'] = {
        'wx_sqlite': {
            'database_name': 'weewx.sdb',
            'database_type': 'SQLite'},
        'forecast_sqlite': {
            'database_name': 'forecast.sdb',
            'database_type': 'SQLite'}}
    cd['DatabaseTypes'] = {
        'SQLite': {
            'driver': 'weedb.sqlite',
            'SQLITE_ROOT': test_dir}}
    cd['StdReport'] = {
        'HTML_ROOT': test_dir + '/html',
        'SKIN_ROOT': test_dir,
        'fc': {
            'skin': skin_dir}}
    cd['StdArchive'] = {
        'data_binding': 'wx_binding'}
    cd['Forecast'] = {
        'data_binding': 'forecast_binding',
        'single_thread': True}
    return cd


class ForecastComparisonTest(unittest.TestCase):

    @staticmethod
    def _run_test(name, skin_contents):
        tdir = get_testdir(name)
        rmtree(tdir)
        create_skin_config(tdir, skin_contents)
        create_forecast_database(tdir, time.time())
        cd = create_weewx_config(tdir)
        si = weewx.station.StationInfo(**cd['Station'])
        ts = int(time.time())
        t = weewx.reportengine.StdReportEngine(cd, si, ts)
        t.run()
        return tdir

    @staticmethod
    def _plots(tdir):
        """Names of the plots that were actually generated.

        The report engine logs and swallows anything a generator throws, so
        checking the images is the only way a broken generator fails the test.
        """
        html_dir = tdir + '/html'
        if not os.path.isdir(html_dir):
            return []
        return sorted(f[:-4] for f in os.listdir(html_dir)
                      if f.endswith('.png'))

    @staticmethod
    def _make_generator(tdir, skin_contents=ONE_PLOT_SKIN):
        """Build a ForecastPlotGenerator to be run directly.

        StdReportEngine logs whatever a generator throws and carries on, so
        tests that care about the generator's own behavior invoke it
        themselves and let the exception out.
        """
        create_skin_config(tdir, skin_contents)
        cd = create_weewx_config(tdir)
        skin_dict = weewx.reportengine.build_skin_dict(cd, 'fc')
        skin_dict['SKIN_ROOT'] = cd['StdReport']['SKIN_ROOT']
        skin_dict['HTML_ROOT'] = cd['StdReport']['HTML_ROOT']
        si = weewx.station.StationInfo(**cd['Station'])
        return forecast.ForecastPlotGenerator(
            cd, skin_dict, int(time.time()), True, si)

    def test_one_source_one_obs(self):
        tdir = self._run_test('test_one_source_one_obs', '''
[ForecastPlotGenerator]
    source = NWS
    [[plots]]
        [[[temp]]]
[Generators]
    generator_list = forecast.ForecastPlotGenerator
''')
        self.assertEqual(self._plots(tdir), ['temp'])

    def test_one_source_multiple_obs(self):
        tdir = self._run_test('test_one_source_multiple_obs', '''
[ForecastPlotGenerator]
    source = NWS
    [[plots]]
        [[[temp]]]
        [[[humidity]]]
        [[[pop]]]
[Generators]
    generator_list = forecast.ForecastPlotGenerator
''')
        self.assertEqual(self._plots(tdir), ['humidity', 'pop', 'temp'])

    def test_multiple_source_one_obs(self):
        tdir = self._run_test('test_multiple_source_one_obs', '''
[ForecastPlotGenerator]
    source = NWS, WU, Aeris
    [[plots]]
        [[[temp]]]
[Generators]
    generator_list = forecast.ForecastPlotGenerator
''')
        self.assertEqual(self._plots(tdir), ['temp'])

    def test_multiple_source_multiple_obs(self):
        tdir = self._run_test('test_multiple_source_multiple_obs', '''
[ForecastPlotGenerator]
    source = NWS, WU, Aeris
    [[plots]]
        [[[temp]]]
        [[[humidity]]]
        [[[pop]]]
[Generators]
    generator_list = forecast.ForecastPlotGenerator
''')
        self.assertEqual(self._plots(tdir), ['humidity', 'pop', 'temp'])

    def test_one_source_over_time(self):
        tdir = self._run_test('test_one_source_over_time', '''
[ForecastPlotGenerator]
    source = WU
    issued_since = 1454457600
    [[plots]]
        [[[temp]]]
[Generators]
    generator_list = forecast.ForecastPlotGenerator
''')
        self.assertEqual(self._plots(tdir), ['temp'])

    def test_data_type(self):
        tdir = self._run_test('test_data_type', '''
[ForecastPlotGenerator]
    source = WU
    issued_since = 1454457600
    [[plots]]
        [[[the_temp]]]
            data_type = temp
[Generators]
    generator_list = forecast.ForecastPlotGenerator
''')
        self.assertEqual(self._plots(tdir), ['the_temp'])

    def test_issued_offset(self):
        tdir = self._run_test('test_issued_offset', '''
[ForecastPlotGenerator]
    source = WU
    issued_since = -86400
    [[plots]]
        [[[temp]]]
[Generators]
    generator_list = forecast.ForecastPlotGenerator
''')
        self.assertEqual(self._plots(tdir), ['temp'])

    def test_overlapping_columns(self):
        tdir = self._run_test('test_overlapping_columns', '''
[ForecastPlotGenerator]
    source = WU
    issued_since = -86400
    [[plots]]
        [[[temp_a]]]
            data_type = temp
        [[[temp_b]]]
            data_type = temp
[Generators]
    generator_list = forecast.ForecastPlotGenerator
''')
        self.assertEqual(self._plots(tdir), ['temp_a', 'temp_b'])

    def test_no_data(self):
        """A forecast database with nothing in it plots nothing, quietly.

        An empty (or not-yet-downloaded) source used to hand the image
        generator a negative time_length, which threw ViolatedPrecondition for
        every plot, every report cycle.
        """
        tdir = get_testdir('test_no_data')
        rmtree(tdir)
        gen = self._make_generator(tdir)
        create_empty_forecast_database(tdir)
        gen.run()
        self.assertEqual(self._plots(tdir), [])

    def test_unknown_source(self):
        """Same for a source that has no records of its own."""
        tdir = get_testdir('test_unknown_source')
        rmtree(tdir)
        gen = self._make_generator(
            tdir, ONE_PLOT_SKIN.replace('NWS', 'NoSuchSource'))
        create_forecast_database(tdir, time.time())
        gen.run()
        self.assertEqual(self._plots(tdir), [])

    def test_no_stop_event(self):
        """weewx 5.4 and earlier never set the attribute at all."""
        tdir = get_testdir('test_no_stop_event')
        rmtree(tdir)
        gen = self._make_generator(tdir)
        create_forecast_database(tdir, time.time())
        if hasattr(gen, 'stop_event'):
            del gen.stop_event
        gen.run()
        self.assertEqual(self._plots(tdir), ['temp'])

    def test_stop_event_not_set(self):
        """weewx 5.5 hands over an event; unset, it changes nothing."""
        tdir = get_testdir('test_stop_event_not_set')
        rmtree(tdir)
        gen = self._make_generator(tdir)
        create_forecast_database(tdir, time.time())
        gen.stop_event = threading.Event()
        gen.run()
        self.assertEqual(self._plots(tdir), ['temp'])

    def test_stop_event_set(self):
        """A set event stops the generator before it plots anything."""
        tdir = get_testdir('test_stop_event_set')
        rmtree(tdir)
        gen = self._make_generator(tdir)
        create_forecast_database(tdir, time.time())
        gen.stop_event = threading.Event()
        gen.stop_event.set()
        gen.run()
        self.assertEqual(self._plots(tdir), [])
        self.assertFalse(os.path.exists(tdir + '/fpg.sdb'))


# PYTHONPATH=.:/home/weewx/bin python test/test_cmp.py
#
# use '--test test_name' to specify a single test

if __name__ == '__main__':

    # check for a single test, if not then run them all
    testname = None
    if len(sys.argv) == 3 and sys.argv[1] == '--test':
        testname = sys.argv[2]
    if testname is not None:
        unittest.TextTestRunner(verbosity=2).run(
            unittest.TestSuite(map(ForecastComparisonTest, [testname])))
    else:
        unittest.main()
