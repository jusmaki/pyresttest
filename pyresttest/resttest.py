#!/usr/bin/env python
import sys
import os
import inspect
import traceback
import yaml
import pycurl
import json
import csv
import logging
import time
from optparse import OptionParser
from mimetools import Message  # For headers handling

try:
    from cStringIO import StringIO
except:
    from StringIO import StringIO

# Pyresttest internals
from binding import Context
import generators
from generators import parse_generator
from parsing import flatten_dictionaries, lowercase_keys, safe_to_bool, safe_to_json
import validators
from validators import Failure
from tests import Test, DEFAULT_TIMEOUT
from benchmarks import Benchmark, AGGREGATES, METRICS, parse_benchmark
"""
Executable class, ties everything together into the framework.
Module responsibilities:
- Read & import test test_files
- Parse test configs
- Provide executor methods for sets of tests and benchmarks
- Collect and report on test/benchmark results
- Perform analysis on benchmark results
"""

LOGGING_LEVELS = {'debug': logging.DEBUG,
    'info': logging.INFO,
    'warning': logging.WARNING,
    'error': logging.ERROR,
    'critical': logging.CRITICAL}

logging.basicConfig(format='%(levelname)s:%(message)s')
logger = logging.getLogger('pyresttest')

class cd:
    """Context manager for changing the current working directory"""
    # http://stackoverflow.com/questions/431684/how-do-i-cd-in-python/13197763#13197763

    def __init__(self, newPath):
        self.newPath = newPath

    def __enter__(self):
        if self.newPath:  # Don't CD to nothingness
            self.savedPath = os.getcwd()
            os.chdir(self.newPath)

    def __exit__(self, etype, value, traceback):
        if self.newPath:  # Don't CD to nothingness
            os.chdir(self.savedPath)

class TestConfig:
    """ Configuration for a test run """
    timeout = DEFAULT_TIMEOUT  # timeout of tests, in seconds
    print_bodies = False  # Print response bodies in all cases
    retries = 0  # Retries on failures
    test_parallel = False  # Allow parallel execution of tests in a test set, for speed?
    interactive = False

    # Binding and creation of genenerators
    variable_binds = None
    generators = None  # Map of generator name to generator function

    def __str__(self):
        return json.dumps(self, default=safe_to_json)

class TestSet:
    """ Encapsulates a set of tests and test configuration for them """
    tests = list()
    benchmarks = list()
    config = TestConfig()

    def __init__(self):
        self.config = TestConfig()
        self.tests = list()
        self.benchmarks = list()

    def __str__(self):
        return json.dumps(self, default=safe_to_json)

class BenchmarkResult:
    """ Stores results from a benchmark for reporting use """
    group = None
    name = u'unnamed'

    results = dict()  # Benchmark output, map the metric to the result array for that metric
    aggregates = list()  # List of aggregates, as tuples of (metricname, aggregate, result)
    failures = 0  # Track call count that failed

    def __init__(self):
        self.aggregates = list()
        self.results = list()

    def __str__(self):
        return json.dumps(self, default=safe_to_json)

class TestResponse:
    """ Encapsulates everything about a test response """
    test = None #Test run
    response_code = None

    body = None #Response body, if tracked

    passed = False
    response_headers = None
    failures = None

    def __init__(self):
        self.failures = list()

    def __str__(self):
        return json.dumps(self, default=safe_to_json)

    def unicode_body(self):
        return unicode(self.body.decode('UTF-8'))


def read_test_file(path):
    """ Read test file at 'path' in YAML """
    #TODO allow use of safe_load_all to handle multiple test sets in a given doc
    teststruct = yaml.safe_load(os.path.expandvars(read_file(path)))
    return teststruct

def parse_headers(header_string):
    """ Parse a header-string into individual headers
        Implementation based on: http://stackoverflow.com/a/5955949/95122
    """
    # First line is request line, strip it out
    if not header_string:
        return dict()
    request, headers = header_string.split('\r\n', 1)
    if not headers:
        return dict()
    else:
        header_msg = Message(StringIO(headers))
        return dict(header_msg.items())

