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

import os
import shutil
import unittest

from lxml import etree
from nose.plugins.attrib import attr

from openquake.db.models import OqCalculation
from openquake.nrml import nrml_schema_file

from tests.utils import helpers


BCR_DEMO_BASE = 'demos/benefit_cost_ratio'


CONFIG = '%s/config.gem' % BCR_DEMO_BASE
COMPUTED_OUTPUT = '%s/computed_output' % BCR_DEMO_BASE
RESULT = '%s/bcr-map.xml' % COMPUTED_OUTPUT

NRML = 'http://openquake.org/xmlns/nrml/0.3'
GML = 'http://www.opengis.net/gml'


class BCRQATestCase(unittest.TestCase):

    @attr('qa')
    def test_bcr(self):
        # Verify the EAL (Original and Retrofitted) and BCR values to
        # hand-computed results.

        # For the EAL values, a delta of 0.0009 (3 decimal places of precision)
        # is considered reasonable.

        # For the BCR, a delta of 0.009 (2 decimal places of precision) is
        # considered reasonable.

        expected_result = {
        #    site location
            (-122.0, 38.225): {
                # assetRef  eal_orig  eal_retrof  bcr
                    'a1':   (0.009379,  0.006586,  0.483091)
            }
        }

        helpers.run_job(CONFIG)
        calc_record = OqCalculation.objects.latest("id")
        self.assertEqual('succeeded', calc_record.status)

        result = self._parse_bcr_map(RESULT)

        try:
            self._assert_bcr_results_equal(expected_result, result)
        finally:
            shutil.rmtree(COMPUTED_OUTPUT)

    def _assert_bcr_results_equal(self, expected, actual, eal_delta=0.0009,
                                  bcr_delta=0.009):
        """Given a pair of dicts assert that they are equal.

        Result values do not have to be exact and the following default deltas
        are used:

        For EAL values, a delta of
        0.0009 (3 decimal places of precision) is allowed. For BCR values, a
        delta of 0.009 (2 decimal places of precision) is allowed."""
        self.assertEqual(len(expected), len(actual))

        for site, exp_value in expected.items():
            for asset_ref, (eal_orig, eal_retrof, bcr) in exp_value.items():

                act_eal_orig, act_eal_retrof, act_bcr = (
                    actual[site][asset_ref])

                # Verify 'EAL, Original'
                self.assertAlmostEqual(eal_orig, act_eal_orig,
                                       delta=eal_delta)
                # Verify 'EAL, Retrofitted'
                self.assertAlmostEqual(eal_retrof, act_eal_retrof,
                                       delta=eal_delta)
                # Verify BCR
                self.assertAlmostEqual(bcr, act_bcr, delta=bcr_delta)

    def _parse_bcr_map(self, filename):
        self.assertTrue(os.path.exists(filename))
        schema = etree.XMLSchema(file=nrml_schema_file())
        parser = etree.XMLParser(schema=schema)
        tree = etree.parse(filename, parser=parser)

        bcrnodes = tree.getroot().findall(
            '{%(ns)s}riskResult/{%(ns)s}benefitCostRatioMap/{%(ns)s}BCRNode' %
            {'ns': NRML}
        )
        result = {}

        for bcrnode in bcrnodes:
            [site] = bcrnode.findall('{%s}site/{%s}Point/{%s}pos' %
                                     (NRML, GML, GML))
            assets = {}
            valuenodes = bcrnode.findall('{%s}benefitCostRatioValue' % NRML)
            for valuenode in valuenodes:
                values = []
                for tag in ('expectedAnnualLossOriginal',
                            'expectedAnnualLossRetrofitted',
                            'benefitCostRatio'):
                    [node] = valuenode.findall('{%s}%s' % (NRML, tag))
                    values.append(float(node.text))
                assets[valuenode.attrib['assetRef']] = tuple(values)
            result[tuple(map(float, site.text.split()))] = assets

        return result
