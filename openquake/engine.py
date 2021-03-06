# -*- coding: utf-8 -*-

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


"""The 'Engine' is responsible for instantiating calculators and running jobs.
"""


import os
import re

from datetime import datetime
from ConfigParser import ConfigParser
from lxml import etree

from django.db import close_connection
from django.db import transaction
from django.contrib.gis.db import models
from django.contrib.gis.geos import GEOSGeometry

from openquake.calculators.hazard import CALCULATORS as HAZ_CALCS
from openquake.calculators.risk import CALCULATORS as RISK_CALCS
from openquake.db.models import CalcStats
from openquake.db.models import CharArrayField
from openquake.db.models import FloatArrayField
from openquake.db.models import Input
from openquake.db.models import InputSet
from openquake.db.models import OqCalculation
from openquake.db.models import OqJobProfile
from openquake.db.models import OqUser
from openquake.flags import FLAGS
from openquake import kvs
from openquake import logs
from openquake import shapes
from openquake import xml
from openquake.job import config as jobconf
from openquake.job.params import ARRAY_RE
from openquake.job.params import CALCULATION_MODE
from openquake.job.params import INPUT_FILE_TYPES
from openquake.job.params import PARAMS
from openquake.job.params import PATH_PARAMS
from openquake.kvs import mark_job_as_current
from openquake.parser import exposure
from openquake.supervising import supervisor
from openquake.utils import config as utils_config
from openquake.utils import stats

CALCS = dict(hazard=HAZ_CALCS, risk=RISK_CALCS)
RE_INCLUDE = re.compile(r'^(.*)_INCLUDE')


