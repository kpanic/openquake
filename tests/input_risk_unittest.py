# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010-2012, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version 3
# only, as published by the Free Software Foundation.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License version 3 for more details
# (a copy is included in the LICENSE file that accompanied this code).
#
# You should have received a copy of the GNU Lesser General Public License
# version 3 along with OpenQuake.  If not, see
# <http://www.gnu.org/licenses/lgpl-3.0.txt> for a copy of the LGPLv3 License.


import unittest
import os

from openquake.shapes import Site
from openquake.input.exposure import ExposureDBWriter
from openquake.output.hazard import GmfDBWriter
from openquake.output.hazard import HazardCurveDBWriter
from openquake.parser.exposure import ExposurePortfolioFile
from openquake.calculators.risk.classical.core import ClassicalRiskCalculator
from openquake.calculators.risk.event_based.core import (
    EventBasedRiskCalculator)
from tests.utils import helpers

TEST_FILE = 'exposure-portfolio.xml'


# See data in output_hazard_unittest.py
def HAZARD_CURVE_DATA():
    return [
        (Site(-122.2, 37.5),
         {'investigationTimeSpan': '50.0',
          'IMLValues': [0.778, 1.09, 1.52, 2.13],
          'PoEValues': [0.354, 0.114, 0.023, 0.002],
          'IMT': 'PGA',
          'statistics': 'mean'}),
        (Site(-122.1, 37.5),
         {'investigationTimeSpan': '50.0',
          'IMLValues': [0.778, 1.09, 1.52, 2.13],
          'PoEValues': [0.454, 0.214, 0.123, 0.102],
          'IMT': 'PGA',
          'statistics': 'mean'}),
        (Site(-122.2, 37.5),
         {'investigationTimeSpan': '50.0',
          'IMLValues': [0.778, 1.09, 1.52, 2.13],
          'PoEValues': [0.354, 0.114, 0.023, 0.002],
          'IMT': 'PGA',
          'statistics': 'quantile',
          'quantileValue': 0.25}),
        (Site(-122.1, 37.5),
         {'investigationTimeSpan': '50.0',
          'IMLValues': [0.778, 1.09, 1.52, 2.13],
          'PoEValues': [0.454, 0.214, 0.123, 0.102],
          'IMT': 'PGA',
          'statistics': 'quantile',
          'quantileValue': 0.25}),
        (Site(-122.2, 37.5),
         {'investigationTimeSpan': '50.0',
          'IMLValues': [0.778, 1.09, 1.52, 2.13],
          'PoEValues': [0.354, 0.114, 0.023, 0.002],
          'IMT': 'PGA',
          'endBranchLabel': '1'}),
        (Site(-122.1, 37.5),
         {'investigationTimeSpan': '50.0',
          'IMLValues': [0.778, 1.09, 1.52, 2.13],
          'PoEValues': [0.454, 0.214, 0.123, 0.102],
          'IMT': 'PGA',
          'endBranchLabel': '1'}),
    ]


def GMF_DATA():
    return [
        {
            Site(-117, 40): {'groundMotion': 0.1},
            Site(-116, 40): {'groundMotion': 0.2},
            Site(-116, 41): {'groundMotion': 0.3},
            Site(-117, 41): {'groundMotion': 0.4},
        },
        {
            Site(-117, 40): {'groundMotion': 0.5},
            Site(-116, 40): {'groundMotion': 0.6},
            Site(-116, 41): {'groundMotion': 0.7},
            Site(-117, 41): {'groundMotion': 0.8},
        },
        {
            Site(-117, 42): {'groundMotion': 1.0},
            Site(-116, 42): {'groundMotion': 1.1},
            Site(-116, 41): {'groundMotion': 1.2},
            Site(-117, 41): {'groundMotion': 1.3},
        },
    ]


class HazardCurveDBReadTestCase(unittest.TestCase, helpers.DbTestCase):
    """
    Test the code to read hazard curves from DB.
    """
    def setUp(self):
        self.job = self.setup_classic_job()
        output_path = self.generate_output_path(self.job)
        hcw = HazardCurveDBWriter(output_path, self.job.id)
        hcw.serialize(HAZARD_CURVE_DATA())

    def tearDown(self):
        if hasattr(self, "job") and self.job:
            self.teardown_job(self.job)
        if hasattr(self, "output") and self.output:
            self.teardown_output(self.output)

    def test_read_curve(self):
        """Verify _get_db_curve."""
        the_job = helpers.create_job({}, job_id=self.job.id)
        calculator = ClassicalRiskCalculator(the_job)

        curve1 = calculator._get_db_curve(Site(-122.2, 37.5))
        self.assertEquals(list(curve1.abscissae),
                          [0.005, 0.007, 0.0098, 0.0137])
        self.assertEquals(list(curve1.ordinates),
                          [0.354, 0.114, 0.023, 0.002])

        curve2 = calculator._get_db_curve(Site(-122.1, 37.5))
        self.assertEquals(list(curve2.abscissae),
                          [0.005, 0.007, 0.0098, 0.0137])
        self.assertEquals(list(curve2.ordinates),
                          [0.454, 0.214, 0.123, 0.102])