def parse_testsets(base_url, test_structure, test_files = set(), working_directory = None):
    """ Convert a Python datastructure read from validated YAML to a set of structured testsets
    The data stucture is assumed to be a list of dictionaries, each of which describes:
        - a tests (test structure)
        - a simple test (just a URL, and a minimal test is created)
        - or overall test configuration for this testset
        - an import (load another set of tests into this one, from a separate file)
            - For imports, these are recursive, and will use the parent config if none is present

    Note: test_files is used to track tests that import other tests, to avoid recursive loops

    This returns a list of testsets, corresponding to imported testsets and in-line multi-document sets
    """

    tests_out = list()
    test_config = TestConfig()
    testsets = list()
    benchmarks = list()

    if working_directory is None:
        working_directory = os.path.abspath(os.getcwd())

    #returns a testconfig and collection of tests
    for node in test_structure: #Iterate through lists of test and configuration elements
        if isinstance(node,dict): #Each config element is a miniature key-value dictionary
            node = lowercase_keys(node)
            for key in node:
                if key == u'import':
                    importfile = node[key] #import another file
                    if importfile not in test_files:
                        logger.debug("Importing test sets: " + importfile)
                        test_files.add(importfile)
                        import_test_structure = read_test_file(importfile)
                        with cd(os.path.dirname(os.path.realpath(importfile))):
                            import_testsets = parse_testsets(base_url, import_test_structure, test_files)
                            testsets.extend(import_testsets)
                elif key == u'url': #Simple test, just a GET to a URL
                    mytest = Test()
                    val = node[key]
                    assert isinstance(val,str) or isinstance(val,unicode)
                    mytest.url = base_url + val
                    tests_out.append(mytest)
                elif key == u'test': #Complex test with additional parameters
                    with cd(working_directory):
                        child = node[key]
                        mytest = Test.parse_test(base_url, child)
                        tests_out.append(mytest)
                elif key == u'benchmark':
                    benchmark = parse_benchmark(base_url, node[key])
                    benchmarks.append(benchmark)
                elif key == u'config' or key == u'configuration':
                    test_config = parse_configuration(node[key])
    testset = TestSet()
    testset.tests = tests_out
    testset.config = test_config
    testset.benchmarks = benchmarks
    testsets.append(testset)
    return testsets

def parse_configuration(node):
    """ Parse input config to configuration information """
    test_config = TestConfig()

    node = lowercase_keys(flatten_dictionaries(node))  # Make it usable

    for key, value in node.items():
        if key == u'timeout':
            test_config.timeout = int(value)
        elif key == u'print_bodies':
            test_config.print_bodies = safe_to_bool(value)
        elif key == u'retries':
            test_config.retries = int(value)
        elif key == u'variable_binds':
            test_config.variable_binds = flatten_dictionaries(value)
        elif key == u'generators':
            flat = flatten_dictionaries(value)
            gen_map = dict()
            for generator_name, generator_config in flat.items():
                gen = parse_generator(generator_config)
                gen_map[str(generator_name)] = gen
            test_config.generators = gen_map

    return test_config

def read_file(path):
    """ Read an input into a file, doing necessary conversions around relative path handling """
    with open(path, "r") as f:
        string = f.read()
        f.close()
    return string