# Silencing 'Too many instance attributes'
# pylint: disable=R0902
class CalculationProxy(object):
    """Contains everything a calculator needs to run a calculation. This
    includes: an :class:`OqJobProfile` object, an :class:`OqCalclation`, and a
    dictionary of all of the calculation config params (which is a basically a
    duplication of the :class:`OqJobProfile` member; in the future we would
    like to remove this duplication).

    This class also contains handful of utility methods for determining the
    sites of interest for a calculation, querying the calculation status, etc.
    """

    # Silencing 'Too many arguments'
    # pylint: disable=R0913
    def __init__(self, params, calculation_id, sections=list(), base_path=None,
                 serialize_results_to=list(), oq_job_profile=None,
                 oq_calculation=None):
        """
        :param dict params: Dict of job config params.
        :param int calculation_id:
            ID of the corresponding oq_calculation db record.
        :param list sections: List of config file sections. Example::
            ['HAZARD', 'RISK']
        :param str base_path: base directory containing job input files
        :param oq_job_profile:
            :class:`openquake.db.models.OqJobProfile` instance; database
            representation of the job profile / calculation configuration.
        :param oq_calculation:
            :class:`openquake.db.models.OqCalculation` instance; database
            representation of the runtime thing we refer to as the
            'calculation'.
        """
        self._calculation_id = calculation_id
        mark_job_as_current(calculation_id)  # enables KVS gc

        self.sites = []
        self.blocks_keys = []
        self.params = params
        self.sections = list(set(sections))
        self.serialize_results_to = []
        self._base_path = base_path
        self.serialize_results_to = list(serialize_results_to)

        self.oq_job_profile = oq_job_profile
        self.oq_calculation = oq_calculation

    @property
    def base_path(self):
        """Directory containing the input files for this calculation.

        The base_path also acts as the base directory for calculation outputs.
        """
        if self._base_path is not None:
            return self._base_path
        else:
            return self.params.get('BASE_PATH')

    @staticmethod
    def from_kvs(job_id):
        """Return the job in the underlying kvs system with the given id."""
        params = kvs.get_value_json_decoded(
            kvs.tokens.generate_job_key(job_id))
        calculation = OqCalculation.objects.get(id=job_id)
        job_profile = calculation.oq_job_profile
        job = CalculationProxy(params, job_id, oq_job_profile=job_profile,
                               oq_calculation=calculation)
        return job

    @staticmethod
    def get_status_from_db(job_id):
        """
        Get the status of the database record belonging to job ``job_id``.

        :returns: one of strings 'pending', 'running', 'succeeded', 'failed'.
        """
        return OqCalculation.objects.get(id=job_id).status

    @staticmethod
    def is_job_completed(job_id):
        """
        Return ``True`` if the :meth:`current status <get_status_from_db>`
        of the job ``job_id`` is either 'succeeded' or 'failed'. Returns
        ``False`` otherwise.
        """
        status = CalculationProxy.get_status_from_db(job_id)
        return status == 'succeeded' or status == 'failed'

    def has(self, name):
        """Return false if this job doesn't have the given parameter defined,
        or parameter's string value otherwise."""
        return name in self.params and self.params[name]

    @property
    def job_id(self):
        """Return the id of this job."""
        return self._calculation_id

    @property
    def key(self):
        """Returns the kvs key for this job."""
        return kvs.tokens.generate_job_key(self.job_id)

    @property
    def region(self):
        """Compute valid region with appropriate cell size from config file."""
        if not self.has('REGION_VERTEX'):
            return None

        region = shapes.RegionConstraint.from_coordinates(
            self._extract_coords('REGION_VERTEX'))

        region.cell_size = float(self['REGION_GRID_SPACING'])
        return region

    def __getitem__(self, name):
        defined_param = PARAMS.get(name)
        if (hasattr(defined_param, 'to_job')
            and defined_param.to_job is not None
            and self.params.get(name) is not None):
            return defined_param.to_job(self.params.get(name))
        return self.params.get(name)

    def __eq__(self, other):
        return self.params == other.params

    def __str__(self):
        return str(self.params)

    def _slurp_files(self):
        """Read referenced files and write them into kvs, keyed on their
        sha1s."""
        kvs_client = kvs.get_client()
        if self.base_path is None:
            logs.LOG.debug("Can't slurp files without a base path, homie...")
            return
        for key, val in self.params.items():
            if key[-5:] == '_FILE':
                path = os.path.join(self.base_path, val)
                with open(path) as data_file:
                    logs.LOG.debug("Slurping %s" % path)
                    blob = data_file.read()
                    file_key = kvs.tokens.generate_blob_key(self.job_id, blob)
                    kvs_client.set(file_key, blob)
                    self.params[key] = file_key
                    self.params[key + "_PATH"] = path

    def to_kvs(self):
        """Store this job into kvs."""
        self._slurp_files()
        key = kvs.tokens.generate_job_key(self.job_id)
        data = self.params.copy()
        data['debug'] = FLAGS.debug
        kvs.set_value_json_encoded(key, data)

    def sites_to_compute(self):
        """Return the sites used to trigger the computation on the
        hazard subsystem.

        If the SITES parameter is specified, the computation is triggered
        only on the sites specified in that parameter, otherwise
        the region is used.

        If the COMPUTE_HAZARD_AT_ASSETS_LOCATIONS parameter is specified,
        the hazard computation is triggered only on sites defined in the risk
        exposure file and located inside the region of interest.
        """

        if self.sites:
            return self.sites

        if jobconf.RISK_SECTION in self.sections \
                and self.has(jobconf.COMPUTE_HAZARD_AT_ASSETS):

            print "COMPUTE_HAZARD_AT_ASSETS_LOCATIONS selected, " \
                "computing hazard on exposure sites..."

            self.sites = read_sites_from_exposure(self)
        elif self.has(jobconf.SITES):

            coords = self._extract_coords(jobconf.SITES)
            sites = []

            for coord in coords:
                sites.append(shapes.Site(coord[0], coord[1]))

            self.sites = sites
        else:
            self.sites = self._sites_for_region()

        return self.sites

    def _extract_coords(self, config_param):
        """Extract from a configuration parameter the list of coordinates."""
        verts = self[config_param]
        return zip(verts[1::2], verts[::2])

    def _sites_for_region(self):
        """Return the list of sites for the region at hand."""
        region = shapes.Region.from_coordinates(
            self._extract_coords('REGION_VERTEX'))

        region.cell_size = self['REGION_GRID_SPACING']
        return [site for site in region]

    def build_nrml_path(self, nrml_file):
        """Return the complete output path for the given nrml_file"""
        return os.path.join(self['BASE_PATH'], self['OUTPUT_DIR'], nrml_file)

    def extract_values_from_config(self, param_name, separator=' ',
                                   check_value=lambda _: True):
        """Extract the set of valid values from the configuration file."""

        def _acceptable(value):
            """Return true if the value taken from the configuration
            file is valid, false otherwise."""
            try:
                value = float(value)
            except ValueError:
                return False
            else:
                return check_value(value)

        values = []

        if param_name in self.params:
            raw_values = self.params[param_name].split(separator)
            values = [float(x) for x in raw_values if _acceptable(x)]

        return values

    @property
    def imls(self):
        "Return the intensity measure levels as specified in the config file"
        if self.has('INTENSITY_MEASURE_LEVELS'):
            return self['INTENSITY_MEASURE_LEVELS']
        return None

    def _record_initial_stats(self):
        '''
        Report initial job stats (such as start time) by adding a
        uiapi.calc_stats record to the db.
        '''
        calc_stats = CalcStats(oq_calculation=self.oq_calculation)
        calc_stats.start_time = datetime.utcnow()
        calc_stats.num_sites = len(self.sites_to_compute())

        calc_mode = CALCULATION_MODE[self['CALCULATION_MODE']]
        if jobconf.HAZARD_SECTION in self.sections:
            if calc_mode != 'scenario':
                calc_stats.realizations = self["NUMBER_OF_LOGIC_TREE_SAMPLES"]

        calc_stats.save()