class GmfDBReadTestCase(unittest.TestCase, helpers.DbTestCase):
    """
    Test the code to read the ground motion fields from DB.
    """
    def setUp(self):
        self.job = self.setup_classic_job()
        for gmf in GMF_DATA():
            output_path = self.generate_output_path(self.job)
            hcw = GmfDBWriter(output_path, self.job.id)
            hcw.serialize(gmf)

    def tearDown(self):
        if hasattr(self, "job") and self.job:
            self.teardown_job(self.job)
        if hasattr(self, "output") and self.output:
            self.teardown_output(self.output)

    def test_site_keys(self):
        """Verify _sites_to_gmf_keys"""
        params = {
            'REGION_VERTEX': '40,-117, 42,-117, 42,-116, 40,-116',
            'REGION_GRID_SPACING': '1.0'}

        the_job = helpers.create_job(params, job_id=self.job.id)
        calculator = EventBasedRiskCalculator(the_job)

        keys = calculator._sites_to_gmf_keys([Site(-117, 40), Site(-116, 42)])

        self.assertEquals(["0!0", "2!1"], keys)

    def test_read_gmfs(self):
        """Verify _get_db_gmfs."""
        params = {
            'REGION_VERTEX': '40,-117, 42,-117, 42,-116, 40,-116',
            'REGION_GRID_SPACING': '1.0'}

        the_job = helpers.create_job(params, job_id=self.job.id)
        calculator = EventBasedRiskCalculator(the_job)

        self.assertEquals(3, len(calculator._gmf_db_list(self.job.id)))

        # only the keys in gmfs are used
        gmfs = calculator._get_db_gmfs([], self.job.id)
        self.assertEquals({}, gmfs)

        # only the keys in gmfs are used
        sites = [Site(lon, lat)
                        for lon in xrange(-117, -115)
                        for lat in xrange(40, 43)]
        gmfs = calculator._get_db_gmfs(sites, self.job.id)
        # avoid rounding errors
        for k, v in gmfs.items():
            gmfs[k] = [round(i, 1) for i in v]

        self.assertEquals({
                '0!0': [0.1, 0.5, 0.0],
                '0!1': [0.2, 0.6, 0.0],
                '1!0': [0.4, 0.8, 1.3],
                '1!1': [0.3, 0.7, 1.2],
                '2!0': [0.0, 0.0, 1.0],
                '2!1': [0.0, 0.0, 1.1],
                }, gmfs)


class ExposureDBWriterTestCase(unittest.TestCase, helpers.DbTestCase):
    """
    Test the code to serialize exposure model to DB.
    """
    def setUp(self):
        self.writer = ExposureDBWriter(self.default_user())

    def test_read_exposure(self):
        path = os.path.join(helpers.SCHEMA_EXAMPLES_DIR, TEST_FILE)
        parser = ExposurePortfolioFile(path)

        # call tested function
        self.writer.serialize(parser)

        # test results
        model = self.writer.model

        self.assertFalse(model is None)

        # check model fields
        self.assertEquals('Collection of existing building in downtown Pavia',
                          model.description)
        self.assertEquals('buildings', model.category)
        self.assertEquals('EUR', model.stco_unit)

        # check asset instances
        assets = sorted(model.exposuredata_set.all(), key=lambda e: e.value)

        def _to_site(pg_point):
            return Site(pg_point.x, pg_point.y)

        self.assertEquals('asset_01', assets[0].asset_ref)
        self.assertEquals(150000, assets[0].value)
        self.assertEquals('RC/DMRF-D/LR', assets[0].taxonomy)
        self.assertEquals(Site(9.15000, 45.16667), _to_site(assets[0].site))

        self.assertEquals('asset_02', assets[1].asset_ref)
        self.assertEquals(250000, assets[1].value)
        self.assertEquals('RC/DMRF-D/HR', assets[1].taxonomy)
        self.assertEquals(Site(9.15333, 45.12200), _to_site(assets[1].site))

        self.assertEquals('asset_03', assets[2].asset_ref)
        self.assertEquals(500000, assets[2].value)
        self.assertEquals('RC/DMRF-D/LR', assets[2].taxonomy)
        self.assertEquals(Site(9.14777, 45.17999), _to_site(assets[2].site))