def run_test(mytest, test_config = TestConfig(), context = None):
    """ Put together test pieces: configure & run actual test, return results """

    # Initialize a context if not supplied
    my_context = context
    if my_context is None:
        my_context = Context()

    mytest.update_context_before(my_context)
    templated_test = mytest.realize(my_context)
    curl = templated_test.configure_curl(timeout=test_config.timeout, context=my_context)
    result = TestResponse()
    result.test = templated_test

    # reset the body, it holds values from previous runs otherwise
    headers = StringIO()
    body = StringIO()
    curl.setopt(pycurl.WRITEDATA, body)
    curl.setopt(pycurl.HEADERFUNCTION, headers.write)

    result.passed = None

    if test_config.interactive:
        print "==================================="
        print "%s" % mytest.name
        print "-----------------------------------"
        print "REQUEST:"
        print "%s %s" % (mytest.method, mytest.url)
        if mytest.body is not None:
            print "\n%s" % mytest.body
        raw_input("Press ENTER when ready: ")

    if (mytest.delay > 0.0):
        logger.debug("Delay sleeping " + str(mytest.delay) + " before HTTP request")
        time.sleep(mytest.delay)

    retries = test_config.retries
    retry_sleep = 1.0
    while True:
        try:
            curl.perform() #Run the actual call
        except Exception, e:
            # Curl exception occurred (network error), do not pass go, do not collect $200
            trace = traceback.format_exc()
            result.failures.append(Failure(message="Curl Exception: {0}".format(e), details=trace, failure_type=validators.FAILURE_CURL_EXCEPTION))
            result.passed = False
            curl.close()
            return result

        response_code = curl.getinfo(pycurl.RESPONSE_CODE)
        if (response_code == 503 and retries > 0):
            logger.debug("Retry sleeping " + str(retry_sleep) + " seconds after HTTP 503")
            time.sleep(retry_sleep)
            retry_sleep = retry_sleep * 2.0
            retries = retries - 1
        else:
            break

    # Retrieve values
    result.body = body.getvalue()
    body.close()
    result.response_headers = headers.getvalue()
    headers.close()

    result.response_code = response_code

    logger.debug("Initial Test Result, based on expected response code: "+str(response_code in mytest.expected_status))

    if response_code in mytest.expected_status:
        result.passed = True
    else:
        # Invalid response code
        result.passed = False
        failure_message = "Invalid HTTP response code: response code {0} not in expected codes [{1}]".format(response_code, mytest.expected_status)
        result.failures.append(Failure(message=failure_message, details=None, failure_type=validators.FAILURE_INVALID_RESPONSE))

    # Parse HTTP headers
    try:
        result.response_headers = parse_headers(result.response_headers)
    except Exception, e:
        result.failures.append(Failure(message="Header parsing exception: {0}".format(e), details=trace, failure_type=validators.TEST_EXCEPTION))
        result.passed = False
        curl.close()
        return result

    #print str(test_config.print_bodies) + ',' + str(not result.passed) + ' , ' + str(test_config.print_bodies or not result.passed)

    head = result.response_headers

    # execute validator on body
    if result.passed is True:
        body = result.body
        if mytest.validators is not None and isinstance(mytest.validators, list):
            logger.debug("executing this many validators: " + str(len(mytest.validators)))
            failures = result.failures
            for validator in mytest.validators:
                validate_result = validator.validate(body=body, headers=head, context=my_context)
                if not validate_result:
                    result.passed = False
				# Proxy for checking if it is a Failure object, because of import issues with isinstance there
                if hasattr(validate_result, 'details'):
                    failures.append(validate_result)
                # TODO add printing of validation for interactive mode
        else:
            logger.debug("no validators found")

        # Only do context updates if test was successful
        mytest.update_context_after(result.body, my_context)

    #Print response body if override is set to print all *OR* if test failed (to capture maybe a stack trace)
    if test_config.print_bodies or not result.passed:
        if test_config.interactive:
            print "RESPONSE:"
        print result.body.decode("string-escape")

    # TODO add string escape on body output
    logger.debug(result)

    curl.close()
    return result

def run_benchmark(benchmark, test_config = TestConfig(), context = None):
    """ Perform a benchmark, (re)using a given, configured CURL call to do so
        The actual analysis of metrics is performed separately, to allow for testing
    """

    # Context handling
    my_context = context
    if my_context is None:
        my_context = Context()

    warmup_runs = benchmark.warmup_runs
    benchmark_runs = benchmark.benchmark_runs
    message = ''  #Message is name of benchmark... print it?

    if (benchmark_runs <= 0):
        raise Exception("Invalid number of benchmark runs, must be > 0 :" + benchmark_runs)

    result = TestResponse()

    # TODO create and use a curl-returning configuration function
    # TODO create and use a post-benchmark cleanup function
    # They should use is_dynamic/is_context_modifier to determine if they need to
    #  worry about context and re-reading/retemplating and only do it if needed
    #    - Also, they will need to be smart enough to handle extraction functions
    #  For performance reasons, we don't want to re-run templating/extraction if
    #   we do not need to, and do not want to save request bodies.

    #Initialize variables to store output
    output = BenchmarkResult()
    output.name = benchmark.name
    output.group = benchmark.group
    metricnames = list(benchmark.metrics)
    metricvalues = [METRICS[name] for name in metricnames]  # Metric variable for curl, to avoid hash lookup for every metric name
    results = [list() for x in xrange(0, len(metricnames))]  # Initialize arrays to store results for each metric
    curl = pycurl.Curl()

    #Benchmark warm-up to allow for caching, JIT compiling, on client
    logger.info('Warmup: ' + message + ' started')
    for x in xrange(0, warmup_runs):
        benchmark.update_context_before(my_context)
        templated = benchmark.realize(my_context)
        curl = templated.configure_curl(timeout=test_config.timeout, context=my_context, curl_handle=curl)
        curl.setopt(pycurl.WRITEFUNCTION, lambda x: None) #Do not store actual response body at all.
        curl.perform()

    logger.info('Warmup: ' + message + ' finished')

    logger.info('Benchmark: ' + message + ' starting')

    for x in xrange(0, benchmark_runs):  # Run the actual benchmarks
        # Setup benchmark
        benchmark.update_context_before(my_context)
        templated = benchmark.realize(my_context)
        curl = templated.configure_curl(timeout=test_config.timeout, context=my_context, curl_handle=curl)
        curl.setopt(pycurl.WRITEFUNCTION, lambda x: None) #Do not store actual response body at all.

        try:  # Run the curl call, if it errors, then add to failure counts for benchmark
            curl.perform()
        except Exception:
            output.failures = output.failures + 1
            curl.close()
            curl = pycurl.Curl()
            continue  # Skip metrics collection

        # Get all metrics values for this run, and store to metric lists
        for i in xrange(0, len(metricnames)):
            results[i].append( curl.getinfo(metricvalues[i]) )


    curl.close()
    logger.info('Benchmark: ' + message + ' ending')

    temp_results = dict()
    for i in xrange(0, len(metricnames)):
        temp_results[metricnames[i]] = results[i]
    output.results = temp_results
    return analyze_benchmark_results(output, benchmark)