def read_sites_from_exposure(calc_proxy):
    """Given the exposure model specified in the job config, read all sites
    which are located within the region of interest.

    :param calc_proxy:
        ACalculationProxy object with an EXPOSURE parameter defined
    :type calc_proxy:
        :py:class:`openquake.engine.CalculationProxy`

    :returns: a list of :py:class:`openquake.shapes.Site` objects
    """

    sites = []
    path = os.path.join(calc_proxy.base_path,
                        calc_proxy.params[jobconf.EXPOSURE])

    reader = exposure.ExposurePortfolioFile(path)
    constraint = calc_proxy.region

    logs.LOG.debug(
        "Constraining exposure parsing to %s" % constraint)

    for site, _asset_data in reader.filter(constraint):

        # we don't want duplicates (bug 812395):
        if not site in sites:
            sites.append(site)

    return sites


def _job_from_file(config_file, output_type, owner_username='openquake'):
    """
    Create a job from external configuration files.

    NOTE: This function is deprecated. Please use
    :function:`openquake.engine.import_job_profile`.

    :param config_file:
        The external configuration file path
    :param output_type:
        Where to store results:
        * 'db' database
        * 'xml' XML files *plus* database
    :param owner_username:
        oq_user.user_name which defines the owner of all DB artifacts created
        by this function.
    """

    # output_type can be set, in addition to 'db' and 'xml', also to
    # 'xml_without_db', which has the effect of serializing only to xml
    # without requiring a database at all.
    # This allows to run tests without requiring a database.
    # This is not documented in the public interface because it is
    # essentially a detail of our current tests and ci infrastructure.
    assert output_type in ('db', 'xml')

    params, sections = _parse_config_file(config_file)
    params, sections = _prepare_config_parameters(params, sections)
    job_profile = _prepare_job(params, sections)

    validator = jobconf.default_validators(sections, params)
    is_valid, errors = validator.is_valid()

    if not is_valid:
        raise jobconf.ValidationException(errors)

    owner = OqUser.objects.get(user_name=owner_username)
    # openquake-server creates the calculation record in advance and stores
    # the calculation id in the config file
    calculation_id = params.get('OPENQUAKE_JOB_ID')
    if not calculation_id:
        # create the database record for this calculation
        calculation = OqCalculation(owner=owner, path=None)
        calculation.oq_job_profile = job_profile
        calculation.save()
        calculation_id = calculation.id

    if output_type == 'db':
        serialize_results_to = ['db']
    else:
        serialize_results_to = ['db', 'xml']

    base_path = params['BASE_PATH']

    job = CalculationProxy(params, calculation_id, sections=sections,
                           base_path=base_path,
                           serialize_results_to=serialize_results_to)
    job.to_kvs()

    return job


def _parse_config_file(config_file):
    """
    We have a single configuration file which may contain a risk section and
    a hazard section. This input file must be in the ConfigParser format
    defined at: http://docs.python.org/library/configparser.html.

    There may be a general section which may define configuration includes in
    the format of "sectionname_include = someconfigname.gem". These too must be
    in the ConfigParser format.
    """

    config_file = os.path.abspath(config_file)
    base_path = os.path.abspath(os.path.dirname(config_file))

    if not os.path.exists(config_file):
        raise jobconf.ValidationException(
            ["File '%s' not found" % config_file])

    parser = ConfigParser()
    parser.read(config_file)

    params = {}
    sections = []

    for section in parser.sections():
        for key, value in parser.items(section):
            key = key.upper()
            # Handle includes.
            if RE_INCLUDE.match(key):
                config_file = os.path.join(os.path.dirname(config_file), value)
                new_params, new_sections = _parse_config_file(config_file)
                sections.extend(new_sections)
                params.update(new_params)
            else:
                sections.append(section)
                params[key] = value

    params['BASE_PATH'] = base_path

    return params, list(set(sections))


def _prepare_config_parameters(params, sections):
    """
    Pre-process configuration parameters removing unknown ones.
    """

    calc_mode = CALCULATION_MODE[params['CALCULATION_MODE']]
    new_params = dict()

    for name, value in params.items():
        try:
            param = PARAMS[name]
        except KeyError:
            print 'Ignoring unknown parameter %r' % name
            continue

        if calc_mode not in param.modes:
            msg = "Ignoring %s in %s, it's meaningful only in "
            msg %= (name, calc_mode)
            print msg, ', '.join(param.modes)
            continue

        new_params[name] = value

    # make file paths absolute
    for name in PATH_PARAMS:
        if name not in new_params:
            continue

        new_params[name] = os.path.join(params['BASE_PATH'], new_params[name])

    # Set default parameters (if applicable).
    # TODO(LB): This probably isn't the best place for this code (since we may
    # want to implement similar default param logic elsewhere). For now,
    # though, it will have to do.

    # If job is classical and hazard+risk:
    if calc_mode == 'classical' and set(['HAZARD', 'RISK']).issubset(sections):
        if params.get('COMPUTE_MEAN_HAZARD_CURVE'):
            # If this param is already defined, display a message to the user
            # that this config param is being ignored and set to the default:
            print "Ignoring COMPUTE_MEAN_HAZARD_CURVE; defaulting to 'true'."
        # The value is set to a string because validators still expected job
        # config params to be strings at this point:
        new_params['COMPUTE_MEAN_HAZARD_CURVE'] = 'true'

    return new_params, sections


def _insert_input_files(params, input_set):
    """Create uiapi.input records for all input files"""

    # insert input files in input table
    for param_key, file_type in INPUT_FILE_TYPES.items():
        if param_key not in params:
            continue
        path = params[param_key]
        in_model = Input(input_set=input_set, path=path,
                         input_type=file_type, size=os.path.getsize(path))
        in_model.save()

    # insert soft-linked source models in input table
    if 'SOURCE_MODEL_LOGIC_TREE_FILE' in params:
        for path in _get_source_models(params['SOURCE_MODEL_LOGIC_TREE_FILE']):
            in_model = Input(input_set=input_set, path=path,
                             input_type='source', size=os.path.getsize(path))
            in_model.save()