def analyze_benchmark_results(benchmark_result, benchmark):
    """ Take a benchmark result containing raw benchmark results, and do aggregation by
    applying functions

    Aggregates come out in format of metricname, aggregate_name, result """

    output = BenchmarkResult()
    output.name = benchmark_result.name
    output.group = benchmark_result.group
    output.failures = benchmark_result.failures

    # Copy raw metric arrays over where necessary
    raw_results = benchmark_result.results
    temp = dict()
    for metric in benchmark.raw_metrics:
        temp[metric] = raw_results[metric]
    output.results = temp

    # Compute aggregates for each metric, and add tuples to aggregate results
    aggregate_results = list()
    for metricname, aggregate_list in benchmark.aggregated_metrics.iteritems():
        numbers = raw_results[metricname]
        for aggregate_name in aggregate_list:
            if numbers:  # Only compute aggregates if numbers exist
                aggregate_function = AGGREGATES[aggregate_name]
                aggregate_results.append( (metricname, aggregate_name, aggregate_function(numbers)) )
            else:
                aggregate_results.append( (metricname, aggregate_name, None) )

    output.aggregates = aggregate_results
    return output


def metrics_to_tuples(raw_metrics):
    """ Converts metric dictionary of name:values_array into list of tuples
        Use case: writing out benchmark to CSV, etc

        Input:
        {'metric':[value1,value2...], 'metric2':[value1,value2,...]...}

        Output: list, with tuple header row, then list of tuples of values
        [('metric','metric',...), (metric1_value1,metric2_value1, ...) ... ]
    """
    if not isinstance(raw_metrics, dict):
        raise TypeError("Input must be dictionary!")

    metrics = sorted(raw_metrics.keys())
    arrays = [raw_metrics[metric] for metric in metrics]

    num_rows = len(arrays[0])  # Assume all same size or this fails
    output = list()
    output.append(tuple(metrics))  # Add headers

    # Create list of tuples mimicking 2D array from input
    for row in xrange(0, num_rows):
        new_row = tuple([arrays[col][row] for col in xrange(0, len(arrays))])
        output.append(new_row)
    return output

def write_benchmark_json(file_out, benchmark_result, benchmark, test_config = TestConfig()):
    """ Writes benchmark to file as json """
    json.dump(benchmark_result, file_out, default=safe_to_json)

def write_benchmark_csv(file_out, benchmark_result, benchmark, test_config = TestConfig()):
    """ Writes benchmark to file as csv """
    writer = csv.writer(file_out)
    writer.writerow(('Benchmark', benchmark_result.name))
    writer.writerow(('Benchmark Group', benchmark_result.group))
    writer.writerow(('Failures', benchmark_result.failures))

    # Write result arrays
    if benchmark_result.results:
        writer.writerow(('Results',''))
        writer.writerows(metrics_to_tuples(benchmark_result.results))
    if benchmark_result.aggregates:
        writer.writerow(('Aggregates',''))
        writer.writerows(benchmark_result.aggregates)