@transaction.commit_on_success(using='job_init')
def _prepare_job(params, sections, owner_username='openquake'):
    """
    Create a new OqCalculation and fill in the related OqJobProfile entry.

    Returns the newly created job object.

    :param dict params:
        The job config params.
    :params sections:
        The job config file sections, as a list of strings.

    :returns:
        A new :class:`openquake.db.models.OqJobProfile` object.
    """

    @transaction.commit_on_success(using='job_init')
    def _get_job_profile(input_set, calc_mode, job_type, owner):
        """Create an OqJobProfile, save it to the db, commit, and return."""
        job_profile = OqJobProfile(input_set=input_set, calc_mode=calc_mode,
                                   job_type=job_type)

        _insert_input_files(params, input_set)
        _store_input_parameters(params, calc_mode, job_profile)

        job_profile.owner = owner
        job_profile.save()

        return job_profile

    # TODO specify the owner as a command line parameter
    owner = OqUser.objects.get(user_name=owner_username)

    input_set = InputSet(upload=None, owner=owner)
    input_set.save()

    calc_mode = CALCULATION_MODE[params['CALCULATION_MODE']]
    job_type = [s.lower() for s in sections
        if s.upper() in [jobconf.HAZARD_SECTION, jobconf.RISK_SECTION]]

    job_profile = _get_job_profile(input_set, calc_mode, job_type, owner)
    job_profile.owner = owner

    # When querying this record from the db, Django changes the values
    # slightly (with respect to geometry, for example). Thus, we want a
    # "fresh" copy of the record from the db.
    return OqJobProfile.objects.get(id=job_profile.id)


def _get_source_models(logic_tree):
    """Returns the source models soft-linked by the given logic tree.

    :param str logic_tree: path to a source model logic tree file
    :returns: list of source model file paths
    """

    # can be removed if we don't support .inp files
    if not logic_tree.endswith('.xml'):
        return []

    base_path = os.path.dirname(os.path.abspath(logic_tree))
    model_files = []

    uncert_mdl_tag = xml.NRML + 'uncertaintyModel'

    for _event, elem in etree.iterparse(logic_tree):
        if elem.tag == uncert_mdl_tag:
            e_text = elem.text.strip()
            if e_text.endswith('.xml'):
                model_files.append(os.path.join(base_path, e_text))

    return model_files


def _store_input_parameters(params, calc_mode, job_profile):
    """Store parameters in uiapi.oq_job_profile columns"""

    for name, param in PARAMS.items():
        if calc_mode in param.modes and param.default is not None:
            setattr(job_profile, param.column, param.default)

    for name, value in params.items():
        param = PARAMS[name]
        value = value.strip()

        if param.type in (models.BooleanField, models.NullBooleanField):
            value = value.lower() not in ('0', 'false')
        elif param.type == models.PolygonField:
            ewkt = shapes.polygon_ewkt_from_coords(value)
            value = GEOSGeometry(ewkt)
        elif param.type == models.MultiPointField:
            ewkt = shapes.multipoint_ewkt_from_coords(value)
            value = GEOSGeometry(ewkt)
        elif param.type == FloatArrayField:
            value = [float(v) for v in ARRAY_RE.split(value) if len(v)]
        elif param.type == CharArrayField:
            if param.to_db is not None:
                value = param.to_db(value)
            value = [str(v) for v in ARRAY_RE.split(value) if len(v)]
        elif param.to_db is not None:
            value = param.to_db(value)
        elif param.type == None:
            continue

        setattr(job_profile, param.column, value)

    if job_profile.imt != 'sa':
        job_profile.period = None
        job_profile.damping = None