# Method to call when writing benchmark file
OUTPUT_METHODS = {u'csv' : write_benchmark_csv, u'json': write_benchmark_json}

def log_failure(failure, context=None, test_config=TestConfig()):
    """ Log a failure from a test """
    logger.error("Test Failure, failure type: {0}, Reason: {1}".format(failure.failure_type, failure.message))
    if failure.details:
        logger.error("Validator/Error details:"+str(failure.details))

def run_testsets(testsets):
    """ Execute a set of tests, using given TestSet list input """
    group_results = dict() #results, by group
    group_failure_counts = dict()
    total_failures = 0
    myinteractive = False

    for testset in testsets:
        mytests = testset.tests
        myconfig = testset.config
        mybenchmarks = testset.benchmarks
        context = Context()

        # Bind variables & add generators if pertinent
        if myconfig.variable_binds:
            context.bind_variables(myconfig.variable_binds)
        if myconfig.generators:
            for key, value in myconfig.generators.items():
                context.add_generator(key, value)

        #Make sure we actually have tests to execute
        if not mytests and not mybenchmarks:
            # no tests in this test set, probably just imports.. skip to next test set
            break

        myinteractive = True if myinteractive or myconfig.interactive else False

        #Run tests, collecting statistics as needed
        for test in mytests:
            #Initialize the dictionaries to store test fail counts and results
            if test.group not in group_results:
                group_results[test.group] = list()
                group_failure_counts[test.group] = 0

            result = run_test(test, test_config = myconfig, context=context)
            result.body = None  # Remove the body, save some memory!

            if not result.passed: #Print failure, increase failure counts for that test group
                # Use result test URL to allow for templating
                logger.error('Test Failed: '+test.name+" URL="+result.test.url+" Group="+test.group+" HTTP Status Code: "+str(result.response_code))

                # Print test failure reasons
                if result.failures:
                    for failure in result.failures:
                        log_failure(failure, context=context, test_config=myconfig)

                #Increment test failure counts for that group (adding an entry if not present)
                failures = group_failure_counts[test.group]
                failures = failures + 1
                group_failure_counts[test.group] = failures

            else: #Test passed, print results
                logger.info('Test Succeeded: '+test.name+" URL="+test.url+" Group="+test.group)

            #Add results for this test group to the resultset
            group_results[test.group].append(result)

            # handle stop_on_failure flag
            if not result.passed and test.stop_on_failure is not None and test.stop_on_failure:
                print 'STOP ON FAILURE! stopping test set execution, continuing with other test sets'
                break

        for benchmark in mybenchmarks:  # Run benchmarks, analyze, write
            if not benchmark.metrics:
                logger.debug('Skipping benchmark, no metrics to collect')
                continue

            logger.info("Benchmark Starting: "+benchmark.name+" Group: "+benchmark.group)
            benchmark_result = run_benchmark(benchmark, myconfig, context=context)
            print benchmark_result
            logger.info("Benchmark Done: "+benchmark.name+" Group: "+benchmark.group)

            if benchmark.output_file:  # Write file
                logger.debug('Writing benchmark to file in format: '+benchmark.output_format)
                write_method = OUTPUT_METHODS[benchmark.output_format]
                my_file =  open(benchmark.output_file, 'w')  # Overwrites file
                logger.debug("Benchmark writing to file: " + benchmark.output_file)
                write_method(my_file, benchmark_result, benchmark, test_config = myconfig)
                my_file.close()

    if myinteractive:
        # a break for when interactive bits are complete, before summary data
        print "==================================="

    #Print summary results
    for group in sorted(group_results.keys()):
        test_count = len(group_results[group])
        failures = group_failure_counts[group]
        total_failures = total_failures + failures
        if (failures > 0):
            print u'Test Group '+group+u' FAILED: '+ str((test_count-failures))+'/'+str(test_count) + u' Tests Passed!'
        else:
            print u'Test Group '+group+u' SUCCEEDED: '+ str((test_count-failures))+'/'+str(test_count) + u' Tests Passed!'

    return total_failures

def register_extensions(modules):
    """ Import the modules and register their respective extensions """
    if isinstance(modules, basestring):  # Catch supplying just a string arg
        modules = [modules]
    for ext in modules:
        # Get the package prefix and final module name
        segments = ext.split('.')
        module = segments.pop()
        package = '.'.join(segments)
        module = __import__(ext, globals(), locals(), package)  # Necessary to get the root module back

        # Extensions are registered by applying a register function to sets of registry name/function pairs inside an object
        extension_applies = {
            'VALIDATORS': validators.register_validator,
            'COMPARATORS': validators.register_comparator,
            'VALIDATOR_TESTS': validators.register_test,
            'EXTRACTORS': validators.register_extractor,
            'GENERATORS': generators.register_generator
        }

        has_registry = False
        for registry_name, register_function in extension_applies.items():
            if hasattr(module, registry_name):
                registry = getattr(module, registry_name)
                for key, val in registry.items():
                    register_function(key, val)
                if registry:
                    has_registry = True

        if not has_registry:
            raise ImportError("Extension to register did not contain any registries: {0}".format(ext))

# AUTOIMPORTS, these should run just before the main method, to ensure everything else is loaded
try:
    import jsonschema
    register_extensions('ext.validator_jsonschema')
except ImportError, ie:
    logging.warn("Failed to load jsonschema validator, make sure the jsonschema module is installed if you wish to use schema validators.")

def main(args):
    """
    Execute a test against the given base url.

    Keys allowed for args:
        url          - REQUIRED - Base URL
        test         - REQUIRED - Test file (yaml)
        print_bodies - OPTIONAL - print response body
        log          - OPTIONAL - set logging level {debug,info,warning,error,critical} (default=warning)
        interactive  - OPTIONAL - mode that prints info before and after test exectuion and pauses for user input for each test
    """

    if 'log' in args and args['log'] is not None:
        logger.setLevel(LOGGING_LEVELS.get(args['log'].lower(), logging.NOTSET))

    if 'import_extensions' in args and args['import_extensions']:
        extensions = args['import_extensions'].split(';')

        # We need to add current folder to working path to import modules
        working_folder = args['cwd']
        if working_folder not in sys.path:
            sys.path.insert(0, working_folder)
        register_extensions(extensions)

    test_file = args['test']
    test_structure = read_test_file(test_file)
    tests = parse_testsets(args['url'], test_structure, working_directory=os.path.dirname(test_file))

    # Override configs from command line if config set
    for t in tests:
        if 'print_bodies' in args and args['print_bodies'] is not None and bool(args['print_bodies']):
            t.config.print_bodies = safe_to_bool(args['print_bodies'])

        if 'interactive' in args and args['interactive'] is not None:
            t.config.interactive = safe_to_bool(args['interactive'])

        if 'retries' in args and args['retries'] is not None:
            t.config.retries = args['retries']
            logger.debug("Enable retries " + str(t.config.retries))

    # Execute all testsets
    failures = run_testsets(tests)

    sys.exit(failures)

def command_line_run(args_in):
    """ Runs everything needed to execute from the command line, so main method is callable without arg parsing """
    parser = OptionParser(usage="usage: %prog base_url test_filename.yaml [options] ")
    parser.add_option(u"--print-bodies", help="Print all response bodies", action="store", type="string", dest="print_bodies")
    parser.add_option(u"--log", help="Logging level", action="store", type="string")
    parser.add_option(u"--interactive", help="Interactive mode", action="store", type="string")
    parser.add_option(u"--url", help="Base URL to run tests against", action="store", type="string")
    parser.add_option(u"--test", help="Test file to use", action="store", type="string")
    parser.add_option(u'--import_extensions', help='Extensions to import, separated by semicolons', action="store", type="string")
    parser.add_option(u'--retries', help='Retry request NUM times if transient problems occur', action="store", type="int")

    (args, unparsed_args) = parser.parse_args(args_in)
    args = vars(args)

    # Handle url/test as named, or, failing that, positional arguments
    if not args['url'] or not args['test']:
        if len(unparsed_args) == 2:
            args[u'url'] = unparsed_args[0]
            args[u'test'] = unparsed_args[1]
        elif len(unparsed_args) == 1 and args['url']:
            args['test'] = unparsed_args[0]
        elif len(unparsed_args) == 1 and args['test']:
            args['url'] = unparsed_args[0]
        else:
            parser.print_help()
            parser.error("wrong number of arguments, need both url and test filename, either as 1st and 2nd parameters or via --url and --test")

    args['cwd'] = os.path.realpath(os.path.abspath(os.getcwd()))  # So modules can be loaded from current folder
    main(args)

#Allow import into another module without executing the main method
if(__name__ == '__main__'):
    command_line_run(sys.argv[1:])