def run_calculation(job_profile, params, sections, output_type='db'):
    """Given an :class:`openquake.db.models.OqJobProfile` object, create a new
    :class:`openquake.db.models.OqCalculation` object and run the calculation.

    NOTE: The params and sections parameters are temporary but will be required
    until we can run calculations purely using Django model objects as
    calculator input.

    Returns the calculation object when the calculation concludes.

    :param job_profile:
        :class:`openquake.db.models.OqJobProfile` instance.
    :param params:
        A dictionary of config parameters parsed from the calculation
        config file.
    :param sections:
        A list of sections parsed from the calculation config file.
    :param output_type:
        'db' or 'xml' (defaults to 'db')

    :returns:
        :class:`openquake.db.models.OqCalculation` instance.
    """
    if not output_type in ('db', 'xml'):
        raise RuntimeError("output_type must be 'db' or 'xml'")

    calculation = OqCalculation(owner=job_profile.owner)
    calculation.oq_job_profile = job_profile
    calculation.status = 'running'
    calculation.save()

    # Clear any counters for this calculation_id, prior to running the
    # calculation.
    # We do this just to make sure all of the counters behave properly and can
    # provide accurate data about a calculation in-progress.
    stats.delete_job_counters(calculation.id)

    # Make the job/calculation ID generally available.
    utils_config.Config().job_id = calculation.id

    serialize_results_to = ['db']
    if output_type == 'xml':
        serialize_results_to.append('xml')

    calc_proxy = CalculationProxy(params, calculation.id, sections=sections,
                                  serialize_results_to=serialize_results_to,
                                  oq_job_profile=job_profile,
                                  oq_calculation=calculation)

    # closing all db connections to make sure they're not shared between
    # supervisor and job executor processes. otherwise if one of them closes
    # the connection it immediately becomes unavailable for other
    close_connection()

    calc_pid = os.fork()
    if not calc_pid:
        # calculation executor process
        try:
            logs.init_logs_amqp_send(level=FLAGS.debug, job_id=calculation.id)
            _launch_calculation(calc_proxy, sections)
        except Exception, ex:
            logs.LOG.critical("Calculation failed with exception: '%s'"
                              % str(ex))
            calculation.status = 'failed'
            calculation.save()
            raise
        else:
            calculation.status = 'succeeded'
            calculation.save()
        return

    supervisor_pid = os.fork()
    if not supervisor_pid:
        # supervisor process
        supervisor_pid = os.getpid()
        calculation.supervisor_pid = supervisor_pid
        calculation.job_pid = calc_pid
        calculation.save()
        supervisor.supervise(calc_pid, calculation.id)
        return

    # parent process

    # ignore Ctrl-C as well as supervisor process does. thus only
    # job executor terminates on SIGINT
    supervisor.ignore_sigint()
    # wait till both child processes are done
    os.waitpid(calc_pid, 0)
    os.waitpid(supervisor_pid, 0)

    return calculation


def _launch_calculation(calc_proxy, sections):
    """Instantiate calculator(s) and actually run the calculation.

    :param calc_proxy:
        :class:`openquake.engine.CalculationProxy` instance.
    :param sections:
        List of config file sections. Example::
            ['general', 'HAZARD', 'RISK']
    """
    # TODO(LB):
    # In the future, this should be moved to the analyze() method of the base
    # Calculator class, or something like that. For now, we don't want it there
    # because it would get called twice in a Hazard+Risk calculation. This is
    # going to need some thought.
    # Ignoring 'Access to a protected member'
    # pylint: disable=W0212
    calc_proxy._record_initial_stats()

    calc_proxy.to_kvs()

    output_dir = os.path.join(calc_proxy.base_path, calc_proxy['OUTPUT_DIR'])
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    calc_mode = calc_proxy.oq_job_profile.calc_mode

    for job_type in ('hazard', 'risk'):
        if not job_type.upper() in sections:
            continue

        calc_class = CALCS[job_type][calc_mode]

        calculator = calc_class(calc_proxy)
        logs.LOG.debug("Launching calculation with id=%s and type='%s'"
                       % (calc_proxy.job_id, job_type))

        calculator.analyze()
        calculator.pre_execute()
        calculator.execute()
        calculator.post_execute()


def import_job_profile(path_to_cfg):
    """Given the path to a job config file, create a new
    :class:`openquake.db.models.OqJobProfile` and save it to the DB, and return
    it.

    :param str path_to_cfg:
        Path to a job config file.

    :returns:
        A tuple of :class:`openquake.db.models.OqJobProfile` instance,
        params dict, and sections list.
        NOTE: The params and sections are temporary. These should be removed
        from the return value the future whenever possible to keep the API
        clean.
    """
    params, sections = _parse_config_file(path_to_cfg)
    params, sections = _prepare_config_parameters(params, sections)

    validator = jobconf.default_validators(sections, params)
    is_valid, errors = validator.is_valid()

    if not is_valid:
        raise jobconf.ValidationException(errors)

    job_profile = _prepare_job(params, sections)
    return job_profile, params, sections
